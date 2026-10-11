"""``scriptit auth`` — connect the workstation CLI to the Script.it platform.

Login on Script.it Cloud is a browser approval. The CLI keeps a random
verifier and opens — or, where it cannot, prints —
``/app/cli-auth?challenge=<sha256(verifier)>``. The signed-in page approves
that digest, and the CLI polls the backend with the verifier until the
approval lands, receiving a backend-minted Firebase custom token that it
exchanges for an ID + refresh token pair via the public identitytoolkit
endpoint. The stored record is that pair plus what a refresh needs (the API
key, the deployment's URLs, the expiry) — no password ever touches the CLI, and
nothing in it is reusable as one.

Only the digest leaves this process, so nothing printed, and nothing a
transcript carrying the URL holds, can be redeemed by anyone else; and the
browser never has to reach this machine, so one flow serves a laptop, an SSH
host and a hosted agent's container alike. Self-hosted deployments sign in
through their Keycloak realm's device grant instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sys
import time
import webbrowser
from typing import Any, Dict, Optional

import requests

from scriptit_cli.analytics import analytics
from scriptit_cli.client import detect_client
from scriptit_cli.config import _config_dir
from scriptit_cli.output import emit, fail, note, warn
from scriptit_cli.remote_auth import (
    RemoteAuthError,
    credentials_path,
    current_profile_name,
    delete_credentials,
    exchange_custom_token,
    get_fresh_id_token,
    keycloak_device_login,
    list_profiles,
    load_credentials,
    save_credentials,
    set_current_profile,
)
from scriptit_cli.util import update_json_file

DEFAULT_API_URL = "https://api.script.it"
LOGIN_TIMEOUT_SECONDS = 300

PENDING_LOGIN_FILE = "pending-login.json"

# How often a waiting CLI asks whether its login was approved. Each poll is one
# indexed DELETE on the backend, and nobody reading a code off a screen can
# tell this apart from anything faster.
_APPROVAL_POLL_SECONDS = 2

# How long a printed approval link stays resumable. A hosted agent's harness
# may time out the waiting command; running it again inside this window shows
# the same link and collects an approval already given, rather than asking the
# user to approve a second one.
_PENDING_LOGIN_MAX_AGE_SECONDS = 1800


def _fetch_auth_config(api_url: str) -> Dict[str, Any]:
    resp = requests.get(f"{api_url}/api/v1/auth/config", timeout=30)
    if resp.status_code != 200:
        raise RemoteAuthError(f"could not reach {api_url}/api/v1/auth/config ({resp.status_code})")
    return resp.json()


def _should_open_browser(no_browser: bool) -> bool:
    """Whether to pop a browser window for the login URL.

    An agent harness has its own browser and cannot see the one
    ``webbrowser.open`` launches, so opening it puts a login page in front of
    the human instead of the agent — while the agent, which is the caller,
    learns nothing. :func:`detect_client` already knows when a harness is
    driving (it reads the same markers used for session attribution), so the
    URL is simply printed there and whoever is driving decides what to do
    with it.
    """
    return not no_browser and detect_client() is None


def _pending_login_path() -> str:
    return os.path.join(_config_dir(), PENDING_LOGIN_FILE)


def _is_resumable(entry: Any, now: float) -> bool:
    if not isinstance(entry, dict) or not entry.get("verifier"):
        return False
    try:
        return now - float(entry.get("created_at")) < _PENDING_LOGIN_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return False


def _take_verifier(key: str) -> str:
    """This login's pending verifier if it is still resumable, else a new one.

    Kept beside the credential store, 0600, until the approval is collected or
    refused. It is a secret for as long as it lives: whoever holds it collects
    any approval of its challenge.
    """
    now = time.time()
    taken: Dict[str, str] = {}

    def _mutate(data: Dict[str, Any]) -> Dict[str, Any]:
        live = {k: v for k, v in data.items() if _is_resumable(v, now)}
        if key in live:
            taken["verifier"] = str(live[key]["verifier"])
        else:
            taken["verifier"] = secrets.token_urlsafe(32)
            live[key] = {"verifier": taken["verifier"], "created_at": now}
        return live

    update_json_file(_pending_login_path(), _mutate)
    return taken["verifier"]


def _forget_verifier(key: str) -> None:
    update_json_file(
        _pending_login_path(), lambda data: {k: v for k, v in data.items() if k != key}
    )


def _response_detail(resp: requests.Response) -> str:
    with contextlib.suppress(ValueError, KeyError, TypeError):
        return str(resp.json()["detail"])
    return resp.text[:300]


def _approval_refused(status: int, detail: str) -> RemoteAuthError:
    if status in (401, 404, 405):
        # A backend without the redeem route answers the unauthenticated poll
        # from its auth middleware, before routing.
        return RemoteAuthError(
            "this Script.it deployment does not support browser-approved login — "
            "sign in to it with an earlier scriptit-cli release"
        )
    if status == 403:
        return RemoteAuthError("the approving account is suspended or deleted")
    return RemoteAuthError(f"login approval failed ({status}): {detail}")


def _receive_via_approval(
    api_url: str,
    login_url_base: str,
    pending_key: str,
    timeout: int,
    open_browser: bool = False,
) -> Dict[str, Any]:
    """Show an approval link, then poll until a signed-in browser approves it.

    Only ``sha256(verifier)`` leaves this process. The verifier is forgotten
    once the backend answers for it either way — collected, or refused — and
    kept on a timeout, so running the command again resumes the same link.
    """
    verifier = _take_verifier(pending_key)
    challenge = hashlib.sha256(verifier.encode("utf-8")).hexdigest()
    url = f"{login_url_base}?challenge={challenge}"
    # `note` flushes: agents and scripts read this through pipes, and the URL
    # must be visible before the wait begins.
    if open_browser:
        note("Opening your browser to approve this login…")
        note(f"  {url}")
        note("(If the browser doesn't open, open the URL yourself.)")
        # A headless or misconfigured machine simply doesn't open one; the URL
        # is already printed above, which is the fallback.
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    else:
        note("Open this URL in a browser where you're signed in to Script.it:")
        note(f"  {url}")
    note(f"Approve it if the page shows the code {challenge[:4].upper()}-{challenge[4:8].upper()}.")
    note("If this command is interrupted, run it again: it resumes this same login.")

    deadline = time.monotonic() + timeout
    last_error: Optional[requests.RequestException] = None
    while True:
        try:
            resp = requests.post(
                f"{api_url}/api/v1/auth/cli/redeem",
                json={"verifier": verifier},
                timeout=30,
            )
        except requests.RequestException as exc:
            # A blip while waiting is not a failed login: an approval already
            # given is still there for the next poll.
            last_error = exc
        else:
            last_error = None
            detail = _response_detail(resp)
            if resp.status_code == 200:
                _forget_verifier(pending_key)
                with contextlib.suppress(ValueError):
                    body = resp.json()
                    if (
                        isinstance(body, dict)
                        and body.get("custom_token")
                        and body.get("firebase_api_key")
                    ):
                        return body
                raise RemoteAuthError("login approval returned an unexpected response")
            if not (resp.status_code == 400 and detail == "authorization_pending"):
                _forget_verifier(pending_key)
                raise _approval_refused(resp.status_code, detail)
        if time.monotonic() >= deadline:
            break
        time.sleep(_APPROVAL_POLL_SECONDS)

    reason = f" (last error: {last_error})" if last_error else ""
    raise RemoteAuthError(
        f"not approved within {timeout}s{reason} — run the same command again to keep "
        "waiting on this link"
    )


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
        no_browser: bool = False,
    ) -> None:
        """Connect this machine to your Script.it account.

        Args:
            api_url: Platform API origin (default: existing login's, else
                https://api.script.it). Pass a self-hosted deployment's
                origin to sign in to that instead.
            app_url: Frontend origin override (normally discovered from the API).
            manual: The same as --no-browser; kept so instructions that
                pass it keep working.
            timeout: Seconds to wait for the approval. Run again after a
                timeout or an interruption, the command resumes the same link.
            profile: Named profile to store this login under (multiple
                accounts side by side; switch with `scriptit auth use`,
                override per-invocation with SCRIPTIT_PROFILE).
            no_browser: Print the login URL instead of opening a window.
                Implied when an agent harness is driving.
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

        try:
            payload = _receive_via_approval(
                api_url,
                f"{app_url}/app/cli-auth",
                pending_key=f"{current_profile_name(profile)} {api_url}",
                timeout=timeout,
                open_browser=_should_open_browser(no_browser or manual),
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
        keycloak = config.get("keycloak") or {}
        if not (keycloak.get("url") and keycloak.get("realm") and keycloak.get("client_id")):
            fail("the API did not advertise Keycloak realm details")
        # The realm's own CLI client when the backend names one: a session
        # issued to it is what the backend treats as an outside agent, so the
        # account can govern it. A backend that predates the field advertises
        # only the web client, which is then the only way in.
        client_id = str(keycloak.get("cli_client_id") or keycloak["client_id"])
        try:
            creds = keycloak_device_login(
                str(keycloak["url"]),
                str(keycloak["realm"]),
                client_id,
                timeout=timeout,
            )
        except RemoteAuthError as exc:
            fail(str(exc))
        creds["api_url"] = api_url
        save_credentials(creds, profile=profile)
        _verify_stored_login(api_url, creds, profile)

    def browser_url(
        self,
        next: str = "/app",
        profile: Optional[str] = None,
    ) -> None:
        """Print a one-time URL that signs a browser in as this machine's user.

        The inverse of `login`. There a signed-in browser lends a session to
        the CLI; here a signed-in CLI lends one to a browser — for an agent
        that can already drive the platform through this CLI but has no way to
        *see* it.

        An agent cannot log in on the user's behalf: the login page mints its
        token because it is already signed in, and an agent's browser is not,
        so the only way through would be typing the user's password. Borrowing
        this machine's existing session is what an agent can do instead.

        The URL is a credential. It is good for 60 seconds, once, and the code
        rides in the fragment so it reaches no server log. Treat it as you
        would a password for that minute: the backend records every redeem and
        emails the account when one arrives from an unfamiliar address.

        Script.it Cloud only. An on-prem deployment authenticates through
        Keycloak, which has no equivalent, and the backend says so rather than
        failing obscurely.

        Args:
            next: Same-origin path to land on, e.g. `/app/s/<session_id>`.
                The page refuses anything that would leave the origin.
            profile: Named profile to use (default: the active one).
        """
        creds = load_credentials(profile)
        if not creds:
            fail("not logged in — run `scriptit auth login`")

        app_url = str(creds.get("app_url") or "").rstrip("/")
        if not app_url:
            fail(
                "this login recorded no browser origin — run `scriptit auth login` "
                "again to refresh it"
            )

        api_url = str(creds["api_url"]).rstrip("/")
        try:
            token = get_fresh_id_token(creds)
        except RemoteAuthError as exc:
            fail(str(exc))

        try:
            resp = requests.post(
                f"{api_url}/api/v1/auth/browser-handoff",
                headers={"Authorization": f"Bearer {token}"},
                json={"client": detect_client()},
                timeout=30,
            )
        except requests.RequestException as exc:
            fail(f"could not reach {api_url}: {exc}")

        if resp.status_code != 200:
            # 409 is the deployment saying it cannot do this at all (Keycloak).
            # Its wording is the useful part, so it is passed through rather
            # than replaced with a guess about why.
            detail = resp.text[:300]
            with contextlib.suppress(ValueError, KeyError, TypeError):
                detail = resp.json()["detail"]
            fail(f"could not mint a browser link ({resp.status_code}): {detail}")

        body = resp.json()
        # The code goes in the fragment, never the query string: a fragment is
        # not sent to any server and never lands in an access log or a Referer.
        url = f"{app_url}/app/cli-handoff#code={body['code']}&next={next}"
        expires_in = int(body.get("expires_in") or 60)

        def _human() -> None:
            print(url)
            print(
                f"\nOpen it within {expires_in}s — it works once. Anyone who reads it "
                "in that window can sign in as you.",
                file=sys.stderr,
            )

        emit({"url": url, "expires_in": expires_in}, _human)

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
