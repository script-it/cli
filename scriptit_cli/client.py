"""Which harness, if any, is driving this CLI.

Its own module because everything wants it and it depends on nothing: the
session labels itself with it, analytics reports it, and `auth login` uses it
to decide whether opening a browser window would help anyone. Left in
``remote``, the analytics module could only reach it through an import inside a
function, since ``remote`` imports analytics itself.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

# Harness markers, checked in order. Self-declaration via SCRIPTIT_CLIENT
# (documented in the entry-point skill) always wins; these cover harnesses
# that export an identifying variable without being asked.
_CLIENT_ENV_MARKERS: List[Tuple[str, str]] = [
    ("CLAUDECODE", "claude-code"),
    ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),
    ("CODEX_THREAD_ID", "codex"),
    ("CODEX_SANDBOX", "codex"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("GEMINI_CLI", "gemini-cli"),
]


def detect_client() -> Optional[str]:
    """The driving harness's slug (``claude-code``, ``codex``, ...), or None.

    ``SCRIPTIT_CLIENT`` is the explicit contract — agents are told to set it
    before ``scriptit session new``. Known harness env markers are a
    best-effort fallback; both are self-reported, so this is attribution
    for the session UI and analytics, not a security boundary.
    """
    explicit = os.environ.get("SCRIPTIT_CLIENT", "").strip().lower()
    if explicit:
        return "".join(ch for ch in explicit if ch.isalnum() or ch in "-_")[:32] or None
    for var, slug in _CLIENT_ENV_MARKERS:
        if os.environ.get(var):
            return slug
    return None
