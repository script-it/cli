---
name: scriptit
description: Work on the user's Script.it workspace — build/run automation scripts, manage triggers and integrations, or run commands in their cloud sandbox. Use whenever the task involves Script.it, scriptit scripts/SKILL.md automations, or the user's Script.it sandbox.
---

# Script.it remote access

You are working against the user's **Script.it sandbox** through the `scriptit`
CLI. Everything executes remotely in their sandbox — never run Script.it DSL
locally. All your commands are visible to the user as a session in the
Script.it app.

## Bootstrap (start of every task)

1. `scriptit auth status` — confirm the machine is connected (if not, ask the
   user to run `scriptit auth login`; don't run it yourself, it opens their
   browser).
2. `scriptit session new` — creates the session your work will run in and
   prints the **context bundle**: how sessions work, the URL where the user
   can watch this session, the account's **connected integrations** (each
   one's name, API base URL and granted scopes — this is what tells you what
   `llm-gateway`, `web-search-gateway` etc. actually are), and the live index
   of all skills (platform + the user's workspace scripts).
   **Read every line of it.** It runs to tens of thousands of characters, and
   most hosts cap inline command output below that and write the rest to a
   file. If yours truncated it or gave you a path, open the file and read to
   the end — the integrations and skills are at the bottom.
   Continuing earlier work? `scriptit session current` shows the session
   you are already on — reuse it instead of starting another.
3. `scriptit describe --concepts` — the platform reference: the script /
   block / trigger model, `${{ }}` expressions, path syntax, the Python
   `@block()` API, and how to call integrations. Read it before authoring or
   editing any script; it is the same model the in-product agent works from.

The session is attributed to whichever harness is driving it: the CLI reads
the marker your host already sets (`CLAUDECODE`, `CODEX_SANDBOX`,
`CURSOR_TRACE_ID`, `GEMINI_CLI`, ...). Only if you are something else does it
need telling — `export SCRIPTIT_CLIENT=<your-slug>` before step 2.

## Sessions

Everything you run happens inside a **session** — the same kind of conversation
the in-app agent works in, and the user can watch it live in the Script.it app.

- Your **working directory is the session's own directory** in the sandbox.
  The user's workspaces are mounted inside it, so a script at
  `/workspaces/<wid>/my-script` is `workspaces/<wid>/my-script` from where you
  stand. Both forms work.
- That mount is the same storage the app reads, not a copy: a file you edit
  shows up immediately for the user and for every other session.
- `data_files/` (relative to your cwd) is this session's scratch space. Put
  intermediate files there rather than in a workspace, which is the user's.
- The session is **sticky** — it survives across commands and shells until you
  start another. `scriptit session current` tells you which one you are on;
  `new` starts a fresh one, `use <id>` reattaches, `list` shows recent ones.
  Prefix a single command with `SCRIPTIT_SESSION=<id>` to retarget just it.
- Commands in a session **run one at a time**; a second waits for the first.
  Do not try to parallelize by firing several at once.

Start a new session for a new piece of work, and stay in it while that work
continues — the transcript is what the user reads to follow along.

## How commands work

- Any `scriptit` verb runs in the sandbox automatically: `start`, `status`,
  `logs`, `wait`, `describe`, `validate`, `new`, `trigger *`,
  `integrations *`, `skills *`, ...
- `scriptit start <script-name>` accepts a bare script name and resolves it
  across the mounted workspaces (ambiguous names error with the candidate
  paths; `workspaces/<wid>/<script>` always works explicitly).
- Arbitrary shell in the sandbox: `scriptit exec -- <command>`. The exit code
  you get back is the command's own. `--timeout <secs>` bounds it in the
  sandbox (exit 124), `--cwd <dir>` runs it elsewhere.
- Files live in the sandbox at `/workspaces/<workspace-id>/...`:
  `scriptit fs ls|read|write|push|pull`. Use `push`/`pull` to move files
  between this machine and the sandbox (binary-safe).
- The platform's live event stream caps streamed text at ~8KB, but every
  command is also tee'd to a session file, so output past the cap is read
  back automatically — you get the whole thing and the real exit code.
- Script runs (`scriptit start`) always report full structured state
  regardless of output size (`scriptit status <run-id>` / `logs` / `wait`).

## Skills

`scriptit skills list` shows everything available — platform skills (docx,
pdf, media, browser, triggers, ...) and the user's own workspace scripts.
`scriptit skills show <name>` prints the full skill. Load a relevant skill
before non-trivial work in its domain.

## Authoring and running scripts

Scripts are SKILL.md-based folders in a workspace. Scaffold with
`scriptit new <name>` (run it in the sandbox via passthrough), edit files via
`scriptit fs write`/`push`, validate with `scriptit validate <dir>`, run with
`scriptit start <name>` — a bare name resolves across the mounted workspaces,
so take it from `scriptit skills list` rather than hunting the filesystem.

`scriptit status <run-id>` is the whole answer once a run finishes: `result`
is its return value (for a script run, every block's keyed by block id) and
`files_written` lists what it wrote. Use `logs <run-id>` for stdout and
`wait <run-id>` to block until it is done.

`files_written` paths are relative to `data_files/` under your cwd — **not**
the script's own folder, even when the block declares an `output_directory`.

## Opening the app for the user

The context bundle prints this session's app URL. Open URLs with your
harness's browser tool if it has one, otherwise `open` / `xdg-open`.

- **A script** is `<session-url>/workspaces/<wid>/<script-dir>`. After you
  create or update one, open it — the product renders it as blocks, inputs and
  run history, which is what the user should be looking at rather than the
  YAML you just wrote. The session URL itself is where they watch a run.
- **Connecting an integration** is `<session-url>?settings=integrations`,
  which opens the integrations tab over the session.

## Integrations

`scriptit integrations list` is the account's connected integrations — name,
API base URL, granted scopes. Check it before writing a script that calls one.

**You cannot connect an integration.** Connecting means OAuth consent or
entering a credential in a browser; no `scriptit` verb substitutes for it. When
a script needs something that is not connected, open the integrations URL above
for the user, tell them exactly which integration to connect, and re-check with
`scriptit integrations list` once they say they're done.
