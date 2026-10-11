"""The Claude Code plugin manifest releases with the package.

Claude Code offers plugin users an update only when `.claude-plugin/plugin.json`
changes its `version`. Holding it equal to `__version__` means every release of
this client also delivers the skills it documents, and a skill change reaches
plugin users with the next release.
"""

from __future__ import annotations

import json
from pathlib import Path

from scriptit_cli import __version__

PLUGIN_DIR = Path(__file__).resolve().parent.parent / ".claude-plugin"


def test_plugin_version_matches_package_version():
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    assert manifest["version"] == __version__, (
        "bump .claude-plugin/plugin.json `version` together with scriptit_cli.__version__"
    )


def test_marketplace_entry_leaves_version_to_the_manifest():
    # plugin.json's version silently wins over a marketplace entry's, so a
    # second copy here could only drift.
    marketplace = json.loads((PLUGIN_DIR / "marketplace.json").read_text())
    assert all("version" not in entry for entry in marketplace["plugins"])
