"""Filesystem layout constants for Orb.

Single source of truth for where daemon state, session snapshots, traces,
and logs live. Previously these were scattered: daemon CWD defaulted to
``/tmp/orb-daemon-*``, registries lived in the daemon's CWD, and session
internals polluted the user's workdir at ``{workdir}/.orb/``.

The layout now:

::

    ~/.orb/
    ├── config.json           — user config
    ├── run.log               — daemon log
    └── daemon/               — persistent daemon anchor (never /tmp)
        ├── daemon.json       — pid/host/port
        ├── registry.json     — session index
        └── sessions/
            └── {session_id}/
                ├── snapshot.json
                ├── dashboard.json
                └── traces/{run_id}.json

A session's ``workdir`` is **only** the sandbox root for agent file ops.
Orb never writes its own state into that directory.
"""
from __future__ import annotations

from pathlib import Path


def orb_home() -> Path:
    """Root Orb directory. Resolved lazily so tests can monkey-patch Path.home()."""
    return Path.home() / ".orb"


def daemon_home() -> Path:
    """Persistent anchor for the long-running daemon process."""
    return orb_home() / "daemon"


def daemon_state_file() -> Path:
    """Pid / host / port of the currently running daemon."""
    return daemon_home() / "daemon.json"


def daemon_registry_file() -> Path:
    """Session-id index that survives daemon restarts."""
    return daemon_home() / "registry.json"


def daemon_sessions_dir() -> Path:
    """Per-session Orb state (snapshots, dashboard, traces)."""
    return daemon_home() / "sessions"


def session_state_dir(session_id: str) -> Path:
    """Internal state dir for a single session — not the user's workdir."""
    return daemon_sessions_dir() / session_id


def ensure_daemon_home() -> Path:
    """Create ``~/.orb/daemon/`` (and parents) if missing. Returns the path."""
    home = daemon_home()
    home.mkdir(parents=True, exist_ok=True)
    return home
