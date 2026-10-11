"""``scriptit auth login``: the browser-approved login.

The CLI prints ``/app/cli-auth?challenge=<sha256(verifier)>`` and polls
``POST /api/v1/auth/cli/redeem`` with the verifier until a signed-in browser
approves the challenge. The properties worth a tripwire:

- **The verifier never leaves the process** — not in the URL, not on screen.
- **An interrupted wait resumes the same link**, so a hosted agent's harness
  timing the command out does not cost the user a second approval.
- **A refusal ends the link**; only a timeout keeps it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

import pytest
import requests

from scriptit_cli import auth
from scriptit_cli.errors import RemoteAuthError

API = "https://api.example"
LOGIN = "https://app.example/app/cli-auth"
KEY = "default https://api.example"


class _Resp:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> object:
        return self._body


PENDING = _Resp(400, {"detail": "authorization_pending"})
APPROVED = _Resp(
    200,
    {
        "custom_token": "CT",
        "firebase_api_key": "AIza",
        "user_id": "uid-1",
        "user_email": "a@example.com",
    },
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.time, "sleep", lambda _s: None)


def _redeem_answers(monkeypatch: pytest.MonkeyPatch, *answers: object) -> list:
    """Make the redeem poll return ``answers`` in turn (an exception is raised);
    returns the verifiers it was polled with."""
    queue = list(answers)
    polled: list = []

    def fake_post(url, json=None, timeout=None, **_kw):
        assert url == f"{API}/api/v1/auth/cli/redeem"
        polled.append(json["verifier"])
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(auth.requests, "post", fake_post)
    return polled


def _pending_store() -> dict:
    try:
        with open(auth._pending_login_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _printed(capsys) -> str:
    """Everything the command showed: progress is stdout in human mode."""
    out, err = capsys.readouterr()
    return out + err


def _printed_challenge(text: str) -> str:
    match = re.search(r"\?challenge=([0-9a-f]{64})", text)
    assert match, text
    return match.group(1)


def test_the_url_carries_the_digest_and_never_the_verifier(monkeypatch, capsys) -> None:
    polled = _redeem_answers(monkeypatch, PENDING, APPROVED)

    payload = auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    shown = _printed(capsys)
    verifier = polled[0]
    challenge = _printed_challenge(shown)
    assert challenge == hashlib.sha256(verifier.encode("utf-8")).hexdigest()
    assert verifier not in shown
    # The code the page shows beside the approve button.
    assert f"{challenge[:4].upper()}-{challenge[4:8].upper()}" in shown
    assert payload["custom_token"] == "CT"
    assert payload["user_email"] == "a@example.com"


def test_a_collected_approval_forgets_the_verifier(monkeypatch) -> None:
    _redeem_answers(monkeypatch, APPROVED)

    auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    assert KEY not in _pending_store()


def test_an_interrupted_wait_resumes_the_same_link(monkeypatch, capsys) -> None:
    """The harness kills the command before the user approves; the re-run must
    poll with the verifier behind the link the user is about to approve."""
    first = _redeem_answers(monkeypatch, PENDING)
    with pytest.raises(RemoteAuthError, match="run the same command again"):
        auth._receive_via_approval(API, LOGIN, KEY, timeout=0)
    first_link = _printed_challenge(_printed(capsys))

    second = _redeem_answers(monkeypatch, APPROVED)
    auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    assert second[0] == first[0]
    assert _printed_challenge(_printed(capsys)) == first_link


def test_a_stale_pending_link_is_replaced(monkeypatch) -> None:
    first = _redeem_answers(monkeypatch, PENDING)
    with pytest.raises(RemoteAuthError):
        auth._receive_via_approval(API, LOGIN, KEY, timeout=0)

    later = time.time() + auth._PENDING_LOGIN_MAX_AGE_SECONDS + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    second = _redeem_answers(monkeypatch, APPROVED)
    auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    assert second[0] != first[0]


def test_logins_to_different_profiles_do_not_share_a_link(monkeypatch) -> None:
    a = _redeem_answers(monkeypatch, PENDING)
    with pytest.raises(RemoteAuthError):
        auth._receive_via_approval(API, LOGIN, "work https://api.example", timeout=0)
    b = _redeem_answers(monkeypatch, PENDING)
    with pytest.raises(RemoteAuthError):
        auth._receive_via_approval(API, LOGIN, "personal https://api.example", timeout=0)

    assert a[0] != b[0]


def test_the_pending_store_is_private(monkeypatch) -> None:
    _redeem_answers(monkeypatch, PENDING)
    with pytest.raises(RemoteAuthError):
        auth._receive_via_approval(API, LOGIN, KEY, timeout=0)

    assert os.stat(auth._pending_login_path()).st_mode & 0o777 == 0o600


def test_a_network_blip_while_waiting_keeps_waiting(monkeypatch) -> None:
    _redeem_answers(monkeypatch, requests.ConnectionError("reset"), PENDING, APPROVED)

    payload = auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    assert payload["custom_token"] == "CT"


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (_Resp(403, {"detail": "cli_login_account_refused"}), "suspended or deleted"),
        (_Resp(401, {"detail": "Not authenticated"}), "does not support browser-approved login"),
    ],
)
def test_a_refusal_ends_the_link(monkeypatch, answer, message) -> None:
    _redeem_answers(monkeypatch, answer)

    with pytest.raises(RemoteAuthError, match=message):
        auth._receive_via_approval(API, LOGIN, KEY, timeout=30)
    assert KEY not in _pending_store()


def test_a_server_error_while_waiting_keeps_waiting(monkeypatch, capsys) -> None:
    """A deploy rolling or a failed mint answers 5xx and leaves the approval
    in place; the next poll collects it."""
    _redeem_answers(
        monkeypatch,
        _Resp(503, {"detail": "custom_token_mint_failed"}),
        _Resp(502, {"detail": "Bad Gateway"}),
        APPROVED,
    )

    payload = auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    assert payload["custom_token"] == "CT"
    shown = _printed(capsys)
    assert "503 custom_token_mint_failed" in shown
    assert "502 Bad Gateway" in shown


def test_a_server_error_that_outlasts_the_wait_keeps_the_link(monkeypatch) -> None:
    first = _redeem_answers(monkeypatch, _Resp(503, {"detail": "custom_token_mint_failed"}))
    with pytest.raises(RemoteAuthError, match="503 custom_token_mint_failed"):
        auth._receive_via_approval(API, LOGIN, KEY, timeout=0)

    second = _redeem_answers(monkeypatch, APPROVED)
    auth._receive_via_approval(API, LOGIN, KEY, timeout=30)

    assert second[0] == first[0]


def test_manual_login_stores_the_approved_session(monkeypatch, capsys) -> None:
    """End to end through ``AuthCommands.login``: config, approval, the
    custom-token exchange, and the backend's confirmation."""
    from scriptit_cli.remote_auth import load_credentials

    monkeypatch.setattr(
        auth,
        "_fetch_auth_config",
        lambda _api: {"mode": "firebase", "app_url": "https://app.example"},
    )
    _redeem_answers(monkeypatch, PENDING, APPROVED)
    exchanged: dict = {}

    def fake_exchange(api_key, custom_token, app_url=None):
        exchanged.update(api_key=api_key, custom_token=custom_token, app_url=app_url)
        return {"id_token": "ID", "refresh_token": "RT", "id_token_expires_at": time.time() + 3600}

    monkeypatch.setattr(auth, "exchange_custom_token", fake_exchange)
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *a, **k: _Resp(200, {"id": "uid-1", "email": "a@example.com"}),
    )

    auth.AuthCommands().login(api_url=API, manual=True)

    assert exchanged == {"api_key": "AIza", "custom_token": "CT", "app_url": "https://app.example"}
    creds = load_credentials()
    assert creds["refresh_token"] == "RT"
    assert creds["user_email"] == "a@example.com"
    assert "a@example.com" in capsys.readouterr().out


