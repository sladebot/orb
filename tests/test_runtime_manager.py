"""Tests for the multi-tenant RuntimeManager.

Covers session registry semantics, broadcast fan-out, and isolation
between concurrent sessions. Full end-to-end run coverage lives in
test_runtime_session.py and test_runtime_fsm_integration.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orb.runtime import RuntimeManager
from orb.runtime.graph_runtime import GraphRuntime
from orb.runtime.run_state import RunState


class TestRegistry:
    def test_manager_starts_with_no_sessions(self):
        mgr = RuntimeManager()
        assert mgr.list_sessions() == []
        assert mgr.active_session_count() == 0

    def test_create_session_returns_graph_runtime(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        assert isinstance(session, GraphRuntime)
        assert session in mgr.list_sessions()

    def test_create_session_scopes_workdir(self, tmp_path: Path):
        mgr = RuntimeManager()
        workdir = str(tmp_path)
        session = mgr.create_session(workdir=workdir, session_path=tmp_path / "a.json")
        assert session._conversation_session.workdir == workdir  # noqa: SLF001

    def test_multiple_sessions_are_independent(self, tmp_path: Path):
        mgr = RuntimeManager()
        a = mgr.create_session(workdir=str(tmp_path / "a"), session_path=tmp_path / "a.json")
        b = mgr.create_session(workdir=str(tmp_path / "b"), session_path=tmp_path / "b.json")
        assert a is not b
        assert a._conversation_session.session_id != b._conversation_session.session_id  # noqa: SLF001
        assert mgr.get_session(a._conversation_session.session_id) is a  # noqa: SLF001
        assert mgr.get_session(b._conversation_session.session_id) is b  # noqa: SLF001

    def test_get_session_missing_returns_none(self):
        mgr = RuntimeManager()
        assert mgr.get_session("does-not-exist") is None

    def test_delete_session_removes_from_registry(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        session_id = session._conversation_session.session_id  # noqa: SLF001
        assert mgr.delete_session(session_id) is True
        assert mgr.get_session(session_id) is None
        assert mgr.delete_session(session_id) is False  # idempotent

    def test_delete_session_cancels_in_flight_run(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        # Drive into RUNNING so delete has work to do
        session._fsm.fire("start_run_begin")             # noqa: SLF001
        session._fsm.fire("orchestrator_task_created")   # noqa: SLF001
        assert session.run_state is RunState.RUNNING
        deleted = mgr.delete_session(session._conversation_session.session_id)  # noqa: SLF001
        assert deleted is True
        # The FSM went through stop_requested
        assert session.run_state is RunState.STOPPING


class TestSharedConfiguration:
    def test_configure_propagates_to_existing_sessions(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        mgr.configure(providers={"mock": object()}, config=None, model_overrides=None, tier_override=None)
        assert session._providers == mgr._providers  # noqa: SLF001

    def test_configure_seeds_subsequent_sessions(self, tmp_path: Path):
        mgr = RuntimeManager()
        mgr.configure(providers={"mock": object()}, config=None, model_overrides=None, tier_override=None)
        session = mgr.create_session(session_path=tmp_path / "a.json")
        assert session._providers == mgr._providers  # noqa: SLF001


@pytest.mark.asyncio
class TestBroadcastFanout:
    async def test_session_fsm_event_reaches_manager_subscriber(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        captured: list[str] = []

        async def collector(data: str) -> None:
            captured.append(data)

        mgr.subscribe(collector)
        # Driving the FSM fires the broadcast through the session's
        # listener, which forwards through the manager to our collector.
        session._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured, "broadcast did not reach the manager subscriber"
        payload = json.loads(captured[-1])
        assert payload["type"] == "run_state_changed"
        # Every broadcast carries the originating session_id so
        # multiplexed WebSocket clients can filter locally.
        assert payload["session_id"] == session._conversation_session.session_id  # noqa: SLF001

    async def test_two_sessions_tag_events_with_different_ids(self, tmp_path: Path):
        mgr = RuntimeManager()
        a = mgr.create_session(session_path=tmp_path / "a.json")
        b = mgr.create_session(session_path=tmp_path / "b.json")
        events: list[str] = []
        mgr.subscribe(lambda data: events.append(data) or asyncio.sleep(0))

        async def collect(data: str) -> None:
            events.append(data)

        mgr._subscribers.clear()  # noqa: SLF001
        mgr.subscribe(collect)

        a._fsm.fire("start_run_begin")  # noqa: SLF001
        b._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        ids = {json.loads(e)["session_id"] for e in events}
        assert a._conversation_session.session_id in ids  # noqa: SLF001
        assert b._conversation_session.session_id in ids  # noqa: SLF001

    async def test_unsubscribe_stops_manager_notifications(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        seen: list[str] = []

        async def collector(data: str) -> None:
            seen.append(data)

        mgr.subscribe(collector)
        session._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0)
        before = len(seen)

        mgr.unsubscribe(collector)
        session._fsm.fire("orchestrator_task_created")  # noqa: SLF001
        await asyncio.sleep(0)
        # No new events hit the collector after unsubscribe.
        assert len(seen) == before
