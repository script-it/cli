---
name: scriptit
description: Work on the user's Script.it workspace — build/run automation scripts, manage triggers and integrations, or run commands in their cloud sandbox. Use whenever the task involves Script.it, scriptit scripts/SKILL.md automations, or the user's Script.it sandbox.
---

# Script.it remote access

You drive the user's **Script.it sandbox** through the `scriptit` CLI.
Everything executes remotely, in their sandbox — never run Script.it DSL on
this machine. Every command you run is visible to the user as a session in the
Script.it app.

This file is deliberately short. It tells you how to start; the platform tells
you the rest, and tells you it fresh each time, so nothing here can go stale
against the user's actual account.

## Bootstrap (start of every task)

1. `scriptit auth status` — confirm the machine is connected. If it is not, ask
   the user to run `scriptit auth login`; don't run it yourself, it opens their
   browser.

2. `scriptit session new` — creates the session your work runs in, and prints
   the **context bundle**: how sessions work, where the user can watch this
   one, what integrations the account has connected, and the live index of
   every skill and script available.

   **Read every line of it.** It runs to tens of thousands of characters, and
   most hosts cap inline command output below that and write the rest to a
   file. If yours truncated it or handed you a path, open the file and read to
   the end — what is connected and what exists is at the bottom.

   Continuing earlier work? `scriptit session current` shows the session you
   are already on. Reuse it rather than starting another.

3. `scriptit describe --concepts` — the platform reference: the script / block
   / trigger model, `${{ }}` expressions, path syntax, the Python `@block()`
   API, and how to call integrations. Read it before authoring or editing any
   script; it is the same model the in-product agent works from.

Then load whatever skill fits the task — `scriptit skills show <name>` — from
the index step 2 printed.

The session is attributed to whichever harness is driving it: the CLI reads the
marker your host already sets (`CLAUDECODE`, `CODEX_SANDBOX`,
`CURSOR_TRACE_ID`, `GEMINI_CLI`, ...). Only if you are none of those does it
need telling — `export SCRIPTIT_CLIENT=<your-slug>` before step 2.
