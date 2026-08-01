"""``scriptit auth`` — connect the workstation CLI to the Script.it platform.

Login is a loopback browser flow (the pattern gcloud/gh use): the CLI starts a
one-shot listener on ``127.0.0.1:<random port>``, opens the signed-in web app
at ``/app/cli-auth?port=<port>&state=<nonce>``, and the page delivers a
backend-minted Firebase custom token straight to the listener. The CLI
exchanges it for an ID + refresh token pair via the public identitytoolkit
endpoint. The stored record is that pair plus what a refresh needs (the API
key, the deployment's URLs, the expiry) — no password ever touches the CLI, and
nothing in it is reusable as one.
``--manual`` covers remote/SSH machines: the page shows a copyable code
instead.
"""

from __future__ import annotations

import base64
import contextlib
import json
import queue
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

import requests

from scriptit_cli.analytics import analytics
from scriptit_cli.output import emit, fail, note, warn
from scriptit_cli.remote_auth import (
    RemoteAuthError,
    credentials_path,
    current_profile_name,
    delete_credentials,
    exchange_custom_token,
    get_fresh_id_token,
    list_profiles,
    load_credentials,
    save_credentials,
    set_current_profile,
)

DEFAULT_API_URL = "https://api.script.it"
LOGIN_TIMEOUT_SECONDS = 300


