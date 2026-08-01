"""Shared fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_scriptit_config_dir(tmp_path, monkeypatch):
    """Point ``XDG_CONFIG_HOME`` at a per-test temp dir.

    The credential and state stores are keyed off that variable, so without
    this a developer machine that has run ``scriptit auth login`` has its real
    credentials read — and overwritten — by the store tests.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def reset_analytics(monkeypatch):
    """Unbind the analytics singleton between tests.

    It is bound once per process by whichever code path authenticated, which
    is right for a CLI that runs one command and wrong for a suite where each
    test starts from "collects nothing". Re-running ``__init__`` on the
    instance rebinds every reference at once, including the one ``main``
    imported.
    """
    from scriptit_cli.analytics import Analytics, analytics

    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    Analytics.__init__(analytics)
    yield
    Analytics.__init__(analytics)
