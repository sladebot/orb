from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ControllerAction(str, Enum):
    """A post-routing runtime action the controller may request."""

    CONTINUE = "continue"
    STOP_EARLY = "stop_early"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RETRY_NODE = "retry_node"
    KILL_NODE = "kill_node"
    REDUCE_FANOUT = "reduce_fanout"
    FALLBACK_TOPOLOGY = "fallback_topology"


@dataclass(frozen=True)
class ControllerContext:
    """Minimal runtime snapshot used to evaluate execution policy.

    This is intentionally small and extensible. Later phases can feed in
    richer telemetry without changing the controller shape.
    """

    query: str = ""
    topology_id: str = ""
    task_type: str = ""
    routing_mode: str = ""
    agent_id: str = ""
    stage: str = ""
    budget_total: int = 0
    budget_remaining: int = 0
    timeout_s: float = 0.0
    elapsed_s: float = 0.0
    fanout: int = 0
    max_fanout: int = 0
    escalation_allowed: bool = False
    stop_early_allowed: bool = False
    signals: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControllerDecision:
    """A requested policy action from the execution controller."""

    action: ControllerAction = ControllerAction.CONTINUE
    reason: str = ""
    target: str = ""
    topology_id: str = ""
    retry_after_s: float | None = None
    fanout_limit: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        return self.action == ControllerAction.CONTINUE


@dataclass(frozen=True)
class ControllerIntervention:
    """A materialized controller action that was applied or observed."""

    action: ControllerAction
    reason: str = ""
    target: str = ""
    topology_id: str = ""
    stage: str = ""
    applied: bool = True
    details: dict[str, Any] = field(default_factory=dict)


class ExecutionController(ABC):
    """Base class for post-routing runtime policy."""

    @abstractmethod
    async def evaluate(self, context: ControllerContext) -> ControllerDecision:
        """Return the next controller action for the current runtime state."""
        raise NotImplementedError

    async def record(self, intervention: ControllerIntervention) -> None:
        """Hook for implementations that want to persist controller activity."""
        return None


class NoopExecutionController(ExecutionController):
    """Default controller that never intervenes."""

    async def evaluate(self, context: ControllerContext) -> ControllerDecision:
        return ControllerDecision(
            action=ControllerAction.CONTINUE,
            reason="No execution policy configured.",
            topology_id=context.topology_id,
        )


class DefaultExecutionController(ExecutionController):
    """Phase 3 controller with simple, explicit runtime policies."""

    async def evaluate(self, context: ControllerContext) -> ControllerDecision:
        requested_topology = str(context.metadata.get("requested_topology") or "auto")
        recommended_topology = str(
            context.metadata.get("recommended_topology")
            or context.metadata.get("classifier_topology")
            or context.topology_id
            or ""
        )
        compact_topology = str(context.metadata.get("compact_topology") or "")
        completion_count = int(context.metadata.get("completion_count") or 0)

        if context.timeout_s > 0 and context.elapsed_s >= context.timeout_s:
            return ControllerDecision(
                action=ControllerAction.TIMEOUT,
                reason=f"Execution exceeded timeout of {context.timeout_s:.2f}s.",
                topology_id=context.topology_id,
            )

        if context.budget_total > 0 and context.budget_remaining <= 0:
            return ControllerDecision(
                action=ControllerAction.BUDGET_EXHAUSTED,
                reason="Execution exhausted the configured message budget.",
                topology_id=context.topology_id,
            )

        if context.stage == "planning":
            if context.max_fanout > 0 and context.fanout > context.max_fanout:
                fallback_topology = compact_topology or recommended_topology
                if fallback_topology and fallback_topology != context.topology_id:
                    return ControllerDecision(
                        action=ControllerAction.FALLBACK_TOPOLOGY,
                        reason=(
                            f"Selected topology fan-out ({context.fanout}) exceeds configured "
                            f"max_fanout ({context.max_fanout})."
                        ),
                        topology_id=fallback_topology,
                        details={"fanout": context.fanout, "max_fanout": context.max_fanout},
                    )
                return ControllerDecision(
                    action=ControllerAction.REDUCE_FANOUT,
                    reason=(
                        f"Selected topology fan-out ({context.fanout}) exceeds configured "
                        f"max_fanout ({context.max_fanout})."
                    ),
                    topology_id=context.topology_id,
                    fanout_limit=context.max_fanout,
                    details={"fanout": context.fanout, "max_fanout": context.max_fanout},
                )

            if (
                context.escalation_allowed
                and requested_topology not in ("", "auto")
                and recommended_topology
                and recommended_topology != context.topology_id
            ):
                return ControllerDecision(
                    action=ControllerAction.FALLBACK_TOPOLOGY,
                    reason=str(
                        context.metadata.get("escalation_reason")
                        or "Pinned topology is weaker than the recommended topology."
                    ),
                    topology_id=recommended_topology,
                )

            if (
                context.stop_early_allowed
                and requested_topology not in ("", "auto")
                and compact_topology
                and compact_topology != context.topology_id
            ):
                return ControllerDecision(
                    action=ControllerAction.FALLBACK_TOPOLOGY,
                    reason=str(
                        context.metadata.get("stop_early_reason")
                        or "Pinned topology is heavier than needed for this task."
                    ),
                    topology_id=compact_topology,
                )

        if context.stage == "completion" and context.stop_early_allowed and completion_count >= 1:
            return ControllerDecision(
                action=ControllerAction.STOP_EARLY,
                reason=str(
                    context.metadata.get("stop_early_reason")
                    or "A satisfactory completion is already available."
                ),
                topology_id=context.topology_id,
                target=str(context.metadata.get("completed_by") or ""),
                details={"completion_count": completion_count},
            )

        return ControllerDecision(
            action=ControllerAction.CONTINUE,
            reason="Execution policy allows the run to continue.",
            topology_id=context.topology_id,
        )
