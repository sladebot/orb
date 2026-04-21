"""Regression tests for the daemon + session filesystem layout.

Invariants that must hold:
  1. Daemon always anchors at ``~/.orb/daemon/`` (never ``/tmp/orb-daemon-*``).
  2. Session internal state (snapshots, traces, dashboard) lives under
     ``~/.orb/daemon/sessions/{session_id}/`` — NOT under the user's workdir.
  3. Session.workdir is still available to the sandbox for agent file ops.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orb.cli import paths as orb_paths
from orb.cli import main as main_mod
from orb.runtime import manager as manager_mod
from orb.runtime.manager import RuntimeManager


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ~/.orb so we don't clobber the real user's state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    # Some modules cached DAEMON_STATE_FILE at import time — patch it too.
    monkeypatch.setattr(main_mod, "DAEMON_STATE_FILE", str(home / ".orb" / "daemon" / "daemon.json"))
    yield home


def test_daemon_home_is_under_dot_orb(fake_home):
    assert orb_paths.daemon_home() == fake_home / ".orb" / "daemon"


def test_daemon_state_file_lives_under_daemon_home(fake_home):
    assert orb_paths.daemon_state_file().parent == orb_paths.daemon_home()


def test_daemon_registry_file_lives_under_daemon_home(fake_home):
    assert orb_paths.daemon_registry_file() == orb_paths.daemon_home() / "registry.json"


def test_session_state_dir_is_keyed_by_session_id(fake_home):
    assert orb_paths.session_state_dir("abc-123") == (
        orb_paths.daemon_home() / "sessions" / "abc-123"
    )


def test_resolve_daemon_workdir_always_returns_daemon_home(fake_home):
    """The legacy /tmp/orb-daemon-* fallback must be gone."""
    resolved = main_mod._resolve_daemon_workdir(None)  # noqa: SLF001
    assert resolved == orb_paths.daemon_home()
    # Guard against the specific pre-refactor artifact (tempfile.mkdtemp
    # with prefix "orb-daemon-"), not all /tmp/-rooted paths — CI runners
    # root their tmp at /tmp so a plain "/tmp/" check fails there.
    assert "orb-daemon-" not in str(resolved)


def test_resolve_daemon_workdir_ignores_user_workdir(fake_home, tmp_path):
    """--workdir no longer relocates the daemon anchor; session.workdir is
    the only knob users have, and it's per-session not daemon-wide.
    """
    resolved = main_mod._resolve_daemon_workdir(str(tmp_path / "somewhere"))  # noqa: SLF001
    assert resolved == orb_paths.daemon_home()


def test_manager_registry_path_is_under_daemon_home(fake_home):
    assert manager_mod._registry_path() == orb_paths.daemon_registry_file()  # noqa: SLF001


def test_session_state_does_not_pollute_user_workdir(fake_home, tmp_path):
    """A session with workdir=/repo must NOT write its internals under /repo/.orb."""
    manager = RuntimeManager()
    user_workdir = tmp_path / "user_repo"
    user_workdir.mkdir()

    session = manager.create_session(
        workdir=str(user_workdir),
        session_path=orb_paths.session_state_dir("pinned-sid") / "snapshot.json",
    )

    state_dir = session._workspace_state_dir()  # noqa: SLF001
    assert orb_paths.daemon_home() in state_dir.parents, (
        f"session internal state escaped the daemon home: {state_dir}"
    )
    assert user_workdir not in state_dir.parents, (
        f"session polluted the user's workdir at {user_workdir}: {state_dir}"
    )


def test_session_workdir_still_reachable_for_sandbox(fake_home, tmp_path):
    """Even though internals moved, the session must still report the
    user's workdir so the sandbox and agent file ops use the right root.
    """
    manager = RuntimeManager()
    user_workdir = tmp_path / "user_repo"
    user_workdir.mkdir()
    session = manager.create_session(
        workdir=str(user_workdir),
        session_path=orb_paths.session_state_dir("wd-sid") / "snapshot.json",
    )
    assert session._conversation_session.workdir == str(user_workdir)  # noqa: SLF001
