"""Unit tests for the workstation remote-CLI layer.

Covers the pure pieces of :mod:`scriptit_cli.remote` and
:mod:`scriptit_cli.remote_auth`: shell-wrapper construction, exit-sentinel
parsing, SSE framing, the profile credential store, per-profile state, and
sandbox detection. Transport behavior (dispatch and SSE collection against a
live deployment) needs an account, so it is exercised by hand before release.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time

import pytest

from scriptit_cli import errors, remote, remote_auth, util

SENTINEL = "__SCRIPTIT_CLI_EXIT_deadbeefdeadbeef__:"
LOG_PATH = "data_files/.scriptit-cli-deadbeefdeadbeef.log"


# ---------------------------------------------------------------------------
# build_shell_wrapper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo a; echo b",
        "echo $HOME",
        "echo `date`",
        "echo hi # comment",
        "echo trailing\\",
        "line1\nline2",
        'echo "q u o"',
    ],
)
def test_every_command_reports_its_real_exit_code(command: str) -> None:
    """Exit codes are forwarded verbatim, whatever the command looks like.

    An `&&`/`||` sentinel can only say 0-or-1, so a tool whose 2 means
    something different from its 1 was indistinguishable from a plain failure.
    """
    wrapped = remote.build_shell_wrapper(command, SENTINEL, LOG_PATH)
    assert wrapped.startswith("printf %s ")
    payload = base64.b64decode(wrapped.split()[2]).decode("utf-8")
    assert command in payload  # verbatim, never re-quoted
    assert f"{SENTINEL}%d" in payload and '"$?"' in payload
    assert payload.endswith(f") | tee {LOG_PATH}")
    assert "\n" not in wrapped  # the dispatched string stays single-line


def test_the_tee_stays_out_of_the_block_data_dir() -> None:
    """A block's `files_written` is a before/after diff of the session's
    `data_files/`, so a log tee'd there is reported as a file the user's script
    wrote — on every run made through this client. The run dir is the other
    subtree the file API exposes, and nothing diffs it."""
    assert not remote.RemoteClient._LOG_PATH.startswith("data_files/")
    assert remote.RemoteClient._LOG_PATH.startswith("scriptit/")
    # The wrapper creates whatever directory the path names — a `tee` into a
    # missing directory fails, and recovery would silently stop working.
    wrapped = remote.build_shell_wrapper("echo hi", SENTINEL, "somedir/x.log")
    payload = base64.b64decode(wrapped.split()[2]).decode("utf-8")
    assert payload.startswith("mkdir -p somedir")


def test_dispatched_string_names_the_command_for_the_transcript() -> None:
    """The payload is base64, so the app would otherwise show nothing about
    what ran. The comment is the only readable part, and must stay inert."""
    wrapped = remote.build_shell_wrapper("scriptit start weekly-report", SENTINEL, LOG_PATH)
    assert wrapped.endswith("# scriptit start weekly-report")
    assert "$" not in wrapped  # nothing for the outer expansion pass to find


@pytest.mark.parametrize(
    "hostile",
    ["echo $(id)", "echo `id`", 'echo "x"', "echo a\\", "rm -rf / # ;", "a\nb"],
)
def test_ui_comment_cannot_carry_shell_syntax(hostile: str) -> None:
    """The comment puts command text back into the dispatched string, which is
    the one place user input could act again."""
    comment = remote._ui_comment(hostile)
    assert not (set(comment) & set("$`\\\"'();|&<>\n"))


# ---------------------------------------------------------------------------
# split_exit_sentinel
# ---------------------------------------------------------------------------


def test_split_exit_sentinel_reads_code_and_strips_tail() -> None:
    raw = f"line1\nline2\n{SENTINEL}7\n"
    output, code = remote.split_exit_sentinel(raw, SENTINEL)
    assert output == "line1\nline2"
    assert code == 7


def test_split_exit_sentinel_missing_means_unknown() -> None:
    output, code = remote.split_exit_sentinel("truncated output...", SENTINEL)
    assert code is None
    assert output == "truncated output..."


def test_split_exit_sentinel_ignores_trailing_noise() -> None:
    # Late background output flushed after the sentinel line must not be
    # folded into the exit code (0 + "42" is 0, not 42).
    raw = f"work done\n{SENTINEL}0\n[background] thread 42 closed\n"
    _, code = remote.split_exit_sentinel(raw, SENTINEL)
    assert code == 0


def test_split_exit_sentinel_ignores_forged_prefix() -> None:
    forged = "__SCRIPTIT_CLI_EXIT_0000000000000000__:0"
    raw = f"{forged}\nreal output\n{SENTINEL}1\n"
    output, code = remote.split_exit_sentinel(raw, SENTINEL)
    assert code == 1
    assert forged in output


# ---------------------------------------------------------------------------
# Keycloak device grant (PKCE)
# ---------------------------------------------------------------------------


def test_pkce_pair_is_rfc7636_s256() -> None:
    """The platform's Keycloak client pins a PKCE method, and Keycloak enforces
    it on the device endpoint too — a wrong pair fails only at token exchange,
    long after the user has approved."""
    import base64
    import hashlib

    verifier, challenge = remote_auth._pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "=" not in challenge
    assert remote_auth._pkce_pair()[0] != verifier  # fresh per login


# ---------------------------------------------------------------------------
# interrupt handling
# ---------------------------------------------------------------------------


def test_interrupt_reports_cleanly_not_as_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An agent harness enforcing a per-command timeout SIGINTs the CLI
    mid-dispatch; that must exit 130 with one line, not a stack trace."""
    monkeypatch.setattr(remote, "load_credentials", lambda: {"api_url": "http://x"})
    monkeypatch.setattr(remote, "RemoteClient", lambda *a, **k: _InterruptingClient())

    code = remote.remote_dispatch_if_applicable(["status", "run-1"])

    assert code == remote.INTERRUPTED_EXIT_CODE == 130
    err = capsys.readouterr().err
    assert "interrupted" in err and "still be running" in err
    assert "Traceback" not in err


