"""Workstation credentials for the remote CLI (``scriptit auth login``).

Storage lives in the config directory: ``$XDG_CONFIG_HOME/scriptit/
credentials.json`` (else ``~/.config/scriptit/credentials.json``), chmod 600.
The file holds the platform ``api_url`` plus a Firebase **refresh token** —
never a password. ID tokens are short-lived (~1h) and refreshed lazily through
the public securetoken endpoint; the refreshed token is persisted so parallel
CLI invocations reuse it instead of each spending a refresh round-trip.

Profiles let one machine hold credentials for several deployments at once; the
active one is picked by ``SCRIPTIT_PROFILE`` or ``scriptit auth use``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

import requests

from scriptit_cli.config import _config_dir
from scriptit_cli.errors import RemoteAuthError
from scriptit_cli.output import note
from scriptit_cli.util import update_json_file

CREDENTIALS_FILE = "credentials.json"

_IDENTITY_TOOLKIT_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
# Google's public token endpoint, not a credential — hence the suppression.
_SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"  # noqa: S105

# Refresh when the ID token has less than this many seconds left — covers
# clock skew and the request's own flight time.
_REFRESH_MARGIN_SECONDS = 120


ENV_PROFILE = "SCRIPTIT_PROFILE"
DEFAULT_PROFILE = "default"


def credentials_path() -> str:
    """Absolute path of the (multi-profile) credential store."""
    return os.path.join(_config_dir(), CREDENTIALS_FILE)


def _normalize_store(data: Any) -> Dict[str, Any]:
    """Coerce whatever is on disk into ``{"profiles": {...}, "current": name}``.

    A flat pre-profiles blob (top-level ``api_url``) becomes a single
    ``default`` profile; the next save persists the new shape.
    """
    if not isinstance(data, dict):
        return {"profiles": {}, "current": DEFAULT_PROFILE}
    if data.get("api_url"):  # flat single-account layout
        return {"profiles": {DEFAULT_PROFILE: data}, "current": DEFAULT_PROFILE}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    return {"profiles": profiles, "current": data.get("current") or DEFAULT_PROFILE}


def _load_store() -> Dict[str, Any]:
    """The store as it is on disk, normalized."""
    try:
        with open(credentials_path(), encoding="utf-8") as f:
            return _normalize_store(json.load(f))
    except (OSError, ValueError):
        return {"profiles": {}, "current": DEFAULT_PROFILE}


def current_profile_name(profile: Optional[str] = None) -> str:
    """The profile in effect: explicit arg → ``SCRIPTIT_PROFILE`` → stored
    ``current`` → ``default``."""
    if profile:
        return profile
    env = os.environ.get(ENV_PROFILE, "").strip()
    if env:
        return env
    return str(_load_store().get("current") or DEFAULT_PROFILE)


def list_profiles() -> Dict[str, Dict[str, Any]]:
    """All stored profiles, keyed by name."""
    return dict(_load_store()["profiles"])


def load_credentials(profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The selected profile's credential blob, or ``None`` when not logged in."""
    store = _load_store()
    name = current_profile_name(profile)
    creds = store["profiles"].get(name)
    if isinstance(creds, dict) and creds.get("api_url"):
        creds.setdefault("profile", name)
        return creds
    return None


def save_credentials(creds: Dict[str, Any], profile: Optional[str] = None) -> None:
    """Store ``creds`` under the profile and make that profile current."""
    name = current_profile_name(profile)
    payload = {k: v for k, v in creds.items() if k != "profile"}

    def _mutate(store: Dict[str, Any]) -> Dict[str, Any]:
        store = _normalize_store(store)
        store["profiles"][name] = payload
        store["current"] = name
        return store

    update_json_file(credentials_path(), _mutate)


