from __future__ import annotations

import asyncio
import logging

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentStatus
from ..messaging.bus import MessageBus
from ..messaging.message import Message, MessageType
from ..tracing.logger import EventLogger
from .types import OrchestratorConfig, RunResult

logger = logging.getLogger(__name__)


class Orchestrator:
    """Manages agent lifecycle, injects tasks, and collects results."""

    def __init__(
        self,
        agents: dict[str, LLMAgent],
        bus: MessageBus,
        config: OrchestratorConfig | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        self.agents = agents
        self.bus = bus
        self.config = config or OrchestratorConfig()
        self._completions: dict[str, str] = {}
        self._event_logger = event_logger
        self._completion_event = asyncio.Event()

        if self._event_logger:
            self.bus.on_event(self._event_logger)

    async def _on_agent_complete(self, agent_id: str, result: str) -> None:
        self._completions[agent_id] = result
        logger.info(f"Agent {agent_id} completed ({len(self._completions)}/{len(self.agents)})")
        if len(self._completions) >= len(self.agents):
            self._completion_event.set()

    async def run(self, query: str) -> RunResult:
        """Run the agent graph with the given query."""
        self._completions.clear()
        self._completion_event.clear()

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

        return RunResult(
            success=len(self._completions) > 0,
            completions=dict(self._completions),
            message_count=self.bus.message_count,
            timed_out=timed_out,
        )
