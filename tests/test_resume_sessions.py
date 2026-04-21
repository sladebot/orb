"""Tests for session resumption — registry-backed discovery and API."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orb.runtime.manager import RuntimeManager


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_list_known_sessions_includes_active_runtime(fake_home, tmp_path):
    mgr = RuntimeManager()
    workdir = tmp_path / "repo"; workdir.mkdir()
    runtime = mgr.create_session(workdir=str(workdir))

    sessions = mgr.list_known_sessions()
    ids = {s["session_id"] for s in sessions}
    assert runtime._conversation_session.session_id in ids  # noqa: SLF001

    entry = next(s for s in sessions if s["session_id"] == runtime._conversation_session.session_id)  # noqa: SLF001
    assert entry["active"] is True
    assert entry["workdir"] == str(workdir)


def test_list_known_sessions_includes_registry_only_entries(fake_home, tmp_path):
    """Sessions from a prior daemon run (registered + snapshot on disk)
    must surface in the picker even though they aren't loaded in memory.
    """
    from orb.cli.paths import session_state_dir, daemon_home

    mgr = RuntimeManager()

    # Seed the registry as if a previous daemon created session 'prior-sid'.
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        "prior-sid": {"workdir": "/some/repo", "session_path": "dummy"},
    }))
    # And drop its snapshot on disk so list_known_sessions considers it real.
    snapshot = session_state_dir("prior-sid") / "snapshot.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({
        "session_id": "prior-sid", "generation": 1, "agent_carryover": {}, "updated_at": 42.0,
    }))

    sessions = mgr.list_known_sessions()
    prior = next((s for s in sessions if s["session_id"] == "prior-sid"), None)
    assert prior is not None
    assert prior["active"] is False
    assert prior["workdir"] == "/some/repo"


def test_list_known_sessions_skips_registry_entries_without_snapshot(fake_home, tmp_path):
    """A ghost entry (in registry but snapshot deleted) must not surface —
    clicking 'resume' on a ghost would 404 in the UI.
    """
    from orb.cli.paths import daemon_home

    mgr = RuntimeManager()
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        "ghost-sid": {"workdir": "/gone", "session_path": "gone"},
    }))

    sessions = mgr.list_known_sessions()
    ids = {s["session_id"] for s in sessions}
    assert "ghost-sid" not in ids


@pytest.mark.asyncio
async def test_api_v1_sessions_include_known_surfaces_registry_entries(fake_home, tmp_path):
    """GET /api/v1/sessions?include=known must return registry-backed sessions."""
    from aiohttp.test_utils import TestClient, TestServer
    from orb.cli.paths import session_state_dir, daemon_home
    from web.server import DashboardServer
    from web.state import DashboardState

    # Seed a registry-only session before the server boots.
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        "seeded-sid": {"workdir": str(tmp_path), "session_path": "x"},
    }))
    snapshot = session_state_dir("seeded-sid") / "snapshot.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({"session_id": "seeded-sid"}))

    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18077)
    ts = TestServer(server._app)  # noqa: SLF001
    await ts.start_server()
    async with TestClient(ts) as client:
        default_resp = await client.get("/api/v1/sessions")
        default_ids = {s["session_id"] for s in (await default_resp.json())["data"]["sessions"]}
        assert "seeded-sid" not in default_ids  # default list is active-only

        known_resp = await client.get("/api/v1/sessions", params={"include": "known"})
        known_env = await known_resp.json()
        known_ids = {s["session_id"] for s in known_env["data"]["sessions"]}
        assert "seeded-sid" in known_ids
        entry = next(s for s in known_env["data"]["sessions"] if s["session_id"] == "seeded-sid")
        assert entry["active"] is False
        assert entry["workdir"] == str(tmp_path)
    await ts.close()


@pytest.mark.asyncio
async def test_tui_attach_to_session_updates_session_id(monkeypatch):
    """Calling OrbTUI._attach_to_session must swap the attached session and
    dispatch the incoming init payload so every widget re-renders.
    """
    from unittest.mock import MagicMock, AsyncMock
    from orb.cli.tui import OrbTUI

    tui = OrbTUI.__new__(OrbTUI)
    tui._server_scheme = "http"
    tui._server_host = "127.0.0.1"
    tui._server_port = 1337
    tui._session_id = "old-sid"

    class _FakeResp:
        async def json(self):
            return {"ok": True, "data": {"type": "init", "session_id": "new-sid", "agents": []}}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeHTTP:
        def get(self, url):
            assert url.endswith("/api/v1/sessions/new-sid/state")
            return _FakeResp()

    tui._http_session = _FakeHTTP()
    handled = []
    tui._handle_server_event = lambda data: handled.append(data)

    await tui._attach_to_session("new-sid")

    assert tui._session_id == "new-sid"
    assert handled, "init event was not dispatched after attach"
    assert handled[0].get("session_id") == "new-sid"


def test_list_known_sessions_sorted_most_recent_first(fake_home, tmp_path):
    from orb.cli.paths import session_state_dir, daemon_home
    import time as _time

    mgr = RuntimeManager()
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        "old-sid": {"workdir": "/a", "session_path": "x"},
        "new-sid": {"workdir": "/b", "session_path": "y"},
    }))
    for sid, mtime in (("old-sid", 100.0), ("new-sid", 999.0)):
        snapshot = session_state_dir(sid) / "snapshot.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps({"session_id": sid}))
        import os as _os
        _os.utime(snapshot, (mtime, mtime))

    sessions = mgr.list_known_sessions()
    ids_ordered = [s["session_id"] for s in sessions if s["session_id"] in {"old-sid", "new-sid"}]
    assert ids_ordered == ["new-sid", "old-sid"]
