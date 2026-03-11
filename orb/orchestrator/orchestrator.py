from __future__ import annotations

import asyncio
import logging

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentStatus
from ..messaging.bus import MessageBus
from ..messaging.message import Message, MessageType
from ..tracing.logger import EventLogger
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
        sandbox=None,
    ) -> None:
        self.agents = agents
        self.bus = bus
        self.config = config or OrchestratorConfig()
        self._completions: dict[str, str] = {}
        self._event_logger = event_logger
        self._completion_event = asyncio.Event()
        self._consensus_sent = False
        self._consensus_lock = asyncio.Lock()
        self._sandbox = sandbox

        if self._event_logger:
            self.bus.on_event(self._event_logger)

    async def _on_agent_complete(self, agent_id: str, result: str) -> None:
        self._completions[agent_id] = result
        logger.info(f"Agent {agent_id} completed ({len(self._completions)}/{len(self.agents)})")

        synthesis = self.config.synthesis_agent

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
                    except Exception:
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
                        except Exception:
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
                    except Exception:
                        logger.warning("Could not notify synthesis agent")
        else:
            # No synthesis agent — done when all agents complete.
            if len(self._completions) >= len(self.agents):
                self._completion_event.set()

    async def run(self, query: str) -> RunResult:
        """Run the agent graph with the given query."""
        self._completions.clear()
        self._completion_event.clear()
        self._consensus_sent = False

        if self._event_logger:
            self._event_logger.reset()

        # Wire up completion callbacks
        for agent in self.agents.values():
            agent._on_complete = self._on_agent_complete

        # Start all agents
        tasks = []
        for agent in self.agents.values():
            tasks.append(agent.start())

        # Inject the initial task to the entry agent
        entry = self.config.entry_agent
        if entry not in self.agents:
            return RunResult(success=False, error=f"Entry agent {entry!r} not found")

        initial_msg = Message(
            from_="user",
            to=entry,
            type=MessageType.TASK,
            payload=query,
        )

        # Direct delivery to entry agent (user is not a graph node)
        await self.agents[entry].channel.send(initial_msg)

        if self._event_logger:
            self._event_logger("injected", initial_msg)

        # Wait for completion or timeout
        try:
            await asyncio.wait_for(
                self._completion_event.wait(),
                timeout=self.config.timeout,
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning("Orchestrator timed out")

        # Stop all agents
        for agent in self.agents.values():
            await agent.stop()

        # Cancel any remaining tasks
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        sandbox_dir = str(self._sandbox.root) if self._sandbox else None

        # Clean up sandbox after the run (files remain until cleanup() is called
        # explicitly — callers that want to inspect outputs should do so before this)
        if self._sandbox:
            self._sandbox.cleanup()

        return RunResult(
            success=len(self._completions) > 0,
            completions=dict(self._completions),
            message_count=self.bus.message_count,
            timed_out=timed_out,
            sandbox_dir=sandbox_dir,
        )