class _InterruptingClient:
    def shell(self, _command: str):
        raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


def test_iter_sse_events_frames_and_multiline_data() -> None:
    stream = [
        b": keepalive",
        b"",
        b"event: session_update",
        b'data: {"a":',
        b"data: 1}",
        b"",
        b"event: message_complete",
        b"data: {}",
        b"",
    ]
    events = list(remote._iter_sse_events(iter(stream)))
    assert events[0][0] == "session_update"
    assert json.loads(events[0][1]) == {"a": 1}
    assert events[1] == ("message_complete", "{}")


# ---------------------------------------------------------------------------
# message ids
# ---------------------------------------------------------------------------


def test_make_message_id_shape() -> None:
    mid = remote.make_message_id()
    assert mid.startswith("msg_")
    body = mid[len("msg_") :]
    assert len(body) == 26
    int(body[:12], 16)  # 12 hex chars encoding the time component


# ---------------------------------------------------------------------------
# credential store (profiles) + state
# ---------------------------------------------------------------------------


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_flat_store_migrates_to_default_profile() -> None:
    path = remote_auth.credentials_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"api_url": "http://flat", "refresh_token": "r"}, f)
    creds = remote_auth.load_credentials()
    assert creds is not None
    assert creds["api_url"] == "http://flat"
    assert creds["profile"] == remote_auth.DEFAULT_PROFILE


def test_profiles_roundtrip_and_switching(monkeypatch: pytest.MonkeyPatch) -> None:
    def loaded() -> dict:
        creds = remote_auth.load_credentials()
        assert creds is not None
        return creds

    remote_auth.save_credentials({"api_url": "http://a"}, profile="a")
    remote_auth.save_credentials({"api_url": "http://b"}, profile="b")
    assert remote_auth.current_profile_name() == "b"  # last save wins
    assert loaded()["api_url"] == "http://b"
    assert remote_auth.set_current_profile("a")
    assert loaded()["api_url"] == "http://a"
    monkeypatch.setenv(remote_auth.ENV_PROFILE, "b")
    assert loaded()["api_url"] == "http://b"
    monkeypatch.delenv(remote_auth.ENV_PROFILE)
    assert not remote_auth.set_current_profile("missing")
    assert remote_auth.delete_credentials(profile="b")
    assert sorted(remote_auth.list_profiles()) == ["a"]
    assert _mode(remote_auth.credentials_path()) == 0o600


def test_state_is_keyed_per_profile_and_0600() -> None:
    remote.update_state("a:http://x", sandbox_id="s1")
    remote.update_state("b:http://x", sandbox_id="s2")
    assert remote.load_state("a:http://x")["sandbox_id"] == "s1"
    assert remote.load_state("b:http://x")["sandbox_id"] == "s2"
    assert remote.load_state("c:http://x") == {}
    assert _mode(remote._state_path()) == 0o600


# ---------------------------------------------------------------------------
# dispatch routing
# ---------------------------------------------------------------------------


def test_routing_depends_on_the_verb_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This client never runs automation itself, so a verb it does not answer
    is always forwarded. Gating that on Script.it's environment variables — as
    an install that shares a machine with the sandbox CLI might — would stop
    forwarding for anyone whose shell exports them."""
    monkeypatch.setenv("SCRIPTIT_API_URL", "http://backend:8000")
    monkeypatch.setenv("SCRIPTIT_LOCAL", "1")
    monkeypatch.setattr(remote, "load_credentials", lambda: {"api_url": "http://x"})
    forwarded = []

    class _Client:
        def shell(self, command: str):
            forwarded.append(command)
            return ("", 0)

    monkeypatch.setattr(remote, "RemoteClient", lambda *a, **k: _Client())

    assert remote.remote_dispatch_if_applicable([]) is None
    assert remote.remote_dispatch_if_applicable(["auth", "status"]) is None
    assert remote.remote_dispatch_if_applicable(["start", "my script"]) == 0
    assert forwarded == ["scriptit start 'my script'"]


# ---------------------------------------------------------------------------
# keycloak device login
# ---------------------------------------------------------------------------


def test_device_login_requests_openid_and_prefers_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend checks ``aud == KEYCLOAK_CLIENT_ID``. Keycloak puts the
    client in ``aud`` on the ID token only — its access token carries no
    matching audience — so a login that hands back the access token is
    rejected *after* the user has already approved in the browser."""
    seen: dict = {}

    class _Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, json.dumps(body)

        def json(self):
            return self._body

    def fake_post(url, data=None, timeout=None, **kw):
        if url.endswith("/auth/device"):
            seen["device"] = data
            return _Resp(
                200,
                {
                    "device_code": "D",
                    "user_code": "U",
                    "verification_uri": "http://kc/device",
                    "interval": 0,
                    "expires_in": 60,
                },
            )
        seen["token"] = data
        return _Resp(
            200,
            {
                "access_token": "ACCESS",
                "id_token": "IDTOK",
                "refresh_token": "R",
                "expires_in": 300,
            },
        )

    monkeypatch.setattr(remote_auth.requests, "post", fake_post)
    monkeypatch.setattr(remote_auth.time, "sleep", lambda _s: None)

    creds = remote_auth.keycloak_device_login("http://kc", "scriptit", "scriptit-web")

    assert seen["device"]["scope"] == "openid"  # else no id_token is issued
    assert seen["device"]["code_challenge_method"] == "S256"
    assert seen["token"]["code_verifier"]
    assert creds["id_token"] == "IDTOK"  # NOT the access token
    assert creds["mode"] == "keycloak"


