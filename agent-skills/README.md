# Script.it agent skills

Automations your teammates and your AI agents can both run, built from readable
steps a human can verify.

These skills connect your coding agent — Claude Code, Codex, or any host that
reads Agent Skills — to your [Script.it](https://script.it) workspace through
the open-source `scriptit` CLI. The agent can find, write, run and schedule
your scripts in your Script.it cloud environment, with your integrations
already connected, and every command it runs shows up as a session in the
Script.it app.

## Skills

- **[scriptit](scriptit/SKILL.md)** — the entry point. The agent checks that
  the CLI is signed in, creates a session, and fetches that session's live
  instructions with `scriptit context`; the platform supplies the rest.
- **[scriptit-browser-cookies](scriptit-browser-cookies/SKILL.md)** — on
  request, copies your logged-in browser cookies for the sites you name into
  your Script.it environment, so browser automations there run as you.

## Install

You need a Script.it account and the CLI:

```bash
uv tool install git+https://github.com/script-it/cli.git
scriptit auth login
```

Then add the skills to your agent:

- **Claude Code:** `/plugin marketplace add script-it/cli`, then
  `/plugin install scriptit@script-it`.
- **Codex:** `codex plugin marketplace add script-it/cli`, then
  `codex plugin add scriptit@script-it`.
- **Any other host:** copy a skill folder, such as `scriptit/`, into the
  host's skills directory (for example `~/.claude/skills/` or
  `.agents/skills/`).

The [CLI README](../README.md) covers everything else.
