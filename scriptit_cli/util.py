"""Shared helpers: version parsing, and safe updates to the JSON stores."""

from __future__ import annotations

import json
import os
import re
import secrets
from typing import Any, Callable, Dict, Optional

from filelock import FileLock
from packaging.version import InvalidVersion, Version

# How long to wait for another process to finish its read-modify-write. The
# lock is only ever held across a file read, a dict update and an atomic
# rename, so anything approaching this means a wedged process rather than
# contention — better to fail loudly than to write without the lock and
# silently drop the other writer's changes.
_LOCK_TIMEOUT_SECONDS = 10

# A dotted release, optionally epoch-prefixed, plus whatever suffix follows
# (`rc1`, `.post2`, `+local`). At least one dot is required so a bare number
# in surrounding prose ("Python 3 ...") is not read as a version.
_VERSION_CANDIDATE = re.compile(r"(?:\d+!)?\d+(?:\.\d+)+[^\s]*")


def parse_version(text: str) -> Optional[Version]:
    """The first PEP 440 version in some text, or None if there isn't one.

    Version output can be an error string (`command not found`) — a missing
    answer, not an old one, so it must not be read as a version. It can also
    be a whole line (`scriptit 0.2.0`), so a candidate is extracted before
    parsing.

    :class:`~packaging.version.Version` rather than a hand-rolled
    ``(major, minor, patch)`` tuple: that shape silently truncates everything
    PEP 440 puts after the release segment, so `1.2.3rc1` compares *equal* to
    `1.2.3` and an epoch (`1!1.0.0`, which outranks every version without one)
    compares as `1.0.0`.
    """
    for candidate in _VERSION_CANDIDATE.findall(text or ""):
        try:
            return Version(candidate)
        except InvalidVersion:
            continue
    return None


def update_json_file(
    path: str, mutate: Callable[[Dict[str, Any]], Dict[str, Any]], mode: int = 0o600
) -> Dict[str, Any]:
    """Read a JSON object, apply ``mutate``, write it back — atomically, and
    serialized against other processes doing the same.

    Both stores are read-modify-write, and two CLI invocations overlap
    routinely (an agent firing commands, a shell in another terminal). Without
    the lock the writes do not corrupt the file, but the later one silently
    drops whatever the earlier one added — a rotated refresh token, another
    profile, the session another shell just chose.

    ``filelock`` rather than ``fcntl.flock`` so that holds on Windows too: it
    uses the platform's own primitive (``msvcrt.locking`` there, ``flock``
    here), and both are released by the OS when a process dies, so a crash
    cannot wedge the store.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(f"{path}.lock", timeout=_LOCK_TIMEOUT_SECONDS):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}

        data = mutate(data)

        tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        os.chmod(path, mode)
        return data
