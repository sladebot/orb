from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentStatus
from ..messaging.bus import MessageBus
from ..messaging.channel import ChannelClosed
from ..messaging.message import Message, MessageType
from ..messaging.middleware import BudgetExhausted
from ..runtime.execution_controller import (
    ControllerAction,
    ControllerContext,
    ControllerDecision,
    ControllerIntervention,
    ExecutionController,
)
from ..tracing.logger import EventLogger
from ..tracing.run_trace import RunTrace
from .types import OrchestratorConfig, RunResult

try:
    from ..sandbox.sandbox import Sandbox
except ImportError:
    Sandbox = None  # type: ignore

logger = logging.getLogger(__name__)

CONSENSUS_PREFIX = "Consensus:"


class Orchestrator:
    """Manages agent lifecycle, injects tasks, and collects results."""

    def __init__(
        self,
        agents: dict[str, LLMAgent],
        bus: MessageBus,
        config: OrchestratorConfig | None = None,
        event_logger: EventLogger | None = None,
        trace: RunTrace | None = None,
        topology_id: str = "",
        sandbox=None,
        execution_controller: ExecutionController | None = None,
        controller_context: Mapping[str, object] | None = None,
    ) -> None:
        self.agents = agents
        self.bus = bus
        self.config = config or OrchestratorConfig()
        self._completions: dict[str, str] = {}
        self._event_logger = event_logger
        self._trace = trace or getattr(event_logger, "trace", None)
        self._topology_id = topology_id
        self._completion_event = asyncio.Event()
        self._consensus_sent = False
        self._consensus_lock = asyncio.Lock()
        self._sandbox = sandbox
        self._execution_controller = execution_controller
        self._controller_context = dict(controller_context or {})
        self._controller_interventions: list[dict[str, object]] = []
        self._controller_action_override: ControllerAction | None = None
        self._controller_reason_override = ""
        self._run_started_at = 0.0

        if self._event_logger:
            self.bus.on_event(self._event_logger)

    @property
    def trace(self) -> RunTrace | None:
        return self._trace

    def _record_controller_intervention(
        self,
        decision: ControllerDecision,
        *,
        stage: str,
        applied: bool,
    ) -> None:
        intervention = ControllerIntervention(
            action=decision.action,
            reason=decision.reason,
            target=decision.target,
            topology_id=decision.topology_id,
            stage=stage,
            applied=applied,
            details=dict(decision.details),
        )
        payload = {
            "action": intervention.action.value,
            "reason": intervention.reason,
            "target": intervention.target,
            "topology_id": intervention.topology_id,
            "stage": intervention.stage,
            "applied": intervention.applied,
            "details": dict(intervention.details),
        }
        self._controller_interventions.append(payload)
        if self._trace is not None:
            self._trace.record_stage_finish(
                "controller",
                status=intervention.action.value,
                message=intervention.reason,
                data=payload,
            )

    async def _evaluate_controller(self, *, stage: str, agent_id: str = "") -> ControllerDecision:
        controller = self._execution_controller
        if controller is None:
            return ControllerDecision(
                action=ControllerAction.CONTINUE,
                reason="No execution controller configured.",
                topology_id=self._topology_id,
            )
        context = ControllerContext(
            query=str(self._controller_context.get("query") or ""),
            topology_id=self._topology_id,
            task_type=str(self._controller_context.get("task_type") or ""),
            routing_mode=str(self._controller_context.get("routing_mode") or ""),
            agent_id=agent_id,
            stage=stage,
            budget_total=int(self.config.budget or 0),
            budget_remaining=int(self.bus.budget_remaining),
            timeout_s=float(self.config.timeout or 0.0),
            elapsed_s=max(0.0, asyncio.get_running_loop().time() - self._run_started_at),
            fanout=max(0, len(self.agents) - 1),
            max_fanout=int(getattr(self.config, "max_fanout", 0) or 0),
            escalation_allowed=bool(self._controller_context.get("escalation_allowed")),
            stop_early_allowed=bool(self._controller_context.get("stop_early_allowed")),
            signals=dict(self._controller_context.get("signals") or {}),
            metadata={
                **dict(self._controller_context),
                "completion_count": len(self._completions),
                "agent_count": len(self.agents),
                "completed_by": agent_id,
            },
        )
        return await controller.evaluate(context)

    async def _on_agent_complete(self, agent_id: str, result: str) -> None:
        self._completions[agent_id] = result
        transcript = getattr(self, "_transcript", None)
        if transcript is not None:
            transcript.add_completion(agent_id, result)
        if self._trace is not None:
            self._trace.record_completion(agent_id, result)
        logger.info(f"Agent {agent_id} completed ({len(self._completions)}/{len(self.agents)})")

        controller_decision = await self._evaluate_controller(stage="completion", agent_id=agent_id)
        if controller_decision.action == ControllerAction.STOP_EARLY:
            self._controller_action_override = controller_decision.action
            self._controller_reason_override = controller_decision.reason
            self._record_controller_intervention(controller_decision, stage="completion", applied=True)
            for other_id, other_agent in self.agents.items():
                if other_id == agent_id or other_id in self._completions:
                    continue
                shutdown_msg = Message(
                    from_="controller",
                    to=other_id,
                    type=MessageType.COMPLETE,
                    payload=f"Stop early triggered after completion by {agent_id}. {result[:200]}",
                )
                try:
                    await other_agent.channel.send(shutdown_msg)
                    transcript = getattr(self, "_transcript", None)
                    if transcript is not None:
                        transcript.add_message(shutdown_msg)
                except ChannelClosed:
                    logger.warning("Could not send controller COMPLETE to %s", other_id)
            self._completion_event.set()
            return

        synthesis = self.config.synthesis_agent

        if not synthesis:
            if len(self._completions) >= len(self.agents):
                self._completion_event.set()
            return

        if synthesis and agent_id == synthesis:
            # Synthesis agent finished — gracefully stop remaining workers, then signal done.
            for other_id, other_agent in self.agents.items():
                if other_id != synthesis and other_id not in self._completions:
                    shutdown_msg = Message(
                        from_="orchestrator",
                        to=other_id,
                        type=MessageType.COMPLETE,
                        payload=f"Run complete. {result[:200]}",
                    )
                    try:
                        await other_agent.channel.send(shutdown_msg)
                        if transcript is not None:
                            transcript.add_message(shutdown_msg)
                    except ChannelClosed:
                        logger.warning(f"Could not send shutdown COMPLETE to {other_id}")
            self._completion_event.set()
            return

        # Worker completed. Broadcast COMPLETE to other workers (not synthesis agent).
        async with self._consensus_lock:
            if not self._consensus_sent:
                self._consensus_sent = True
                for other_id, other_agent in self.agents.items():
                    if other_id != agent_id and other_id != synthesis:
                        consensus_msg = Message(
                            from_="orchestrator",
                            to=other_id,
                            type=MessageType.COMPLETE,
                            payload=f"{CONSENSUS_PREFIX} task completed by {agent_id}. {result[:200]}",
                        )
                        try:
                            await other_agent.channel.send(consensus_msg)
                            if transcript is not None:
                                transcript.add_message(consensus_msg)
                        except ChannelClosed:
                            logger.warning(f"Could not send consensus COMPLETE to {other_id}")

        # When all workers are done, forward a summary to the synthesis agent.
        if synthesis:
            workers = [aid for aid in self.agents if aid != synthesis]
            if all(w in self._completions for w in workers):
                synth_agent = self.agents.get(synthesis)
                if synth_agent:
                    summary = "\n\n".join(
                        f"[{wid}]: {self._completions[wid][:400]}" for wid in workers
                    )
                    notify_msg = Message(
                        from_="orchestrator",
                        to=synthesis,
                        type=MessageType.RESPONSE,
                        payload=f"All workers have completed. Synthesize the results:\n\n{summary}",
                    )
                    try:
                        await synth_agent.channel.send(notify_msg)
                        if transcript is not None:
                            transcript.add_message(notify_msg)
                    except ChannelClosed:
                        logger.warning("Could not notify synthesis agent")

    async def run(self, query: str, entry_agent: str | None = None) -> RunResult:
        """Run the agent graph with the given query."""
        self._completions.clear()
        self._completion_event.clear()
        self._consensus_sent = False
        self._controller_interventions = []
        self._controller_action_override = None
        self._controller_reason_override = ""
        self._run_started_at = asyncio.get_running_loop().time()

        if self._event_logger:
            self._event_logger.reset()
        elif self._trace is not None:
            self._trace.reset()

        if self._trace is not None and self._topology_id:
            self._trace.record_topology_choice(
                self._topology_id,
                reason="selected for orchestrated run",
                task_type="software_task",
                candidates=[self._topology_id],
            )

        # Wire up completion callbacks
        for agent in self.agents.values():
            agent._on_complete = self._on_agent_complete
            if self._trace is not None:
                self._trace.record_agent_spawn(
                    agent.node_id,
                    role=agent.config.role,
                    model=getattr(getattr(agent.config, "pinned_model", None), "model_id", ""),
                )

        entry = entry_agent or self.config.entry_agent
        if entry not in self.agents:
            if self._trace is not None:
                self._trace.record_final_outcome(
                    success=False,
                    message="entry agent not found",
                    error=f"Entry agent {entry!r} not found",
                    data={
                        "entry_agent": entry,
                        "controller_action": ControllerAction.CONTINUE.value,
                        "controller_source": "entry_lookup",
                        "controller_applied": False,
                        "budget_total": self.config.budget,
                        "budget_remaining": self.bus.budget_remaining,
                        "message_count": self.bus.message_count,
                    },
                )
            return RunResult(success=False, error=f"Entry agent {entry!r} not found")

        budget_exhausted_event = asyncio.Event()
        budget_failure_reason = ""
        original_route = self.bus.route

        async def monitored_route(msg: Message) -> None:
            nonlocal budget_failure_reason
            try:
                await original_route(msg)
            except BudgetExhausted as exc:
                if not budget_exhausted_event.is_set():
                    budget_failure_reason = str(exc)
                    budget_exhausted_event.set()
                raise

        self.bus.route = monitored_route
        tasks: list[asyncio.Task] = []
        controller_action = ControllerAction.CONTINUE
        controller_source = "completed"
        controller_reason = ""
        run_started_at = self._run_started_at
        timed_out = False

        try:
            # Start all agents
            for agent in self.agents.values():
                tasks.append(agent.start())

            # Inject the initial task to the entry agent
            initial_msg = Message(
                from_="user",
                to=entry,
                type=MessageType.TASK,
                payload=query,
            )

            # Direct delivery to entry agent (user is not a graph node)
            await self.agents[entry].channel.send(initial_msg)
            transcript = getattr(self, "_transcript", None)
            if transcript is not None:
                transcript.add_message(initial_msg)

            if self._event_logger:
                self._event_logger("injected", initial_msg)

            # Wait for completion or controller intervention.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.timeout
            poll_interval = 0.1
            while True:
                if budget_exhausted_event.is_set():
                    controller_action = ControllerAction.BUDGET_EXHAUSTED
                    controller_source = "route_failure"
                    controller_reason = budget_failure_reason or "Global message budget exhausted."
                    logger.warning("Orchestrator budget exhausted: %s", controller_reason)
                    break

                if self.bus.message_count > 0 and self.bus.budget_remaining <= 0:
                    controller_action = ControllerAction.BUDGET_EXHAUSTED
                    controller_source = "budget_consumed"
                    controller_reason = (
                        f"Global message budget exhausted ({self.bus.message_count}/{self.config.budget})"
                    )
                    logger.warning("Orchestrator budget exhausted: %s", controller_reason)
                    break

                if self._completion_event.is_set():
                    if self._controller_action_override is not None:
                        controller_action = self._controller_action_override
                        controller_source = "controller"
                        controller_reason = self._controller_reason_override
                    break

                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    controller_action = ControllerAction.TIMEOUT
                    controller_source = "timeout"
                    controller_reason = f"Orchestrator timed out after {self.config.timeout:.2f}s"
                    logger.warning("Orchestrator timed out")
                    break

                await asyncio.sleep(min(poll_interval, remaining))
        finally:
            self.bus.route = original_route

        # Stop all agents
        for agent in self.agents.values():
            await agent.stop()

        # Cancel any remaining tasks
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        sandbox_dir = str(self._sandbox.root) if self._sandbox else None

        # Clean up sandbox after the run (files remain until cleanup() is called
        # explicitly — callers that want to inspect outputs should do so before this)
        if self._sandbox:
            self._sandbox.cleanup()

        success = controller_action in {ControllerAction.CONTINUE, ControllerAction.STOP_EARLY} and len(self._completions) > 0
        timed_out = controller_action == ControllerAction.TIMEOUT
        error = None if success else controller_action.value
        final_message = controller_reason or ("completed" if success else controller_action.value)

        if self._trace is not None:
            self._trace.record_final_outcome(
                success=success,
                message=final_message,
                result=next(iter(self._completions.values()), ""),
                error=error or "",
                data={
                    "controller_action": controller_action.value,
                    "controller_source": controller_source,
                    "controller_reason": controller_reason,
                    "controller_applied": controller_action != ControllerAction.CONTINUE,
                    "timed_out": timed_out,
                    "elapsed_s": max(0.0, asyncio.get_running_loop().time() - run_started_at),
                    "message_count": self.bus.message_count,
                    "budget_total": self.config.budget,
                    "budget_remaining": self.bus.budget_remaining,
                    "controller_interventions": list(self._controller_interventions),
                    "sandbox_dir": sandbox_dir,
                    "completions": dict(self._completions),
                },
            )

        return RunResult(
            success=success,
            completions=dict(self._completions),
            message_count=self.bus.message_count,
            timed_out=timed_out,
            error=error,
            sandbox_dir=sandbox_dir,
        )
