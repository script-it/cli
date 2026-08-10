# Contributing

Thanks for helping. Issues and pull requests are both welcome.

## Setup

```bash
git clone https://github.com/script-it/cli.git
cd cli
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The suite is fast and needs no network or account — it covers the pure pieces
(command wrapping, exit-sentinel parsing, SSE framing, the credential store,
session resolution). Anything that needs a live environment is exercised by
hand against a real account before release.

`tests/conftest.py` points `XDG_CONFIG_HOME` at a temp directory for every
test. Keep it that way: without it the credential-store tests read and
overwrite the real `~/.config/scriptit/credentials.json` on your machine.

## The boundary

The client forwards commands and formats what comes back; it does not execute
automation, parse scripts, or know what a block is. Its dependencies are
`requests`, `fire`, `filelock` and `packaging` — all small, and each earning
its place: the last two replace hand-rolled code that was wrong on Windows and
wrong about PEP 440.

That boundary is what lets the platform ship features without a client release,
and it is easy to erode by accident. If a change needs the runtime, it belongs
in the platform, not here.

**Where it is currently eroded, and why it matters.** `remote.py` encodes a
fair amount of the sandbox agent's private behaviour: it mints message ids in
the agent's branded format, base64-wraps every command because the agent
`eval`s what it is given, appends an exit sentinel because the agent discards
the child's status, and tees output to a file because the event stream
truncates text. None of that is knowledge a client should hold. It is here
because the session-shell API asks callers to supply it — the platform's own
frontend and backend each carry a copy of the same message-id generator.

The asymmetry is what makes it worth fixing rather than living with: those two
are deployed, so a change reaches them in minutes, while an installed client is
frozen until its owner upgrades. The fix is for that API to accept a plain
command and return a real exit code, which would delete most of `shell()`. Do
not add to this pile.

In practice that means:

- Adding a command that Script.it should answer? Add it there instead — it
  reaches users through the client they already have.
- Adding one this client must answer (credentials, transport, moving bytes,
  session bookkeeping)? Add it to `CLIENT_COMMANDS` in `scriptit_cli/remote.py`,
  to `_KNOWN_TOKENS` in `scriptit_cli/analytics.py`, and give it `--json`
  output — or list it in `JSON_STREAMING_COMMANDS` if it streams raw bytes
  instead of returning a result.
- Never name a client command something the platform also uses. The client
  answers its own names first, so a collision silently shadows the real one.

## Analytics

`scriptit_cli/analytics.py` reports command *names*, never their contents. The
filter is an allowlist (`_KNOWN_TOKENS`), not a pattern — a script name, a
workspace path and a bearer token are all shaped like a verb, so only "is this
one of ours?" can tell them apart. A new client command belongs in that set as
well as in `CLIENT_COMMANDS`; a test asserts the two agree, and another drives
a real invocation with secrets in argv and asserts they reach no payload.

There is no collector and no key here. Events go to the user's own deployment,
on the credential the command already used, so `Analytics` is inert until
something calls `bind()`. That is also why your tests collect nothing by
default, and why `DO_NOT_TRACK=1` is the whole opt-out.

## Platform support

Linux and macOS are what we test; the client is written to work on Windows
too. The store lock goes through `filelock`, which uses each platform's own
primitive, so concurrent credential and state writes serialize everywhere
rather than only on POSIX. Windows is not in CI, so if something there is
broken, an issue with a reproduction would be welcome.

## Pull requests

- Include a test for behavior you change. Write the assertion around *why* it
  matters, not just what the function returns — the transport has several
  properties (exit codes survive truncation, `cd` stays inside the tee'd
  subshell, `--json` keeps stdout clean) that are invisible until they break.
- Keep comments about what the code does now, not what it used to do. Change
  history belongs in commits.
- Run `pytest`, `ruff check` and `ruff format` before pushing. CI runs all
  three on Python 3.9, 3.11 and 3.13.

## Reporting a bug

Include the client version (`scriptit version`), your Python version, the
command you ran, and what happened. If it involves a command that ran in your
environment, `scriptit session current` tells you which session to look at in
the app — that transcript is usually the fastest way to see what went wrong.

Please don't paste credentials, tokens, or the contents of
`~/.config/scriptit/credentials.json` into an issue.
