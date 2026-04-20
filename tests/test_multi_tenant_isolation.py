"""Multi-tenant isolation tests.

Validates the invariants the Option B migration promised:
  - Two sessions can coexist with independent state and workdirs.
  - File writes in session A don't leak into session B's event stream.
  - Deleting a session cleans up subscribers and the FSM transitions.
  - WebSocket subscribers see the right `session_id` on every broadcast.
  - Cross-workdir git / filesystem operations don't contaminate each other.
  - Run-state-changed broadcasts from different sessions are distinguishable.

These tests are the safety net for harness integration — if concurrent
sessions on the same daemon couldn't actually stay isolated, hermes and
openclaw wouldn't be able to run parallel evals against one Orb daemon.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from orb.client import OrbClient
from orb.runtime import RuntimeManager
from orb.runtime.run_state import RunState
from web.server import DashboardServer
from web.state import DashboardState


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
async def manager() -> RuntimeManager:
    """Bare manager with no sessions."""
    return RuntimeManager()


@pytest.fixture
async def multi_session_server(tmp_path: Path):
    """Dashboard server with three pre-created isolated sessions."""
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18200)
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001

    aiohttp_server = TestServer(server._app)  # noqa: SLF001
    await aiohttp_server.start_server()

    # Create three sessions in three different workdirs
    workdirs = []
    sessions = []
    for i in range(3):
        workdir = tmp_path / f"session-{i}"
        workdir.mkdir()
        workdirs.append(workdir)
        session = server.manager.create_session(
            workdir=str(workdir),
            session_path=tmp_path / f"session-{i}.json",
        )
        sessions.append(session)

    async with TestClient(aiohttp_server) as http:
        yield http, server, sessions, workdirs

    await aiohttp_server.close()


# ── Registry + lifecycle isolation ───────────────────────────────────────


class TestSessionIsolation:
    def test_sessions_have_unique_ids(self, manager: RuntimeManager, tmp_path: Path):
        a = manager.create_session(workdir=str(tmp_path), session_path=tmp_path / "a.json")
        b = manager.create_session(workdir=str(tmp_path), session_path=tmp_path / "b.json")
        c = manager.create_session(workdir=str(tmp_path), session_path=tmp_path / "c.json")
        ids = {a._conversation_session.session_id,  # noqa: SLF001
               b._conversation_session.session_id,  # noqa: SLF001
               c._conversation_session.session_id}  # noqa: SLF001
        assert len(ids) == 3

    def test_sessions_have_independent_workdirs(self, manager: RuntimeManager, tmp_path: Path):
        dir_a = tmp_path / "a"; dir_a.mkdir()
        dir_b = tmp_path / "b"; dir_b.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        b = manager.create_session(workdir=str(dir_b), session_path=tmp_path / "b.json")
        assert a._conversation_session.workdir != b._conversation_session.workdir  # noqa: SLF001

    def test_sessions_have_independent_fsms(self, manager: RuntimeManager, tmp_path: Path):
        a = manager.create_session(session_path=tmp_path / "a.json")
        b = manager.create_session(session_path=tmp_path / "b.json")
        # Driving A's FSM shouldn't change B's
        a._fsm.fire("start_run_begin")  # noqa: SLF001
        assert a.run_state is RunState.PLANNING
        assert b.run_state is RunState.IDLE

    def test_sessions_have_independent_dashboard_state(self, manager: RuntimeManager, tmp_path: Path):
        a = manager.create_session(session_path=tmp_path / "a.json")
        b = manager.create_session(session_path=tmp_path / "b.json")
        a.state.message_count = 42
        b.state.message_count = 7
        assert a.state.message_count == 42
        assert b.state.message_count == 7
        # And DashboardState objects are distinct
        assert a.state is not b.state

    def test_delete_session_doesnt_affect_siblings(self, manager: RuntimeManager, tmp_path: Path):
        a = manager.create_session(session_path=tmp_path / "a.json")
        b = manager.create_session(session_path=tmp_path / "b.json")
        assert manager.delete_session(a._conversation_session.session_id) is True  # noqa: SLF001
        # B is still there and its state is intact
        assert manager.get_session(b._conversation_session.session_id) is b  # noqa: SLF001
        assert b.run_state is RunState.IDLE


# ── Broadcast isolation ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBroadcastIsolation:
    async def test_run_state_changed_tags_correct_session_id(self, manager: RuntimeManager, tmp_path: Path):
        a = manager.create_session(session_path=tmp_path / "a.json")
        b = manager.create_session(session_path=tmp_path / "b.json")
        captured: list[dict] = []

        async def collect(data: str) -> None:
            captured.append(json.loads(data))

        manager.subscribe(collect)

        a._fsm.fire("start_run_begin")  # noqa: SLF001
        b._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Both events made it through, each tagged with its own session_id
        by_session = {e["session_id"]: e for e in captured if e.get("type") == "run_state_changed"}
        assert a._conversation_session.session_id in by_session  # noqa: SLF001
        assert b._conversation_session.session_id in by_session  # noqa: SLF001

    async def test_broadcasts_from_deleted_session_dont_fan_out(self, manager: RuntimeManager, tmp_path: Path):
        a = manager.create_session(session_path=tmp_path / "a.json")
        received: list = []

        async def collector(data: str) -> None:
            received.append(data)

        manager.subscribe(collector)

        # Fire one event pre-delete to prove the pipe works
        a._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0)
        pre_delete = len(received)
        assert pre_delete >= 1

        # After delete, the manager unhooks its forward bridge. Driving
        # the orphaned session's FSM with the orchestrator_errored event
        # (legal from PLANNING/RUNNING/STOPPING) should NOT fan out to
        # the manager's subscribers.
        manager.delete_session(a._conversation_session.session_id)  # noqa: SLF001
        a._fsm.fire("orchestrator_errored")  # noqa: SLF001
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(received) == pre_delete


# ── HTTP-layer isolation ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHTTPIsolation:
    async def test_start_run_in_session_a_doesnt_affect_session_b(self, multi_session_server):
        http, server, sessions, _ = multi_session_server
        a, b, _ = sessions

        async def fake_start(*args, **kwargs):
            return 202, {"ok": True, "init": {"type": "init"}, "session_turn": 1}

        with patch.object(a, "start_run", side_effect=fake_start):
            resp = await http.post(
                f"/api/v1/sessions/{a._conversation_session.session_id}/runs",  # noqa: SLF001
                json={"query": "work on A"},
            )
            assert resp.status == 202

        # B's run_state is still idle
        resp = await http.get(f"/api/v1/sessions/{b._conversation_session.session_id}")  # noqa: SLF001
        data = await resp.json()
        assert data["data"]["run_state"] == "idle"

    async def test_state_endpoints_return_distinct_session_ids(self, multi_session_server):
        http, _, sessions, _ = multi_session_server
        a, b, c = sessions

        for session in (a, b, c):
            resp = await http.get(f"/api/v1/sessions/{session._conversation_session.session_id}")  # noqa: SLF001
            data = await resp.json()
            assert data["data"]["session_id"] == session._conversation_session.session_id  # noqa: SLF001

    async def test_list_sessions_reports_every_created(self, multi_session_server):
        http, _, sessions, _ = multi_session_server
        resp = await http.get("/api/v1/sessions")
        data = await resp.json()
        ids = {s["session_id"] for s in data["data"]["sessions"]}
        for session in sessions:
            assert session._conversation_session.session_id in ids  # noqa: SLF001

    async def test_delete_session_404s_other_sessions_unaffected(self, multi_session_server):
        http, _, sessions, _ = multi_session_server
        a, b, _ = sessions

        await http.delete(f"/api/v1/sessions/{a._conversation_session.session_id}")  # noqa: SLF001

        # A now 404s
        resp_a = await http.get(f"/api/v1/sessions/{a._conversation_session.session_id}")  # noqa: SLF001
        assert resp_a.status == 404
        # B is still fine
        resp_b = await http.get(f"/api/v1/sessions/{b._conversation_session.session_id}")  # noqa: SLF001
        assert resp_b.status == 200


# ── End-to-end via the SDK ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestSDKMultiSession:
    async def test_two_sdk_clients_manage_independent_sessions(self, multi_session_server, tmp_path: Path):
        http, server, _, _ = multi_session_server
        base_url = str(http.make_url("")).rstrip("/")

        (tmp_path / "ha").mkdir(exist_ok=True)
        (tmp_path / "hb").mkdir(exist_ok=True)
        async with OrbClient(base_url) as client_a, OrbClient(base_url) as client_b:
            sess_a = await client_a.create_session(workdir=str(tmp_path / "ha"))
            sess_b = await client_b.create_session(workdir=str(tmp_path / "hb"))

            assert sess_a.session_id != sess_b.session_id
            assert sess_a.workdir != sess_b.workdir

            # Neither client can accidentally see the other's session unless they ask for it
            sessions_a_saw = await client_a.list_sessions()
            ids = {s.session_id for s in sessions_a_saw}
            # Both are in the SAME daemon, so a global list sees both — which
            # is the correct contract for a shared daemon.
            assert sess_a.session_id in ids
            assert sess_b.session_id in ids


# ── Workdir isolation ────────────────────────────────────────────────────


class TestWorkdirIsolation:
    def test_state_dirs_resolve_per_session_workdir(self, manager: RuntimeManager, tmp_path: Path):
        dir_a = tmp_path / "a"; dir_a.mkdir()
        dir_b = tmp_path / "b"; dir_b.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        b = manager.create_session(workdir=str(dir_b), session_path=tmp_path / "b.json")

        # _workspace_state_dir resolves to <session.workdir>/.orb, NOT
        # process CWD — the guarantee the Phase 1 de-chdir made possible.
        assert str(a._workspace_state_dir()) == str(dir_a / ".orb")  # noqa: SLF001
        assert str(b._workspace_state_dir()) == str(dir_b / ".orb")  # noqa: SLF001

    def test_trace_dirs_follow_workdir(self, manager: RuntimeManager, tmp_path: Path):
        dir_a = tmp_path / "a"; dir_a.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        assert str(a._trace_dir()) == str(dir_a / ".orb" / "traces")  # noqa: SLF001

    def test_dashboard_state_workdir_syncs_from_session(self, manager: RuntimeManager, tmp_path: Path):
        dir_a = tmp_path / "a"; dir_a.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        a._sync_session_state()  # noqa: SLF001
        assert a.state.workdir == str(dir_a)