def merge_credentials(profile: str, fields: Dict[str, Any]) -> bool:
    """Merge refreshed fields into an existing profile, under the store lock.

    Deliberately not :func:`save_credentials`, which writes the record whole
    and makes the profile current. That is right for a login and wrong for a
    refresh, in two ways a refresh actually hits:

    - a refresh under ``SCRIPTIT_PROFILE=b`` would move the default profile
      from ``a`` to ``b``, silently retargeting every later command;
    - a refresh whose network call was in flight during ``auth logout`` would
      write the profile back, undoing the logout.

    So it merges into whatever is on disk *now*, leaves ``current`` alone, and
    does nothing when the profile is gone. Returns whether it wrote.
    """
    written = False

    def _mutate(store: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal written
        store = _normalize_store(store)
        existing = store["profiles"].get(profile)
        if not isinstance(existing, dict):
            return store
        store["profiles"][profile] = {
            **existing,
            **{k: v for k, v in fields.items() if k != "profile"},
        }
        written = True
        return store

    update_json_file(credentials_path(), _mutate)
    return written


def set_current_profile(name: str) -> bool:
    """Switch the default profile; False when it doesn't exist."""
    found = False

    def _mutate(store: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal found
        store = _normalize_store(store)
        if name in store["profiles"]:
            found = True
            store["current"] = name
        return store

    update_json_file(credentials_path(), _mutate)
    return found


def delete_credentials(profile: Optional[str] = None, all_profiles: bool = False) -> bool:
    """Forget one profile (the current one by default) or every profile."""
    if all_profiles:
        # Emptying under the lock, not unlinking: a concurrent writer holding
        # the lock would otherwise recreate the file right after the delete,
        # and logout would report success over live credentials.
        emptied = False

        def _clear(store: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal emptied
            emptied = bool(_normalize_store(store)["profiles"])
            return {"profiles": {}, "current": DEFAULT_PROFILE}

        update_json_file(credentials_path(), _clear)
        return emptied
    name = current_profile_name(profile)
    removed = False

    def _mutate(store: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal removed
        store = _normalize_store(store)
        if name in store["profiles"]:
            removed = True
            del store["profiles"][name]
            if store.get("current") == name:
                store["current"] = next(iter(store["profiles"]), DEFAULT_PROFILE)
        return store

    update_json_file(credentials_path(), _mutate)
    return removed


def _google_auth_headers(app_url: Optional[str]) -> Dict[str, str]:
    """Headers for identitytoolkit/securetoken calls.

    A deployment's Firebase browser key is HTTP-referrer-restricted to its
    own app origin, and Google rejects referer-less requests against such
    a key outright. The CLI is a first-party client of that key, so it
    presents that origin — the restriction targets third-party *websites*
    embedding the key, where the browser controls the header.
    """
    return {"Referer": f"{app_url.rstrip('/')}/"} if app_url else {}


def _expires_in(value: Any, default: int) -> int:
    """A lifetime in seconds from a provider field that may be junk.

    ``int(body["expires_in"])`` throws on ``None``, ``"soon"`` or a float
    string — after the token was already minted, so the login would fail with
    a traceback despite having worked.
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default


def _auth_post(url: str, *, what: str, timeout: float = 30, **kwargs: Any) -> Dict[str, Any]:
    """POST to an identity provider and return its JSON body.

    Every failure mode arrives as :class:`RemoteAuthError`: a refused
    connection, a DNS failure and a proxy that answers HTML are all "your
    login did not work", and a traceback for any of them reads as a CLI crash.
    The status check stays with each caller, which knows what its own non-200
    means.
    """
    try:
        resp = requests.post(url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise RemoteAuthError(f"{what} failed: {exc}") from None
    if resp.status_code != 200:
        raise RemoteAuthError(f"{what} failed ({resp.status_code}): {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError:
        raise RemoteAuthError(
            f"{what} returned a non-JSON response ({resp.status_code}): {resp.text[:200]}"
        ) from None
    if not isinstance(body, dict):
        raise RemoteAuthError(f"{what} returned an unexpected response shape")
    return body


def _auth_field(body: Dict[str, Any], key: str, what: str) -> Any:
    """A field the provider promised, or a readable error instead of KeyError."""
    if key not in body:
        raise RemoteAuthError(f"{what} response is missing {key!r}")
    return body[key]


def exchange_custom_token(
    api_key: str, custom_token: str, app_url: Optional[str] = None
) -> Dict[str, Any]:
    """Exchange a backend-minted Firebase custom token for ID + refresh tokens."""
    what = "custom-token exchange"
    body = _auth_post(
        _IDENTITY_TOOLKIT_URL,
        what=what,
        params={"key": api_key},
        json={"token": custom_token, "returnSecureToken": True},
        headers=_google_auth_headers(app_url),
        timeout=30,
    )
    return {
        "id_token": _auth_field(body, "idToken", what),
        "refresh_token": _auth_field(body, "refreshToken", what),
        "id_token_expires_at": time.time() + _expires_in(body.get("expiresIn"), 3600),
    }


def _refresh_id_token(
    api_key: str, refresh_token: str, app_url: Optional[str] = None
) -> Dict[str, Any]:
    what = "session refresh (run `scriptit auth login` again)"
    body = _auth_post(
        _SECURE_TOKEN_URL,
        what=what,
        params={"key": api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers=_google_auth_headers(app_url),
        timeout=30,
    )
    return {
        "id_token": _auth_field(body, "id_token", what),
        "refresh_token": body.get("refresh_token") or refresh_token,
        "id_token_expires_at": time.time() + _expires_in(body.get("expires_in"), 3600),
    }


def _refresh_keycloak_token(creds: Dict[str, Any]) -> Dict[str, Any]:
    body = _auth_post(
        str(creds["token_url"]),
        what="session refresh (run `scriptit auth login` again)",
        data={
            "grant_type": "refresh_token",
            "client_id": str(creds["client_id"]),
            "refresh_token": str(creds["refresh_token"]),
        },
        timeout=30,
    )
    # Same choice as login: the backend validates `aud == client_id`, which
    # only Keycloak's ID token carries. Refreshing into the access token
    # would swap a working credential for a rejected one at expiry.
    return {
        "id_token": _bearer_from(body),
        "refresh_token": body.get("refresh_token") or creds["refresh_token"],
        "id_token_expires_at": time.time() + _expires_in(body.get("expires_in"), 300),
    }


def get_fresh_id_token(creds: Optional[Dict[str, Any]] = None) -> str:
    """A currently-valid bearer token, refreshing and persisting when needed.

    Firebase mode refreshes via securetoken; Keycloak mode via the realm's
    token endpoint (stored as ``token_url`` at login).
    """
    creds = creds or load_credentials()
    if not creds:
        raise RemoteAuthError("not logged in — run `scriptit auth login`")
    expires_at = float(creds.get("id_token_expires_at") or 0)
    if creds.get("id_token") and expires_at - time.time() > _REFRESH_MARGIN_SECONDS:
        return str(creds["id_token"])
    try:
        if creds.get("mode") == "keycloak":
            refreshed = _refresh_keycloak_token(creds)
        else:
            refreshed = _refresh_id_token(
                str(creds["firebase_api_key"]),
                str(creds["refresh_token"]),
                app_url=str(creds["app_url"]) if creds.get("app_url") else None,
            )
    except KeyError as exc:
        # An interrupted or hand-edited store is missing what a refresh needs.
        # Say so; a traceback here reads as a CLI crash, not a stale login.
        raise RemoteAuthError(
            f"stored credentials are incomplete (no {exc.args[0]}) — "
            "run `scriptit auth login` to reconnect this machine"
        ) from None
    creds.update(refreshed)
    # Persist into the profile the creds were loaded from, which is not
    # necessarily the store's current profile (SCRIPTIT_PROFILE, --profile).
    # Only the refreshed fields, merged: this command already holds a valid
    # token in memory either way, so a logout that landed mid-refresh must
    # win on disk rather than being written back.
    merge_credentials(str(creds.get("profile") or current_profile_name()), refreshed)
    return str(creds["id_token"])


# ---------------------------------------------------------------------------
# Keycloak device authorization grant (RFC 8628)
# ---------------------------------------------------------------------------


def _bearer_from(body: Dict[str, Any]) -> str:
    """The token to present to the backend, ID token first.

    The backend validates ``aud == KEYCLOAK_CLIENT_ID`` (the same check the
    browser app satisfies by sending Keycloak's ID token). A Keycloak access
    token is audienced at ``account`` unless the realm adds an audience
    mapper, so sending one would be rejected after the user had already
    approved. ``scope=openid`` on the device request is what makes the ID
    token present; fall back to the access token for a realm that withholds
    it rather than failing the login outright.
    """
    return str(body.get("id_token") or body["access_token"])


def _pkce_pair() -> tuple[str, str]:
    """A (verifier, S256 challenge) pair, base64url without padding per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def keycloak_device_login(
    keycloak_url: str, realm: str, client_id: str, timeout: int = 300
) -> Dict[str, Any]:
    """Run the OAuth device grant against a Keycloak realm; returns the
    credential fields to store (token_url/client_id/refresh_token/...).

    Requires the realm client to have the device grant enabled; Keycloak
    reports ``unauthorized_client`` otherwise, which is surfaced verbatim.

    PKCE rides along because the advertised client is the platform's browser
    client, which pins ``pkce.code.challenge.method``: Keycloak enforces that
    on the device endpoint too and rejects a challenge-less request with
    ``Missing parameter: code_challenge_method``. Sending it is correct for a
    public client regardless of whether the realm demands it.
    """
    base = f"{keycloak_url.rstrip('/')}/realms/{realm}/protocol/openid-connect"
    verifier, challenge = _pkce_pair()
    resp = requests.post(
        f"{base}/auth/device",
        data={
            "client_id": client_id,
            "scope": "openid",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RemoteAuthError(
            f"device authorization failed ({resp.status_code}): {resp.text[:300]}"
        )
    grant = resp.json()
    verification = grant.get("verification_uri_complete") or grant.get("verification_uri")
    # `note`, not `print`: under --json stdout is reserved for the one result
    # document, and progress that landed there would corrupt it.
    note("To log in, open this URL and approve the request:")
    note(f"  {verification}")
    if not grant.get("verification_uri_complete"):
        note(f"  code: {grant.get('user_code')}")

    interval = int(grant.get("interval") or 5)
    deadline = time.time() + min(timeout, int(grant.get("expires_in") or timeout))
    while time.time() < deadline:
        time.sleep(interval)
        token_resp = requests.post(
            f"{base}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": grant["device_code"],
                "code_verifier": verifier,
            },
            timeout=30,
        )
        if token_resp.status_code == 200:
            body = token_resp.json()
            return {
                "mode": "keycloak",
                "token_url": f"{base}/token",
                "client_id": client_id,
                "id_token": _bearer_from(body),
                "refresh_token": body["refresh_token"],
                "id_token_expires_at": time.time() + int(body.get("expires_in", 300)),
            }
        error: Dict[str, Any] = {}
        try:
            body = token_resp.json()
            if isinstance(body, dict):
                error = body
        except ValueError:
            pass
        code = error.get("error")
        if code == "authorization_pending":
            continue
        if code == "slow_down":
            interval += 5
            continue
        raise RemoteAuthError(
            f"device login failed ({token_resp.status_code}): {token_resp.text[:300]}"
        )
    raise RemoteAuthError(f"device login timed out after {timeout}s")
