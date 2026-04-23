"""Regression tests for workdir leaks.

Prior bugs:
  1. GraphRuntime._sync_session_state set state.workdir to ``Path.cwd()``
     when the session had no explicit workdir, leaking the daemon's
     launch CWD into the dashboard breadcrumb for every unscoped session.
  2. Orchestrator factory used Path.cwd() as a sandbox root fallback,
     which meant agent file ops could land in the daemon's CWD instead
     of under a session-scoped directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orb.runtime.graph_runtime import GraphRuntime


def test_sync_session_state_does_not_leak_process_cwd(tmp_path, monkeypatch):
    """A session without an explicit workdir must NOT surface Path.cwd()
    to the dashboard state. Leaving state.workdir empty is the correct
    signal "no workdir yet"; the UI can show a placeholder.
    """
    # Anchor the daemon home somewhere isolated so the GraphRuntime's
    # internal state dir doesn't pollute other tests.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    # Force a CWD that would be a noticeable leak if it slipped through.
    leaker = tmp_path / "process_cwd"
    leaker.mkdir()
    monkeypatch.chdir(leaker)

    runtime = GraphRuntime(session_path=tmp_path / "snapshot.json")
    # No session.workdir set → state.workdir must not pick up Path.cwd().
    runtime._sync_session_state()  # noqa: SLF001

    assert runtime.state.workdir != str(leaker), (
        f"state.workdir leaked process CWD: {runtime.state.workdir}"
    )
    # "" is the honest signal — dashboard can render "(daemon default)".
    assert runtime.state.workdir == ""


def test_sync_session_state_reports_explicit_workdir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    picked = tmp_path / "my_repo"
    picked.mkdir()
    runtime = GraphRuntime(session_path=tmp_path / "snapshot.json")
    runtime._conversation_session.workdir = str(picked)  # noqa: SLF001

    runtime._sync_session_state()  # noqa: SLF001
    assert runtime.state.workdir == str(picked)


def test_current_init_event_carries_session_workdir(tmp_path, monkeypatch):
    """The init payload the frontend reads from /api/v1/sessions/{sid}/state
    must reflect the session's workdir — both at top level and in the
    nested ``session`` block.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    picked = tmp_path / "user_repo"
    picked.mkdir()
    runtime = GraphRuntime(session_path=tmp_path / "snapshot.json")
    runtime._conversation_session.workdir = str(picked)  # noqa: SLF001

    init = runtime.current_init_event()
    assert init.get("workdir") == str(picked), init
    session_block = init.get("session") or {}
    assert session_block.get("workdir") == str(picked), session_block
