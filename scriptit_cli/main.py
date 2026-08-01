"""``scriptit`` entry point.

Routing is by verb: ``exec`` and everything this client does not answer itself
go to the sandbox; :data:`~scriptit_cli.remote.CLIENT_COMMANDS` are handled
here. Nothing in this package imports or executes the Script.it runtime — the
sandbox's own CLI is what runs automation.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from typing import List, Tuple

import fire

from scriptit_cli import __version__, output, update_check
from scriptit_cli.analytics import analytics, safe_command
from scriptit_cli.auth import AuthCommands
from scriptit_cli.commands import FsCommands, SandboxCommands, SessionCommands
from scriptit_cli.output import emit, set_json_mode, warn
from scriptit_cli.remote import CLIENT_COMMANDS, remote_dispatch_if_applicable, remote_exec

JSON_FLAG = "--json"


# Commands whose output this client formats, so `--json` is ours to read.
# `exec` is absent on purpose: everything after it is the user's command, and
# consuming a `--json` out of it would change what runs.
JSON_COMMANDS = frozenset({"auth", "fs", "session", "sandbox", "version"})

# ...minus the ones inside those groups that stream raw bytes instead of
# returning a result. `fs read` writes the file unmodified, which is the point
# of it — so rather than promise one JSON document and then not produce one,
# `--json` is a no-op there and the flag is simply consumed.
JSON_STREAMING_COMMANDS = frozenset({("fs", "read")})


def extract_json_flag(argv: List[str]) -> Tuple[List[str], bool]:
    """Pull ``--json`` out of a client invocation, without touching data.

    Two things are never ours to consume: a forwarded verb's flags, which
    belong to the sandbox's CLI, and anything after ``--``, which is opaque
    payload — `scriptit exec -- tool --json` must run `tool --json`.

    A positional value that happens to spell ``--json`` is indistinguishable
    from the flag at this layer (as it is to any flag parser); pass such a
    value as ``--content=--json`` instead.
    """
    if not argv or argv[0] not in JSON_COMMANDS:
        return argv, False
    cut = argv.index("--") if "--" in argv else len(argv)
    head, tail = argv[:cut], argv[cut:]
    if JSON_FLAG not in head:
        return argv, False
    stripped = [a for a in head if a != JSON_FLAG] + tail
    streaming = tuple(stripped[:2]) in JSON_STREAMING_COMMANDS
    return stripped, not streaming


class ScriptIt:
    """Drive your Script.it sandbox from this machine.

    Connect:
        scriptit auth login                    # browser sign-in, stores a token
        scriptit auth status                   # who am I, which deployment

    Work in the sandbox:
        scriptit exec -- <command>             # arbitrary shell
        scriptit exec --timeout 60 -- <cmd>    # ...bounded sandbox-side
        scriptit start <script>                # ...and every other verb, which
        scriptit trigger list                  #    the sandbox's CLI answers
        scriptit describe --concepts           # the platform reference

    Move files and manage the session:
        scriptit fs ls|read|write|push|pull
        scriptit session new|use|current|list
        scriptit sandbox status|wake

    Add --json to any of this client's own commands for machine-readable
    output: stdout becomes one JSON document, everything else goes to stderr.

    Everything runs in a session that is visible in the Script.it app.
    """

    def __init__(self) -> None:
        self.auth = AuthCommands()
        self.fs = FsCommands()
        self.session = SessionCommands()
        self.sandbox = SandboxCommands()

    def version(self) -> None:
        """Print this client's version."""
        emit(
            {"version": __version__},
            lambda: print(f"scriptit-cli {__version__}"),
        )


def _route(argv: List[str]) -> int:
    """Run one invocation and return its exit status."""
    if argv and argv[0] in ("--version", "-V"):
        print(f"scriptit-cli {__version__}")
        return 0

    if argv and argv[0] == "exec":
        return remote_exec(argv[1:])

    handled = remote_dispatch_if_applicable(argv)
    if handled is not None:
        return handled

    # Fire parses sys.argv directly, so the --json it must not see is removed.
    sys.argv = [sys.argv[0], *argv]
    fire.Fire(ScriptIt, name="scriptit")
    return 0


def _ensure_json_error(code: int) -> None:
    """Keep the ``--json`` contract on a failure that never reached `output`.

    `fail()` writes ``{"error": ...}`` for everything this client rejects
    itself, but Fire answers a bad command line by printing usage and exiting
    on its own. Without this, `scriptit session use --json` exits 2 with an
    empty stdout — a caller parsing stdout gets nothing to parse, which is
    exactly what the contract exists to prevent. Fire's own message is already
    on stderr, where a non-result belongs.
    """
    if not output.json_mode() or output.emitted():
        return
    json.dump(
        {"error": f"command failed (exit {code}); see stderr for details"},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


def main() -> None:
    argv, json_mode = extract_json_flag(sys.argv[1:])
    set_json_mode(json_mode)

    notice = update_check.check()
    if notice:
        warn(notice)

    started = time.monotonic()
    code = 0
    try:
        code = _route(argv)
        if code != 0:
            _ensure_json_error(code)
    except SystemExit as exc:
        # Fire exits directly on `--help` and on a usage error; that status is
        # the invocation's outcome and belongs in the event like any other.
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code != 0:
            _ensure_json_error(code)
        raise
    finally:
        # Wrapped: a broken analytics path must not turn a successful command
        # into a traceback, or change the status the caller sees.
        with contextlib.suppress(Exception):
            analytics.track(
                "cli_command_completed",
                command=safe_command(argv),
                forwarded=bool(argv) and argv[0] not in CLIENT_COMMANDS,
                exit_code=code,
                ok=code == 0,
                duration_ms=int((time.monotonic() - started) * 1000),
                json_mode=json_mode,
            )
    sys.exit(code)


if __name__ == "__main__":
    main()
