"""Workstation-side commands for remote sandbox access.

``fs`` wraps the platform's file API; ``session`` manages the sticky anchor
session that remote shell commands run in; ``sandbox`` surfaces lifecycle
state. All of these are client conveniences with no in-sandbox analogue —
every other verb passes through and runs inside the sandbox.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

from scriptit_cli import agentcontext
from scriptit_cli.analytics import analytics
from scriptit_cli.errors import ScriptItError
from scriptit_cli.output import emit, fail
from scriptit_cli.remote import ENV_SESSION, RemoteClient, update_state


def _client_or_exit() -> RemoteClient:
    try:
        return RemoteClient()
    except ScriptItError as exc:
        fail(str(exc))


def _run(fn, *args: Any, **kwargs: Any) -> Any:
    # OSError covers local-file failures (push of a missing file, pull into
    # an unwritable path) — formatted like transport errors, no traceback.
    try:
        return fn(*args, **kwargs)
    except (ScriptItError, OSError) as exc:
        fail(str(exc))


class FsCommands:
    """Files in your sandbox workspaces (paths like /workspaces/<wid>/...)."""

    def ls(self, path: str) -> None:
        """List a sandbox directory."""
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        body: Dict[str, Any] = _run(client.fs_list, sandbox_id, path)
        entries = body.get("entries") or body.get("files") or []

        def _human() -> None:
            for entry in entries:
                if isinstance(entry, dict):
                    name = entry.get("name") or entry.get("path") or "?"
                    is_dir = entry.get("is_directory") or entry.get("is_dir")
                    print(f"{name}{'/' if is_dir else ''}")
                else:
                    print(entry)

        emit({"path": path, "entries": entries}, _human)

    def read(self, path: str) -> None:
        """Print a sandbox file to stdout (bytes pass through unmodified)."""
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        data: bytes = _run(client.fs_read, sandbox_id, path)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def write(self, path: str, content: str) -> None:
        """Write text content to a sandbox file (creates parent dirs)."""
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        _run(client.fs_write, sandbox_id, path, content)
        emit(
            {"path": path, "bytes": len(content.encode("utf-8"))},
            lambda: print(f"Wrote {path}"),
        )

    def push(self, local: str, remote: str) -> None:
        """Upload a local file into the sandbox (binary-safe, ≤30MB)."""
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        _run(client.fs_upload, sandbox_id, remote, local)
        emit(
            {"local": local, "remote": remote},
            lambda: print(f"Pushed {local} -> {remote}"),
        )

    def pull(self, remote: str, local: str) -> None:
        """Download a sandbox file to a local path (parents auto-created).

        A directory destination gets the remote basename appended, so
        ``scriptit fs pull /remote/report.txt .`` lands ``./report.txt``.
        """
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        data: bytes = _run(client.fs_read, sandbox_id, remote)
        # A trailing slash names a directory even before it exists.
        if os.path.isdir(local) or local.endswith(("/", os.sep)):
            local = os.path.join(local, os.path.basename(remote.rstrip("/")))

        def _write_local() -> None:
            parent = os.path.dirname(local)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(local, "wb") as f:
                f.write(data)

        # An unwritable or invalid destination is an ordinary error, not a
        # traceback — the same treatment the remote half already gets.
        _run(_write_local)
        emit(
            {"remote": remote, "local": local, "bytes": len(data)},
            lambda: print(f"Pulled {remote} -> {local} ({len(data)} bytes)"),
        )


_ANCHOR_SOURCES = {
    "env": f"{ENV_SESSION} environment variable",
    "state": "sticky anchor for this login",
    "none": "none yet",
}


class SessionCommands:
    """The anchor session remote commands run in (visible in the app UI)."""

    def new(self, context: bool = True) -> None:
        """Create a fresh anchor session and make it current.

        Prints the context bundle (usage notes + current skills index) unless
        --nocontext.
        """
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        session_id = _run(client.create_session, sandbox_id)
        update_state(client.state_key, sandbox_id=sandbox_id, session_id=session_id)
        # The funnel's first real step: a harness that gets this far has a
        # working credential and a live sandbox to drive.
        analytics.track("cli_session_created")
        bundle = agentcontext.build(client, sandbox_id, session_id) if context else None

        def _human() -> None:
            print(f"Anchor session: {session_id} (sandbox {sandbox_id})\n")
            if bundle:
                agentcontext.render(bundle)

        payload: Dict[str, Any] = {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
        }
        if bundle:
            payload["context"] = bundle
        emit(payload, _human)

    def current(self) -> None:
        """Show the current anchor session, where it came from, and its app URL."""
        client = _client_or_exit()
        session_id, sandbox_id, source = client.anchor()
        # Carried here as well as in the bundle: this is the command an agent
        # runs when it already has a session and just needs to know which, and
        # a link it cannot get here is a link it will try to build itself.
        link = agentcontext.urls(client, session_id)

        def _human() -> None:
            if not session_id:
                print("No anchor session yet — one is created on first use.")
                return
            print(f"session: {session_id}")
            print(f"sandbox: {sandbox_id}")
            print(f"source:  {_ANCHOR_SOURCES[source]}")
            if link["session_url"]:
                print(f"app:     {link['session_url']}")

        emit(
            {
                "session_id": session_id,
                "sandbox_id": sandbox_id,
                "anchor_source": source,
                **link,
            },
            _human,
        )

    def use(self, session_id: str, context: bool = False) -> None:
        """Point remote commands at an existing session id.

        Pass --context to also print the context bundle (usage notes +
        current skills index).
        """
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        if not _run(client.session_exists, sandbox_id, session_id):
            fail(f"session {session_id} not found in sandbox {sandbox_id}")
        update_state(client.state_key, sandbox_id=sandbox_id, session_id=session_id)
        bundle = agentcontext.build(client, sandbox_id, session_id) if context else None

        def _human() -> None:
            print(f"Anchor session set to {session_id}")
            if bundle:
                print()
                agentcontext.render(bundle)

        payload: Dict[str, Any] = {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
        }
        if bundle:
            payload["context"] = bundle
        emit(payload, _human)

    def list(self, page_size: int = 20) -> None:
        """List recent sessions in the sandbox."""
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        resp = _run(
            client.request,
            "GET",
            f"/api/v1/sandbox/{sandbox_id}/proxy/sessions/list",
            params={"pageSize": page_size},
            timeout=60,
        )
        if resp.status_code != 200:
            fail(f"sessions list failed ({resp.status_code})")
        sessions = resp.json().get("sessions") or []
        current, _, _ = client.anchor()

        def _human() -> None:
            for entry in sessions:
                sid = entry.get("id") or entry.get("sessionId")
                title = (entry.get("title") or "").strip() or "(untitled)"
                print(f"{sid}  {title}{' *' if sid == current else ''}")

        emit({"current": current, "sessions": sessions}, _human)


class SandboxCommands:
    """Sandbox lifecycle, as seen from outside."""

    def status(self) -> None:
        """Show your sandboxes and their states."""
        client = _client_or_exit()
        sandboxes = _run(client.list_sandboxes)
        rows = []
        for sb in sandboxes:
            sid = sb.get("sandbox_id")
            resp = _run(
                client.request,
                "GET",
                f"/api/v1/sandbox/{sid}/status",
                wake_on_503=False,
                timeout=30,
            )
            rows.append(
                {
                    "sandbox_id": sid,
                    "state": resp.json().get("state", "unknown")
                    if resp.status_code == 200
                    else "unknown",
                    "is_active": bool(sb.get("is_active")),
                    "workspaces": [
                        w.get("name") or w.get("workspace_id", "?")
                        for w in (sb.get("accessible_workspaces") or [])
                    ],
                }
            )

        def _human() -> None:
            if not rows:
                print("No sandbox yet — one is created on first use.")
                return
            for row in rows:
                active = " (active)" if row["is_active"] else ""
                names = ", ".join(row["workspaces"]) or "-"
                print(f"{row['sandbox_id']}  {row['state']}{active}  workspaces: {names}")

        emit({"sandboxes": rows}, _human)

    def wake(self) -> None:
        """Wake the active sandbox (resume if paused) and wait until ready."""
        client = _client_or_exit()
        sandbox_id = _run(client.ensure_sandbox)
        emit(
            {"sandbox_id": sandbox_id, "state": "ready"},
            lambda: print(f"Sandbox {sandbox_id} is ready."),
        )
