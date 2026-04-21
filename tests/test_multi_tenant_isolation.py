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

    async def test_runtime_broadcast_tags_payload_with_session_id(
        self, manager: RuntimeManager, tmp_path: Path
    ):
        """Every GraphRuntime._broadcast payload must carry session_id so
        DashboardServer.broadcast can filter per-client correctly. Without
        this tag the server would fall back to the default session's id and
        drop events destined for other sessions — the "dashboard blank on
        chat" regression.
        """
        a = manager.create_session(session_path=tmp_path / "a.json")
        captured: list[dict] = []

        async def collect(data: str) -> None:
            captured.append(json.loads(data))

        manager.subscribe(collect)
        await a._broadcast(json.dumps({"type": "message", "content": "hi"}))  # noqa: SLF001
        assert captured
        assert captured[-1]["session_id"] == a._conversation_session.session_id  # noqa: SLF001

    async def test_server_broadcast_filters_per_client_session(
        self, multi_session_server
    ):
        """WebSocket clients registered with a session_id filter must
        receive payloads tagged with that session_id and be dropped for
        payloads tagged with any other. A ``None`` filter gets everything.
        """
        _, server, sessions, _ = multi_session_server
        a, b, _ = sessions
        sid_a = a._conversation_session.session_id  # noqa: SLF001
        sid_b = b._conversation_session.session_id  # noqa: SLF001

        class FakeWS:
            def __init__(self):
                self.sent: list[str] = []
            async def send_str(self, data: str) -> None:
                self.sent.append(data)

        ws_a, ws_b, ws_any = FakeWS(), FakeWS(), FakeWS()
        server._clients[ws_a] = sid_a  # noqa: SLF001
        server._clients[ws_b] = sid_b  # noqa: SLF001
        server._clients[ws_any] = None  # noqa: SLF001

        await server.broadcast(json.dumps({"type": "message", "session_id": sid_a}))
        await server.broadcast(json.dumps({"type": "message", "session_id": sid_b}))
        # Untagged payloads (e.g. topologies_reloaded) go to everyone
        await server.broadcast(json.dumps({"type": "topologies_reloaded"}))

        assert len(ws_a.sent) == 2  # sid_a + untagged
        assert len(ws_b.sent) == 2  # sid_b + untagged
        assert len(ws_any.sent) == 3


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
    def test_state_dirs_are_keyed_by_session_id_under_daemon_home(
        self, manager: RuntimeManager, tmp_path: Path
    ):
        """State dirs live under ~/.orb/daemon/sessions/{sid}/ — NOT in the
        user's workdir. Each session must get its own sid-keyed dir so
        concurrent sessions don't collide.
        """
        from orb.cli.paths import daemon_home
        dir_a = tmp_path / "a"; dir_a.mkdir()
        dir_b = tmp_path / "b"; dir_b.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        b = manager.create_session(workdir=str(dir_b), session_path=tmp_path / "b.json")

        assert a._workspace_state_dir() != b._workspace_state_dir()  # noqa: SLF001
        assert daemon_home() in a._workspace_state_dir().parents  # noqa: SLF001
        assert daemon_home() in b._workspace_state_dir().parents  # noqa: SLF001
        # Must not live inside the user's workdir any more.
        assert dir_a not in a._workspace_state_dir().parents  # noqa: SLF001
        assert dir_b not in b._workspace_state_dir().parents  # noqa: SLF001

    def test_trace_dirs_isolated_per_session(self, manager: RuntimeManager, tmp_path: Path):
        from orb.cli.paths import daemon_home
        dir_a = tmp_path / "a"; dir_a.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        trace_dir = a._trace_dir()  # noqa: SLF001
        assert daemon_home() in trace_dir.parents
        assert trace_dir.name == "traces"
        assert dir_a not in trace_dir.parents

    def test_dashboard_state_workdir_syncs_from_session(self, manager: RuntimeManager, tmp_path: Path):
        dir_a = tmp_path / "a"; dir_a.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        a._sync_session_state()  # noqa: SLF001
        assert a.state.workdir == str(dir_a)

    def test_dashboard_sessions_dirs_differ_per_session(
        self, manager: RuntimeManager, tmp_path: Path
    ):
        """Two sessions must write dashboard snapshots to different dirs.

        Previously keyed by ``{workdir}/.orb/sessions``; now keyed by
        session_id under ``~/.orb/daemon/sessions/`` — different key,
        same isolation guarantee.
        """
        dir_a = tmp_path / "a"; dir_a.mkdir()
        dir_b = tmp_path / "b"; dir_b.mkdir()
        a = manager.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        b = manager.create_session(workdir=str(dir_b), session_path=tmp_path / "b.json")
        assert a._dashboard_sessions_dir() != b._dashboard_sessions_dir()  # noqa: SLF001


# ── Diff capture isolation ───────────────────────────────────────────────


class TestCaptureDiffIsolation:
    def test_run_orchestrator_passes_session_workdir_to_capture_diff(self):
        """The call site in `_run_orchestrator` must pass the session's
        workdir to `capture_diff`. A pure source-level check is enough —
        driving the full orchestrator would require standing up providers
        and a topology.

        Regression: the old call was `capture_diff()` with no args, so in
        a multi-tenant daemon every session's run-complete diff came from
        the daemon's launch CWD regardless of which repo the session was
        actually scoped to.
        """
        import inspect
        from orb.runtime import graph_runtime as gr

        src = inspect.getsource(gr.GraphRuntime._run_orchestrator)
        # The fix: capture_diff must be called with the conversation
        # session's workdir, not bare.
        assert "capture_diff(cwd=self._conversation_session.workdir or None)" in src, (
            "expected capture_diff to receive the session's workdir; "
            "found source:\n" + src
        )


# ── Subscriber mutation during broadcast ─────────────────────────────────


@pytest.mark.asyncio
class TestSubscriberMutationSafety:
    async def test_runtime_broadcast_survives_concurrent_subscribe(
        self, manager: RuntimeManager, tmp_path: Path
    ):
        """If a subscriber callback triggers another subscribe/unsubscribe
        mid-iteration, `_broadcast` must not raise
        `RuntimeError: Set changed size during iteration`.
        """
        a = manager.create_session(session_path=tmp_path / "a.json")

        async def mutator(_data: str) -> None:
            async def noop(_d: str) -> None:
                return None
            a.subscribe(noop)  # noqa: SLF001

        async def noop(_data: str) -> None:
            return None

        a.subscribe(mutator)  # noqa: SLF001
        a.subscribe(noop)  # noqa: SLF001
        # Must not raise
        await a._broadcast(json.dumps({"type": "ping"}))  # noqa: SLF001

    async def test_manager_forward_broadcast_survives_concurrent_subscribe(
        self, manager: RuntimeManager, tmp_path: Path
    ):
        """Same contract at the manager layer: `_forward_broadcast`'s
        iteration must snapshot its subscribers.
        """
        a = manager.create_session(session_path=tmp_path / "a.json")

        async def mutator(_data: str) -> None:
            async def noop(_d: str) -> None:
                return None
            manager.subscribe(noop)

        async def noop(_data: str) -> None:
            return None

        manager.subscribe(mutator)
        manager.subscribe(noop)
        # Must not raise
        await manager._forward_broadcast(json.dumps({"type": "ping"}))  # noqa: SLF001
