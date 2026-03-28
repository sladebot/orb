from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4


class TraceEventKind(Enum):
    TOPOLOGY_CHOICE = "topology_choice"
    AGENT_SPAWN = "agent_spawn"
    MESSAGE_ROUTED = "message_routed"
    INITIAL_INJECTION = "initial_injection"
    STAGE_START = "stage_start"
    STAGE_FINISH = "stage_finish"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    RETRY = "retry"
    VERIFIER_DECISION = "verifier_decision"
    HUMAN_OVERRIDE = "human_override"
    FINAL_OUTCOME = "final_outcome"


@dataclass
class TraceEvent:
    kind: TraceEventKind
    timestamp: float = field(default_factory=time)
    actor: str = ""
    target: str = ""
    stage: str = ""
    status: str = ""
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> TraceEvent:
        data = dict(payload.get("data") or {})
        kind = TraceEventKind(str(payload.get("kind") or TraceEventKind.FINAL_OUTCOME.value))
        return cls(
            kind=kind,
            timestamp=float(payload.get("timestamp") or time()),
            actor=str(payload.get("actor") or ""),
            target=str(payload.get("target") or ""),
            stage=str(payload.get("stage") or ""),
            status=str(payload.get("status") or ""),
            message=str(payload.get("message") or ""),
            data=data,
        )


