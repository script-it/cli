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
(v2 output paging and reconnects, v1 command wrapping and exit-sentinel
parsing, SSE framing, the credential store, session resolution). Anything that needs a live environment is exercised by
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

`remote.py` supports two deployed session transports. The initial SSE frame
selects one before dispatch: v2 uses `session_snapshot`, v1 uses
`load_complete`. V2 sends plain commands, reads output by byte cursor and
gets exit status from shell events or a saved shell row. V1 requires base64
wrapping, an exit sentinel and file recovery after its SSE text cap; keep that
compatibility code isolated.

A shell POST is not safe to repeat after a lost response. Retry only a refusal
that explicitly says it was not submitted. V2 event reconnects send
`Last-Event-ID`, match this invocation's message/shell identity and resume
output reads from the returned byte cursor. A saved truncated preview must
never count as complete command output.

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
  properties (no duplicated execution after a lost acknowledgement, complete
  Unicode output across pages, `--json` keeping stdout clean) that are invisible
  until they break.
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
