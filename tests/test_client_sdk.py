"""Integration tests for the Python SDK against the real v1 API.

Spins up a `DashboardServer` in-process and drives it through the
`orb.client` SDK end-to-end. These tests also double as the "harness
integration smoke test" — if these pass, an external harness using the
same SDK talks to the daemon correctly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from orb.client import OrbClient, OrbSession
from orb.client.client import OrbAPIError
from orb.client.types import Event, SessionSummary
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

    async def test_start_run_can_request_eval_mode(self, live_client, tmp_path: Path):
        client, server = live_client
        session = await client.create_session(workdir=str(tmp_path))
        runtime = server.manager.get_session(session.session_id)

        async def fake_start(*args, **kwargs):
            return 202, {"ok": True, "init": {"type": "init"}, "session_turn": 1}

        with patch.object(runtime, "start_run", side_effect=fake_start) as start_run:
            await session.start_run(query="hello", topology="auto", eval_mode=True)
        assert start_run.call_args.kwargs["eval_mode"] is True

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


class TestEventIsTerminal:
    """`Event.is_terminal` must not flag the resting `idle` state as terminal.

    Otherwise ``wait_for_terminal`` returns on a stale pre-run idle event
    instead of the actual completion/error event.
    """

    def test_idle_is_not_terminal(self):
        ev = Event.from_payload({
            "type": "run_state_changed",
            "session_id": "s",
            "to": "idle",
            "from": "stopping",
            "event": "stop_finished",
        })
        assert ev.is_terminal is False

    def test_completed_is_terminal(self):
        ev = Event.from_payload({
            "type": "run_state_changed",
            "session_id": "s",
            "to": "completed",
            "from": "running",
            "event": "orchestrator_succeeded",
        })
        assert ev.is_terminal is True

    def test_errored_is_terminal(self):
        ev = Event.from_payload({
            "type": "run_state_changed",
            "session_id": "s",
            "to": "errored",
            "from": "running",
            "event": "orchestrator_errored",
        })
        assert ev.is_terminal is True

    def test_non_run_state_event_is_not_terminal(self):
        ev = Event.from_payload({"type": "message", "session_id": "s"})
        assert ev.is_terminal is False


@pytest.mark.asyncio
class TestWaitForTerminal:
    """`wait_for_terminal` must block through a fresh idle → running →
    completed cycle and only return on the true terminal event.
    """

    async def test_blocks_past_initial_idle_until_completed(self, tmp_path: Path):
        """Simulate an event stream: idle → running → completed.

        The stale ``idle`` must not unblock the waiter — only ``completed``
        should.
        """

        async def fake_stream(self):  # noqa: ARG001 — method stub
            yield Event.from_payload({
                "type": "run_state_changed",
                "session_id": "s",
                "from": "stopping",
                "to": "idle",
                "event": "stop_finished",
            })
            yield Event.from_payload({
                "type": "run_state_changed",
                "session_id": "s",
                "from": "planning",
                "to": "running",
                "event": "orchestrator_task_created",
            })
            yield Event.from_payload({
                "type": "run_state_changed",
                "session_id": "s",
                "from": "running",
                "to": "completed",
                "event": "orchestrator_succeeded",
            })

        session = OrbSession.__new__(OrbSession)
        session._client = None  # noqa: SLF001
        session._summary = SessionSummary(  # noqa: SLF001
            session_id="s",
            generation=1,
            workdir=str(tmp_path),
            run_state="running",
            turn=0,
        )
        with patch.object(OrbSession, "stream_events", fake_stream):
            event = await session.wait_for_terminal()
        assert event.to == "completed"

    async def test_returns_immediately_when_already_idle(self, tmp_path: Path):
        """If the session is at rest (idle/completed/errored) when the
        caller waits, return at once without consuming the stream.
        """
        session = OrbSession.__new__(OrbSession)
        session._client = None  # noqa: SLF001
        session._summary = SessionSummary(  # noqa: SLF001
            session_id="s",
            generation=1,
            workdir=str(tmp_path),
            run_state="idle",
            turn=0,
        )

        async def fake_stream(self):  # noqa: ARG001
            raise AssertionError("stream_events must not be consumed when already at rest")
            yield  # pragma: no cover

        with patch.object(OrbSession, "stream_events", fake_stream):
            event = await session.wait_for_terminal()
        assert event.to == "idle"


@pytest.mark.asyncio
class TestRequestNullDataHandling:
    """`OrbClient._request` must not coerce an explicit ``null`` data
    payload into ``{}``. Endpoints that contract to return data should
    raise; endpoints that return nothing (delete/stop) tolerate ``None``.
    """

    async def test_create_session_with_null_data_raises(self, tmp_path: Path):
        """A malformed envelope (`{ok: true, data: null}`) from
        ``create_session`` must raise rather than produce a summary with
        an empty ``session_id`` (which would 404 every subsequent call).
        """
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "data": None})

        client = OrbClient("http://localhost:1337")
        client._http = httpx.AsyncClient(  # noqa: SLF001
            transport=httpx.MockTransport(handler),
            base_url="http://localhost:1337",
        )
        try:
            with pytest.raises(OrbAPIError) as exc:
                await client.create_session(workdir=str(tmp_path))
            assert exc.value.code == "EMPTY_DATA"
        finally:
            await client.close()

    async def test_delete_session_with_null_data_succeeds(self):
        """``delete_session`` legitimately returns nothing; a null-data
        envelope must be treated as a success, not an error.
        """
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "data": None})

        client = OrbClient("http://localhost:1337")
        client._http = httpx.AsyncClient(  # noqa: SLF001
            transport=httpx.MockTransport(handler),
            base_url="http://localhost:1337",
        )
        try:
            await client.delete_session("s")  # must not raise
        finally:
            await client.close()
