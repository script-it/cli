"""Tests for usage analytics — mostly about what must *not* be sent.

The module makes a promise in its docstring: command names leave, command
*contents* never do. That promise is only worth something if it is enforced
somewhere, so the central test drives a real invocation end to end and asserts
the argument bytes appear nowhere in the payload that goes out.
"""

from __future__ import annotations

import json

import pytest

from scriptit_cli import analytics as mod
from scriptit_cli.analytics import Analytics, safe_command

API_URL = "https://api.example.test"


@pytest.fixture
def collector(monkeypatch):
    """A bound Analytics whose deployment records instead of receiving."""
    sent = []

    def _post(url, json, headers, timeout):
        sent.append({"url": url, "body": json, "headers": headers})

        class _Resp:
            status_code = 204

        return _Resp()

    import requests

    monkeypatch.setattr(requests, "post", _post)
    mod.analytics.bind(API_URL, lambda: "test-token")
    return sent


# ---------------------------------------------------------------------------
# safe_command — the filter every event name passes through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["session", "new"], "session new"),
        (["fs", "pull"], "fs pull"),
        (["auth", "login"], "auth login"),
        (["trigger", "create"], "trigger create"),
        # Arguments are dropped, whatever they are.
        (["start", "quarterly-revenue"], "start"),
        (["exec", "--", "cat", "/home/me/.env"], "exec"),
        (["fs", "push", "/local/secrets.json", "/workspaces/w/x"], "fs push"),
        (["run", "/workspaces/w/secret.py"], "run"),
        # A first token that is not a verb reports as a count, not as itself.
        (["/var/data/some/path"], "other"),
        (["--json"], "other"),
        (["Bearer_abc123"], "other"),
        ([], "other"),
    ],
)
def test_safe_command_keeps_verbs_and_drops_data(argv, expected):
    assert safe_command(argv) == expected


def test_safe_command_stops_at_the_first_non_verb():
    # `trigger` survives; the script name after it does not, even though a
    # third token would otherwise be in range.
    assert safe_command(["trigger", "My Script", "run"]) == "trigger"


def test_safe_command_output_is_always_short_and_bounded():
    long_token = "a" * 500
    assert safe_command([long_token, long_token]) == "other"


def test_every_client_command_is_a_known_token():
    """A client verb missing from the allowlist would report as `other`.

    Adding a command to CLIENT_COMMANDS and forgetting this set is the drift
    that quietly turns a real number into an unlabeled bucket.
    """
    from scriptit_cli.remote import CLIENT_COMMANDS

    words = {c for c in CLIENT_COMMANDS if not c.startswith("-")}
    assert words <= mod._KNOWN_TOKENS


# ---------------------------------------------------------------------------
# The end-to-end promise
# ---------------------------------------------------------------------------


def test_a_real_invocation_leaks_nothing_from_its_arguments(collector, monkeypatch):
    """Drive ``main()`` with secrets in argv and inspect what goes on the wire."""
    from scriptit_cli import main as main_mod

    secret = "s3cr3t-database-password"
    path = "/Users/someone/private/keys.pem"
    monkeypatch.setattr(
        main_mod.sys, "argv", ["scriptit", "exec", "--", "psql", f"--password={secret}", path]
    )
    # The command itself never runs; only the analytics wiring is under test.
    monkeypatch.setattr(main_mod, "remote_exec", lambda argv: 7)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 7

    mod.analytics.flush()
    assert len(collector) == 1
    body = json.dumps(collector[0]["body"])
    assert secret not in body
    assert path not in body
    assert "psql" not in body

    event = collector[0]["body"]
    assert event["event_name"] == "cli_command_completed"
    assert event["properties"]["command"] == "exec"
    assert event["properties"]["exit_code"] == 7
    assert event["properties"]["ok"] is False


def test_events_go_to_the_deployment_the_command_authenticated_against(collector):
    mod.analytics.track("cli_command_completed", command="version")
    mod.analytics.flush()
    assert collector[0]["url"] == f"{API_URL}{mod.ENDPOINT}"
    assert collector[0]["headers"]["Authorization"] == "Bearer test-token"


def test_no_identity_of_any_kind_is_in_the_payload(collector):
    """Attribution is the bearer token; the body carries no id at all.

    The sink derives user_id from the token and refuses to read one from the
    body, and nothing is stored on disk to correlate runs — that is the whole
    scheme.
    """
    mod.analytics.track("cli_command_completed", command="version")
    mod.analytics.flush()
    event = collector[0]["body"]
    assert set(event) == {"event_name", "properties"}
    assert not any(key.lower() == "id" or "user" in key.lower() for key in event["properties"])


def test_each_event_is_its_own_request(collector):
    """The sink takes one event per request, not a batch."""
    mod.analytics.track("cli_login_succeeded", mode="firebase")
    mod.analytics.track("cli_command_completed", command="auth login")
    mod.analytics.flush()
    assert [c["body"]["event_name"] for c in collector] == [
        "cli_login_succeeded",
        "cli_command_completed",
    ]


# ---------------------------------------------------------------------------
# Opt-out, and the default of collecting nothing
# ---------------------------------------------------------------------------


def test_an_unbound_client_collects_nothing():
    """Not logged in, or a command that never reached a deployment."""
    fresh = Analytics()
    assert fresh.enabled is False
    fresh.track("cli_command_completed", command="version")
    assert fresh._events == []


def test_do_not_track_wins_over_a_bound_client(collector, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert mod.analytics.enabled is False
    mod.analytics.track("cli_command_completed", command="version")
    mod.analytics.flush()
    assert collector == []


def test_do_not_track_zero_is_not_an_opt_out(collector, monkeypatch):
    # `DO_NOT_TRACK=0` is the convention's way of saying "tracking is fine".
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    assert mod.analytics.enabled is True


def test_there_is_no_analytics_command_to_forward():
    """The opt-out is an env var, so `analytics` stays the platform's word."""
    from scriptit_cli.main import JSON_COMMANDS
    from scriptit_cli.remote import CLIENT_COMMANDS

    assert "analytics" not in CLIENT_COMMANDS
    assert "analytics" not in JSON_COMMANDS


# ---------------------------------------------------------------------------
# Never in the way
# ---------------------------------------------------------------------------


def test_an_unreachable_deployment_does_not_raise(monkeypatch):
    import requests

    def _boom(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "post", _boom)
    mod.analytics.bind(API_URL, lambda: "test-token")
    mod.analytics.track("cli_command_completed", command="version")
    mod.analytics.flush()  # must not raise
    assert mod.analytics._events == []


def test_a_failing_token_refresh_does_not_raise(monkeypatch):
    """An expired credential must cost the event, not the command."""
    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: None)

    def _expired():
        raise RuntimeError("refresh failed")

    mod.analytics.bind(API_URL, _expired)
    mod.analytics.track("cli_command_completed", command="version")
    mod.analytics.flush()
    assert mod.analytics._events == []


def test_the_token_is_only_minted_when_there_is_something_to_send():
    """A command that authenticated but reported nothing must not refresh."""
    calls = []
    mod.analytics.bind(API_URL, lambda: calls.append(1) or "t")
    mod.analytics.flush()
    assert calls == []