# ---------------------------------------------------------------------------
# CLI / sandbox version skew
# ---------------------------------------------------------------------------


def test_version_skew_warns_only_when_the_sandbox_is_older() -> None:
    """The sandbox executes the forwarded verb with its OWN CLI, shipped in the
    sandbox image and released separately from this client — so the versions
    differ routinely and only an older sandbox is worth a word. An equality
    check here would fire on every session."""
    from scriptit_cli import MIN_SANDBOX_CLI_VERSION
    from scriptit_cli import agentcontext as ac

    assert ac.skew_warning(f"scriptit {MIN_SANDBOX_CLI_VERSION}") is None
    assert ac.skew_warning("scriptit 9.9.9") is None
    warning = ac.skew_warning("scriptit 0.1.9")
    assert warning and "0.1.9" in warning and MIN_SANDBOX_CLI_VERSION in warning
    # Never guess from unparseable output — an error there is not a skew claim.
    assert ac.skew_warning("scriptit: command not found") is None
    assert ac.skew_warning("") is None


def test_context_probe_emits_every_section() -> None:
    """The bundle is one dispatch split on a marker; a missing section would
    silently shift the others (version text landing in the integrations slot)."""
    from scriptit_cli import agentcontext as ac

    assert ac.PROBE.count(ac.MARKER) == 2
    assert "scriptit version" in ac.PROBE
    assert "scriptit integrations list" in ac.PROBE
    assert "scriptit skills list" in ac.PROBE


def test_the_context_probe_only_calls_platform_verbs() -> None:
    """Every section comes from a verb, never from a path inside the sandbox.

    Reading a file the platform keeps for its own agent would make a client on
    someone's laptop depend on where that file lives — and silently, since the
    probe discards stderr, so a move would surface as an agent that quietly
    stopped knowing what was connected.
    """
    from scriptit_cli import agentcontext as ac

    assert "/workspaces" not in ac.PROBE
    assert "cat " not in ac.PROBE


# ---------------------------------------------------------------------------
# The app links in the context bundle
# ---------------------------------------------------------------------------


class _FakeClient:
    """Enough of RemoteClient for _context_bundle: an app origin and a shell."""

    def __init__(self, app_url: str) -> None:
        self.app_url = app_url

    def shell(self, command, **kwargs):
        from scriptit_cli import agentcontext as ac

        m = ac.MARKER
        return (f"scriptit 9.9.9\n{m}\nno integrations\n{m}\nsome-skill\n", 0)


def test_the_bundle_points_at_this_session_in_the_app() -> None:
    """The agent is told the URL rather than left to reconstruct it.

    The route shape (`/app/s/<session>/workspaces/<wid>/<rel>`) is the app's,
    not this client's, so a guess made in the harness would be a guess about
    someone else's routing.
    """
    from scriptit_cli import agentcontext as ac

    bundle = ac.build(_FakeClient("https://app.example.test"), "sbx_1", "ses_42")
    assert bundle["session_url"] == "https://app.example.test/app/s/ses_42"
    assert bundle["session_url"] in bundle["links"]
    # The script link is that URL plus the path as it appears under /workspaces.
    assert f"{bundle['session_url']}/workspaces/" in bundle["links"]


def test_the_bundle_points_at_where_integrations_get_connected() -> None:
    """Connecting is a browser flow, so the agent needs somewhere to send the
    user — and `?settings=integrations` is the app's own deep link for it."""
    from scriptit_cli import agentcontext as ac

    bundle = ac.build(_FakeClient("https://app.example.test"), "sbx_1", "ses_42")
    assert bundle["integrations_url"] == (
        "https://app.example.test/app/s/ses_42?settings=integrations"
    )
    assert bundle["integrations_url"] in bundle["links"]


def test_the_links_are_available_without_a_round_trip() -> None:
    """`session current` reports them too, so an agent holding a session id
    never has to rebuild a route this client does not own. Deriving them costs
    no shell dispatch, which is why every session-reporting command can."""
    from scriptit_cli import agentcontext as ac

    link = ac.urls(_FakeClient("https://app.example.test"), "ses_42")
    assert link["session_url"] == "https://app.example.test/app/s/ses_42"
    assert link["integrations_url"].endswith("?settings=integrations")
    # No anchor session yet — nothing to link to.
    assert ac.urls(_FakeClient("https://app.example.test"), None)["session_url"] is None


def test_a_deployment_with_no_app_origin_gets_no_link() -> None:
    """Better no link than a fabricated one pointing at the wrong host."""
    from scriptit_cli import agentcontext as ac

    bundle = ac.build(_FakeClient(""), "sbx_1", "ses_42")
    assert bundle["session_url"] is None
    assert bundle["integrations_url"] is None
    assert bundle["links"] is None
    ac.render(bundle)  # must not raise on the missing section


# ---------------------------------------------------------------------------
# exec options: --cwd / --timeout
# ---------------------------------------------------------------------------


def test_exec_options_parse_before_the_separator() -> None:
    """Only what precedes `--` is ours; the command keeps its own flags, or a
    wrapped tool loses the arguments it was invoked for."""
    args, cwd, timeout = remote._parse_exec_options(
        ["--cwd", "/workspaces/w", "--timeout", "30", "--", "pytest", "--timeout", "5"]
    )
    assert args == ["pytest", "--timeout", "5"]
    assert cwd == "/workspaces/w" and timeout == 30.0

    assert remote._parse_exec_options(["--cwd=/w", "--", "ls"]) == (["ls"], "/w", None)
    assert remote._parse_exec_options(["ls", "-la"]) == (["ls", "-la"], None, None)
    with pytest.raises(remote.RemoteError):
        remote._parse_exec_options(["--timeout", "soon", "--", "ls"])
    with pytest.raises(remote.RemoteError):
        remote._parse_exec_options(["--timeout", "0", "--", "ls"])


