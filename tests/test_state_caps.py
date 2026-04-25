"""Caps on DashboardState collections to prevent unbounded growth.

An agent that rewrites the same file thousands of times, or touches thousands of
distinct files, would otherwise blow up the in-memory state, the persisted
snapshot JSON, and the `init` WS payload sent on every reconnect.
"""
from __future__ import annotations

from web.state import DashboardState, FileChangeRecord, MAX_FILE_CHANGES


def test_file_changes_dedupes_rewrites_by_path() -> None:
    state = DashboardState()

    for i in range(50):
        state.record_file_change(
            path="a.py", agent="writer", content=f"v{i}", old_content=f"v{i-1}"
        )

    # Rewrites collide on the same path, so the list stays at 1 entry.
    assert len(state.file_changes) == 1
    assert state.file_changes[0].content == "v49"
    assert state.file_changes_truncated_count == 0


def test_file_changes_caps_distinct_paths_lru_by_last_touched() -> None:
    state = DashboardState()

    # Seed 250 distinct paths — 50 over the cap of 200.
    for i in range(250):
        state.record_file_change(
            path=f"file_{i:04d}.py", agent="writer", content=f"body {i}"
        )

    assert len(state.file_changes) == MAX_FILE_CHANGES == 200
    # The 50 oldest paths (0..49) should have been evicted.
    kept_paths = {fc.path for fc in state.file_changes}
    for i in range(50):
        assert f"file_{i:04d}.py" not in kept_paths
    # The 200 newest (50..249) should be present.
    for i in range(50, 250):
        assert f"file_{i:04d}.py" in kept_paths
    # Truncation counter reflects 50 evictions.
    assert state.file_changes_truncated_count == 50


def test_file_changes_rewrite_touches_refresh_lru_position() -> None:
    state = DashboardState()

    # Fill to the cap.
    for i in range(MAX_FILE_CHANGES):
        state.record_file_change(path=f"p_{i:04d}.py", agent="w", content=str(i))

    # Refresh the oldest path — it should now be the newest in LRU order.
    state.record_file_change(path="p_0000.py", agent="w", content="refreshed")

    # Adding one brand-new path should evict the NEXT-oldest (p_0001.py),
    # not p_0000.py which was just touched.
    state.record_file_change(path="new.py", agent="w", content="x")

    kept = {fc.path for fc in state.file_changes}
    assert "p_0000.py" in kept
    assert "p_0001.py" not in kept
    assert "new.py" in kept
    assert state.file_changes_truncated_count == 1


def test_to_init_event_respects_cap_and_surfaces_truncated_count() -> None:
    state = DashboardState()
    for i in range(300):
        state.record_file_change(path=f"f_{i:04d}.py", agent="w", content=str(i))

    event = state.to_init_event()

    assert len(event["file_changes"]) == MAX_FILE_CHANGES == 200
    assert event["file_changes_truncated_count"] == 100
    # Newest preserved, oldest evicted.
    paths = [fc["path"] for fc in event["file_changes"]]
    assert paths[-1] == "f_0299.py"
    assert "f_0000.py" not in paths


def test_reset_clears_file_changes_and_truncated_count() -> None:
    state = DashboardState()
    for i in range(250):
        state.record_file_change(path=f"f_{i}.py", agent="w", content=str(i))
    assert state.file_changes_truncated_count == 50

    state.reset()

    assert state.file_changes == []
    assert state.file_changes_truncated_count == 0


# ── Session topology lock: wire contract ──────────────────────────────────
#
# After the first run in a session, the runtime pins ``selected_topology`` +
# ``agent_models`` onto the ``_conversation_session``. Follow-up runs reuse
# the lock instead of re-classifying. Historically the TUI and dashboard had
# no visibility into this: ``/topology`` silently no-op'd and the dashboard's
# topology picker would happily offer a switch the server would ignore.
#
# The fix surfaces the lock state as a first-class field on ``DashboardState``
# and emits it inside ``to_init_event()`` so both clients can render "pinned"
# affordances.


def test_dashboard_state_defaults_for_lock_fields() -> None:
    state = DashboardState()
    assert state.locked_topology == ""
    assert state.locked_agent_models == {}


def test_to_init_event_emits_session_lock_block() -> None:
    state = DashboardState()
    state.locked_topology = "triad"
    state.locked_agent_models = {"coordinator": "opus", "coder": "sonnet"}

    event = state.to_init_event()

    # Both fields must appear somewhere reachable by the wire — we use a
    # ``session`` block (which the dashboard already consumes). Runtime
    # layers above may tack on ``id`` / ``workdir`` / ``locked_model_pin``.
    session = event.get("session") or {}
    assert session.get("locked_topology") == "triad"
    assert session.get("locked_agent_models") == {
        "coordinator": "opus",
        "coder": "sonnet",
    }


def test_to_init_event_unlocked_session_emits_empty_lock() -> None:
    """Pre-lock (first run planning) must still surface the field shape so
    clients don't need to care whether the key is present."""
    state = DashboardState()
    event = state.to_init_event()
    session = event.get("session") or {}
    assert session.get("locked_topology") == ""
    assert session.get("locked_agent_models") == {}


def test_reset_clears_lock_fields() -> None:
    state = DashboardState()
    state.locked_topology = "solo"
    state.locked_agent_models = {"coordinator": "opus"}

    state.reset()

    assert state.locked_topology == ""
    assert state.locked_agent_models == {}
