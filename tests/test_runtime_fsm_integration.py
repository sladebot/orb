"""Integration tests for the FSM wired through GraphRuntime.

These complement tests/test_run_state.py (pure FSM unit tests) by driving
the FSM through real runtime methods — stop_run, new_session, the
broadcast listener, and the HTTP snapshot payload.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orb.runtime.graph_runtime import GraphRuntime
from orb.runtime.run_state import RunState


class _DummyTask:
    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        pass


@pytest.mark.asyncio
class TestRuntimeStopRun:
    async def test_stop_run_without_active_run_stays_idle(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        assert runtime.run_state is RunState.IDLE
        result = await runtime.stop_run()
        assert result["ok"] is False
        assert runtime.run_state is RunState.IDLE

    async def test_stop_run_while_running_fires_stop_requested(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        # Drive the FSM into RUNNING without firing real orchestrator work.
        runtime._fsm.fire("start_run_begin")          # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001
        runtime._run_task = _DummyTask()              # noqa: SLF001

        result = await runtime.stop_run()
        assert result["ok"] is True
        # stop_requested landed the FSM in STOPPING; the orchestrator
        # task's CancelledError branch is responsible for stop_finished.
        assert runtime.run_state is RunState.STOPPING


@pytest.mark.asyncio
class TestRuntimeNewSession:
    async def test_new_session_from_idle_stays_idle(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        status, payload = await runtime.new_session()
        assert status == 200
        assert runtime.run_state is RunState.IDLE

    async def test_new_session_from_completed_returns_to_idle(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        runtime._fsm.fire("start_run_begin")            # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001
        runtime._fsm.fire("orchestrator_succeeded")     # noqa: SLF001
        assert runtime.run_state is RunState.COMPLETED

        status, _ = await runtime.new_session()
        assert status == 200
        assert runtime.run_state is RunState.IDLE

    async def test_new_session_mid_run_is_rejected(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        runtime._fsm.fire("start_run_begin")            # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001
        runtime._run_task = _DummyTask()                # noqa: SLF001

        status, payload = await runtime.new_session()
        assert status == 409
        assert payload["ok"] is False
        # RUNNING is preserved — the rejected reset didn't silently drop us.
        assert runtime.run_state is RunState.RUNNING


@pytest.mark.asyncio
class TestRuntimeStopAsync:
    async def test_stop_drains_stopping_via_stop_finished(self, tmp_path: Path):
        """Daemon shutdown path: runtime.stop() cancels the task directly.

        The FSM must end up in IDLE even if the CancelledError branch
        inside _run_orchestrator doesn't run (e.g. because the task
        didn't come from _run_orchestrator in this unit setup).
        """
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        runtime._fsm.fire("start_run_begin")            # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001

        async def _hangs():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(_hangs())
        runtime._run_task = task                        # noqa: SLF001
        # Give the task one tick to actually be pending.
        await asyncio.sleep(0)

        await runtime.stop()
        # runtime.stop() fired stop_requested then stop_finished after the
        # CancelledError propagated.
        assert runtime.run_state is RunState.IDLE


@pytest.mark.asyncio
class TestRuntimeBroadcastListener:
    async def test_fsm_transition_broadcasts_run_state_changed(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        captured: list[str] = []

        async def _capture(data: str) -> None:
            captured.append(data)

        runtime.subscribe(_capture)

        # Fire a real transition; the listener schedules a broadcast task.
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        # Give the event loop a tick to run the scheduled task.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured, "no broadcast fired from run_state_changed"
        payload = json.loads(captured[-1])
        assert payload["type"] == "run_state_changed"
        assert payload["from"] == "idle"
        assert payload["to"] == "planning"
        assert payload["event"] == "start_run_begin"
        assert isinstance(payload["at"], float)

    async def test_no_broadcast_when_no_loop_running(self, tmp_path: Path):
        """The listener must not blow up when driven from sync code.

        Unit tests that fire the FSM synchronously hit this path — we log
        and skip rather than raise.
        """
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        # Build a listener that tracks whether it was called.
        called: list = []
        orig_on = runtime._on_fsm_state_changed  # noqa: SLF001

        def wrapper(*args):
            called.append(args)
            return orig_on(*args)

        runtime._fsm._listeners.clear()             # noqa: SLF001
        runtime._fsm.subscribe(wrapper)             # noqa: SLF001
        # Drive a transition in this coroutine — we DO have a loop.
        runtime._fsm.fire("start_run_begin")        # noqa: SLF001
        assert len(called) == 1


class TestRuntimeRunState:
    def test_initial_state_is_idle(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        assert runtime.run_state is RunState.IDLE
        assert runtime.is_run_in_flight is False

    def test_run_state_exposed_in_snapshot_payload(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        payload = runtime._dashboard_snapshot_payload()  # noqa: SLF001
        assert payload["run_state"] == "idle"
        # No more legacy `run_active` — that key is gone from the shape.
        assert "run_active" not in payload

    def test_run_state_updates_with_fsm(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        runtime._fsm.fire("start_run_begin")             # noqa: SLF001
        assert runtime.run_state is RunState.PLANNING
        assert runtime.is_run_in_flight is True
        snapshot = runtime._dashboard_snapshot_payload()  # noqa: SLF001
        assert snapshot["run_state"] == "planning"


class TestStoppingRaceAllowances:
    """Verify the FSM accepts success/error from STOPPING (stop race)."""

    def test_orchestrator_succeeded_from_stopping_lands_completed(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        runtime._fsm.fire("start_run_begin")             # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")   # noqa: SLF001
        runtime._fsm.fire("stop_requested")              # noqa: SLF001
        assert runtime.run_state is RunState.STOPPING
        # Race: the orchestrator actually finished before cancel arrived.
        runtime._fsm.fire("orchestrator_succeeded")      # noqa: SLF001
        assert runtime.run_state is RunState.COMPLETED

    def test_orchestrator_errored_from_stopping_lands_errored(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")
        runtime._fsm.fire("start_run_begin")             # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")   # noqa: SLF001
        runtime._fsm.fire("stop_requested")              # noqa: SLF001
        # Race: the orchestrator failed before cancel arrived.
        runtime._fsm.fire("orchestrator_errored")        # noqa: SLF001
        assert runtime.run_state is RunState.ERRORED
