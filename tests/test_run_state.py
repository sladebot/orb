"""Tests for the RunStateMachine in orb.runtime.run_state.

These cover the transition table + listener semantics so the FSM has a
safety net before we wire it into GraphRuntime.
"""

from __future__ import annotations

import pytest

from orb.runtime.run_state import (
    IN_FLIGHT_STATES,
    TERMINAL_STATES,
    InvalidTransitionError,
    RunState,
    RunStateMachine,
    describe_transitions,
)


class TestHappyPath:
    def test_initial_state_is_idle(self):
        fsm = RunStateMachine()
        assert fsm.state is RunState.IDLE
        assert fsm.is_terminal
        assert not fsm.is_in_flight

    def test_full_run_cycle_lands_in_completed(self):
        fsm = RunStateMachine()
        assert fsm.fire("start_run_begin") is RunState.PLANNING
        assert fsm.is_in_flight
        assert fsm.fire("orchestrator_task_created") is RunState.RUNNING
        assert fsm.is_in_flight
        assert fsm.fire("orchestrator_succeeded") is RunState.COMPLETED
        assert fsm.is_terminal
        assert not fsm.is_in_flight

    def test_follow_up_run_starts_from_completed(self):
        """A locked session fires start_run again without going through IDLE."""
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        fsm.fire("orchestrator_succeeded")
        assert fsm.state is RunState.COMPLETED
        assert fsm.fire("start_run_begin") is RunState.PLANNING


class TestStopPath:
    def test_stop_while_planning(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        assert fsm.fire("stop_requested") is RunState.STOPPING
        assert fsm.fire("stop_finished") is RunState.IDLE

    def test_stop_while_running(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        assert fsm.fire("stop_requested") is RunState.STOPPING
        assert fsm.is_in_flight
        assert fsm.fire("stop_finished") is RunState.IDLE
        assert fsm.is_terminal

    def test_stop_finished_from_non_stopping_is_rejected(self):
        fsm = RunStateMachine()
        with pytest.raises(InvalidTransitionError):
            fsm.fire("stop_finished")


class TestErrorPath:
    def test_error_from_running(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        assert fsm.fire("orchestrator_errored") is RunState.ERRORED
        assert fsm.is_terminal

    def test_error_from_planning(self):
        """Classifier crashes during planning before the task launches."""
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        assert fsm.state is RunState.PLANNING
        assert fsm.fire("orchestrator_errored") is RunState.ERRORED

    def test_error_from_idle_is_rejected(self):
        fsm = RunStateMachine()
        with pytest.raises(InvalidTransitionError) as exc:
            fsm.fire("orchestrator_errored")
        assert exc.value.current is RunState.IDLE

    def test_new_run_can_start_after_error(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_errored")
        assert fsm.fire("start_run_begin") is RunState.PLANNING


class TestSessionReset:
    def test_session_reset_from_idle_stays_idle(self):
        fsm = RunStateMachine()
        assert fsm.fire("session_reset") is RunState.IDLE

    def test_session_reset_from_completed(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        fsm.fire("orchestrator_succeeded")
        assert fsm.fire("session_reset") is RunState.IDLE

    def test_session_reset_from_errored(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_errored")
        assert fsm.fire("session_reset") is RunState.IDLE

    def test_session_reset_mid_run_is_rejected(self):
        """new_session() must never silently drop an in-flight run."""
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        with pytest.raises(InvalidTransitionError) as exc:
            fsm.fire("session_reset")
        assert exc.value.current is RunState.RUNNING

    def test_session_reset_while_stopping_is_rejected(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        fsm.fire("stop_requested")
        with pytest.raises(InvalidTransitionError):
            fsm.fire("session_reset")


class TestInvalidTransitions:
    def test_concurrent_start_is_rejected(self):
        """start_run_begin can't fire while PLANNING/RUNNING/STOPPING."""
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        with pytest.raises(InvalidTransitionError):
            fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        with pytest.raises(InvalidTransitionError):
            fsm.fire("start_run_begin")

    def test_unknown_event_raises(self):
        fsm = RunStateMachine()
        with pytest.raises(InvalidTransitionError):
            fsm.fire("totally_made_up")

    def test_can_fire_returns_false_for_impossible_transitions(self):
        fsm = RunStateMachine()
        assert fsm.can_fire("start_run_begin")
        assert not fsm.can_fire("orchestrator_succeeded")
        assert not fsm.can_fire("stop_finished")

    def test_maybe_fire_swallows_illegal(self):
        fsm = RunStateMachine()
        assert fsm.maybe_fire("stop_finished") is None
        assert fsm.state is RunState.IDLE


class TestListeners:
    def test_listener_sees_old_new_and_event(self):
        fsm = RunStateMachine()
        events: list[tuple[RunState, RunState, str]] = []
        fsm.subscribe(lambda old, new, evt: events.append((old, new, evt)))
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        assert events == [
            (RunState.IDLE, RunState.PLANNING, "start_run_begin"),
            (RunState.PLANNING, RunState.RUNNING, "orchestrator_task_created"),
        ]

    def test_unsubscribe_stops_notifications(self):
        fsm = RunStateMachine()
        received: list = []
        unsubscribe = fsm.subscribe(lambda *args: received.append(args))
        fsm.fire("start_run_begin")
        unsubscribe()
        fsm.fire("orchestrator_task_created")
        assert len(received) == 1

    def test_listener_raising_does_not_break_other_listeners(self):
        fsm = RunStateMachine()
        seen: list = []

        def angry(*args):
            raise ValueError("boom")

        fsm.subscribe(angry)
        fsm.subscribe(lambda old, new, evt: seen.append((old, new, evt)))
        # A raising listener is logged and suppressed; the transition still
        # lands and siblings still fire.
        fsm.fire("start_run_begin")
        assert fsm.state is RunState.PLANNING
        assert seen == [(RunState.IDLE, RunState.PLANNING, "start_run_begin")]


class TestStateSets:
    def test_in_flight_and_terminal_are_disjoint(self):
        assert IN_FLIGHT_STATES.isdisjoint(TERMINAL_STATES)

    def test_every_state_is_classified(self):
        all_states = IN_FLIGHT_STATES | TERMINAL_STATES
        assert all_states == set(RunState)


class TestDescribeTransitions:
    def test_describe_returns_tuples(self):
        table = describe_transitions()
        assert any(event == "start_run_begin" for event, _, _ in table)
        assert any(event == "orchestrator_succeeded" for event, _, _ in table)


class TestForceReset:
    def test_force_reset_without_listeners(self):
        fsm = RunStateMachine()
        fsm.fire("start_run_begin")
        fsm.fire("orchestrator_task_created")
        fsm.fire("orchestrator_succeeded")
        called: list = []
        fsm.subscribe(lambda *args: called.append(args))
        fsm.force_reset()
        assert fsm.state is RunState.IDLE
        # Listeners are NOT invoked on force_reset — it's an escape hatch.
        assert called == []
