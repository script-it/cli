"""Everything an agent is told at session start.

`scriptit session new` prints a **context bundle** — the only briefing a
harness gets before it starts driving someone's sandbox. It has to answer, in
one screen: where am I standing, what can I run, what is connected, and how do
I show the user what I did.

Half of it is prose written here; the other half is read live from the
platform (`scriptit version`, `integrations list`, `skills list`) so a change
to the account or the skill catalog never needs a client release. This module
holds both, and the assembly that joins them, so the command surface in
:mod:`scriptit_cli.commands` stays commands.

The prose is agent-facing, not user-facing: it is read once, by a model, as
instructions. That makes it worth the same care as code — every line either
changes what the agent does or is dead weight in its context window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from scriptit_cli import MIN_SANDBOX_CLI_VERSION
from scriptit_cli.errors import ScriptItError
from scriptit_cli.util import parse_version

if TYPE_CHECKING:  # circular at runtime — RemoteClient is only a type here
    from scriptit_cli.remote import RemoteClient


NOTES = """\
Your session
- Everything you run happens inside one Script.it session — the same kind of
  conversation the in-app agent works in, visible live in the Script.it app.
  Your commands appear there with their output, run history and cost.
- Every command's working directory IS that session's directory in the
  sandbox. Your workspaces are mounted inside it, so a script that lives at
  /workspaces/<wid>/my-script is `workspaces/<wid>/my-script` from where you
  are standing. Both forms work; the relative one is shorter.
- That mount is the same storage the app uses, not a copy — a file you edit is
  visible immediately to every other session and to the user.
- `data_files/` (relative to your cwd) is scratch space belonging to this
  session. Write intermediate files there, not into a workspace.
- The session is sticky: it persists across commands and across shells until
  you start another. `scriptit session current` shows which one you are on,
  `new` starts a fresh one, `use <id>` reattaches to an earlier one, `list`
  shows recent ones. `SCRIPTIT_SESSION=<id> scriptit ...` retargets a single
  command without disturbing the sticky one.
- Commands in a session run one at a time — a second one waits for the first.

How to work remotely
- `scriptit` verbs run in your sandbox automatically (start, status, logs,
  trigger, integrations, skills, ...). Arbitrary shell: `scriptit exec -- <cmd>`.
  Cap a slow one with `scriptit exec --timeout <secs> -- <cmd>`.
- Files: `scriptit fs ls|read|write|push|pull` (paths under /workspaces/<wid>/...).
  `push`/`pull` move bytes between the user's machine and the sandbox.
- `scriptit describe --concepts` is the full platform reference: the script /
  block / trigger model, `${{ }}` expressions, path syntax, the Python
  `@block()` API, and how to call integrations. Read it before authoring.
- Skills: `scriptit skills list` (index below), `scriptit skills show <name>`
  for the full skill. Workspace scripts are themselves skills.
- Add `--json` to auth/fs/session/sandbox for machine-readable output
  (`fs read` and `exec` stream raw output instead, by design).

Running a script and finding what it produced
- `scriptit start <name>` takes a bare script name and resolves it across the
  mounted workspaces. The skills index below IS the script index — take the
  name from there instead of searching the filesystem for it.
- `start` streams one JSON line per run: the script's own, then one per block.
  `scriptit list` shows the same runs newest-first if you lose the ids.
- `scriptit status <run-id>` is the whole answer once a run finishes:
  `result` is its return value (for a script run, every block's keyed by
  block id) and `files_written` lists what it wrote. `logs <run-id>` for the
  stdout, `wait <run-id>` to block until it is done.
- `files_written` paths are relative to `data_files/` under your cwd. Block
  outputs land in the session's data dir, never in the script's own folder,
  even when the block declares an `output_directory`: a block that writes
  `color/output.html` put it at `data_files/color/output.html`, not
  `<script>/assets/...`."""

# Rendered only when the login recorded the deployment's browser origin.
# Both links exist because the agent cannot do these things itself: a script is
# a document the product renders, and connecting an integration is a browser
# flow. Handing over a URL is the whole affordance, so it is given exactly
# rather than left to be reconstructed from a route the client does not own.
LINKS = """\

Opening the app for the user
- This session: {session_url}
  Open it with your harness's browser tool if it has one, otherwise `open` /
  `xdg-open`.