def test_cwd_change_stays_inside_the_teed_subshell() -> None:
    """The tee path is relative to the session directory. A `cd` that escaped
    the subshell would write the log somewhere the recovery read never looks,
    silently disabling over-cap output recovery."""
    wrapped = remote.build_shell_wrapper(
        remote.apply_cwd("ls", "/workspaces/my project"), SENTINEL, LOG_PATH
    )
    script = base64.b64decode(wrapped.split()[2]).decode()
    assert script.index("cd '/workspaces/my project'") < script.index("| tee")
    assert script.rstrip().endswith(f"| tee {LOG_PATH}")
    assert "exit 1" in script  # a failed cd must not run the command elsewhere


def test_timeout_bounds_the_command_in_the_sandbox() -> None:
    """A local timeout only stops waiting — the command keeps running there.
    The limit has to be applied sandbox-side, and its 124 reported exactly."""
    wrapped = remote.build_shell_wrapper("sleep 99", SENTINEL, LOG_PATH, timeout_s=5)
    script = base64.b64decode(wrapped.split()[2]).decode()
    assert "timeout 5 bash <<'__SCRIPTIT_CMD_" in script
    assert "sleep 99" in script
    # Real $? sentinel (not the &&/|| binary one), else 124 is reported as 1.
    assert '%d\\n\' "$?"' in script


def test_timeout_payload_survives_hostile_command_text() -> None:
    """A quoted heredoc passes the command through unexpanded; the tag is
    per-call random so command text cannot close it early."""
    nasty = "echo \"$HOME\" `id` '__SCRIPTIT_CMD__' # trailing"
    wrapped = remote.build_shell_wrapper(nasty, SENTINEL, LOG_PATH, timeout_s=9)
    script = base64.b64decode(wrapped.split()[2]).decode()
    assert nasty in script
    tag = script.split("<<'")[1].split("'")[0]
    assert tag not in nasty


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


def test_json_flag_only_consumed_for_client_verbs() -> None:
    """`--json` on a forwarded verb belongs to the sandbox's CLI. Eating it
    here would drop the flag the user actually typed."""
    from scriptit_cli.main import extract_json_flag

    assert extract_json_flag(["session", "current", "--json"]) == (
        ["session", "current"],
        True,
    )
    assert extract_json_flag(["trigger", "list", "--json"]) == (
        ["trigger", "list", "--json"],
        False,
    )
    assert extract_json_flag([]) == ([], False)


def test_json_mode_keeps_stdout_to_one_document(capsys) -> None:
    """The whole point: a caller can parse stdout without stripping chatter."""
    from scriptit_cli import output

    output.set_json_mode(True)
    try:
        output.note("connecting...")
        output.warn("an update is available")
        output.emit({"ok": True}, lambda: print("human"))
    finally:
        output.set_json_mode(False)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}
    assert "connecting..." in captured.err and "update" in captured.err


def test_json_mode_reports_errors_as_the_document(capsys) -> None:
    from scriptit_cli import output

    output.set_json_mode(True)
    try:
        with pytest.raises(SystemExit) as exc:
            output.fail("no sandbox")
    finally:
        output.set_json_mode(False)
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "no sandbox"}


# ---------------------------------------------------------------------------
# update notice
# ---------------------------------------------------------------------------


def test_update_notice_only_for_a_strictly_newer_release() -> None:
    from scriptit_cli import update_check

    assert update_check.is_newer("9.9.9", "0.1.0")
    assert not update_check.is_newer("0.1.0", "0.1.0")
    assert not update_check.is_newer("0.0.9", "0.1.0")
    assert not update_check.is_newer(None, "0.1.0")
    assert not update_check.is_newer("garbage", "0.1.0")


def test_update_check_reads_cache_and_never_blocks(monkeypatch, capsys) -> None:
    """The notice comes from the last run's cache; the refresh is a daemon
    thread whose result is only used next time. A synchronous fetch here would
    put a network round-trip in front of every command."""
    import time as _time

    from scriptit_cli import update_check

    monkeypatch.delenv(update_check.ENV_DISABLE, raising=False)
    fetches = []
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: fetches.append(1))
    update_check._write_cache("99.0.0")

    notice = update_check.check(now=_time.time())
    assert notice and "99.0.0" in notice
    assert not fetches  # cache is fresh

    update_check.check(now=_time.time() + update_check.CHECK_INTERVAL_SECONDS + 1)
    _time.sleep(0.2)
    assert fetches  # stale → refreshed in the background

    monkeypatch.setenv(update_check.ENV_DISABLE, "1")
    assert update_check.check() is None


def test_fires_own_separator_is_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire's help output tells users to type `scriptit -- --help`. Treating
    `--` as a verb would answer its own suggestion with a transport error."""
    monkeypatch.setattr(remote, "load_credentials", lambda: {"api_url": "http://x"})
    assert remote.remote_dispatch_if_applicable(["--", "--help"]) is None
    assert remote.remote_dispatch_if_applicable(["--help"]) is None


# ---------------------------------------------------------------------------
# anchor session resolution
# ---------------------------------------------------------------------------


def test_env_names_a_session_for_one_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SCRIPTIT_SESSION` is how a harness gives one task its own transcript,
    without touching the sticky anchor everything else keeps using."""
    client = remote.RemoteClient.__new__(remote.RemoteClient)
    client.profile, client.api_url = "default", "http://x"
    client.state_key = "default:http://x"
    remote.update_state(client.state_key, session_id="ses_sticky", sandbox_id="sb")

    monkeypatch.delenv(remote.ENV_SESSION, raising=False)
    assert client.anchor() == ("ses_sticky", "sb", "state")

    monkeypatch.setenv(remote.ENV_SESSION, "ses_task")
    assert client.anchor()[::2] == ("ses_task", "env")
    # The override is not persisted — the next shell still sees the sticky one.
    assert remote.load_state(client.state_key)["session_id"] == "ses_sticky"


