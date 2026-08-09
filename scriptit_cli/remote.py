"""Remote transport for the workstation CLI.

``scriptit`` is a thin remote shell: verbs are forwarded verbatim into the
user's sandbox (the platform's session-shell API) and the output is
collected from the session's SSE event stream. The in-sandbox CLI stays the
single behavior surface — this client never executes automation locally, so
every verb outside :data:`CLIENT_COMMANDS` is forwarded unconditionally.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import posixpath
import queue
import re
import secrets
import shlex
import string
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from scriptit_cli.analytics import analytics
from scriptit_cli.client import detect_client
from scriptit_cli.config import _config_dir
from scriptit_cli.errors import RemoteAuthError, RemoteError, ScriptItError
from scriptit_cli.remote_auth import get_fresh_id_token, load_credentials
from scriptit_cli.util import update_json_file

STATE_FILE = "remote_state.json"

# Names the anchor session for one invocation, overriding the sticky one.
ENV_SESSION = "SCRIPTIT_SESSION"

# Verbs this client answers itself; everything else is forwarded into the
# sandbox and answered by the CLI running there.
CLIENT_COMMANDS = {
    "auth",
    "fs",
    "session",
    "sandbox",
    "exec",
    "version",
    "--version",
    "-V",
    "help",
    "--help",
    "-h",
    # Fire's argument separator, and what it tells users to type in its own
    # help output ("Showing help with the command 'scriptit -- --help'").
    # Forwarding it would answer that suggestion with a transport error.
    "--",
}


# How long to wait out a 409 SESSION_BUSY before re-attempting dispatch.
_BUSY_RETRY_SECONDS = 3

# Stream lifecycle for `shell()`; see the state machine there.
_STREAM_BOOTSTRAPPING = 0
_STREAM_LIVE = 1
_STREAM_DISPATCHED = 2


def make_message_id() -> str:
    """Message id, in the shape the session service expects: low 48 bits of
    ``ms * 0x1000 + counter`` as 12 hex chars, then 14 base62.
    Messages are ordered lexicographically by id, so an id of a different
    shape (or time scale) sorts into the wrong place.
    """
    encoded = int(time.time() * 1000) * 0x1000 + 1
    hex_part = format(encoded, "x").rjust(12, "0")[-12:]
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    rand = "".join(secrets.choice(alphabet) for _ in range(14))
    return f"msg_{hex_part}{rand}"


def apply_cwd(command: str, cwd: Optional[str]) -> str:
    """Run ``command`` from ``cwd``, failing rather than running elsewhere.

    The ``cd`` goes into the command itself, so it lands inside the subshell
    :func:`build_shell_wrapper` builds and the tee still resolves relative to
    the session directory. Note this makes the command multi-line, which is
    what routes it down the exact-exit-code branch.
    """
    if not cwd:
        return command
    return f"cd {shlex.quote(cwd)} || exit 1\n{command}"


# Bounded by the sandbox's own `timeout`, so the command dies there rather than
# outliving a client that gave up. Quoted heredoc: the payload reaches bash
# with no expansion and no quoting to get wrong, whatever the command contains.
_HEREDOC_TAG = "__SCRIPTIT_CMD_{token}__"

# What may appear in the trailing UI comment. No shell metacharacter is on
# this list, so nothing from the command can act once it is echoed back into
# the dispatched string.
_UI_COMMENT_ALLOWED = re.compile(r"[^A-Za-z0-9 ._/:@=+-]")


def _format_seconds(seconds: float) -> str:
    """Seconds for GNU ``timeout``, keeping sub-second values.

    ``int()`` would turn 0.5 into ``timeout 0``, which means *no limit* —
    silently removing the bound the caller asked for.
    """
    if seconds == int(seconds):
        return str(int(seconds))
    return f"{seconds:g}"


def _ui_comment(command: str) -> str:
    """A readable trailing comment naming what the payload runs.

    The dispatched string is base64, which tells someone watching the session
    in the app nothing. This puts the command's first line back where they can
    read it, filtered to characters that cannot do anything.
    """
    first = (command.strip().splitlines() or [""])[0]
    safe = " ".join(_UI_COMMENT_ALLOWED.sub(" ", first).split())[:120]
    return f"  # {safe}" if safe else ""


def build_shell_wrapper(
    command: str,
    sentinel: str,
    log_path: str,
    timeout_s: Optional[float] = None,
) -> str:
    """The `session/shell` dispatch string for ``command`` + exit ``sentinel``.

    The session runtime executes shell commands as
    ``eval ${JSON.stringify(command)}`` inside a double-quoted bash -c
    script, so the dispatched string undergoes
    one round of double-quote expansion BEFORE eval: `$`, backticks and
    newlines expand or collapse too early, `#` would comment out anything
    appended, and a trailing ``\\`` folds the wrapper into the command.

    Every command therefore rides a base64 payload piped into bash, where it
    arrives verbatim and the real ``$?`` reaches the sentinel. An `&&`/`||`
    sentinel would keep the command readable in the dispatched string, but it
    can only report 0-or-1 — a tool whose exit 2 means something specific
    would be indistinguishable from a plain failure. The exit code is the
    contract; :func:`_ui_comment` recovers the readability instead.

    Every dispatch also tees combined stdout+stderr into ``log_path``
    (relative to the session's own cwd, and inside a subtree the file API
    exposes): the platform event stream caps live text at ~8KB, so ``shell()``
    falls back to reading this file whole when a command's real output
    overflows it.
    The sentinel is printed **inside** the group being tee'd, so the log is
    a complete transcript — output *and* exit code — not just the part
    ``split_exit_sentinel`` can't score.

    ``timeout_s`` bounds the command sandbox-side; it exits 124 when the limit
    is hit, and forces the base64 branch so that code is reported exactly.
    """
    if timeout_s:
        tag = _HEREDOC_TAG.format(token=secrets.token_hex(4))
        inner = f"timeout {_format_seconds(timeout_s)} bash <<'{tag}'\n{command}\n{tag}"
    else:
        inner = command
    # Subshell parens so a user `exit N` still reaches the sentinel, which
    # reads the real exit code here (this shell is ours).
    # Derived, never hardcoded: a `tee` into a directory that does not exist
    # yet fails, and the two drifting apart would break recovery silently.
    script = (
        f"mkdir -p {posixpath.dirname(log_path)} 2>/dev/null\n"
        f"(\n(\n{inner}\n) 2>&1\n"
        f"printf '\\n{sentinel}%d\\n' \"$?\"\n"
        f") | tee {log_path}"
    )
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"printf %s {encoded} | base64 -d | bash{_ui_comment(command)}"


def split_exit_sentinel(raw: str, sentinel: str) -> Tuple[str, Optional[int]]:
    """Split collected output into (visible output, exit code).

    The exit code is ``None`` when the sentinel never arrived — e.g. the
    stream cap truncated the tail — which callers must treat as unknown,
    never success.
    """
    idx = raw.rfind(sentinel)
    if idx == -1:
        return raw, None
    # Leading contiguous digits only: late output (background-process noise
    # flushed after the sentinel line) must not be folded into the code.
    tail = raw[idx + len(sentinel) :].lstrip()
    digits = ""
    for ch in tail:
        if not ch.isdigit():
            break
        digits += ch
    exit_code = int(digits[:4]) if digits else None
    return raw[:idx].rstrip("\n"), exit_code


# ---------------------------------------------------------------------------
# Client state (chosen sandbox + anchor session per api_url)
# ---------------------------------------------------------------------------


def _state_path() -> str:
    return os.path.join(_config_dir(), STATE_FILE)


def load_state(state_key: str) -> Dict[str, Any]:
    """The persisted client state for one profile+platform (chosen sandbox,
    anchor session), or ``{}``. Keyed by ``<profile>:<api_url>`` so two
    accounts against the same platform never share a session."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            all_state = json.load(f)
        entry = all_state.get(state_key)
        return entry if isinstance(entry, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(state_key: str, **fields: Any) -> None:
    """Merge ``fields`` into one profile+platform's state.

    The merge happens *inside* the lock, against whatever is on disk right
    then. Loading an entry, editing the copy and writing it back would hold a
    stale snapshot across the gap — another process that changed a different
    field in between would have its change reverted, which is how a freshly
    chosen session goes missing after a sandbox update.
    """

    def _mutate(all_state: Dict[str, Any]) -> Dict[str, Any]:
        entry = all_state.get(state_key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(fields)
        all_state[state_key] = entry
        return all_state

    # 0600 like the credential store: this file carries live session ids.
    update_json_file(_state_path(), _mutate)


# ---------------------------------------------------------------------------
# SSE parsing (minimal, requests-based; no extra dependency)
# ---------------------------------------------------------------------------


def _iter_sse_events(lines: Iterable[bytes]) -> Iterable[Tuple[str, str]]:
    """Yield ``(event_name, data)`` pairs from an SSE byte-line stream.

    Handles multi-line ``data:`` fields and ignores comments/keepalives.
    ``event_name`` is ``"message"`` when the stream doesn't name events.
    """
    event_name = "message"
    data_lines: List[str] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            # SSE spec: strip at most ONE leading space after the colon.
            val = line[len("data:") :]
            data_lines.append(val[1:] if val.startswith(" ") else val)
    if data_lines:
        yield event_name, "\n".join(data_lines)


# ---------------------------------------------------------------------------
# Remote client
# ---------------------------------------------------------------------------


class RemoteClient:
    """Authenticated client for the backend REST API + sandbox proxy."""

    def __init__(self) -> None:
        creds = load_credentials()
        if not creds:
            raise RemoteAuthError(
                "not logged in — run `scriptit auth login` "
                "(add `--api-url <origin>` for a self-hosted deployment)"
            )
        self.creds = creds
        self.api_url = str(creds["api_url"]).rstrip("/")
        # The browser origin of the same deployment, recorded at login. Only
        # ever used to build links a human opens; empty when the deployment
        # did not advertise one, in which case callers omit the link rather
        # than guessing an origin.
        self.app_url = str(creds.get("app_url") or "").rstrip("/")
        self.profile = str(creds.get("profile") or "default")
        # State (chosen sandbox, anchor session) is per profile+platform so
        # switching accounts never reuses another account's session.
        self.state_key = f"{self.profile}:{self.api_url}"
        self._http = requests.Session()
        self._cached_user_id: Optional[str] = None
        # Usage events ride this same credential and deployment, so building a
        # client is what makes reporting possible at all — a command that never
        # authenticated reports nothing.
        analytics.bind(self.api_url, lambda: get_fresh_id_token(self.creds))

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {get_fresh_id_token(self.creds)}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        wake_on_503: bool = True,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> requests.Response:
        """One API call, transparently riding out sandbox resume/busy 503s."""
        url = f"{self.api_url}{path}"
        deadline = time.time() + 180 if wake_on_503 else time.time()
        while True:
            try:
                resp = self._http.request(
                    method, url, headers=self._headers(), timeout=timeout, **kwargs
                )
            except requests.RequestException as exc:
                # DNS failure, refused connection, dropped link — surface as
                # the CLI's own error type so every caller prints it cleanly.
                raise RemoteError(f"API request failed: {exc}") from exc
            if resp.status_code != 503 or time.time() >= deadline:
                return resp
            # Unread retry responses would sit on pool connections until GC.
            resp.close()
            # Sandbox paused/resuming/busy: nudge it awake and retry. GETs
            # never auto-resume server-side, so the activity POST is the wake.
            self.post_activity()
            time.sleep(3)

    def post_activity(self) -> None:
        # Best-effort wake nudge: the caller polls readiness regardless.
        with contextlib.suppress(requests.RequestException):
            self._http.post(
                f"{self.api_url}/api/v1/sandbox/activity",
                headers=self._headers(),
                timeout=30,
            )

    def _own_user_id(self) -> str:
        """The caller's backend user id, cached — needed to build the
        ``/workspaces/user_<id>/.sessions/...`` path for truncation recovery.
        The id is not derivable from the bearer token client-side, and
        Keycloak-mode tokens carry no matching claim."""
        cached = self._cached_user_id
        if cached:
            return cached
        resp = self.request("GET", "/api/v1/auth/me", wake_on_503=False, timeout=30)
        if resp.status_code != 200:
            raise RemoteError(f"auth/me failed ({resp.status_code}): {resp.text[:300]}")
        user_id = str(resp.json()["id"])
        self._cached_user_id = user_id
        return user_id

    # -- sandbox lifecycle -------------------------------------------------

    def list_sandboxes(self) -> List[Dict[str, Any]]:
        resp = self.request("GET", "/api/v1/sandbox/list", wake_on_503=False)
        if resp.status_code != 200:
            raise RemoteError(f"sandbox list failed ({resp.status_code}): {resp.text[:300]}")
        return list(resp.json().get("sandboxes") or [])

    def ensure_sandbox(self, timeout_s: float = 240.0) -> str:
        """A ready sandbox id — creating (via /sandbox/init) or waking as needed."""
        state = load_state(self.state_key)
        deadline = time.time() + timeout_s

        sandboxes = self.list_sandboxes()
        chosen: Optional[str] = None
        if state.get("sandbox_id") and any(
            s.get("sandbox_id") == state["sandbox_id"] for s in sandboxes
        ):
            chosen = str(state["sandbox_id"])
        elif sandboxes:
            active = next((s for s in sandboxes if s.get("is_active")), None)
            chosen = str((active or sandboxes[0])["sandbox_id"])

        # /sandbox/init is the ensure-ready endpoint: it auto-creates a
        # sandbox for a fresh user and reports resume progress for a paused
        # one. Poll it until ready either way.
        while True:
            resp = self.request("GET", "/api/v1/sandbox/init", wake_on_503=False, timeout=30)
            body: Dict[str, Any] = {}
            if resp.headers.get("content-type", "").startswith("application/json"):
                body = resp.json()
            # 4xx is terminal (bad credentials, no access) — fail now instead
            # of polling out the full deadline. 5xx stays retryable: resume
            # churn can surface transient server errors, and 429 asks to wait.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise RemoteError(f"sandbox init failed ({resp.status_code}): {resp.text[:300]}")
            status = body.get("status")
            if resp.status_code == 200 and status == "ready":
                break
            if time.time() >= deadline:
                raise RemoteError(
                    f"sandbox not ready after {int(timeout_s)}s (last status: {status or resp.status_code})"
                )
            self.post_activity()
            time.sleep(max(1.0, float(body.get("retry_after_ms") or 3000) / 1000.0))

        if not chosen:
            sandboxes = self.list_sandboxes()
            if not sandboxes:
                raise RemoteError("no sandbox available after init")
            active = next((s for s in sandboxes if s.get("is_active")), None)
            chosen = str((active or sandboxes[0])["sandbox_id"])

        update_state(self.state_key, sandbox_id=chosen)
        return chosen

    # -- anchor session ----------------------------------------------------

    def _proxy(self, sandbox_id: str, path: str) -> str:
        return f"/api/v1/sandbox/{sandbox_id}/proxy/{path}"

    def session_exists(self, sandbox_id: str, session_id: str) -> bool:
        resp = self.request(
            "GET", self._proxy(sandbox_id, f"session/{session_id}/exists"), timeout=30
        )
        return resp.status_code == 200 and bool(resp.json().get("exists"))

    def create_session(self, sandbox_id: str, client_tag: str = "cli") -> str:
        # ACP initialize is idempotent; retry through agent-boot races (425).
        last = ""
        for _ in range(6):
            resp = self.request("POST", self._proxy(sandbox_id, "initialize"), json={}, timeout=60)
            if resp.status_code == 200:
                break
            last = f"{resp.status_code}: {resp.text[:200]}"
            time.sleep(3)
        else:
            raise RemoteError(f"sandbox agent initialize failed ({last})")

        for _ in range(4):
            resp = self.request(
                "POST",
                self._proxy(sandbox_id, "session/new"),
                json={"cwd": "/workspaces", "mcpServers": []},
                timeout=60,
            )
            if resp.status_code == 200:
                body = resp.json()
                session_id = body.get("sessionId") or (body.get("session") or {}).get("id")
                if session_id:
                    break
            time.sleep(2)
        else:
            raise RemoteError("session/new failed")

        # Label the session so the Script.it app can distinguish remote-agent
        # activity — and WHICH harness drives it, when known. Best-effort —
        # a tagging failure never blocks login/exec.
        client = detect_client()
        tags = [client_tag] + ([f"client:{client}"] if client else [])
        title = f"CLI remote session ({client})" if client else "CLI remote session"
        try:
            # RemoteError too: `request` wraps transport failures in it, so
            # catching only RequestException would let a cosmetic call fail
            # session creation after the session already exists. wake_on_503
            # off for the same reason — this must not spend three minutes
            # retrying a title.
            self.request(
                "POST",
                self._proxy(sandbox_id, f"session/{session_id}/tags"),
                json={"add": tags},
                wake_on_503=False,
                timeout=30,
            )
            self.request(
                "PATCH",
                self._proxy(sandbox_id, f"session/{session_id}"),
                json={"title": title},
                wake_on_503=False,
                timeout=30,
            )
        except (ScriptItError, requests.RequestException):
            pass
        return str(session_id)

    def anchor(self) -> Tuple[Optional[str], Optional[str], str]:
        """The anchor session this invocation runs in, and where it came from.

        ``SCRIPTIT_SESSION`` names one for a single invocation — how a harness
        gives a task its own transcript — and otherwise the sticky anchor for
        this profile+platform is used.
        """
        env_session = os.environ.get(ENV_SESSION, "").strip()
        if env_session:
            # No sandbox recorded: a named session is resolved against
            # whichever sandbox the account is using.
            return env_session, None, "env"
        state = load_state(self.state_key)
        if state.get("session_id"):
            return str(state["session_id"]), state.get("sandbox_id"), "state"
        return None, None, "none"

    def ensure_session(self, sandbox_id: str) -> str:
        session_id, _, source = self.anchor()
        if session_id and self.session_exists(sandbox_id, str(session_id)):
            return str(session_id)
        if source == "env":
            # An explicitly named session that does not exist is a mistake
            # worth reporting, not something to silently replace.
            raise RemoteError(
                f"session {session_id} (from {ENV_SESSION}) not found in sandbox {sandbox_id}"
            )
        session_id = self.create_session(sandbox_id)
        update_state(self.state_key, sandbox_id=sandbox_id, session_id=session_id)
        return session_id

    # -- shell over session/shell + SSE ------------------------------------

    # The platform's SSE sanitizer caps every text field at 8000 chars; output
    # past that no longer streams, which is what the tee below recovers.
    _SSE_TEXT_CAP = 8000
    # The session runtime discards the child's exit code, so the CLI
    # appends a sentinel line and parses it out of the collected output. Each shell() call mints
    # its own random-suffixed sentinel so command output can't spoof it.
    _EXIT_SENTINEL_PREFIX = "__SCRIPTIT_CLI_EXIT_"
    # Every dispatch tees its combined output here (relative to the session's
    # own cwd) so output the ~8KB live cap swallowed can still be read back.
    # One fixed name, not one per command: `tee` truncates on open and the
    # server serializes session ops, so the file always holds the current
    # command — and a session accumulates one log, not one per dispatch.
    # A stale read is self-detecting anyway: the sentinel is per-call random,
    # so a previous command's log simply scores as "no exit code".
    #
    # The run dir rather than `data_files/`, though both are exposed by the
    # file API: a block's `files_written` is a before/after diff of the data
    # dir, so a log written there is reported as a file the script wrote.
    _LOG_PATH = "scriptit/.scriptit-cli-last.log"

    def shell(
        self,
        command: str,
        *,
        sandbox_id: Optional[str] = None,
        session_id: Optional[str] = None,
        timeout_s: float = 600.0,
        echo: bool = True,
        cwd: Optional[str] = None,
        command_timeout_s: Optional[float] = None,
    ) -> Tuple[str, Optional[int]]:
        """Run ``command`` in the sandbox session; return (output, exit_code).

        Protocol:

        - connect the session SSE stream with a dedicated events-group header
          (a same-group second subscriber evicts the first — never fight the
          product UI over its stream), wait for ``load_complete`` so history
          replay is done;
        - dispatch the fire-and-forget ``session/shell`` (retrying 409
          SESSION_BUSY — session ops are serialized);
        - collect CUMULATIVE per-tool-call output snapshots from
          ``session_update``/``tool_call_update`` ``content[].content.text``
          (an update without ``content`` means "unchanged");
        - finish on ``message_complete`` whose ``parentMessageId`` is our
          message id (``error`` with ``op == 'shell'`` is the failure path).
        """
        sandbox_id = sandbox_id or self.ensure_sandbox()
        session_id = session_id or self.ensure_session(sandbox_id)
        message_id = make_message_id()
        # Random per-call suffix: output that happens to contain the static
        # prefix can't be mistaken for (or forge) this call's exit marker.
        # Hex only — the sentinel is embedded in a single-quoted printf below.
        sentinel = f"{self._EXIT_SENTINEL_PREFIX}{secrets.token_hex(8)}__:"

        # Every command rides a base64 payload piped into bash: the dispatched
        # string undergoes one round of double-quote expansion before eval, so
        # any `$` we ship is read by the wrong shell, and embedded newlines
        # JSON-escape to `\n` which eval collapses to a literal `n`. Inside the
        # payload the command arrives verbatim and `$?` is its real status.
        wrapped = build_shell_wrapper(
            apply_cwd(command, cwd), sentinel, self._LOG_PATH, command_timeout_s
        )

        events_url = f"{self.api_url}{self._proxy(sandbox_id, f'session/{session_id}/events')}"
        done: "queue.Queue[Optional[str]]" = queue.Queue()
        ready = threading.Event()
        buffers: Dict[str, str] = {}
        buffer_order: List[str] = []
        lock = threading.Lock()
        stop = threading.Event()

        # One state variable under `lock`, because "is the stream usable?" and
        # "has the command been dispatched?" must never be decided against
        # different snapshots. Read separately, a stream that dies between
        # `load_complete` and the POST looks alive to the dispatcher and dead
        # to the reader, and the command goes out with nowhere to collect it —
        # a failure the caller cannot safely retry, since the side effects
        # already happened.
        #
        #   BOOTSTRAPPING -> LIVE      `load_complete`: history replay done
        #   LIVE          -> DISPATCHED the POST is about to go out
        #   DISPATCHED    -> LIVE       409 SESSION_BUSY, retrying
        #
        # A death in BOOTSTRAPPING or LIVE cancels dispatch; in DISPATCHED the
        # command is already running, so it only ends collection.
        stream_error: List[str] = []
        state = _STREAM_BOOTSTRAPPING

        def _stream_died(message: str) -> None:
            nonlocal state
            with lock:
                if state == _STREAM_DISPATCHED:
                    done.put(message)
                    return
                stream_error.append(message)
            ready.set()

        def _reader() -> None:
            nonlocal state
            try:
                headers = self._headers()
                headers["x-scriptit-events-group"] = "cli"
                with self._http.get(
                    events_url, headers=headers, stream=True, timeout=(15, 60)
                ) as resp:
                    if resp.status_code != 200:
                        _stream_died(f"events stream failed ({resp.status_code})")
                        return
                    for name, data in _iter_sse_events(resp.iter_lines(decode_unicode=False)):
                        if stop.is_set():
                            return
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if name == "load_complete":
                            with lock:
                                if state == _STREAM_BOOTSTRAPPING:
                                    state = _STREAM_LIVE
                            ready.set()
                            continue
                        if name == "session_update":
                            update = payload.get("update") or {}
                            kind = update.get("sessionUpdate")
                            call_id = update.get("toolCallId")
                            if not call_id:
                                continue
                            # The armed check lives INSIDE the lock: the 409
                            # retry path disarms + clears under it, so a
                            # stale event can't repopulate right after.
                            if kind == "tool_call":
                                with lock:
                                    if state != _STREAM_DISPATCHED:
                                        continue
                                    if call_id not in buffers:
                                        buffers[call_id] = ""
                                        buffer_order.append(call_id)
                            elif kind == "tool_call_update":
                                content = update.get("content")
                                if not isinstance(content, list):
                                    continue  # unchanged snapshot (deduped) or malformed
                                text = "".join(
                                    (item.get("content") or {}).get("text", "")
                                    for item in content
                                    if isinstance(item, dict)
                                    and item.get("type") == "content"
                                    and isinstance(item.get("content"), dict)
                                )
                                with lock:
                                    if state != _STREAM_DISPATCHED:
                                        continue
                                    if call_id not in buffers:
                                        buffers[call_id] = ""
                                        buffer_order.append(call_id)
                                    # Snapshots are cumulative — replace.
                                    buffers[call_id] = text
                        elif name == "message_complete":
                            if payload.get("parentMessageId") == message_id:
                                done.put(None)
                                return
                        elif name == "error":
                            if (
                                payload.get("parentMessageId") == message_id
                                and payload.get("op") == "shell"
                            ):
                                done.put(str(payload.get("message") or "shell dispatch failed"))
                                return
                # Falling out of the event loop is a third terminal outcome,
                # alongside a terminal event and an exception: the server
                # ended the stream without either. That happens when a second
                # subscriber in the same group takes over the stream — which
                # it does whenever the session is opened in the app —
                # so without this the caller waits out the full command
                # timeout for a verdict that is never coming.
                _stream_died("events stream closed before the command reported back")
            except requests.RequestException as exc:
                _stream_died(f"events stream dropped: {exc}")
            except Exception as exc:
                # Never leave the main thread waiting out the full command
                # timeout for a reader that has already given up.
                _stream_died(f"events stream unexpected error: {exc!r}")

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        if not ready.wait(timeout=60):
            stop.set()
            raise RemoteError("timed out waiting for session event stream bootstrap")
        # Dispatch, riding out SESSION_BUSY (another op — e.g. a UI prompt —
        # is running; ops are serialized per session).
        busy_deadline = time.time() + 60
        while True:
            # The liveness check and the arming are one critical section, so
            # a stream that dies here either loses the race (dispatch goes out
            # and the death becomes a collection error) or wins it (nothing is
            # dispatched at all). What it can never do is both.
            #
            # Arming just BEFORE each attempt matters on its own: session/shell
            # is fire-and-forget on a separate connection, so a fast command's
            # events can arrive before the POST response does — but staying
            # armed through the 409 retry sleep would capture the concurrent
            # op's output.
            with lock:
                failure = stream_error[0] if stream_error else None
                if failure is None:
                    state = _STREAM_DISPATCHED
            if failure is not None:
                stop.set()
                raise RemoteError(failure)
            resp = self.request(
                "POST",
                self._proxy(sandbox_id, f"session/{session_id}/shell"),
                json={"command": wrapped, "message_id": message_id},
                timeout=60,
            )
            if resp.status_code in (200, 202):
                break
            if resp.status_code == 409 and time.time() < busy_deadline:
                resp.close()
                # Back to LIVE: nothing of ours is running, so a stream death
                # during the retry sleep must cancel the next attempt rather
                # than be filed as a collection failure. The concurrent op's
                # tool output may have been captured while armed — drop it so
                # it can't pollute our result.
                with lock:
                    if state == _STREAM_DISPATCHED:
                        state = _STREAM_LIVE
                    buffers.clear()
                    buffer_order.clear()
                time.sleep(_BUSY_RETRY_SECONDS)
                continue
            stop.set()
            raise RemoteError(f"shell dispatch failed ({resp.status_code}): {resp.text[:300]}")

        def _combined() -> str:
            with lock:
                return "".join(buffers[c] for c in buffer_order)

        # Echo loop: print appended output as snapshots grow, holding back a
        # small tail so the exit sentinel never reaches the terminal.
        holdback = len(sentinel) + 16
        printed_len = 0
        error: Optional[str] = None
        deadline = time.time() + timeout_s
        while True:
            try:
                # Short poll so live echo flushes promptly.
                error = done.get(timeout=0.1)
                break
            except queue.Empty:
                pass
            if echo:
                text = _combined()
                visible = max(0, len(text) - holdback)
                if visible > printed_len:
                    sys.stdout.write(text[printed_len:visible])
                    sys.stdout.flush()
                    printed_len = visible
            if time.time() >= deadline:
                error = f"timed out after {int(timeout_s)}s waiting for command completion"
                break
        stop.set()

        raw = _combined()
        output, exit_code = split_exit_sentinel(raw, sentinel)
        truncated = exit_code is None and len(raw) >= self._SSE_TEXT_CAP - 100

        # The live stream lost the tail (and with it the sentinel) — every
        # dispatch tees its full output to log_path, so read that back
        # instead of reporting an unknown exit status on a command that
        # actually ran to completion (e.g. `scriptit describe --concepts`,
        # which alone is ~23KB — well past the live cap).
        if truncated:
            try:
                uid = self._own_user_id()
                full_path = f"/workspaces/user_{uid}/.sessions/{session_id}/{self._LOG_PATH}"
                recovered_raw = self.fs_read(sandbox_id, full_path).decode(
                    "utf-8", errors="replace"
                )
                recovered_output, recovered_code = split_exit_sentinel(recovered_raw, sentinel)
                if recovered_code is not None:
                    output, exit_code = recovered_output, recovered_code
                    truncated = False
            except ScriptItError:
                pass  # fall through — still reported as unknown below

        if echo:
            visible_out = output
            if len(visible_out) > printed_len:
                sys.stdout.write(visible_out[printed_len:])
            if visible_out and not visible_out.endswith("\n"):
                sys.stdout.write("\n")
            if truncated:
                sys.stdout.write(
                    "[scriptit: live output truncated at ~8KB by the platform event "
                    "stream — redirect to a file and use `scriptit fs read` for full "
                    "output]\n"
                )
            sys.stdout.flush()

        if error:
            raise RemoteError(error)
        return output, exit_code

    # -- files -------------------------------------------------------------

    def fs_list(self, sandbox_id: str, path: str) -> Dict[str, Any]:
        """List a sandbox directory via the backend file routes."""
        resp = self.request(
            "GET", f"/api/v1/file/{sandbox_id}/list", params={"path": path}, timeout=60
        )
        if resp.status_code != 200:
            raise RemoteError(f"list failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def fs_read(self, sandbox_id: str, path: str) -> bytes:
        """Read a sandbox file's bytes (base64 over the wire, decoded here)."""
        resp = self.request(
            "GET", f"/api/v1/file/{sandbox_id}/read", params={"path": path}, timeout=120
        )
        if resp.status_code == 404:
            raise RemoteError(f"not found: {path}")
        if resp.status_code != 200:
            raise RemoteError(f"read failed ({resp.status_code}): {resp.text[:300]}")
        return base64.b64decode(resp.json().get("content_base64") or "")

    def fs_write(self, sandbox_id: str, path: str, content: str) -> None:
        """Write text content to a sandbox file (parents auto-created)."""
        resp = self.request(
            "POST",
            f"/api/v1/file/{sandbox_id}/write",
            json={"path": path, "content": content},
            timeout=120,
        )
        if resp.status_code != 200:
            raise RemoteError(f"write failed ({resp.status_code}): {resp.text[:300]}")

    def fs_upload(self, sandbox_id: str, path: str, local_file: str) -> None:
        """Upload a local file into the sandbox (multipart, binary-safe)."""
        with open(local_file, "rb") as f:
            resp = self.request(
                "POST",
                f"/api/v1/file/{sandbox_id}/upload",
                data={"path": path},
                files={"file": (os.path.basename(local_file), f)},
                timeout=300,
            )
        if resp.status_code != 200:
            raise RemoteError(f"upload failed ({resp.status_code}): {resp.text[:300]}")


# ---------------------------------------------------------------------------
# main() dispatch hook
# ---------------------------------------------------------------------------


def remote_dispatch_if_applicable(argv: List[str]) -> Optional[int]:
    """Forward the invocation into the sandbox.

    Returns an exit code when the invocation was handled remotely (or refused),
    or ``None`` for a verb this client answers itself.
    """
    if not argv or argv[0] in CLIENT_COMMANDS:
        return None

    if load_credentials() is None:
        sys.stderr.write(
            "scriptit: not connected to a sandbox. This command runs inside your "
            "Script.it sandbox; run `scriptit auth login` first.\n"
        )
        return 2

    try:
        client = RemoteClient()
        command = "scriptit " + shlex.join(argv)
        _, exit_code = client.shell(command)
        return exit_code if exit_code is not None else _unknown_exit()
    except ScriptItError as exc:
        sys.stderr.write(f"scriptit: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return _interrupted_exit()


# Exit status when the command ran but its real status is unknowable (the
# exit sentinel was lost to the platform's ~8KB live-output cap). Unknown is
# NOT success — callers driving the CLI must not proceed as if it passed.
UNKNOWN_EXIT_CODE = 125

# Ctrl-C, or an agent harness enforcing a per-command timeout. Shell
# convention for SIGINT; also NOT success.
INTERRUPTED_EXIT_CODE = 130


def _interrupted_exit() -> int:
    """Report an interrupt as one line, not a stack trace.

    Dispatch is fire-and-forget, so the command keeps running in the sandbox
    after the local process goes away — say so, since the next thing the
    caller does is usually re-run it.
    """
    sys.stderr.write(
        "scriptit: interrupted — the command may still be running in your "
        "sandbox. Check `scriptit session current` / `scriptit list` before "
        "re-running.\n"
    )
    return INTERRUPTED_EXIT_CODE


def _unknown_exit() -> int:
    sys.stderr.write(
        f"scriptit: command completed but its exit status is unknown "
        f"(output exceeded the live stream cap) — exiting {UNKNOWN_EXIT_CODE}; "
        "redirect output to a file to get a real status\n"
    )
    return UNKNOWN_EXIT_CODE


def _parse_timeout(value: str) -> float:
    """Seconds from a flag value, or a message the user can act on.

    Rejects the values that would otherwise reach the sandbox as a broken
    limit: a non-number, zero or negative (GNU ``timeout 0`` means no limit),
    and ``nan``/``inf``, which parse as floats and then blow up at format
    time.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise RemoteError(f"--timeout expects seconds, got {value!r}") from None
    if not math.isfinite(seconds):
        raise RemoteError(f"--timeout expects a finite number of seconds, got {value!r}")
    if seconds <= 0:
        raise RemoteError("--timeout must be greater than zero")
    return seconds


def _parse_exec_options(argv: List[str]) -> Tuple[List[str], Optional[str], Optional[float]]:
    """Split client flags off the front of ``exec``'s arguments.

    Only what precedes ``--`` is inspected, so the command keeps its own
    ``--cwd``/``--timeout`` untouched.
    """
    args = list(argv)
    cwd: Optional[str] = None
    timeout: Optional[float] = None
    flags = {"--cwd": "cwd", "--timeout": "timeout"}
    while args and args[0] != "--":
        token = args[0]
        name = flags.get(token)
        if name is not None:
            # `--` is the separator, never a value: consuming it would make
            # the flag swallow it and the real command run with a bogus
            # setting. A missing value would otherwise run the flag itself.
            if len(args) < 2 or args[1] == "--":
                raise RemoteError(f"{token} needs a value")
            value, args = args[1], args[2:]
        elif "=" in token and flags.get(token.split("=", 1)[0]) is not None:
            head, value = token.split("=", 1)
            name, args = flags[head], args[1:]
        else:
            break
        if name == "cwd":
            if not value:
                raise RemoteError("--cwd needs a directory")
            cwd = value
        else:
            timeout = _parse_timeout(value)
    if args and args[0] == "--":
        args = args[1:]
    return args, cwd, timeout


def remote_exec(argv: List[str]) -> int:
    """``scriptit exec [--cwd DIR] [--timeout SECS] [--] <cmd...>``."""
    try:
        args, cwd, command_timeout = _parse_exec_options(argv)
    except RemoteError as exc:
        sys.stderr.write(f"scriptit: {exc}\n")
        return 2
    if not args:
        sys.stderr.write("usage: scriptit exec [--cwd DIR] [--timeout SECS] -- <command...>\n")
        return 2
    try:
        client = RemoteClient()
        command = args[0] if len(args) == 1 else shlex.join(args)
        # Outlive the sandbox-side limit so its 124 is what surfaces, not a
        # client that stopped listening just before it arrived.
        wait_s = command_timeout + 30 if command_timeout else 600.0
        _, exit_code = client.shell(
            command, cwd=cwd, command_timeout_s=command_timeout, timeout_s=wait_s
        )
        return exit_code if exit_code is not None else _unknown_exit()
    except ScriptItError as exc:
        sys.stderr.write(f"scriptit: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return _interrupted_exit()