def _fetch_auth_config(api_url: str) -> Dict[str, Any]:
    resp = requests.get(f"{api_url}/api/v1/auth/config", timeout=30)
    if resp.status_code != 200:
        raise RemoteAuthError(f"could not reach {api_url}/api/v1/auth/config ({resp.status_code})")
    return resp.json()


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot loopback receiver for the browser's payload POST."""

    # Per-connection socket timeout (http.server applies it in setup()):
    # a client declaring a Content-Length it never sends can only tie up
    # the listener for this long, not for the whole login window.
    timeout = 10

    payload_queue: "queue.Queue[Dict[str, Any]]"
    expected_state: str
    # The app origin the payload legitimately comes from — CORS is scoped to
    # it (not `*`) so an arbitrary page can't read responses while probing.
    allowed_origin: str

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        # Chrome's Private Network Access preflights public-site → 127.0.0.1
        # requests and blocks the POST unless the listener acknowledges it.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/cli-callback":
            self.send_response(404)
            self.end_headers()
            return
        # Browsers stamp cross-origin POSTs with an unforgeable-from-JS
        # Origin — reject other websites outright. Absent Origin (curl,
        # tests) still passes; the state nonce remains the real gate.
        origin = self.headers.get("Origin")
        if origin and origin != self.allowed_origin:
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            # A negative length makes `rfile.read` block until the peer goes
            # away, hanging the login on a malformed request.
            if length < 0:
                self.send_response(400)
                self._cors()
                self.end_headers()
                return
            # The real payload is ~2KB; anything local can POST here while
            # the listener is up, so cap reads instead of trusting the header.
            if length > 65536:
                self.send_response(413)
                self._cors()
                self.end_headers()
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError):
            payload = None
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("state") != self.expected_state
            or not payload.get("custom_token")
            # Every consumer indexes this; missing, it becomes a KeyError
            # traceback several steps later instead of a rejected payload.
            or not payload.get("firebase_api_key")
        ):
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
        self.payload_queue.put(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _receive_via_loopback(
    login_url_base: str, state: str, timeout: int, allowed_origin: str
) -> Dict[str, Any]:
    payload_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    handler = type(
        "_BoundHandler",
        (_CallbackHandler,),
        {
            "payload_queue": payload_queue,
            "expected_state": state,
            "allowed_origin": allowed_origin,
        },
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"{login_url_base}?port={port}&state={state}"
    # flush: agents and scripts read this through pipes, where Python
    # block-buffers stdout — the URL must be visible before we block.
    note("Opening your browser to complete login…")
    note(f"  {url}")
    note("(If the browser doesn't open, paste the URL yourself.)")
    # A headless or misconfigured machine simply doesn't open one; the URL
    # is already printed above, which is the fallback.
    with contextlib.suppress(Exception):
        webbrowser.open(url)

    try:
        return payload_queue.get(timeout=timeout)
    except queue.Empty:
        raise RemoteAuthError(
            f"login timed out after {timeout}s — retry, or use `scriptit auth login --manual`"
        ) from None
    finally:
        server.shutdown()
        server.server_close()


def _receive_via_manual(login_url_base: str, state: str) -> Dict[str, Any]:
    url = f"{login_url_base}?state={state}"
    note("Open this URL in a browser where you're signed in to Script.it:")
    note(f"  {url}")
    # The prompt goes through `note` rather than `input`'s own argument: that
    # one always writes to stdout, which under --json is reserved for the
    # result document.
    note("Paste the code shown on that page: ")
    code = input().strip()
    try:
        payload = json.loads(base64.b64decode(code).decode("utf-8"))
    except (ValueError, OSError):
        raise RemoteAuthError("that code couldn't be decoded — copy it exactly") from None
    if not isinstance(payload, dict):
        raise RemoteAuthError("that code didn't contain a login payload")
    if (
        payload.get("v") != 1
        or not payload.get("custom_token")
        or not payload.get("firebase_api_key")
    ):
        raise RemoteAuthError("unrecognized code payload")
    if payload.get("state") != state:
        raise RemoteAuthError("code was minted for a different login attempt — retry")
    return payload


def _verify_stored_login(api_url: str, creds: Dict[str, Any], profile: Optional[str]) -> None:
    """Confirm the credential we just stored is one the backend accepts.

    Both login modes end here: a token that mints cleanly can still be
    rejected — a Keycloak realm that withholds the ID token is the case this
    catches — and finding out now beats finding out on the user's next command.

    Rollback names the profile explicitly. ``delete_credentials()`` with no
    argument resolves the *ambient* profile, so under ``SCRIPTIT_PROFILE`` a
    failed login into one profile would delete a different, working one.
    """
    try:
        resp = requests.get(
            f"{api_url}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {get_fresh_id_token(creds)}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        warn(f"Warning: login stored but /auth/me failed: {exc}")
        return
    if resp.status_code != 200:
        delete_credentials(profile=creds.get("profile") or profile)
        fail(f"backend rejected the new credential ({resp.status_code}): {resp.text[:200]}")
    me = resp.json()
    # Attribution is the bearer token the backend just accepted; nothing about
    # the user is read out of this response and sent anywhere.
    analytics.bind(api_url, lambda: get_fresh_id_token(creds))
    analytics.track("cli_login_succeeded", mode=creds.get("mode"))
    emit(
        {
            "logged_in": True,
            "api_url": api_url,
            "user": {"id": me.get("id"), "email": me.get("email")},
            "profile": creds.get("profile") or profile or current_profile_name(),
            "credentials_file": credentials_path(),
        },
        lambda: print(f"Logged in to {api_url} as {me.get('email') or me.get('id')}"),
    )


class AuthCommands:
    """Authentication for remote (workstation) use of the scriptit CLI."""

    def login(
        self,
        api_url: Optional[str] = None,
        app_url: Optional[str] = None,
        manual: bool = False,
        timeout: int = LOGIN_TIMEOUT_SECONDS,
        profile: Optional[str] = None,
    ) -> None:
        """Connect this machine to your Script.it account.

        Args:
            api_url: Platform API origin (default: existing login's, else
                https://api.script.it). Pass a self-hosted deployment's
                origin to sign in to that instead.
            app_url: Frontend origin override (normally discovered from the API).
            manual: Print the login URL and accept a pasted code instead of
                listening on localhost (for SSH / remote machines).
            timeout: Seconds to wait for the browser handoff.
            profile: Named profile to store this login under (multiple
                accounts side by side; switch with `scriptit auth use`,
                override per-invocation with SCRIPTIT_PROFILE).
        """
        existing = load_credentials(profile) or {}
        api_url = str(api_url or existing.get("api_url") or DEFAULT_API_URL).rstrip("/")

        try:
            config = _fetch_auth_config(api_url)
        except (RemoteAuthError, ValueError, requests.RequestException) as exc:
            fail(f"cannot reach {api_url}: {exc}")

        if config.get("mode") == "keycloak":
            self._login_keycloak(api_url, config, timeout, profile)
            return
        if config.get("mode") != "firebase":
            fail(f"unknown auth mode {config.get('mode')!r}")

        app_url = (app_url or config.get("app_url") or "").rstrip("/")
        if not app_url:
            fail("the API did not advertise an app URL; pass --app-url")

        state = secrets.token_urlsafe(24)
        login_url_base = f"{app_url}/app/cli-auth"
        try:
            if manual:
                payload = _receive_via_manual(login_url_base, state)
            else:
                payload = _receive_via_loopback(
                    login_url_base, state, timeout, allowed_origin=app_url
                )
            tokens = exchange_custom_token(
                str(payload["firebase_api_key"]),
                str(payload["custom_token"]),
                app_url=app_url,
            )
        except RemoteAuthError as exc:
            fail(str(exc))

        creds: Dict[str, Any] = {
            "mode": "firebase",
            "api_url": api_url,
            "app_url": app_url,
            "firebase_api_key": payload["firebase_api_key"],
            "user_email": payload.get("user_email"),
            **tokens,
        }
        save_credentials(creds, profile=profile)

        # Verify end-to-end: the stored credential must satisfy the backend.
        _verify_stored_login(api_url, creds, profile)

    def _login_keycloak(
        self,
        api_url: str,
        config: Dict[str, Any],
        timeout: int,
        profile: Optional[str] = None,
    ) -> None:
        """RFC 8628 device grant against the realm advertised by /auth/config."""
        from scriptit_cli.remote_auth import keycloak_device_login

        keycloak = config.get("keycloak") or {}
        if not (keycloak.get("url") and keycloak.get("realm") and keycloak.get("client_id")):
            fail("the API did not advertise Keycloak realm details")
        try:
            creds = keycloak_device_login(
                str(keycloak["url"]),
                str(keycloak["realm"]),
                str(keycloak["client_id"]),
                timeout=timeout,
            )
        except RemoteAuthError as exc:
            fail(str(exc))
        creds["api_url"] = api_url
        save_credentials(creds, profile=profile)
        _verify_stored_login(api_url, creds, profile)

    def list(self) -> None:
        """List stored login profiles (the active one is starred)."""
        profiles = list_profiles()
        current = current_profile_name() if profiles else None
        rows = [
            {
                "profile": name,
                "email": creds.get("user_email"),
                "api_url": creds.get("api_url"),
                "active": name == current,
            }
            for name, creds in sorted(profiles.items())
        ]

        def _human() -> None:
            if not rows:
                print("No profiles. Run `scriptit auth login`.")
                return
            for row in rows:
                marker = " *" if row["active"] else ""
                print(f"{row['profile']}{marker}  {row['email'] or '?'}  {row['api_url']}")

        emit({"profiles": rows, "current": current}, _human)

    def use(self, profile: str) -> None:
        """Make a stored profile the default for subsequent commands."""
        if not set_current_profile(profile):
            fail(f"no profile named '{profile}' — see `scriptit auth list`")
        emit({"profile": profile}, lambda: print(f"Active profile: {profile}"))

    def status(self, profile: Optional[str] = None) -> None:
        """Show who this machine is connected as."""
        creds = load_credentials(profile)
        if not creds:
            emit(
                {"logged_in": False},
                lambda: print("Not logged in. Run `scriptit auth login`."),
            )
            return
        api_url = creds["api_url"]
        try:
            token = get_fresh_id_token(creds)
            resp = requests.get(
                f"{api_url}/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except (RemoteAuthError, requests.RequestException) as exc:
            fail(
                f"logged in to {api_url} but the credential is unusable: {exc} "
                f"(credentials file: {credentials_path()})"
            )
            return
        if resp.status_code != 200:
            fail(f"logged in to {api_url}, but /auth/me returned {resp.status_code}")
            return
        me = resp.json()
        profile_name = current_profile_name(profile)
        payload = {
            "logged_in": True,
            "api_url": api_url,
            "user": {"id": me.get("id"), "email": me.get("email")},
            "profile": profile_name,
            "credentials_file": credentials_path(),
        }

        def _human() -> None:
            print(
                f"Logged in to {api_url} as {me.get('email') or me.get('id')} "
                f"(profile: {profile_name})"
            )
            print(f"Credentials file: {credentials_path()}")

        emit(payload, _human)

    def logout(self, profile: Optional[str] = None, all_profiles: bool = False) -> None:
        """Forget stored credentials (current profile; --all-profiles for every one)."""
        removed = delete_credentials(profile=profile, all_profiles=all_profiles)
        emit(
            {"removed": bool(removed)},
            lambda: print(
                "Logged out (local credentials removed)."
                if removed
                else "Nothing to remove — not logged in."
            ),
        )