def test_named_session_that_is_gone_is_an_error_not_a_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently creating a different session would run the command somewhere
    the caller did not ask for, and report success."""
    client = remote.RemoteClient.__new__(remote.RemoteClient)
    client.profile, client.api_url = "default", "http://x"
    client.state_key = "default:http://x"
    monkeypatch.setenv(remote.ENV_SESSION, "ses_missing")
    monkeypatch.setattr(remote.RemoteClient, "session_exists", lambda *a: False)

    with pytest.raises(remote.RemoteError, match="ses_missing"):
        client.ensure_session("sb")


# ---------------------------------------------------------------------------
# regressions from review
# ---------------------------------------------------------------------------


def test_keycloak_refresh_keeps_using_the_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend validates `aud == client_id`, which only the ID token
    carries. Refreshing into the access token swaps a working credential for a
    rejected one — and only at expiry, long after login looked fine."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": "ACCESS-no-matching-aud",
                "id_token": "ID-correct-aud",
                "refresh_token": "r2",
                "expires_in": 300,
            }

    monkeypatch.setattr(remote_auth.requests, "post", lambda *a, **k: _Resp())
    out = remote_auth._refresh_keycloak_token(
        {"token_url": "http://kc/token", "client_id": "c", "refresh_token": "r1"}
    )
    assert out["id_token"] == "ID-correct-aud"


def test_timeout_values_that_would_silently_disable_the_limit() -> None:
    """`timeout 0` means *no limit* in GNU timeout, and nan/inf format into
    nonsense — both would drop the bound the caller asked for."""
    assert remote._format_seconds(0.5) == "0.5"  # not int() -> "0"
    assert remote._format_seconds(30.0) == "30"
    for bad in ("nan", "inf", "-inf", "0", "-5", "soon"):
        with pytest.raises(remote.RemoteError):
            remote._parse_exec_options(["--timeout", bad, "--", "ls"])


def test_flag_without_a_value_is_rejected_not_executed() -> None:
    """`scriptit exec --timeout -- ls` used to run `--timeout` as the command."""
    for argv in (["--timeout"], ["--cwd"], ["--cwd", "--", "ls"]):
        with pytest.raises(remote.RemoteError):
            remote._parse_exec_options(argv)


def test_sub_second_timeout_reaches_the_sandbox_intact() -> None:
    wrapped = remote.build_shell_wrapper("sleep 5", SENTINEL, LOG_PATH, timeout_s=0.5)
    payload = base64.b64decode(wrapped.split()[2]).decode()
    assert "timeout 0.5 bash" in payload


def test_concurrent_writers_do_not_drop_each_others_profiles(tmp_path) -> None:
    """Both stores are read-modify-write. Unserialized, the later writer
    silently discards whatever the earlier one added — a rotated refresh
    token, or another shell's session."""
    import threading

    path = str(tmp_path / "store.json")

    def add(name: str) -> None:
        for i in range(25):
            util.update_json_file(path, lambda d, n=f"{name}{i}": {**d, n: True})

    threads = [threading.Thread(target=add, args=(n,)) for n in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(path, encoding="utf-8") as f:
        written = json.load(f)
    assert len(written) == 75, f"lost {75 - len(written)} concurrent writes"


def test_not_logged_in_reports_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commonest error path there is. It reaches users before anything
    else does, so a NameError here is the first thing a new user would see."""
    monkeypatch.setattr(remote, "load_credentials", lambda: None)
    with pytest.raises(errors.RemoteAuthError, match="not logged in"):
        remote.RemoteClient()


# ---------------------------------------------------------------------------
# second review round
# ---------------------------------------------------------------------------


def test_json_flag_never_eats_command_data() -> None:
    """`scriptit exec -- tool --json` must run `tool --json`. Consuming a flag
    from after `--` changes what the user asked to run."""
    from scriptit_cli.main import extract_json_flag

    assert extract_json_flag(["exec", "--", "tool", "--json"]) == (
        ["exec", "--", "tool", "--json"],
        False,
    )
    assert extract_json_flag(["exec", "--json", "--", "tool"]) == (
        ["exec", "--json", "--", "tool"],
        False,
    )
    # A client command keeps its own flag, and still nothing past `--`.
    assert extract_json_flag(["fs", "pull", "a", "b", "--json"]) == (
        ["fs", "pull", "a", "b"],
        True,
    )
    assert extract_json_flag(["trigger", "list", "--json"])[1] is False


def _client_for_stream_test(monkeypatch, get_response):
    client = remote.RemoteClient.__new__(remote.RemoteClient)
    client.api_url, client.profile = "http://x", "default"
    client.state_key = "default:http://x"
    client._http = type("S", (), {"get": staticmethod(get_response)})()
    client._cached_user_id = "u1"
    monkeypatch.setattr(remote.RemoteClient, "_headers", lambda self: {})
    monkeypatch.setattr(remote.RemoteClient, "ensure_sandbox", lambda self: "sb")
    monkeypatch.setattr(remote.RemoteClient, "ensure_session", lambda self, s: "ses")
    return client


def test_stream_failure_before_bootstrap_blocks_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command must not be POSTed when there is nowhere to collect its
    output — otherwise the caller sees a failure for work that already ran,
    and retrying repeats its side effects."""
    posted = []
    monkeypatch.setattr(
        remote.RemoteClient,
        "request",
        lambda self, method, path, **kw: posted.append(path),
    )

    class _Ctx:
        def __enter__(self):
            raise remote.requests.RequestException("offline")

        def __exit__(self, *a):
            return False

    client = _client_for_stream_test(monkeypatch, lambda *a, **k: _Ctx())
    with pytest.raises(remote.RemoteError, match="events stream"):
        client.shell("echo hi")
    assert posted == [], f"dispatched anyway: {posted}"


def test_stream_failure_after_bootstrap_ends_collection_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the command is running, a dropped stream is a collection failure —
    it must surface, not strand the caller until the command timeout."""
    accepted = type("R", (), {"status_code": 202, "text": ""})()
    monkeypatch.setattr(remote.RemoteClient, "request", lambda self, *a, **k: accepted)

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def iter_lines(decode_unicode=False):
            yield b"event: load_complete"
            yield b"data: {}"
            yield b""
            raise remote.requests.RequestException("dropped mid-stream")

    client = _client_for_stream_test(monkeypatch, lambda *a, **k: _Resp())
    with pytest.raises(remote.RemoteError, match="dropped"):
        client.shell("echo hi", timeout_s=10)


def test_state_merge_keeps_another_writers_field() -> None:
    """A read-modify-write across the lock boundary reverts whatever changed
    in the gap — the way a freshly chosen session goes missing."""
    remote.update_state("k", session_id="ses_1", sandbox_id="sb_1")
    remote.update_state("k", sandbox_id="sb_2")  # another process
    remote.update_state("k", session_id="ses_2")  # this one
    assert remote.load_state("k") == {"session_id": "ses_2", "sandbox_id": "sb_2"}


def test_corrupt_update_cache_never_breaks_a_command(tmp_path, monkeypatch) -> None:
    """This runs before every invocation; valid-but-wrong-shaped JSON must not
    be the thing that fails one."""
    from scriptit_cli import update_check

    monkeypatch.delenv(update_check.ENV_DISABLE, raising=False)
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: None)
    cache = update_check._cache_path()
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    for junk in ("[]", '"nope"', "null", "{oops"):
        with open(cache, "w", encoding="utf-8") as f:
            f.write(junk)
        assert update_check.check() is None


def test_logout_all_empties_under_the_lock_and_reports_truthfully() -> None:
    remote_auth.save_credentials({"api_url": "http://a", "refresh_token": "r"}, profile="a")
    assert remote_auth.delete_credentials(all_profiles=True) is True
    assert remote_auth.list_profiles() == {}
    # Nothing left to remove: saying "removed" would be a lie.
    assert remote_auth.delete_credentials(all_profiles=True) is False


def _sse(event: str, payload: dict):
    yield f"event: {event}".encode()
    yield f"data: {json.dumps(payload)}".encode()
    yield b""


def test_shell_collects_output_and_the_exit_code_end_to_end(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The core contract, over a faithful fake of the event stream: cumulative
    snapshots replace rather than append, the sentinel is stripped from what
    the user sees, and its code is what the caller gets."""
    sent = {}

    def _request(self, method, path, **kw):
        if method == "POST" and path.endswith("/shell"):
            sent["command"] = kw["json"]["command"]
            sent["message_id"] = kw["json"]["message_id"]
        return type("R", (), {"status_code": 202, "text": ""})()

    monkeypatch.setattr(remote.RemoteClient, "request", _request)

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def iter_lines(decode_unicode=False):
            yield from _sse("load_complete", {})
            while "message_id" not in sent:
                time.sleep(0.01)
            sentinel = sent["command"]  # the payload carries our sentinel
            yield from _sse(
                "session_update",
                {
                    "update": {"sessionUpdate": "tool_call", "toolCallId": "t1"},
                },
            )
            # A cumulative snapshot, then a fuller one that must REPLACE it.
            for text in ("partial", f"hello world\n{_sentinel_of(sentinel)}9\n"):
                yield from _sse(
                    "session_update",
                    {
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "t1",
                            "content": [{"type": "content", "content": {"text": text}}],
                        },
                    },
                )
            yield from _sse("message_complete", {"parentMessageId": sent["message_id"]})

    client = _client_for_stream_test(monkeypatch, lambda *a, **k: _Resp())
    output, code = client.shell("echo hello world", timeout_s=10, echo=False)

    assert output == "hello world"  # snapshot replaced, sentinel stripped
    assert code == 9  # the command's real status
    assert "base64 -d | bash" in sent["command"]


