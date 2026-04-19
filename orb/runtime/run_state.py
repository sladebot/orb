"""Explicit state machine for a GraphRuntime run lifecycle.

The original runtime tracked "am I running?" with a derived property over
`_run_task` plus a handful of implicit flags (`state.completed`,
`_last_result is not None`, etc). That worked for the happy path but left
several states implicit:

  - Planning (inside start_run before the orchestrator task launches)
  - Stopping (cancel requested but task still winding down)
  - Errored (task done, no result, no completed flag)

Making these explicit lets the HTTP handlers, WebSocket broadcaster, and
tests query one source of truth instead of reconstructing state from
several flags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class RunState(str, Enum):
    """The six run-lifecycle states a GraphRuntime can be in.

    `str` mixin so the state serializes cleanly as JSON (e.g. in the init
    event payload) and matches nicely against string constants from
    external callers.
    """

    IDLE = "idle"
    PLANNING = "planning"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERRORED = "errored"


class InvalidTransitionError(RuntimeError):
    """Raised when a caller asks for a transition the current state forbids."""

    def __init__(self, event: str, current: RunState, allowed: frozenset[RunState]):
        super().__init__(
            f"Cannot fire {event!r} from state {current.value!r}; "
            f"only allowed from {sorted(s.value for s in allowed)!r}"
        )
        self.event = event
        self.current = current
        self.allowed = allowed


@dataclass(frozen=True)
class Transition:
    """A named move in the run-lifecycle FSM."""

    event: str
    from_states: frozenset[RunState]
    to_state: RunState


# ── Transition table ────────────────────────────────────────────────────────
#
# Listed in roughly the order they fire during a normal run so the happy
# path reads top-to-bottom. Each event name corresponds to one
# RunStateMachine.fire() call inside GraphRuntime.
#
# Happy path:
#     IDLE → PLANNING → RUNNING → COMPLETED → IDLE (on new_session)
#
# Stop path:
#     PLANNING/RUNNING → STOPPING → IDLE
#
# Error path:
#     PLANNING/RUNNING → ERRORED → IDLE (on new_session or next start_run)
#
_TRANSITIONS: dict[str, Transition] = {
    t.event: t
    for t in [
        Transition(
            event="start_run_begin",
            # Allow kicking off a new run from any "terminal" state. In
            # particular, start_run from COMPLETED is the common case for
            # follow-up turns inside a locked session.
            from_states=frozenset({RunState.IDLE, RunState.COMPLETED, RunState.ERRORED}),
            to_state=RunState.PLANNING,
        ),
        Transition(
            event="orchestrator_task_created",
            from_states=frozenset({RunState.PLANNING}),
            to_state=RunState.RUNNING,
        ),
        Transition(
            event="orchestrator_succeeded",
            # STOPPING is included for the stop-vs-natural-completion race:
            # stop_run fires stop_requested (→ STOPPING) then cancels the
            # task. If the task completed before cancel was delivered, the
            # actual outcome wins over the late stop request — otherwise the
            # FSM would be stuck in STOPPING forever.
            from_states=frozenset({RunState.RUNNING, RunState.STOPPING}),
            to_state=RunState.COMPLETED,
        ),
        Transition(
            event="orchestrator_errored",
            # Planning failures (e.g. classifier unreachable) also land here
            # so we never hold the FSM in PLANNING after an aborted start.
            # STOPPING is included for the same race reason as
            # orchestrator_succeeded above.
            from_states=frozenset({RunState.PLANNING, RunState.RUNNING, RunState.STOPPING}),
            to_state=RunState.ERRORED,
        ),
        Transition(
            event="stop_requested",
            # Can stop while planning (rare — planning is quick) or running.
            from_states=frozenset({RunState.PLANNING, RunState.RUNNING}),
            to_state=RunState.STOPPING,
        ),
        Transition(
            event="stop_finished",
            from_states=frozenset({RunState.STOPPING}),
            to_state=RunState.IDLE,
        ),
        Transition(
            event="session_reset",
            # new_session() can fire from any terminal state — it resets
            # everything and returns us to IDLE. We forbid it from the
            # in-flight states (PLANNING/RUNNING/STOPPING) so callers get
            # an explicit error instead of silently dropping an active run.
            from_states=frozenset({RunState.IDLE, RunState.COMPLETED, RunState.ERRORED}),
            to_state=RunState.IDLE,
        ),
    ]
}


# States where a run is "in flight" from the caller's perspective —
# planning, running, or winding down via stop. Anything else is terminal.
IN_FLIGHT_STATES: frozenset[RunState] = frozenset(
    {RunState.PLANNING, RunState.RUNNING, RunState.STOPPING}
)

# States where the FSM is "resting" and a new run can begin.
TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.IDLE, RunState.COMPLETED, RunState.ERRORED}
)


StateListener = Callable[[RunState, RunState, str], None]
"""Signature for state-change callbacks: (from_state, to_state, event)."""


@dataclass
class RunStateMachine:
    """Owns the current RunState and mediates every transition.

    Keeps a list of listeners so callers (e.g. the dashboard broadcaster)
    can react to every state change without polling.
    """

    _state: RunState = RunState.IDLE
    _listeners: list[StateListener] = field(default_factory=list)

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def is_in_flight(self) -> bool:
        """True iff a run is active (PLANNING/RUNNING/STOPPING)."""
        return self._state in IN_FLIGHT_STATES

    @property
    def is_terminal(self) -> bool:
        """True iff the FSM is resting (IDLE/COMPLETED/ERRORED)."""
        return self._state in TERMINAL_STATES

    def can_fire(self, event: str) -> bool:
        """Whether ``event`` is a legal transition from the current state."""
        transition = _TRANSITIONS.get(event)
        if transition is None:
            return False
        return self._state in transition.from_states

    def fire(self, event: str) -> RunState:
        """Apply ``event`` and notify listeners. Raises on invalid transitions."""
        transition = _TRANSITIONS.get(event)
        if transition is None:
            raise InvalidTransitionError(event, self._state, frozenset())
        if self._state not in transition.from_states:
            raise InvalidTransitionError(event, self._state, transition.from_states)
        old_state = self._state
        self._state = transition.to_state
        for listener in list(self._listeners):
            try:
                listener(old_state, self._state, event)
            except Exception:
                # A bad listener must never stop the FSM from advancing or
                # prevent its siblings from being notified.
                logger.exception(
                    "RunStateMachine listener raised during %s (%s -> %s)",
                    event, old_state.value, self._state.value,
                )
        return self._state

    def maybe_fire(self, event: str) -> RunState | None:
        """Fire ``event`` if legal, returning the new state; else None.

        Useful for idempotent cleanup paths where a transition may already
        have been applied (e.g. a stop that raced with natural completion).
        """
        if self.can_fire(event):
            return self.fire(event)
        return None

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Register a state-change listener; returns an unsubscribe function."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def force_reset(self) -> None:
        """Force back to IDLE without firing listeners.

        Escape hatch for cases where the runtime is rebuilt from scratch
        (e.g. at daemon startup). Avoid in normal flow — prefer
        ``session_reset`` so listeners see the move.
        """
        self._state = RunState.IDLE


def describe_transitions() -> list[tuple[str, list[str], str]]:
    """Return the transition table as plain tuples for docs or inspection."""
    return [
        (t.event, sorted(s.value for s in t.from_states), t.to_state.value)
        for t in _TRANSITIONS.values()
    ]
