"""A once-a-day "newer version available" notice.

Deliberately not self-update: a CLI that rewrites itself surprises people and
fights whatever package manager installed it. The notice tells you; `pip
install -U` is yours to run.

Nothing here can delay a command. The version that gets compared is whatever
the last run cached, so the check on the request path is a file read; the
refresh happens on a daemon thread whose result is only ever used next time.
Set ``SCRIPTIT_NO_UPDATE_CHECK=1`` to silence it entirely.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from typing import Optional, Tuple

from scriptit_cli import __version__
from scriptit_cli.config import _config_dir
from scriptit_cli.util import parse_version, update_json_file

CACHE_FILE = "update-check.json"
PYPI_URL = "https://pypi.org/pypi/scriptit-cli/json"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_FETCH_TIMEOUT_SECONDS = 3.0
ENV_DISABLE = "SCRIPTIT_NO_UPDATE_CHECK"


def _cache_path() -> str:
    return os.path.join(_config_dir(), CACHE_FILE)


def _read_cache() -> Tuple[float, Optional[str]]:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        # This runs before every command, so nothing in here may be the thing
        # that fails one. The container and both fields are checked
        # separately: a well-formed object with `"latest": 123` passes an
        # isinstance check on the object and then raises inside the version
        # comparison instead.
        if not isinstance(data, dict):
            return 0.0, None
        latest = data.get("latest")
        if not isinstance(latest, str):
            latest = None
        return float(data.get("checked_at") or 0), latest
    except (OSError, ValueError, TypeError):
        return 0.0, None


def _write_cache(latest: Optional[str]) -> None:
    # A read-only or full config dir must not break the command.
    with contextlib.suppress(OSError):
        update_json_file(
            _cache_path(),
            lambda _cache: {"checked_at": time.time(), "latest": latest},
            mode=0o644,
        )


def is_newer(latest: Optional[str], current: str = __version__) -> bool:
    """True when ``latest`` is a strictly higher release than ``current``.

    PEP 440 ordering, so a prerelease does not read as its own final release
    (`0.2.0rc1` must not silence the notice for `0.2.0`) and a post-release or
    epoch is ranked as its author intended.
    """
    a, b = parse_version(latest or ""), parse_version(current)
    return bool(a and b and a > b)


def _fetch_latest() -> None:
    try:
        import requests

        resp = requests.get(PYPI_URL, timeout=_FETCH_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            # Record the attempt so an unpublished or unreachable index is not
            # re-fetched on every invocation.
            _write_cache(None)
            return
        _write_cache(resp.json().get("info", {}).get("version"))
    except Exception:
        _write_cache(None)


def check(now: Optional[float] = None) -> Optional[str]:
    """The notice to print, if any, and schedule a refresh when stale."""
    if os.environ.get(ENV_DISABLE, "").strip():
        return None
    checked_at, latest = _read_cache()
    if (now or time.time()) - checked_at > CHECK_INTERVAL_SECONDS:
        threading.Thread(target=_fetch_latest, daemon=True).start()
    if not is_newer(latest):
        return None
    return (
        f"scriptit-cli {latest} is available (you have {__version__}) — "
        "upgrade with `pip install -U scriptit-cli`"
    )