def _sentinel_of(dispatched: str) -> str:
    """Recover the per-call sentinel from the dispatched payload."""
    import base64 as b64

    script = b64.b64decode(dispatched.split()[2]).decode()
    marker = script.split("printf '\\n")[1]
    return marker.split("%d")[0]


def test_a_stream_dying_before_a_redispatch_cancels_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Liveness and arming are one critical section.

    The gate is the same one a post-bootstrap death races; the 409 retry path
    is where it can be hit deterministically, because the client is provably
    parked between two dispatch attempts. Reading the two states separately,
    the retry goes out into a stream that is already gone.
    """
    posted: list[str] = []
    may_die = threading.Event()
    baseline = threading.active_count()

    busy = type("R", (), {"status_code": 409, "text": "busy", "close": lambda self: None})()
    monkeypatch.setattr(
        remote.RemoteClient,
        "request",
        lambda self, method, path, **kw: (posted.append(path), busy)[1],
    )
    # The retry pause is where the stream dies: let the reader raise, then
    # wait for its thread to actually finish so the failure is recorded
    # before the next attempt reaches the gate.
    monkeypatch.setattr(remote, "_BUSY_RETRY_SECONDS", 0)

    real_sleep = time.sleep

    def _wait_for_reader_to_die(_seconds: float) -> None:
        may_die.set()
        deadline = time.time() + 5
        while threading.active_count() > baseline and time.time() < deadline:
            real_sleep(0.01)

    monkeypatch.setattr(remote.time, "sleep", _wait_for_reader_to_die)

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def iter_lines(decode_unicode=False):
            yield b"event: load_complete"
            yield b"data: {}"
            yield b""
            may_die.wait(timeout=5)
            raise remote.requests.RequestException("dropped during the retry pause")

    client = _client_for_stream_test(monkeypatch, lambda *a, **k: _Resp())
    with pytest.raises(remote.RemoteError, match="dropped during the retry pause"):
        client.shell("echo hi", timeout_s=10)
    assert len(posted) == 1, f"redispatched into a dead stream: {posted}"


# ---------------------------------------------------------------------------
# Credential refresh is a merge into one profile, not a rewrite of the store
# ---------------------------------------------------------------------------


def _store_two_profiles() -> None:
    remote_auth.save_credentials({"api_url": "http://a", "id_token": "ta"}, profile="a")
    remote_auth.save_credentials({"api_url": "http://b", "id_token": "tb"}, profile="b")
    remote_auth.set_current_profile("a")


def _expired(profile: str) -> dict:
    return {
        "profile": profile,
        "api_url": f"http://{profile}",
        "mode": "firebase",
        "id_token": "stale",
        "id_token_expires_at": 0,
        "refresh_token": "r",
        "firebase_api_key": "k",
    }


def test_a_refresh_does_not_move_the_default_profile(monkeypatch) -> None:
    """`SCRIPTIT_PROFILE=b` names a profile for one invocation. A refresh that
    made it the stored default would silently retarget every later command."""
    _store_two_profiles()
    monkeypatch.setattr(
        remote_auth,
        "_refresh_id_token",
        lambda *a, **k: {"id_token": "fresh", "id_token_expires_at": time.time() + 3600},
    )
    remote_auth.get_fresh_id_token(_expired("b"))
    with open(remote_auth.credentials_path()) as f:
        store = json.load(f)
    assert store["current"] == "a", "a refresh switched the default profile"
    assert store["profiles"]["b"]["id_token"] == "fresh"
    assert store["profiles"]["a"]["id_token"] == "ta"


def test_a_refresh_landing_after_a_logout_does_not_restore_credentials(monkeypatch) -> None:
    """The refresh call is network I/O, so a logout can land while it is in
    flight. Writing the record back would undo a security action the user
    explicitly took."""
    _store_two_profiles()

    def _refresh(*a, **k):
        # The logout happens while the provider call is outstanding.
        remote_auth.delete_credentials(all_profiles=True)
        return {"id_token": "fresh", "id_token_expires_at": time.time() + 3600}

    monkeypatch.setattr(remote_auth, "_refresh_id_token", _refresh)
    assert remote_auth.get_fresh_id_token(_expired("a")) == "fresh"
    assert remote_auth.list_profiles() == {}, "logout was undone by an in-flight refresh"


def test_a_refresh_keeps_fields_another_writer_added(monkeypatch) -> None:
    """A merge, not a rewrite: the store is shared with concurrent commands."""
    remote_auth.save_credentials({"api_url": "http://a", "id_token": "ta"}, profile="a")
    monkeypatch.setattr(
        remote_auth,
        "_refresh_id_token",
        lambda *a, **k: {"id_token": "fresh", "id_token_expires_at": time.time() + 3600},
    )
    remote_auth.merge_credentials("a", {"app_url": "http://app"})  # another process
    remote_auth.get_fresh_id_token(_expired("a"))
    stored = remote_auth.load_credentials("a")
    assert stored["id_token"] == "fresh"
    assert stored["app_url"] == "http://app"


# ---------------------------------------------------------------------------
# Authentication failures are RemoteAuthError, never a traceback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        lambda *a, **k: (_ for _ in ()).throw(remote.requests.ConnectionError("no route")),
        lambda *a, **k: type("R", (), {"status_code": 200, "text": "<html>", "json": _not_json})(),
        lambda *a, **k: type(
            "R", (), {"status_code": 200, "text": "[]", "json": lambda self: []}
        )(),
    ],
    ids=["connection-refused", "html-from-a-proxy", "wrong-json-shape"],
)
def test_identity_provider_failures_are_reported_not_raised(monkeypatch, failure) -> None:
    """A captive portal, a dead link and a proxy that answers HTML are all
    'your login did not work' — a traceback for any of them reads as a crash
    in the CLI rather than a problem with the network."""
    monkeypatch.setattr(remote_auth.requests, "post", failure)
    with pytest.raises(errors.RemoteAuthError):
        remote_auth.exchange_custom_token("key", "custom")
    with pytest.raises(errors.RemoteAuthError):
        remote_auth._refresh_id_token("key", "refresh")


def _not_json(self):
    raise ValueError("not json")


def test_a_provider_omitting_a_promised_field_is_reported(monkeypatch) -> None:
    ok = type("R", (), {"status_code": 200, "text": "{}", "json": lambda self: {"idToken": "t"}})
    monkeypatch.setattr(remote_auth.requests, "post", lambda *a, **k: ok())
    with pytest.raises(errors.RemoteAuthError, match="refreshToken"):
        remote_auth.exchange_custom_token("key", "custom")


def test_a_junk_expiry_falls_back_instead_of_crashing(monkeypatch) -> None:
    """`expires_in` arrives after the token was already minted, so a bad value
    would fail a login that in fact worked."""
    for value in (None, "soon", "", -1):
        body = {"idToken": "t", "refreshToken": "r", "expiresIn": value}
        resp = type("R", (), {"status_code": 200, "text": "", "json": lambda self, b=body: b})
        monkeypatch.setattr(remote_auth.requests, "post", lambda *a, _r=resp, **k: _r())
        creds = remote_auth.exchange_custom_token("key", "custom")
        assert creds["id_token_expires_at"] > time.time()


# ---------------------------------------------------------------------------
# The --json contract holds on failures this client did not generate
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, capsys, argv):
    from scriptit_cli import main as main_mod
    from scriptit_cli import output

    monkeypatch.setenv("SCRIPTIT_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(main_mod.sys, "argv", ["scriptit", *argv])
    output.set_json_mode(False)
    output._emitted = False
    code = 0
    try:
        main_mod.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_parser_error_still_answers_json_on_stdout(monkeypatch, capsys) -> None:
    """`--json` promises stdout is one document and errors look like
    `{"error": ...}`. The argument parser rejects a bad command line on its
    own, so without this a caller parsing stdout gets an empty stream — the
    exact failure the contract exists to prevent."""
    code, out, err = _run_main(monkeypatch, capsys, ["session", "use", "--json"])
    assert code != 0
    assert json.loads(out).get("error"), f"stdout was not a JSON error: {out!r}"
    assert err, "the parser's own message belongs on stderr"


def test_a_client_error_is_not_answered_twice(monkeypatch, capsys) -> None:
    """`fail()` already wrote the document; a second one would make stdout two
    documents and unparseable."""
    code, out, _ = _run_main(monkeypatch, capsys, ["session", "current", "--json"])
    assert code != 0
    assert json.loads(out).get("error")  # exactly one document parses


def test_json_mode_is_not_claimed_for_a_command_that_streams(monkeypatch) -> None:
    """`fs read` writes the file's bytes unmodified — that is the point of it.
    Promising one JSON document there and then not producing one is worse than
    not claiming the flag."""
    from scriptit_cli.main import extract_json_flag

    assert extract_json_flag(["fs", "read", "--json", "/p"]) == (["fs", "read", "/p"], False)
    assert extract_json_flag(["fs", "ls", "--json", "/p"]) == (["fs", "ls", "/p"], True)


def test_a_cached_version_of_the_wrong_type_never_breaks_a_command(tmp_path, monkeypatch) -> None:
    """The cache is read before every invocation. A well-formed object whose
    `latest` is a number passes an isinstance check on the object and then
    raises inside the version comparison."""
    from scriptit_cli import update_check

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = update_check._cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for latest in (123, [], {"v": 1}, True):
        with open(path, "w") as f:
            json.dump({"checked_at": time.time(), "latest": latest}, f)
        assert update_check.check() is None


def test_a_stream_that_just_ends_is_a_failure_not_a_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader has three terminal outcomes, not two.

    The server ends the stream cleanly — no error event, no exception — when a
    second subscriber in the same group takes over, which happens whenever the
    session is opened in the app. Handling only terminal events and exceptions
    leaves the caller waiting out the whole command timeout for a verdict that
    is never coming.
    """
    accepted = type("R", (), {"status_code": 202, "text": ""})()
    monkeypatch.setattr(remote.RemoteClient, "request", lambda self, *a, **k: accepted)

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def iter_lines(decode_unicode=False):
            yield b"event: load_complete"
            yield b"data: {}"
            yield b""
            # ...and then the server simply stops, mid-command.

    client = _client_for_stream_test(monkeypatch, lambda *a, **k: _Resp())
    started = time.monotonic()
    with pytest.raises(remote.RemoteError, match="closed before the command reported back"):
        client.shell("sleep 30", timeout_s=30)
    assert time.monotonic() - started < 5, "waited out the command timeout instead of reporting"


