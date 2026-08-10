"""The Script.it remote CLI.

A thin client for driving a Script.it sandbox from a workstation: it forwards
verbs into the sandbox and streams back output, so the sandbox's own CLI stays
the single behavior surface and no automation ever executes locally.
"""

__version__ = "0.1.0"

# Oldest sandbox CLI that answers everything this client forwards or parses:
# `scriptit skills list|show`, `scriptit describe --concepts`, and bare-name
# `scriptit start <name>` resolution. The sandbox's CLI ships inside the
# sandbox image on the platform's release schedule, independently of this
# package, so `session new` checks it and says so once when it is older.
MIN_SANDBOX_CLI_VERSION = "0.2.1"

__all__ = ["MIN_SANDBOX_CLI_VERSION", "__version__"]
