# Script.it agent skills

Automations your teammates and your AI agents can both run, built from readable
steps a human can verify.

The Script.it plugin connects your coding agent — Claude Code, Codex, or any
host that reads Agent Skills — to your [Script.it](https://script.it) workspace
through the open-source `scriptit` CLI. The agent can find, write, run and
schedule your scripts in your Script.it cloud environment, with your
integrations already connected, and every command it runs shows up as a
session in the Script.it app.

## The skill

- **[scriptit](scriptit/SKILL.md)** — the entry point. The agent checks that
  the CLI is signed in, creates a session, and fetches that session's live
  instructions with `scriptit context`; the platform supplies the rest.

## Install

You need a Script.it account and the CLI:

```bash
uv tool install scriptit-cli   # released package, from PyPI
# or the latest main: uv tool install git+https://github.com/script-it/cli.git
scriptit auth login
```

Then add the plugin to your agent:

- **Claude Code:** `/plugin marketplace add script-it/cli`, then
  `/plugin install scriptit@script-it`.
- **Codex:** `codex plugin marketplace add script-it/cli`, then
  `codex plugin add scriptit@script-it`.
- **Any other host:** copy the `scriptit/` folder into the host's skills
  directory (for example `~/.claude/skills/` or `.agents/skills/`).

## What it sends, and where

The skill has the agent run the `scriptit` CLI on your machine. The plugin
itself has no MCP servers, hooks or scripts.

- **Your Script.it deployment** (`api.script.it`, or the one you signed in to
  with `--api-url`) receives every command the agent runs and the files it
  moves with `scriptit fs`. Commands run in your Script.it environment, and
  each one is recorded in a session you can see in the app.
- **Signing in** to Script.it cloud goes through your browser and exchanges
  tokens with Google's Firebase Authentication
  (`identitytoolkit.googleapis.com`, `securetoken.googleapis.com`); a
  self-hosted deployment uses its own Keycloak instead. The CLI keeps the
  resulting login at `~/.config/scriptit/credentials.json` (mode 0600).
  `scriptit auth browser-url` mints a one-time link, valid for 60 seconds,
  that opens the app signed in as you.
- **The CLI** checks PyPI at most once a day for a newer release
  (`SCRIPTIT_NO_UPDATE_CHECK=1` turns it off), and sends anonymous usage
  analytics — command names and exit codes, never their arguments — to your
  own deployment (`DO_NOT_TRACK=1` turns it off).

## Not in the plugin

**[scriptit-browser-cookies](../extra-skills/scriptit-browser-cookies/SKILL.md)**
lives outside this folder and is not part of the plugin. When you ask it to,
it reads your local Chrome cookies for the sites you name and uploads them to
your Script.it environment, so browser automations there run as you. To use
it, copy its folder into your agent's skills directory.

The [CLI README](../README.md) covers everything else.
