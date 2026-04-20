"""Integration tests for the Python SDK against the real v1 API.

Spins up a `DashboardServer` in-process and drives it through the
`orb.client` SDK end-to-end. These tests also double as the "harness
integration smoke test" — if these pass, an external harness using the
same SDK talks to the daemon correctly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from orb.client import OrbClient, OrbSession
from orb.client.client import OrbAPIError
from orb.client.types import SessionSummary
from web.server import DashboardServer
from web.state import DashboardState


@pytest.fixture
async def live_client(tmp_path: Path):
    """A `DashboardServer` reachable through the SDK's OrbClient.

    We point httpx at the aiohttp test server's socket by replacing the
    SDK's underlying AsyncClient with one connected to the TestServer's
    URL. That way every SDK method exercises the real wire format.
    """
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18150)
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001
    aiohttp_server = TestServer(server._app)  # noqa: SLF001
    await aiohttp_server.start_server()
    async with TestClient(aiohttp_server) as http:
        base_url = str(http.make_url("")).rstrip("/")
        client = OrbClient(base_url)
        try:
            yield client, server
        finally:
            await client.close()
    await aiohttp_server.close()


@pytest.mark.asyncio
class TestClientLifecycle:
    async def test_health_returns_counts(self, live_client):
        client, _ = live_client
        data = await client.health()
        assert "active_sessions" in data

    async def test_list_sessions_returns_typed_summaries(self, live_client):
        client, _ = live_client
        sessions = await client.list_sessions()
        assert len(sessions) >= 1
        assert all(isinstance(s, SessionSummary) for s in sessions)

    async def test_create_session_returns_orb_session(self, live_client, tmp_path: Path):
        client, _ = live_client
        workdir = str(tmp_path)
        session = await client.create_session(workdir=workdir)
        assert isinstance(session, OrbSession)
        assert session.workdir == workdir
        assert session.run_state == "idle"

    async def test_get_session_round_trips(self, live_client, tmp_path: Path):
        client, _ = live_client
        created = await client.create_session(workdir=str(tmp_path))
        fetched = await client.get_session(created.session_id)
        assert fetched.session_id == created.session_id
        assert fetched.workdir == created.workdir

    async def test_get_missing_session_raises(self, live_client):
        client, _ = live_client
        with pytest.raises(OrbAPIError) as exc:
            await client.get_session("no-such-session")
        assert exc.value.code == "SESSION_NOT_FOUND"
        assert exc.value.status == 404

    async def test_delete_session_removes_it(self, live_client, tmp_path: Path):
        client, server = live_client
        session = await client.create_session(workdir=str(tmp_path))
        session_id = session.session_id
        await session.delete()
        assert server.manager.get_session(session_id) is None


@pytest.mark.asyncio
class TestRunControl:
    async def test_start_run_returns_typed_summary(self, live_client, tmp_path: Path):
        client, server = live_client
        session = await client.create_session(workdir=str(tmp_path))
        runtime = server.manager.get_session(session.session_id)

        async def fake_start(*args, **kwargs):
            return 202, {"ok": True, "init": {"type": "init"}, "session_turn": 1}

        with patch.object(runtime, "start_run", side_effect=fake_start):
            run = await session.start_run(query="hello", topology="auto")
        assert run.session_id == session.session_id
        assert run.session_turn == 1

    async def test_empty_query_raises(self, live_client, tmp_path: Path):
        client, _ = live_client
        session = await client.create_session(workdir=str(tmp_path))
        with pytest.raises(OrbAPIError) as exc:
            await session.start_run(query="")
        assert exc.value.code == "QUERY_EMPTY"

    async def test_stop_when_idle_raises_no_run_in_flight(self, live_client, tmp_path: Path):
        client, _ = live_client
        session = await client.create_session(workdir=str(tmp_path))
        with pytest.raises(OrbAPIError) as exc:
            await session.stop_run()
        assert exc.value.code == "NO_RUN_IN_FLIGHT"

    async def test_inject_requires_message(self, live_client, tmp_path: Path):
        client, _ = live_client
        session = await client.create_session(workdir=str(tmp_path))
        with pytest.raises(OrbAPIError) as exc:
            await session.inject(target="coordinator", message="")
        assert exc.value.code == "MESSAGE_EMPTY"


@pytest.mark.asyncio
class TestSessionState:
    async def test_state_returns_init_dict(self, live_client, tmp_path: Path):
        client, _ = live_client
        session = await client.create_session(workdir=str(tmp_path))
        state = await session.state()
        assert state["type"] == "init"
        assert "run_state" in state

    async def test_refresh_updates_the_summary(self, live_client, tmp_path: Path):
        client, server = live_client
        session = await client.create_session(workdir=str(tmp_path))
        # Mutate via server to simulate state drift
        runtime = server.manager.get_session(session.session_id)
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        summary = await session.refresh()
        assert summary.run_state == "planning"
