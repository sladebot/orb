"""Dataclass wrappers for Orb API responses.

Keeps the client surface strongly-typed so callers get IDE autocomplete
and can pattern-match on event types. Every dataclass has a ``from_dict``
classmethod that accepts the raw payload shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionSummary:
    """Projection of a session's state used in list/create/get responses."""

    session_id: str
    generation: int
    workdir: str
    run_state: str
    turn: int
    locked_topology: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionSummary":
        return cls(
            session_id=str(data.get("session_id") or ""),
            generation=int(data.get("generation") or 1),
            workdir=str(data.get("workdir") or ""),
            run_state=str(data.get("run_state") or "idle"),
            turn=int(data.get("turn") or 0),
            locked_topology=str(data.get("locked_topology") or ""),
        )


@dataclass
class RunSummary:
    """Projection of a run's metadata returned by ``start_run``."""

    session_id: str
    run_state: str
    session_turn: int
    init: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSummary":
        return cls(
            session_id=str(data.get("session_id") or ""),
            run_state=str(data.get("run_state") or "planning"),
            session_turn=int(data.get("session_turn") or 0),
            init=dict(data.get("init") or {}),
        )


@dataclass
class Event:
    """A single WebSocket event from the v1 stream.

    Every event has a ``type`` discriminator and carries the
    ``session_id`` it originated from. Event-specific fields live in
    ``data``; helpers below surface the common ones as attributes for
    the most frequent event types.
    """

    type: str
    session_id: str
    data: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Event":
        return cls(
            type=str(payload.get("type") or ""),
            session_id=str(payload.get("session_id") or ""),
            data=payload,
        )

    # Common accessor sugar ---------------------------------------------

    @property
    def from_state(self) -> str:
        """For ``run_state_changed``: the state we were in."""
        return str(self.data.get("from") or "")

    @property
    def to(self) -> str:
        """For ``run_state_changed``: the state we transitioned into."""
        return str(self.data.get("to") or "")

    @property
    def event_name(self) -> str:
        """For ``run_state_changed``: the named FSM transition."""
        return str(self.data.get("event") or "")

    @property
    def path(self) -> str:
        """For ``file_write``: the path that was modified."""
        return str(self.data.get("path") or "")

    @property
    def agent(self) -> str:
        """For ``message`` / ``agent_status`` / ``file_write``."""
        return str(self.data.get("agent") or self.data.get("from") or "")

    @property
    def is_terminal(self) -> bool:
        """True for run_state_changed events that mark the end of a run.

        Deliberately excludes ``idle`` — the FSM emits idle both as the
        resting pre-run state and as the post-stop recovery state, so a
        stale idle event from a prior transition would falsely unblock
        :meth:`OrbSession.wait_for_terminal` on a fresh run. Only
        ``completed`` and ``errored`` unambiguously mean "this run is
        done"; the stop → idle case is handled explicitly by
        :meth:`OrbSession.wait_for_terminal`.
        """
        if self.type != "run_state_changed":
            return False
        return self.to in {"completed", "errored"}