@dataclass
class RunTrace:
    session_id: str = ""
    run_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[TraceEvent] = field(default_factory=list)

    def reset(self) -> None:
        self.run_id = uuid4().hex
        self.created_at = time()
        self.updated_at = self.created_at
        self.metadata.clear()
        self.events.clear()

    def _append(self, event: TraceEvent) -> TraceEvent:
        self.events.append(event)
        self.updated_at = time()
        return event

    def record_event(
        self,
        kind: TraceEventKind,
        *,
        actor: str = "",
        target: str = "",
        stage: str = "",
        status: str = "",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        return self._append(TraceEvent(
            kind=kind,
            actor=actor,
            target=target,
            stage=stage,
            status=status,
            message=message,
            data=dict(data or {}),
        ))

    def record_topology_choice(
        self,
        topology_id: str,
        *,
        reason: str = "",
        task_type: str = "",
        candidates: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        if candidates is not None:
            payload["candidates"] = list(candidates)
        if task_type:
            payload["task_type"] = task_type
        return self.record_event(
            TraceEventKind.TOPOLOGY_CHOICE,
            target=topology_id,
            message=reason,
            data=payload,
        )

    def record_agent_spawn(
        self,
        agent_id: str,
        *,
        role: str = "",
        model: str = "",
        topology_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        if role:
            payload["role"] = role
        if model:
            payload["model"] = model
        if topology_id:
            payload["topology_id"] = topology_id
        return self.record_event(
            TraceEventKind.AGENT_SPAWN,
            actor=agent_id,
            data=payload,
        )

    def record_completion(
        self,
        agent_id: str,
        result: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        return self.record_event(
            TraceEventKind.STAGE_FINISH,
            actor=agent_id,
            stage="completion",
            status="completed",
            message=result,
            data=payload,
        )

    def record_message_routed(
        self,
        event: str,
        *,
        actor: str = "",
        target: str = "",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        payload["event"] = event
        return self.record_event(
            TraceEventKind.MESSAGE_ROUTED,
            actor=actor,
            target=target,
            message=message,
            data=payload,
        )

    def record_initial_injection(
        self,
        *,
        target: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        return self.record_event(
            TraceEventKind.INITIAL_INJECTION,
            target=target,
            message=message,
            data=data,
        )

    def record_stage_start(
        self,
        stage: str,
        *,
        actor: str = "",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        return self.record_event(
            TraceEventKind.STAGE_START,
            actor=actor,
            stage=stage,
            message=message,
            data=data,
        )

    def record_stage_finish(
        self,
        stage: str,
        *,
        actor: str = "",
        status: str = "ok",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        return self.record_event(
            TraceEventKind.STAGE_FINISH,
            actor=actor,
            stage=stage,
            status=status,
            message=message,
            data=data,
        )

    def record_llm_response(
        self,
        *,
        actor: str,
        model: str,
        provider: str = "",
        usage: dict[str, Any] | None = None,
        stop_reason: str = "",
        tool_call_count: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        payload["model"] = model
        if provider:
            payload["provider"] = provider
        if usage:
            payload["usage"] = dict(usage)
        if stop_reason:
            payload["stop_reason"] = stop_reason
        if tool_call_count is not None:
            payload["tool_call_count"] = tool_call_count
        return self.record_event(
            TraceEventKind.LLM_RESPONSE,
            actor=actor,
            data=payload,
        )

    def record_tool_call(
        self,
        tool_name: str,
        *,
        actor: str = "",
        status: str = "ok",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        payload["tool_name"] = tool_name
        return self.record_event(
            TraceEventKind.TOOL_CALL,
            actor=actor,
            status=status,
            message=message,
            data=payload,
        )

    def record_retry(
        self,
        *,
        actor: str = "",
        stage: str = "",
        attempt: int = 0,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        if attempt:
            payload["attempt"] = attempt
        return self.record_event(
            TraceEventKind.RETRY,
            actor=actor,
            stage=stage,
            message=message,
            data=payload,
        )

    def record_verifier_decision(
        self,
        decision: str,
        *,
        actor: str = "",
        stage: str = "",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        payload["decision"] = decision
        return self.record_event(
            TraceEventKind.VERIFIER_DECISION,
            actor=actor,
            stage=stage,
            message=message,
            data=payload,
        )

    def record_human_override(
        self,
        *,
        actor: str = "human",
        action: str = "",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        if action:
            payload["action"] = action
        return self.record_event(
            TraceEventKind.HUMAN_OVERRIDE,
            actor=actor,
            message=message,
            data=payload,
        )

    def record_final_outcome(
        self,
        *,
        success: bool,
        message: str = "",
        result: str = "",
        error: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        payload = dict(data or {})
        payload["success"] = success
        if result:
            payload["result"] = result
        if error:
            payload["error"] = error
        return self.record_event(
            TraceEventKind.FINAL_OUTCOME,
            status="success" if success else "failure",
            message=message,
            data=payload,
        )

    def topology_choice(self) -> str:
        for event in self.events:
            if event.kind == TraceEventKind.TOPOLOGY_CHOICE:
                return event.target
        return str(self.metadata.get("topology_id") or "")

    def agent_ids(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            if event.kind == TraceEventKind.AGENT_SPAWN and event.actor and event.actor not in seen:
                seen.append(event.actor)
        return seen

    def counts_by_kind(self) -> dict[str, int]:
        counts = Counter(event.kind.value for event in self.events)
        return dict(counts)

    def token_usage_by_agent(self) -> dict[str, dict[str, int]]:
        usage_by_agent: dict[str, dict[str, int]] = {}
        for event in self.events:
            if event.kind != TraceEventKind.LLM_RESPONSE or not event.actor:
                continue
            usage = event.data.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            bucket = usage_by_agent.setdefault(event.actor, {})
            for key, value in usage.items():
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    bucket[key] = bucket.get(key, 0) + int(value)
        return usage_by_agent

    def stage_latencies(self) -> dict[str, float]:
        starts: dict[str, list[float]] = {}
        latencies: dict[str, float] = {}
        for event in self.events:
            if event.kind == TraceEventKind.STAGE_START and event.stage:
                starts.setdefault(event.stage, []).append(event.timestamp)
            elif event.kind == TraceEventKind.STAGE_FINISH and event.stage:
                stage_starts = starts.get(event.stage) or []
                if stage_starts:
                    started_at = stage_starts.pop(0)
                    latencies[event.stage] = latencies.get(event.stage, 0.0) + max(0.0, event.timestamp - started_at)
        return latencies

    def summary(self) -> dict[str, Any]:
        event_count = len(self.events)
        first_ts = self.events[0].timestamp if self.events else None
        last_ts = self.events[-1].timestamp if self.events else None
        topology_event = next(
            (event for event in self.events if event.kind == TraceEventKind.TOPOLOGY_CHOICE),
            None,
        )
        final_event = next(
            (event for event in reversed(self.events) if event.kind == TraceEventKind.FINAL_OUTCOME),
            None,
        )
        summary = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "event_count": event_count,
            "counts_by_kind": self.counts_by_kind(),
            "topology_id": self.topology_choice(),
            "agent_ids": self.agent_ids(),
            "agent_count": len(self.agent_ids()),
            "token_usage_by_agent": self.token_usage_by_agent(),
            "stage_latencies": self.stage_latencies(),
            "first_event_at": first_ts,
            "last_event_at": last_ts,
            "duration_s": (last_ts - first_ts) if first_ts is not None and last_ts is not None else 0.0,
            "task_type": str((topology_event.data.get("task_type") if topology_event else "") or ""),
            "routing_reason": str((topology_event.message if topology_event else "") or ""),
            "routing_mode": str((topology_event.data.get("routing_mode") if topology_event else "") or ""),
            "classifier_model": str((topology_event.data.get("classifier_model") if topology_event else "") or ""),
            "classifier_provider": str((topology_event.data.get("classifier_provider") if topology_event else "") or ""),
            "escalation_allowed": bool((topology_event.data.get("escalation_allowed") if topology_event else False) or False),
            "stop_early_allowed": bool((topology_event.data.get("stop_early_allowed") if topology_event else False) or False),
            "escalation_reason": str((topology_event.data.get("escalation_reason") if topology_event else "") or ""),
            "stop_early_reason": str((topology_event.data.get("stop_early_reason") if topology_event else "") or ""),
            "routing_candidates": list((topology_event.data.get("candidate_details") if topology_event else []) or []),
        }
        if final_event is not None:
            summary["success"] = bool(final_event.data.get("success"))
            summary["result"] = str(final_event.data.get("result") or "")
            summary["error"] = str(final_event.data.get("error") or "")
            summary["final_message"] = final_event.message
        else:
            summary["success"] = None
            summary["result"] = ""
            summary["error"] = ""
            summary["final_message"] = ""
        return summary

    def summary_text(self) -> str:
        summary = self.summary()
        outcome = "unknown"
        if summary["success"] is True:
            outcome = "success"
        elif summary["success"] is False:
            outcome = "failure"
        topology = summary["topology_id"] or "unknown"
        return (
            f"Session {summary['session_id'] or 'unknown'} | "
            f"Run {summary['run_id']} | topology={topology} | "
            f"events={summary['event_count']} | outcome={outcome}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "events": [event.to_dict() for event in self.events],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict) -> RunTrace:
        trace = cls(
            session_id=str(payload.get("session_id") or payload.get("metadata", {}).get("session_id") or ""),
            run_id=str(payload.get("run_id") or uuid4().hex),
            created_at=float(payload.get("created_at") or time()),
            updated_at=float(payload.get("updated_at") or time()),
            metadata=dict(payload.get("metadata") or {}),
        )
        if trace.session_id and not trace.metadata.get("session_id"):
            trace.metadata["session_id"] = trace.session_id
        trace.events = [
            TraceEvent.from_dict(event)
            for event in (payload.get("events") or [])
            if isinstance(event, dict)
        ]
        return trace

    @classmethod
    def from_json(cls, payload: str) -> RunTrace:
        return cls.from_dict(json.loads(payload))

    @classmethod
    def load(cls, path: Path) -> RunTrace:
        return cls.from_dict(json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = time()
        if self.session_id and not self.metadata.get("session_id"):
            self.metadata["session_id"] = self.session_id
        path.write_text(self.to_json())
