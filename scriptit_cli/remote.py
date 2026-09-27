"""Remote transport for the workstation CLI.

``scriptit`` is a thin remote shell: verbs are forwarded verbatim into the
user's sandbox (the platform's session-shell API). Native output comes from
byte-cursor pages and completion from SSE; v1 uses SSE plus file recovery.
The in-sandbox CLI stays the single behavior surface — this client never
executes automation locally, so
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

from scriptit_cli import __version__
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
    "context",
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

# The v2 session-stream version this client reads: the bridge's frames and the
# native shell events matched below. Every v2 snapshot names the bridge's own
# (`WIRE_VERSION` in the platform's `sandbox/src/native/wire.ts`).
WIRE_VERSION = 1


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

    The guarded ``cd`` is part of the remote command, not a client-side path.
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


def _iter_sse_events(lines: Iterable[bytes]) -> Iterable[Tuple[str, str, Optional[str]]]:
    """Yield ``(event_name, data, id)`` frames from an SSE byte-line stream.

    Handles multi-line ``data:`` fields and ignores comments/keepalives.
    ``event_name`` is ``"message"`` when the stream doesn't name events.
    """
    event_name = "message"
    data_lines: List[str] = []
    event_id = None
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines), event_id
            event_name = "message"
            data_lines = []
            event_id = None
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("id:"):
            event_id = line[len("id:") :].strip()
        elif line.startswith("data:"):
            # SSE spec: strip at most ONE leading space after the colon.
            val = line[len("data:") :]
            data_lines.append(val[1:] if val.startswith(" ") else val)
    if data_lines:
        yield event_name, "\n".join(data_lines), event_id


def _error_message(resp: requests.Response) -> str:
    """An error response's own message: its JSON `message`, `error` or
    `detail`, else the start of its text."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if isinstance(body.get(key), str):
                return body[key]
    return resp.text[:300]


def _wire_version(snapshot: Dict[str, Any]) -> Any:
    wire = snapshot.get("wire")
    return wire.get("version") if isinstance(wire, dict) else None


