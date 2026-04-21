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

    async def test_list_trace_sessions_merges_across_sessions(
        self, tmp_path: Path, monkeypatch,
    ):
        """`list_trace_sessions` must aggregate across every registered
        session. Each session owns a state dir under
        ``~/.orb/daemon/sessions/{sid}/`` with a ``snapshot.json``.
        """
        import json as _json
        from orb.cli.paths import session_state_dir

        # Redirect ~/.orb/daemon/ into tmp_path so the test doesn't
        # touch the real user home (and doesn't race other tests).
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

        mgr = RuntimeManager()
        dir_a = tmp_path / "a"; dir_a.mkdir()
        dir_b = tmp_path / "b"; dir_b.mkdir()
        a = mgr.create_session(workdir=str(dir_a), session_path=tmp_path / "a.json")
        b = mgr.create_session(workdir=str(dir_b), session_path=tmp_path / "b.json")

        # Persist a minimal ConversationSession snapshot for each; the
        # snapshot is what list_trace_sessions scans.
        for sess in (a, b):
            sid = sess._conversation_session.session_id  # noqa: SLF001
            target = session_state_dir(sid) / "snapshot.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": sid,
                "generation": 1,
                "agent_carryover": {},
                "updated_at": 1.0,
            }
            target.write_text(_json.dumps(payload))

        merged = mgr.list_trace_sessions()
        ids = {s["session_id"] for s in merged["sessions"]}
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


class TestRegistryRestore:
    """After a daemon restart the in-memory session registry is empty but
    disk snapshots still exist. `try_restore` resurrects a session from
    the persisted index so a page refresh doesn't blank the dashboard.
    """

    def test_try_restore_returns_none_for_unknown_id(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = RuntimeManager()
        assert mgr.try_restore("nonexistent") is None

    def test_try_restore_resurrects_session_after_restart(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        workdir = tmp_path / "project"
        workdir.mkdir()

        # Session #1 — daemon A
        mgr_a = RuntimeManager()
        session = mgr_a.create_session(
            workdir=str(workdir), session_path=workdir / ".orb" / "sessions" / "s.json"
        )
        sid = session._conversation_session.session_id  # noqa: SLF001
        # Force a persist so the session file exists on disk (mimics what
        # any run would do naturally).
        session._persist_session()  # noqa: SLF001

        # Daemon B — fresh process, empty in-memory registry, but the
        # on-disk registry.json + snapshot survive.
        mgr_b = RuntimeManager()
        assert mgr_b.get_session(sid) is None
        restored = mgr_b.try_restore(sid)
        assert restored is not None
        assert restored._conversation_session.workdir == str(workdir)  # noqa: SLF001
        assert restored._conversation_session.session_id == sid  # noqa: SLF001
        # And it's now in the live registry
        assert mgr_b.get_session(sid) is restored

    def test_try_restore_drops_index_entry_when_snapshot_is_gone(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        workdir = tmp_path / "project"
        workdir.mkdir()
        mgr = RuntimeManager()
        session = mgr.create_session(
            workdir=str(workdir), session_path=workdir / ".orb" / "sessions" / "s.json"
        )
        sid = session._conversation_session.session_id  # noqa: SLF001
        session._persist_session()  # noqa: SLF001
        # Simulate a disk wipe: both the session file and any snapshots gone.
        (workdir / ".orb" / "sessions" / "s.json").unlink()

        mgr2 = RuntimeManager()
        assert mgr2.try_restore(sid) is None

    def test_delete_session_removes_registry_entry(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        sid = session._conversation_session.session_id  # noqa: SLF001
        mgr.delete_session(sid)

        mgr2 = RuntimeManager()
        assert mgr2.try_restore(sid) is None