def test_login_on_a_desktop_opens_the_approval_link(monkeypatch, capsys) -> None:
    """With no agent driving, the CLI opens the same link it prints — and
    that link is the only thing handed to the browser."""
    from scriptit_cli import client

    for var, _slug in client._CLIENT_ENV_MARKERS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("SCRIPTIT_CLIENT", raising=False)
    monkeypatch.setattr(
        auth,
        "_fetch_auth_config",
        lambda _api: {"mode": "firebase", "app_url": "https://app.example"},
    )
    polled = _redeem_answers(monkeypatch, APPROVED)
    opened: list = []
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        auth,
        "exchange_custom_token",
        lambda *a, **k: {
            "id_token": "ID",
            "refresh_token": "RT",
            "id_token_expires_at": time.time() + 3600,
        },
    )
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *a, **k: _Resp(200, {"id": "uid-1", "email": "a@example.com"}),
    )

    auth.AuthCommands().login(api_url=API)

    challenge = hashlib.sha256(polled[0].encode("utf-8")).hexdigest()
    assert opened == [f"https://app.example/app/cli-auth?challenge={challenge}"]
    assert opened[0] in _printed(capsys)


def test_manual_never_opens_a_browser(monkeypatch) -> None:
    from scriptit_cli import client

    for var, _slug in client._CLIENT_ENV_MARKERS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("SCRIPTIT_CLIENT", raising=False)
    monkeypatch.setattr(
        auth,
        "_fetch_auth_config",
        lambda _api: {"mode": "firebase", "app_url": "https://app.example"},
    )
    _redeem_answers(monkeypatch, _Resp(403, {"detail": "cli_login_account_refused"}))
    opened: list = []
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: opened.append(url) or True)

    with pytest.raises(SystemExit):
        auth.AuthCommands().login(api_url=API, manual=True)
    assert opened == []