class _SessionEvents:
    """One invocation's event listener; v2 reconnects resume its durable cursor."""

    def __init__(self, client: RemoteClient, sandbox_id: str, session_id: str) -> None:
        self.client = client
        self.sandbox_id, self.session_id = sandbox_id, session_id
        self.message_id = make_message_id()
        self.url = f"{client.api_url}{client._proxy(sandbox_id, f'session/{session_id}/events')}"
        self.group = f"cli-{secrets.token_hex(12)}"
        self.protocol: Optional[int] = None
        self.frames: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.armed = False
        self.error: Optional[str] = None
        self.response: Optional[requests.Response] = None

    def __enter__(self) -> _SessionEvents:
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop.set()
        response = self.response
        if response is not None:
            # requests.close can wait for the reader's socket lock. Cleanup
            # must not delay a completed command or Ctrl-C until a heartbeat.
            threading.Thread(target=response.close, daemon=True).start()

    def arm(self) -> None:
        # V1 has no resumable result stream. Check its liveness and start
        # collecting in one critical section before issuing the POST.
        with self.lock:
            if self.error:
                raise RemoteError(self.error)
            self.armed = True

    def disarm(self) -> None:
        with self.lock:
            self.armed = False
            while not self.frames.empty():
                self.frames.get_nowait()

    def poll(self, timeout: float = 0.1) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            name, payload = self.frames.get(timeout=timeout)
        except queue.Empty:
            return None
        if name == "_stream_error":
            raise RemoteError(payload["message"])
        return name, payload

    def _fail(self, message: str) -> None:
        with self.lock:
            self.error = message
            self.frames.put(("_stream_error", {"message": message}))
        self.ready.set()

    def _read(self) -> None:
        last_id = None
        while not self.stop.is_set():
            try:
                headers = {**self.client._headers(), "x-scriptit-events-group": self.group}
                if last_id is not None:
                    headers["Last-Event-ID"] = last_id
                with self.client._http.get(
                    self.url, headers=headers, stream=True, timeout=(15, 60)
                ) as resp:
                    self.response = resp
                    if resp.status_code != 200:
                        message = f"events stream failed ({resp.status_code})"
                        if resp.status_code < 500:
                            self._fail(f"{message}: {_error_message(resp)}")
                            return
                        raise requests.RequestException(message)
                    for name, data, event_id in _iter_sse_events(
                        resp.iter_lines(decode_unicode=False)
                    ):
                        if self.stop.is_set():
                            return
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if name == "session_snapshot" and _wire_version(payload) != WIRE_VERSION:
                            # Its shell events may not be the ones matched below,
                            # so waiting could only end at the command timeout.
                            self._fail(
                                "this sandbox's session stream is version "
                                f"{_wire_version(payload)} and scriptit-cli {__version__} "
                                f"reads version {WIRE_VERSION}: upgrade scriptit-cli"
                            )
                            return
                        if self.protocol is None and name in ("load_complete", "session_snapshot"):
                            self.protocol = 1 if name == "load_complete" else 2
                            self.ready.set()
                        if name == "error" and self.protocol != 1:
                            message = str(payload.get("message") or "session event stream failed")
                            if payload.get("retryable") is True:
                                raise requests.RequestException(message)
                            self._fail(message)
                            return
                        with self.lock:
                            if self.armed:
                                self.frames.put((name, payload))
                        if event_id is not None:
                            last_id = event_id
                message = "events stream closed before the command reported back"
            except requests.RequestException as exc:
                message = f"events stream dropped: {exc}"
            except Exception as exc:
                self._fail(f"events stream unexpected error: {exc!r}")
                return
            finally:
                self.response = None
            if self.protocol == 1:
                self._fail(message)
                return
            self.stop.wait(1)


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
        """One API call; only reads automatically retry sandbox resume 503s."""
        url = f"{self.api_url}{path}"
        retry = wake_on_503 and method.upper() in ("GET", "HEAD")
        deadline = time.time() + 180 if retry else time.time()
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

    def agent_context(self, sandbox_id: str, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """Return the complete sandbox-assembled external-agent context."""
        resp = self.request(
            "POST",
            self._proxy(sandbox_id, "agent-context"),
            json=context_data,
            wake_on_503=False,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RemoteError(f"agent context unavailable ({resp.status_code}): {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise RemoteError("agent context returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("schema_version") != 1:
            raise RemoteError("agent context returned an unsupported schema")
        if not isinstance(body.get("markdown"), str) or not body["markdown"].strip():
            raise RemoteError("agent context omitted its rendered instructions")
        return body

    def session_exists(self, sandbox_id: str, session_id: str) -> bool:
        resp = self.request(
            "GET", self._proxy(sandbox_id, f"session/{session_id}/exists"), timeout=30
        )
        return resp.status_code == 200 and bool(resp.json().get("exists"))

    def ensure_session_attribution(
        self,
        sandbox_id: str,
        session_id: str,
        client_tag: str = "cli",
    ) -> Optional[str]:
        """Idempotently attribute a bound session to this CLI and harness."""
        detected = detect_client()
        state = load_state(self.state_key)
        persisted = state.get("client") if state.get("session_id") == session_id else None
        client = detected or (persisted if isinstance(persisted, str) and persisted else None)
        tags = [client_tag] + ([f"client:{client}"] if client else [])
        resp = self.request(
            "POST",
            self._proxy(sandbox_id, f"session/{session_id}/tags"),
            json={"add": tags},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RemoteError(f"session attribution failed ({resp.status_code}): {resp.text[:300]}")
        return client

    def create_session(self, sandbox_id: str, client_tag: str = "cli") -> str:
        # Initialize is idempotent; retry through agent-boot races (425).
        last = ""
        for _ in range(6):
            resp = self.request("POST", self._proxy(sandbox_id, "initialize"), json={}, timeout=60)
            if resp.status_code == 200:
                break
            last = f"{resp.status_code}: {resp.text[:200]}"
            time.sleep(3)
        else:
            raise RemoteError(f"sandbox agent initialize failed ({last})")
        # A v1 bridge answers with the ACP initialize result and requires the
        # ACP session fields; a v2 bridge derives the session itself and
        # refuses unknown fields.
        v1 = "protocolVersion" in resp.json()
        new_session_body = {"cwd": "/workspaces", "mcpServers": []} if v1 else {}

        for _ in range(4):
            resp = self.request(
                "POST",
                self._proxy(sandbox_id, "session/new"),
                json=new_session_body,
                timeout=60,
            )
            if resp.status_code == 200:
                body = resp.json()
                session_id = body.get("sessionId") or (body.get("session") or {}).get("id")
                if session_id:
                    break
            elif 400 <= resp.status_code < 500:
                raise RemoteError(
                    f"session/new failed ({resp.status_code}): {_error_message(resp)}"
                )
            time.sleep(2)
        else:
            raise RemoteError("session/new failed")

        # Attribute the session before returning it so the app never silently
        # loses which external harness is driving. The display title remains
        # cosmetic and may fail without invalidating the usable session.
        client = self.ensure_session_attribution(sandbox_id, str(session_id), client_tag)
        title = f"CLI remote session ({client})" if client else "CLI remote session"
        with contextlib.suppress(ScriptItError, requests.RequestException):
            # RemoteError too: `request` wraps transport failures in it, so
            # catching only RequestException would let a cosmetic call fail
            # session creation after the session already exists. wake_on_503
            # off for the same reason — this must not spend three minutes
            # retrying a title.
            self.request(
                "PATCH",
                self._proxy(sandbox_id, f"session/{session_id}"),
                json={"title": title},
                wake_on_503=False,
                timeout=30,
            )
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
            client = self.ensure_session_attribution(sandbox_id, str(session_id))
            if source == "state" and client:
                update_state(self.state_key, client=client)
            return str(session_id)
        if source == "env":
            # An explicitly named session that does not exist is a mistake
            # worth reporting, not something to silently replace.
            raise RemoteError(
                f"session {session_id} (from {ENV_SESSION}) not found in sandbox {sandbox_id}"
            )
        session_id = self.create_session(sandbox_id)
        update_state(
            self.state_key,
            sandbox_id=sandbox_id,
            session_id=session_id,
            client=detect_client(),
        )
        return session_id

    # -- shell over session/shell + SSE ------------------------------------

    # V1 compatibility: its SSE sanitizer caps every text field at 8000 chars.
    _SSE_TEXT_CAP = 8000
    # The V1 session runtime discards the child's exit code, so the CLI
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
        """Select the session's wire protocol before submitting one command."""
        sandbox_id = sandbox_id or self.ensure_sandbox()
        session_id = session_id or self.ensure_session(sandbox_id)
        command = apply_cwd(command, cwd)
        with _SessionEvents(self, sandbox_id, session_id) as events:
            if not events.ready.wait(timeout=60):
                raise RemoteError("timed out waiting for session event stream bootstrap")
            if events.error:
                raise RemoteError(events.error)
            if events.protocol == 1:
                return self._shell_v1(
                    command,
                    events,
                    timeout_s,
                    echo,
                    command_timeout_s,
                )
            if command_timeout_s:
                command = (
                    f"timeout {_format_seconds(command_timeout_s)} bash -c {shlex.quote(command)}"
                )
            return self._shell_v2(command, events, timeout_s, echo)

    def _submit_shell(self, command: str, events: _SessionEvents) -> Optional[Dict[str, Any]]:
        """Retry only a refusal that proves this command was not submitted.

        An ambiguous v2 response leaves collection running: the matching start
        or completed row can still establish the command's outcome.
        """
        deadline = time.monotonic() + 60
        while True:
            events.arm()
            try:
                resp = self.request(
                    "POST",
                    self._proxy(events.sandbox_id, f"session/{events.session_id}/shell"),
                    json={"command": command, "message_id": events.message_id},
                    wake_on_503=False,
                    timeout=60,
                )
            except RemoteError:
                if events.protocol == 2:
                    return None
                raise
            if resp.status_code in (200, 202) and events.protocol == 1:
                return None
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if resp.status_code in (200, 202):
                shell = body.get("shell")
                if body.get("message_id") == events.message_id and isinstance(shell, dict):
                    return shell
                return None
            detail = body
            while isinstance(detail.get("detail"), dict):
                detail = detail["detail"]
            code = detail.get("code")
            refused = (resp.status_code == 409 and code == "SESSION_BUSY") or (
                resp.status_code == 503
                and (
                    code in {"SHELL_NOT_SUBMITTED", "SANDBOX_RESUMING", "SANDBOX_UPDATING"}
                    or detail.get("submission_state") == "not_submitted"
                )
            )
            if refused and time.monotonic() < deadline:
                retry_ms = detail.get("retry_after_ms")
                delay = (
                    max(0, retry_ms / 1000)
                    if isinstance(retry_ms, (int, float))
                    else _BUSY_RETRY_SECONDS
                )
                resp.close()
                events.disarm()
                time.sleep(min(delay, max(0, deadline - time.monotonic())))
                continue
            if (
                events.protocol == 2
                and resp.status_code >= 500
                and not refused
                and code not in ("SHELL_NOT_SUBMITTED", "SESSION_MATERIALIZE_FAILED")
                and detail.get("submission_state") != "not_submitted"
            ):
                return None
            raise RemoteError(f"shell dispatch failed ({resp.status_code}): {resp.text[:300]}")

    def _shell_v2(
        self,
        command: str,
        events: _SessionEvents,
        timeout_s: float,
        echo: bool,
    ) -> Tuple[str, Optional[int]]:
        shell = self._submit_shell(command, events)
        shell_id = shell.get("id") if shell else None
        result: Optional[Dict[str, Any]] = None
        cursor = 0
        chunks: List[str] = []
        deadline = time.monotonic() + timeout_s

        def append(text: str) -> None:
            chunks.append(text)
            if echo:
                sys.stdout.write(text)
                sys.stdout.flush()

        def observe(name: str, payload: Dict[str, Any]) -> None:
            nonlocal shell_id, result
            if name == "session_snapshot":
                for row in payload.get("rows", []):
                    if row.get("type") != "shell" or row.get("id") != events.message_id:
                        continue
                    shell_id = row.get("shellID")
                    if row.get("status") != "running":
                        result = row
            elif name == "session_event":
                kind = payload.get("type")
                if kind not in ("session.shell.started", "session.shell.ended"):
                    return
                data = payload.get("data") or {}
                info = data.get("shell") or {}
                own_message = (info.get("metadata") or {}).get("messageID") == events.message_id
                if not own_message and (not shell_id or info.get("id") != shell_id):
                    return
                shell_id = info.get("id")
                if kind == "session.shell.ended":
                    result = {**info, "output": data.get("output")}

        while True:
            frame = events.poll(0)
            while frame is not None:
                observe(*frame)
                frame = events.poll(0)
            if time.monotonic() >= deadline:
                if result is not None:
                    raise RemoteError("complete shell output could not be recovered")
                raise RemoteError(
                    "command completion could not be confirmed; it may still be running. "
                    "Check `scriptit session current` before re-running."
                )
            if shell_id:
                try:
                    resp = self.request(
                        "GET",
                        self._proxy(
                            events.sandbox_id,
                            f"session/{events.session_id}/shell/{shell_id}/output",
                        ),
                        params={"cursor": cursor, "limit": 65536},
                        wake_on_503=False,
                        timeout=min(30, max(0.1, deadline - time.monotonic())),
                    )
                except RemoteError:
                    resp = None
                if resp is None or resp.status_code >= 500 or resp.status_code in (408, 429):
                    # Reads can resume from the same cursor after a network
                    # drop; submission is never repeated to recover output.
                    frame = events.poll(min(0.5, max(0, deadline - time.monotonic())))
                    if frame is not None:
                        observe(*frame)
                    continue
                if resp.status_code == 404:
                    if result is not None:
                        saved = result.get("output")
                        if (
                            not isinstance(saved, dict)
                            or saved.get("truncated") is not False
                            or type(saved.get("size")) is not int
                            or saved.get("cursor") != saved["size"]
                            or not isinstance(saved.get("output"), str)
                        ):
                            raise RemoteError("complete shell output is no longer available")
                        collected = "".join(chunks)
                        if not saved["output"].startswith(collected):
                            raise RemoteError(
                                "persisted shell output does not match collected output"
                            )
                        append(saved["output"][len(collected) :])
                        break
                elif resp.status_code == 200:
                    try:
                        page = resp.json()
                    except ValueError as exc:
                        raise RemoteError("shell output returned invalid JSON") from exc
                    if (
                        not isinstance(page, dict)
                        or not isinstance(page.get("output"), str)
                        or type(page.get("cursor")) is not int
                        or type(page.get("size")) is not int
                        or not cursor <= page["cursor"] <= page["size"]
                        or page.get("truncated") is not False
                    ):
                        raise RemoteError("shell output returned an incomplete page")
                    next_cursor = page["cursor"]
                    if page["output"] and next_cursor == cursor:
                        raise RemoteError("shell output did not advance its byte cursor")
                    if next_cursor > cursor and not page["output"]:
                        raise RemoteError("shell output omitted captured bytes")
                    append(page["output"])
                    advanced = next_cursor > cursor
                    cursor = next_cursor
                    if result is not None and cursor == page["size"]:
                        saved = result.get("output")
                        saved = saved if isinstance(saved, dict) else {}
                        if isinstance(saved.get("size"), int) and cursor < saved["size"]:
                            raise RemoteError("shell output ended before its recorded size")
                        break
                    if cursor < page["size"]:
                        if advanced:
                            continue
                        if result is not None:
                            raise RemoteError("shell output ended before its recorded size")
                else:
                    raise RemoteError(
                        f"shell output failed ({resp.status_code}): {resp.text[:300]}"
                    )
            frame = events.poll(0.1)
            if frame is not None:
                observe(*frame)

        output = "".join(chunks)
        status = result.get("status")
        exit_code = result.get("exit")
        if status not in ("exited", "timeout", "killed"):
            exit_code = None
        elif type(exit_code) is not int or (exit_code == 0 and status != "exited"):
            exit_code = {"timeout": 124, "killed": 130}.get(status)
        return output, exit_code

    def _shell_v1(
        self,
        command: str,
        events: _SessionEvents,
        timeout_s: float,
        echo: bool,
        command_timeout_s: Optional[float],
    ) -> Tuple[str, Optional[int]]:
        """Compatibility with load_complete/tool_call_update sandbox streams."""
        sentinel = f"{self._EXIT_SENTINEL_PREFIX}{secrets.token_hex(8)}__:"
        wrapped = build_shell_wrapper(command, sentinel, self._LOG_PATH, command_timeout_s)
        self._submit_shell(wrapped, events)
        buffers: Dict[str, str] = {}
        holdback = len(sentinel) + 16
        printed_len = 0
        deadline = time.monotonic() + timeout_s
        while True:
            frame = events.poll()
            if frame is not None:
                name, payload = frame
                if name == "session_update":
                    update = payload.get("update") or {}
                    call_id = update.get("toolCallId")
                    content = update.get("content")
                    if call_id and update.get("sessionUpdate") == "tool_call":
                        buffers.setdefault(call_id, "")
                    elif call_id and isinstance(content, list):
                        buffers[call_id] = "".join(
                            (item.get("content") or {}).get("text", "")
                            for item in content
                            if isinstance(item, dict)
                            and item.get("type") == "content"
                            and isinstance(item.get("content"), dict)
                        )
                elif (
                    name == "message_complete"
                    and payload.get("parentMessageId") == events.message_id
                ):
                    break
                elif (
                    name == "error"
                    and payload.get("parentMessageId") == events.message_id
                    and payload.get("op") == "shell"
                ):
                    raise RemoteError(str(payload.get("message") or "shell dispatch failed"))
            if echo:
                text = "".join(buffers.values())
                visible = max(0, len(text) - holdback)
                if visible > printed_len:
                    sys.stdout.write(text[printed_len:visible])
                    sys.stdout.flush()
                    printed_len = visible
            if time.monotonic() >= deadline:
                raise RemoteError(
                    f"timed out after {int(timeout_s)}s waiting for command completion"
                )

        raw = "".join(buffers.values())
        output, exit_code = split_exit_sentinel(raw, sentinel)
        truncated = exit_code is None and len(raw) >= self._SSE_TEXT_CAP - 100
        if truncated:
            try:
                uid = self._own_user_id()
                full_path = f"/workspaces/user_{uid}/.sessions/{events.session_id}/{self._LOG_PATH}"
                recovered_raw = self.fs_read(events.sandbox_id, full_path).decode(
                    "utf-8", errors="replace"
                )
                recovered_output, recovered_code = split_exit_sentinel(recovered_raw, sentinel)
                if recovered_code is not None:
                    output, exit_code = recovered_output, recovered_code
                    truncated = False
            except ScriptItError:
                pass
        if echo:
            sys.stdout.write(output[printed_len:])
            if output and not output.endswith("\n"):
                sys.stdout.write("\n")
            if truncated:
                sys.stdout.write(
                    "[scriptit: live output truncated at ~8KB by the platform event "
                    "stream — redirect to a file and use `scriptit fs read` for full output]\n"
                )
            sys.stdout.flush()
        return output, exit_code

    # -- files -------------------------------------------------------------

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


# Exit status when the command ran but its real status is unknowable. Unknown is
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
        f"scriptit: command completed but its exit status is unknown — exiting {UNKNOWN_EXIT_CODE}; "
        "check the session before re-running\n"
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


def _parse_exec_options(argv: List[str]) -> Tuple[List[str], Optional[str], Optional[float], bool]:
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
        if token in ("--help", "-h"):
            return [], cwd, timeout, True
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
    return args, cwd, timeout, False


def remote_exec(argv: List[str]) -> int:
    """``scriptit exec [--cwd DIR] [--timeout SECS] [--] <cmd...>``."""
    try:
        args, cwd, command_timeout, help_requested = _parse_exec_options(argv)
    except RemoteError as exc:
        sys.stderr.write(f"scriptit: {exc}\n")
        return 2
    if help_requested or not args:
        stream = sys.stdout if help_requested else sys.stderr
        stream.write("usage: scriptit exec [--cwd DIR] [--timeout SECS] -- <command...>\n")
        if help_requested:
            stream.write("Run a shell command in the current sandbox session.\n")
            return 0
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
