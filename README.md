# scriptit-cli

<!-- PyPI badges are held back until the package is published; shields.io
     renders "package or version not found" for an unpublished name.
[![PyPI](https://img.shields.io/pypi/v/scriptit-cli.svg)](https://pypi.org/project/scriptit-cli/)
[![Python](https://img.shields.io/pypi/pyversions/scriptit-cli.svg)](https://pypi.org/project/scriptit-cli/)
-->
[![CI](https://github.com/bespo-ai/script.it-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/bespo-ai/script.it-cli/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Your [Script.it](https://script.it) workspace, from the terminal — and from any
coding agent.

Script.it turns a knowledge-work task into a script you can read, edit and
depend on: describe what you want, connect the apps it needs, then run it on
demand, on a schedule, or from a webhook. Your scripts run in an isolated cloud
environment with your integrations already connected.

This CLI drives that environment from your machine.

## Install

**Before you start.** Python 3.9 or newer, and a Script.it account — create one
at [script.it](https://script.it), or point the CLI at a self-hosted deployment
with `--api-url`.

**1. Install the CLI.** It goes in its own isolated environment, so its
dependencies never mix with a project's:

```bash
uv tool install git+https://github.com/bespo-ai/script.it-cli.git
```

Installing from git while the package is pre-release; a `pip install
scriptit-cli` will replace this once it is published.

<details>
<summary>Other installers</summary>

```bash
# pipx — same isolation as uv tool
pipx install git+https://github.com/bespo-ai/script.it-cli.git

# plain pip, into whatever environment is active
pip install git+https://github.com/bespo-ai/script.it-cli.git
```

Prefer `uv tool` or `pipx`: they keep the CLI and its dependencies out of
whatever environment happens to be active.

No `uv`? `curl -LsSf https://astral.sh/uv/install.sh | sh`, or use `pipx`.
</details>

**2. Check it landed.**

```bash
scriptit version
```

**3. Sign in.** This opens your browser. Your password never reaches the CLI —
it stores a refresh token, plus the short-lived token it trades that for, at
`~/.config/scriptit/credentials.json` (mode 0600):

```bash
scriptit auth login
```

On a machine with no browser (SSH, a container), add `--manual` and paste the
code it asks for. For an on-prem deployment, add
`--api-url https://<your-deployment>` — the CLI reads the auth mode from it and
switches to a device-code flow by itself.

**4. See what you have, then run one.** `skills list` is the index of your
scripts (alongside the platform's own skills); `start` takes a bare name from
it and resolves it across your workspaces:

```bash
scriptit skills list
scriptit start <name-from-that-list>
```

### Set up your coding agent

Install the entry-point skill once, and any agent that reads skills knows how
to drive your workspace:

```bash
mkdir -p ~/.claude/skills/scriptit
curl -sSL https://raw.githubusercontent.com/bespo-ai/script.it-cli/main/agent-skills/scriptit/SKILL.md \
  -o ~/.claude/skills/scriptit/SKILL.md
```

For Codex-style hosts, paste that file into `AGENTS.md` instead.
[Use it from a coding agent](#use-it-from-a-coding-agent) explains what the
skill does and why it stays this small.

### Keeping it current, and removing it

```bash
uv tool upgrade scriptit-cli
uv tool uninstall scriptit-cli
```

Logging out forgets the stored credential without uninstalling anything:

```bash
scriptit auth logout
```

## Why you might want it

- **Work on your automations with a coding agent.** Point Claude Code, Codex or
  Cursor at your Script.it workspace and let it read, write and run your
  scripts. That is what this CLI was built for — see
  [Use it from a coding agent](#use-it-from-a-coding-agent).
- **Stay in the terminal.** Run a script, tail its logs, check a trigger, move a
  file, without switching to the browser.
- **Script your scripts.** The commands this client answers speak `--json`, so
  you can wire Script.it into whatever you already automate with.

## How it works

The CLI is a thin remote client. Commands you type run **in your Script.it
environment**, not on your machine:

```
scriptit (your machine)
  │  Bearer <token>                     (scriptit auth login)
  ▼
Script.it  ──►  your isolated environment  ──►  your script, your integrations
```

Nothing automation-related runs locally. No scripts execute on your machine, and
your integration credentials are never copied to it. Everything you run lands in
a real session you can watch live in the Script.it app — the same view your
teammates see.

## Connect

[Install](#install) covers signing in. Beyond that, keep several accounts or
deployments side by side as named profiles:

```bash
scriptit auth login --api-url https://scriptit.internal.example --profile work
scriptit auth use work
scriptit auth status
```

## Use

```bash
scriptit start weekly-report          # run a script
scriptit logs <run-id>                # ...and read what it did
scriptit trigger list                 # what fires it, and when
scriptit integrations list            # what it can reach
scriptit describe --concepts          # the full platform reference
```

Run any shell command in your environment:

```bash
scriptit exec -- ls /workspaces
scriptit exec --timeout 300 -- pytest -q
scriptit exec --cwd /workspaces/<workspace-id> -- ls
```

`--timeout` is enforced **where the command runs**, so it exits `124` there
rather than outliving a client that stopped waiting.

Move files both ways:

```bash
scriptit fs ls /workspaces/<workspace-id>
scriptit fs push ./report.csv /workspaces/<workspace-id>/assets/report.csv
scriptit fs pull /workspaces/<workspace-id>/logs/run.log .
```

### Sessions

Every command runs inside a **session** — the same kind of conversation the
in-app agent works in, and you can watch it live in the app. A session is
sticky: it persists across commands and across shells until you start another.

```bash
scriptit session current              # which session am I on, and why
scriptit session new                  # start a fresh one
scriptit session use ses_abc123       # reattach to an earlier one
scriptit session list                 # recent sessions
```

The session has its own directory, and that directory is the working directory
for every command you run. Your workspaces are mounted inside it, so a script at
`/workspaces/<wid>/my-script` is also `workspaces/<wid>/my-script` from where
commands land. It is a mount, not a copy — anything you change is immediately
visible in the app and to every other session. `data_files/` is the session's
own scratch space.

To run one command in a different session without disturbing the sticky one:

```bash
SCRIPTIT_SESSION=ses_abc123 scriptit exec -- ls
```

That is also how an agent harness gives each task its own transcript.

### Machine-readable output

Add `--json` to any of `auth`, `fs`, `session`, `sandbox` or `version`.
**stdout becomes exactly one JSON document, and everything else — progress,
prompts, warnings, errors — goes to stderr**, so you can pipe stdout straight
into a parser:

```bash
scriptit sandbox status --json | jq -r '.sandboxes[].state'
scriptit session new --json | jq -r '.context.skills'
```

On failure the document is `{"error": "..."}` and the exit code is non-zero —
including when the argument parser is the thing that rejected the command, so
there is no case where stdout comes back empty.

`auth login --json` still opens a browser and prints its progress, but to
stderr; the document it returns is the resulting login. `fs read` is the one
exception: it writes the file's bytes unmodified, so `--json` is accepted and
ignored rather than promising a document it will not produce. `exec` likewise
streams the command's own output. Forwarded commands pass `--json` through to
Script.it, which decides what it means.

### Which commands are which

This client answers `auth`, `exec`, `fs`, `session`, `sandbox` and `version`
itself. **Everything else is forwarded** — `start`, `status`, `logs`, `wait`,
`validate`, `describe`, `trigger *`, `integrations *`, `skills *`, … — and
answered by Script.it.

That is deliberate: the platform stays the single source of behavior, so a
feature added there works through the client you already have. No upgrade
needed, and no command list here to fall out of date.

`scriptit --help` covers the local commands; `scriptit describe --concepts`
covers the platform.

## Use it from a coding agent

One file — [`agent-skills/scriptit/`](agent-skills/scriptit/SKILL.md) — installed
into the agent host once; [Set up your coding agent](#set-up-your-coding-agent)
above has the command.

The skill carries almost no platform knowledge on purpose. It bootstraps with
`scriptit session new`, which returns a live context bundle — how sessions
work, the app links for watching the session and connecting integrations, your
connected integrations, and the current index of skills and scripts — so the
agent's picture of your account is correct without the skill ever being
updated.

## Good to know

- **Sessions run one command at a time.** A second command queues rather than
  interleaving; the client waits for you.
- **Paused environments wake automatically.** Any command resumes yours and
  waits until it is ready, so there is nothing to start by hand.
- **Live output is capped at ~8KB** by the platform's event stream. The client
  also tees each command to a file and reads the remainder back, so you get
  complete output and a real exit code anyway. Script runs report structured
  state regardless of size.
- **No interactive shell.** Commands are dispatched fire-and-forget and their
  output is collected from an event stream, with no channel for stdin — so
  `scriptit exec -- vim` cannot work. Use one-shot commands, or the app.
- **Update notices, not auto-update.** The client checks at most once a day, in
  the background, and prints one line to stderr when a newer release exists.
  `SCRIPTIT_NO_UPDATE_CHECK=1` silences it.
- **Anonymous usage analytics, opt-out.** Command names and exit codes, never
  their contents or arguments. `DO_NOT_TRACK=1` turns it off.

### Exit codes

| Code | Meaning |
|---|---|
| the command's own | forwarded verbatim, exactly as it exited |
| `2` | not connected — run `scriptit auth login` |
| `124` | `--timeout` expired; the command was stopped |
| `125` | it ran, but its status could not be recovered. **Not success.** |
| `130` | you interrupted the client; the command may still be running |

## Environment variables

| Variable | What it does |
|---|---|
| `SCRIPTIT_PROFILE` | use a named profile for one invocation |
| `SCRIPTIT_SESSION` | run in a named session for one invocation |
| `SCRIPTIT_CLIENT` | label the session with the agent driving it (`claude-code`, `codex`, …) |
| `XDG_CONFIG_HOME` | where credentials and state live (default `~/.config`) |

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
for setup and the one architectural rule this repo keeps.

## Security

Found a vulnerability? Please report it privately through GitHub's
[**Report a vulnerability**](https://github.com/bespo-ai/script.it-cli/security/advisories/new)
form rather than opening an issue — see [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE)