# ---------------------------------------------------------------------------
# Version comparison follows PEP 440, not a truncated (major, minor, patch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latest,current,expected,why",
    [
        ("1.2.3", "1.2.3rc1", True, "a final release is newer than its own rc"),
        ("1.2.3rc1", "1.2.3", False, "an rc is not newer than the final"),
        ("1.2.3.post1", "1.2.3", True, "a post-release is newer"),
        ("2.0.0", "1!1.0.0", False, "an epoch outranks everything without one"),
        ("1.10.0", "1.9.0", True, "ordinary ordering still holds"),
        ("1.2.3", "1.2.3", False, "equal is not newer"),
    ],
)
def test_update_notice_ranks_versions_by_pep440(latest, current, expected, why) -> None:
    """A truncated (major, minor, patch) tuple gets three of these wrong.

    It drops everything PEP 440 puts after the release segment, so `1.2.3rc1`
    reads as *equal* to `1.2.3` — the notice never fires for someone sitting on
    a prerelease — and an epoch is read as the version it prefixes.
    """
    from scriptit_cli import update_check

    assert update_check.is_newer(latest, current) is expected, why


@pytest.mark.parametrize(
    "text,expected",
    [
        ("scriptit 0.2.0", "0.2.0"),
        ("scriptit-cli 1.2.3rc1", "1.2.3rc1"),
        # A bare integer in surrounding prose is not a version.
        ("Python 3 scriptit 0.2.0", "0.2.0"),
        # A missing answer, not an old one.
        ("command not found", None),
        ("", None),
        ("scriptit: error", None),
    ],
)
def test_a_version_is_extracted_from_whatever_the_command_printed(text, expected) -> None:
    parsed = util.parse_version(text)
    assert (str(parsed) if parsed else None) == expected


def test_the_store_lock_serializes_concurrent_writers(tmp_path) -> None:
    """The lock is what keeps a read-modify-write from losing the other side.

    Threads rather than processes so the test stays fast, but the lock is the
    same object either way: without it the later writer overwrites whatever the
    earlier one added between its read and its write.
    """
    path = str(tmp_path / "store.json")
    barrier = threading.Barrier(4)

    def _writer(name: str) -> None:
        barrier.wait()
        for _ in range(10):
            util.update_json_file(path, lambda d, n=name: {**d, n: d.get(n, 0) + 1})

    workers = [threading.Thread(target=_writer, args=(f"w{i}",)) for i in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(30)

    with open(path) as f:
        final = json.load(f)
    assert final == {f"w{i}": 10 for i in range(4)}, f"lost updates: {final}"
