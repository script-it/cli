"""The exception hierarchy.

Every failure the user should see as one line rather than a traceback derives
from :class:`ScriptItError`. Handlers catch the base, so a new error type is
covered everywhere the day it is added — the previous per-type tuples meant
each new call site had to remember the full list, and one eventually would not.
"""

from __future__ import annotations


class ScriptItError(RuntimeError):
    """Anything the CLI should report as a message, not a stack trace."""


class RemoteAuthError(ScriptItError):
    """Credential problems: not logged in, refresh failed, store unusable."""


class RemoteError(ScriptItError):
    """Transport-level failures talking to Script.it."""
