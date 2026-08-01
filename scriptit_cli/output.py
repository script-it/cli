"""Human vs machine output.

``--json`` makes stdout exactly one JSON document and sends everything else —
progress, warnings, errors — to stderr. That contract is what lets a caller
pipe stdout straight into a parser without stripping chatter first, which
matters here because the usual caller is an agent.

Human mode is unchanged: prose on stdout, errors on stderr.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, NoReturn

_json_mode = False
_emitted = False


def set_json_mode(enabled: bool) -> None:
    global _json_mode
    _json_mode = enabled


def json_mode() -> bool:
    return _json_mode


def emitted() -> bool:
    """Whether the one JSON document has already been written.

    The entry point uses this to answer a failure that never reached this
    module — an argument parser rejecting the command line, say — without
    writing a second document over a real result.
    """
    return _emitted


def emit(payload: Any, human: Callable[[], None]) -> None:
    """The command's result: one JSON document, or whatever ``human`` prints."""
    global _emitted
    if _json_mode:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        _emitted = True
    else:
        human()


def note(message: str) -> None:
    """Progress and advisories — never part of the result.

    Flushed: callers print a URL and then block waiting for the browser, and
    through a pipe an unflushed line would not appear until the wait ended.
    """
    print(message, file=sys.stderr if _json_mode else sys.stdout, flush=True)


def warn(message: str) -> None:
    """Always stderr: a warning is not a result in either mode."""
    print(message, file=sys.stderr, flush=True)


def fail(message: str, code: int = 1) -> NoReturn:
    """Report an error and exit.

    In JSON mode this is still the one document on stdout — a caller that
    parses stdout gets a structured reason instead of an empty stream plus
    prose it would have to read.
    """
    global _emitted
    if _json_mode:
        json.dump({"error": message}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        _emitted = True
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)
