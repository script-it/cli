"""Anonymous usage analytics.

Sends: the command's name (``session new``, ``start``, ``trigger list``), how
long it took, whether it succeeded, the client/Python/OS versions, and which
agent harness is driving when one identifies itself.

Never sends: your command line, arguments, script names, file paths, file
contents, command output, environment variables, tokens, or your email.
Commands are reduced to a name drawn from a fixed allowlist before anything
leaves the process (:func:`safe_command`), so a script name, a path or a prompt
cannot ride along even by accident.

Set ``DO_NOT_TRACK=1`` to turn it off.

Events go to your own Script.it deployment, over the connection this client is
already authenticated on. No third party, no collector to configure, and no key
in this package. Nothing is sent before you log in.

Events are buffered in memory and flushed once at exit, on a daemon thread with
a hard time budget — a slow or unreachable network drops them rather than
delaying or failing a command.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import platform
import threading
from typing import Any, Callable, Dict, List, Optional

import requests

from scriptit_cli import __version__
from scriptit_cli.client import detect_client

ENDPOINT = "/api/v1/telemetry"

# The cross-tool convention, so one setting covers every CLI on the machine
# instead of one per vendor.
ENV_DO_NOT_TRACK = "DO_NOT_TRACK"

# Budget for the flush. A CLI that pauses to phone home is a CLI people rip
# out, so this is deliberately shorter than any sane request.
#
# Connect and read are split because they fail differently: an unreachable
# deployment is the common outage and shows up as a connect that never
# completes, so that half is short. One that answered slowly is already working
# and gets the longer half. The join is the real ceiling — when it expires the
# interpreter exits and the daemon thread dies with it.
_POST_TIMEOUT_SECONDS = (1.0, 2.0)
_FLUSH_BUDGET_SECONDS = 2.5

# The only words that may leave this process from a command line. An
# allowlist, not a pattern: a script name, a workspace path and a bearer token
# are all shaped exactly like a verb, so nothing but "is this one of ours?"
# can tell them apart. `start quarterly-revenue` must report as `start`.
#
# The cost is that a verb the platform adds later reports as `other` until this
# set catches up — the right direction to fail, and the `other` count is the
# signal that it needs updating.
_KNOWN_TOKENS = frozenset(
    {
        # this client's own commands and their subcommands
        "auth",
        "context",
        "current",
        "exec",
        "fs",
        "help",
        "login",
        "logout",
        "ls",
        "pull",
        "push",
        "read",
        "sandbox",
        "session",
        "use",
        "version",
        "wake",
        "write",
        # the platform CLI's verbs and subcommands, as of this release
        "cancel",
        "catalog",
        "clean",
        "compile",
        "config",
        "connect",
        "create",
        "delete",
        "describe",
        "disable",
        "edit",
        "enable",
        "eval",
        "events",
        "get",
        "info",
        "init",
        "inspect",
        "integrations",
        "list",
        "logs",
        "memory",
        "migrate",
        "migrate_workspace",
        "new",
        "new_block",
        "onboarding",
        "path",
        "reconcile",
        "result",
        "revoke",
        "rotate",
        "run",
        "runs",
        "script",
        "seed",
        "sequence",
        "set",
        "share",
        "show",
        "skills",
        "source",
        "start",
        "status",
        "sync",
        "tail",
        "test",
        "trigger",
        "unshare",
        "update",
        "update_block",
        "update_script",
        "validate",
        "wait",
    }
)


def safe_command(argv: List[str]) -> str:
    """The command's *name*, with everything user-supplied removed.

    At most the first two tokens survive, and only if they are in
    :data:`_KNOWN_TOKENS`. ``["start", "daily-report"]`` becomes ``"start"``;
    ``["exec", "--", "cat", "secrets.env"]`` becomes ``"exec"``. Anything
    unrecognized reports as ``"other"``, so an unknown command contributes a
    count and nothing else.
    """
    tokens: List[str] = []
    for token in argv[:2]:
        lowered = token.lower()
        if lowered not in _KNOWN_TOKENS:
            break
        tokens.append(lowered)
    if not tokens:
        return "other"
    return " ".join(tokens)


class Analytics:
    """Buffers events and posts them once, at exit.

    Inert until :meth:`bind` supplies somewhere to send them, which is what
    makes "not logged in" and "never reached a deployment" collect nothing
    without a separate check: the credential the events ride on is the same one
    the command needed.
    """

    def __init__(self) -> None:
        self._api_url: Optional[str] = None
        self._token: Optional[Callable[[], str]] = None
        self._events: List[Dict[str, Any]] = []
        self._registered = False

    def bind(self, api_url: str, token: Callable[[], str]) -> None:
        """Point at the deployment this invocation authenticated against.

        ``token`` is called at flush time, on the daemon thread, so a command
        that never needed a token does not mint one just to report itself.
        """
        self._api_url = api_url.rstrip("/")
        self._token = token

    @property
    def enabled(self) -> bool:
        if os.environ.get(ENV_DO_NOT_TRACK, "").strip() not in ("", "0"):
            return False
        return bool(self._api_url and self._token)

    def track(self, event: str, **properties: Any) -> None:
        if not self.enabled:
            return
        self._events.append(
            {
                "event_name": event,
                "properties": {
                    "cli_version": __version__,
                    "python_version": platform.python_version(),
                    "os": platform.system().lower(),
                    "arch": platform.machine().lower(),
                    "client": detect_client(),
                    **{k: v for k, v in properties.items() if v is not None},
                },
            }
        )
        if not self._registered:
            atexit.register(self.flush)
            self._registered = True

    def _post(self) -> None:
        """Ship the buffer. One request per event — the sink takes one at a
        time, and a run emits one or two."""
        token = self._token
        if token is None:  # unreachable: `enabled` gates every caller
            return
        headers = {"Authorization": f"Bearer {token()}"}
        url = f"{self._api_url}{ENDPOINT}"
        for event in self._events:
            requests.post(url, json=event, headers=headers, timeout=_POST_TIMEOUT_SECONDS)

    def flush(self) -> None:
        """Send what is buffered, or give up — whichever comes first.

        The daemon thread is what makes the budget a real ceiling: when the
        join times out the interpreter exits and the thread dies with it, so an
        unresponsive deployment costs the time budget and nothing more.
        """
        if not self._events or not self.enabled:
            return

        def _run() -> None:
            with contextlib.suppress(Exception):
                self._post()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(_FLUSH_BUDGET_SECONDS)
        self._events = []


analytics = Analytics()
