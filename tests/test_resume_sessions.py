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


def test_try_restore_requires_snapshot_file(fake_home, tmp_path):
    """A registry entry with only dashboard.json (no snapshot.json) must
    be treated as dead. Otherwise GraphRuntime loads a blank
    ConversationSession with a fresh session_id, and we'd "restore" the
    stale id into an empty runtime — silently losing prior context.
    """
    from orb.cli.paths import session_state_dir, daemon_home

    sid = "dashboard-only"
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        sid: {"workdir": str(tmp_path), "session_path": str(session_state_dir(sid) / "snapshot.json")},
    }))
    state_dir = session_state_dir(sid)
    state_dir.mkdir(parents=True, exist_ok=True)
    # Only dashboard.json — no snapshot.json.
    (state_dir / "dashboard.json").write_text(json.dumps({"type": "init"}))

    mgr = RuntimeManager()
    assert mgr.try_restore(sid) is None, (
        "try_restore hydrated a session whose ConversationSession snapshot is gone"
    )
    # Registry entry should also be pruned so the ghost doesn't resurface.
    assert sid not in json.loads(registry_path.read_text())


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
async def test_tui_attach_rejects_error_envelope(monkeypatch):
    """If the state fetch returns an error envelope (e.g. 404 after a
    session was deleted between listing and click), the TUI must not
    coerce it into an init event and must leave _session_id untouched.
    """
    from orb.cli.tui import OrbTUI

    tui = OrbTUI.__new__(OrbTUI)
    tui._server_scheme = "http"
    tui._server_host = "127.0.0.1"
    tui._server_port = 1337
    tui._session_id = "old-sid"

    class _FakeResp:
        status = 404
        ok = False
        async def json(self):
            return {"ok": False, "code": "SESSION_NOT_FOUND", "error": "gone"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeHTTP:
        def get(self, url):
            return _FakeResp()

    tui._http_session = _FakeHTTP()
    handled = []
    tui._handle_server_event = lambda data: handled.append(data)

    await tui._attach_to_session("missing-sid")

    # Must NOT have switched session or dispatched anything.
    assert tui._session_id == "old-sid", "attached to a failed session!"
    assert handled == [], f"dispatched event from error envelope: {handled}"


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
        status = 200
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


def test_try_restore_sanitizes_stale_running_state(fake_home, tmp_path):
    """If the previous daemon died mid-run, the on-disk dashboard snapshot
    will claim ``run_state: 'running'`` and have agents with status
    'running'. A freshly restored session has no orchestrator, so those
    claims are lies. try_restore must normalize them to 'idle' + 'errored'
    with a human-readable note.
    """
    from orb.cli.paths import session_state_dir, daemon_home

    sid = "crashed-sid"
    # Registry entry + a stale snapshot on disk.
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        sid: {"workdir": str(tmp_path), "session_path": str(session_state_dir(sid) / "snapshot.json")},
    }))
    state_dir = session_state_dir(sid)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "snapshot.json").write_text(json.dumps({
        "session_id": sid, "generation": 1, "agent_carryover": {}, "updated_at": 1.0,
    }))
    (state_dir / "dashboard.json").write_text(json.dumps({
        "type": "init",
        "session_id": sid,
        "run_state": "running",
        "agents": [
            {"id": "coder", "role": "Coder", "status": "running", "model": "x"},
            {"id": "tester", "role": "Tester", "status": "completed", "model": "y"},
        ],
    }))

    mgr = RuntimeManager()
    runtime = mgr.try_restore(sid)
    assert runtime is not None

    # Live FSM must be idle (no orchestrator, no running task).
    from orb.runtime.run_state import RunState
    assert runtime.run_state == RunState.IDLE

    # And the persisted snapshot must be rewritten so future loads are clean.
    snapshot = json.loads((state_dir / "dashboard.json").read_text())
    assert snapshot["run_state"] == "errored", snapshot
    # Previously-running agents should no longer claim to be running.
    statuses = {a["id"]: a["status"] for a in snapshot["agents"]}
    assert statuses["coder"] != "running", snapshot
    # Agents that had already completed must be left alone.
    assert statuses["tester"] == "completed"


def test_try_restore_leaves_clean_snapshot_alone(fake_home, tmp_path):
    """A snapshot that already says run_state='completed' must not be
    mutated by the recovery step — no false errors injected.
    """
    from orb.cli.paths import session_state_dir, daemon_home

    sid = "clean-sid"
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        sid: {"workdir": str(tmp_path), "session_path": str(session_state_dir(sid) / "snapshot.json")},
    }))
    state_dir = session_state_dir(sid)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "snapshot.json").write_text(json.dumps({
        "session_id": sid, "generation": 1, "agent_carryover": {},
    }))
    dashboard = {
        "type": "init", "session_id": sid, "run_state": "completed",
        "agents": [{"id": "coder", "role": "Coder", "status": "completed"}],
    }
    (state_dir / "dashboard.json").write_text(json.dumps(dashboard))

    mgr = RuntimeManager()
    mgr.try_restore(sid)

    snapshot = json.loads((state_dir / "dashboard.json").read_text())
    assert snapshot["run_state"] == "completed"
    assert snapshot["agents"][0]["status"] == "completed"


def test_restored_session_keeps_conversation_history(fake_home, tmp_path):
    """A session restored after a crash must retain its prior
    conversation so a new run starts with full context, not a blank slate.
    """
    from orb.cli.paths import session_state_dir, daemon_home

    sid = "history-sid"
    registry_path = daemon_home() / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        sid: {"workdir": str(tmp_path), "session_path": str(session_state_dir(sid) / "snapshot.json")},
    }))
    state_dir = session_state_dir(sid)
    state_dir.mkdir(parents=True, exist_ok=True)
    # Prior conversation: 2 turns + carryover about the repo.
    (state_dir / "snapshot.json").write_text(json.dumps({
        "session_id": sid,
        "generation": 3,
        "updated_at": 100.0,
        "agent_carryover": {"coder": [{"speaker": "coder", "content": "edited src/app.py"}]},
        "turns": [
            {"speaker": "user", "audience": "coordinator", "kind": "task", "content": "add auth"},
            {"speaker": "user", "audience": "coordinator", "kind": "task", "content": "now rate-limit it"},
        ],
        "locked_topology": "triad",
    }))

    mgr = RuntimeManager()
    runtime = mgr.try_restore(sid)
    assert runtime is not None

    sess = runtime._conversation_session  # noqa: SLF001
    assert sess.session_id == sid
    assert sess.generation == 3
    assert "coder" in sess.agent_carryover
    assert sess.locked_topology == "triad"
    assert len(sess.turns) == 2


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
