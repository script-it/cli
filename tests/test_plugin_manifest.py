"""The Claude Code plugin manifest releases with the package.

Claude Code offers plugin users an update only when the plugin's
`.claude-plugin/plugin.json` changes its `version`. Holding it equal to
`__version__` means every release of this client also delivers the skills it
documents, and a skill change reaches plugin users with the next release.
"""

from __future__ import annotations

import json
from pathlib import Path

from scriptit_cli import __version__

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = ROOT / "agent-skills" / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

# Skills in agent-skills/ that ship only as folders to copy, outside the plugin.
# The browser-cookies skill sends login cookies off the machine, which the
# directory listing's attestation rules out.
COPY_ONLY_SKILLS = {"scriptit-browser-cookies"}


def test_plugin_version_matches_package_version():
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["version"] == __version__, (
        "bump the plugin manifest's `version` together with scriptit_cli.__version__"
    )


def test_plugin_lists_every_skill_folder_except_copy_only_ones():
    # Codex reads only the listed folders when `skills` names them, and skips a
    # `"./"` pointing at the plugin root, so each skill is listed by name.
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    listed = {Path(entry).name for entry in manifest["skills"]}
    on_disk = {p.parent.name for p in (ROOT / "agent-skills").glob("*/SKILL.md")}
    assert listed == on_disk - COPY_ONLY_SKILLS


def test_marketplace_entry_leaves_version_to_the_manifest():
    # plugin.json's version silently wins over a marketplace entry's, so a
    # second copy here could only drift.
    marketplace = json.loads(MARKETPLACE.read_text())
    assert all("version" not in entry for entry in marketplace["plugins"])