- A script: {session_url}/workspaces/<wid>/<script-dir>
  (append the script's path exactly as it appears under /workspaces).
  After you create or update a script, open that URL — the product renders it
  as blocks, inputs and run history, which is what the user should be looking
  at rather than the YAML you just wrote.
- Connecting an integration: {integrations_url}
  Connecting is a browser flow — OAuth consent, or entering a credential — so
  it cannot happen from here, and no `scriptit` verb can stand in for it. When
  a script needs something that is not in the connected list below, open that
  URL for the user and tell them which integration to connect; then re-check
  with `scriptit integrations list`."""

# One dispatch, several payloads — a session/shell round-trip costs an SSE
# bootstrap, so the probe emits every section and the printer splits them.
MARKER = "===SCRIPTIT-CONTEXT-SPLIT==="
PROBE = (
    "scriptit version 2>&1;"
    f" echo '{MARKER}';"
    " scriptit integrations list 2>/dev/null;"
    f" echo '{MARKER}'; scriptit skills list 2>&1"
)


def skew_warning(sandbox_version_output: str) -> Optional[str]:
    """A warning when the sandbox's CLI predates what this client expects.

    Verbs are forwarded verbatim and executed by the sandbox's own CLI, which
    ships with the sandbox image on the platform's release schedule — this
    client is released separately, so the two versions differ routinely and a
    mere mismatch says nothing. What matters is the sandbox being *older* than
    ``MIN_SANDBOX_CLI_VERSION``: then a verb this client forwards can be
    missing over there, and the failure reads as a broken command rather than
    a version gap.
    """
    remote = parse_version(sandbox_version_output)
    minimum = parse_version(MIN_SANDBOX_CLI_VERSION)
    if remote is None or minimum is None or remote >= minimum:
        return None
    return (
        f"Note: your sandbox runs Script.it CLI {remote}, "
        f"older than the {MIN_SANDBOX_CLI_VERSION} this client expects. Verbs run "
        "in the sandbox, so some commands may be missing there until its image "
        "is updated."
    )


def urls(client: "RemoteClient", session_id: Optional[str]) -> Dict[str, Optional[str]]:
    """The app links for a session — pure string work, no round-trip.

    Cheap enough that every command reporting a session can carry them, which
    is the point: an agent that has a session id in hand should never have to
    re-derive a route this client does not own. Both are None when the login
    recorded no browser origin; a fabricated host would be worse than no link.
    """
    session_url = f"{client.app_url}/app/s/{session_id}" if client.app_url and session_id else None
    return {
        "session_url": session_url,
        # `?settings=integrations` is the app's own deep link: it opens the
        # session and the integrations tab over it, so the user lands where
        # they connect without losing sight of the work that needed it.
        "integrations_url": f"{session_url}?settings=integrations" if session_url else None,
    }


def build(client: "RemoteClient", sandbox_id: str, session_id: str) -> Dict[str, Any]:
    """The session-start context bundle: usage notes, the app links for this
    session, connected integrations, live skills index.

    The live half comes from platform verbs, so this client never has to know
    where anything lives in the sandbox — `integrations list` returns each
    connected account with its api_base_url, scopes and docs, which is what an
    agent needs to construct calls. Composed at session start so all of it is
    current; failures never block session setup.
    """
    link = urls(client, session_id)
    bundle: Dict[str, Any] = {
        "notes": NOTES,
        **link,
        "links": LINKS.format(**link) if link["session_url"] else None,
        "integrations": None,
        "skills": None,
        "skew_warning": None,
        "unavailable": None,
    }
    try:
        output, _ = client.shell(
            PROBE,
            sandbox_id=sandbox_id,
            session_id=session_id,
            timeout_s=120.0,
            echo=False,
        )
    except ScriptItError as exc:
        bundle["unavailable"] = str(exc)
        return bundle
    version_out, _, rest = output.partition(MARKER)
    integrations, _, skills = rest.partition(MARKER)
    bundle["skew_warning"] = skew_warning(version_out)
    bundle["integrations"] = integrations.strip() or None
    bundle["skills"] = skills.strip() or None
    return bundle


# Printed first, because a harness that truncates decides what to keep by
# position. The bundle runs to tens of thousands of characters — most agent
# hosts cap inline command output well below that and spill the rest to a
# file, so an agent can silently act on the first screen and never see the
# skills index or what is connected.
_PREAMBLE = """\
=== Script.it session context: read all of it before running anything ===
This is instructions, not output. Below: how sessions work, the app links to
open for the user, the account's connected integrations, and the full skills
index. It is long. If your harness truncated this or wrote it to a file, open
that file and read to the end — acting on the first part alone means working
without knowing what is connected or what skills exist.
"""


def render(bundle: Dict[str, Any]) -> None:
    """Print the bundle for a human/agent reading stdout.

    `--json` callers get the dict itself; this is the other half of that same
    payload, so the two never say different things.
    """
    print(_PREAMBLE)
    print(bundle["notes"])
    if bundle["links"]:
        print(bundle["links"])
    if bundle["unavailable"]:
        print(f"\n(session context unavailable: {bundle['unavailable']})")
        return
    if bundle["skew_warning"]:
        print(f"\n{bundle['skew_warning']}")
    print(
        f"\n{bundle['integrations']}" if bundle["integrations"] else "\n(no integrations connected)"
    )
    print("\nAvailable skills:")
    print(bundle["skills"] or "(none found)")
