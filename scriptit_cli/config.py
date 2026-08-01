"""Where the client keeps its state.

Credentials and the per-profile anchor-session state live in one directory, so
a machine is disconnected by removing it.
"""

from __future__ import annotations

import os


def _config_dir() -> str:
    """``$XDG_CONFIG_HOME/scriptit``, else ``~/.config/scriptit``.

    The same directory the in-sandbox CLI uses, so a machine that has both
    installed shares one set of credentials.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "scriptit")
