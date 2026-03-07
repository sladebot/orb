from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from ..messaging.channel import AgentChannel, ChannelClosed
from ..messaging.bus import MessageBus
from ..messaging.message import Message
from .types import AgentConfig, AgentStatus

logger = logging.getLogger(__name__)


class AgentNode(ABC):
    """Abstract base class for graph agent nodes."""

    def __init__(self, config: AgentConfig, channel: AgentChannel, bus: MessageBus) -> None:
        self.config = config
        self.channel = channel
        self.bus = bus
        self.status = AgentStatus.IDLE
        self._task: asyncio.Task | None = None

    @property
    def node_id(self) -> str:
        return self.config.node_id

    @property
    def neighbors(self) -> set[str]:
        return self.bus.graph.get_neighbors(self.node_id)

    def start(self) -> asyncio.Task:
        self.status = AgentStatus.RUNNING
        self._task = asyncio.create_task(self._run_loop(), name=f"agent-{self.node_id}")
        return self._task

    async def stop(self) -> None:
        self.channel.close()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run_loop(self) -> None:
        try:
            while True:
                msg = await self.channel.receive()
                try:
                    await self.process(msg)
                except Exception:
                    logger.exception(f"Agent {self.node_id} error processing message {msg.id}")
        except ChannelClosed:
            logger.info(f"Agent {self.node_id} channel closed, stopping")
        except Exception:
            logger.exception(f"Agent {self.node_id} unexpected error")
            self.status = AgentStatus.ERROR

    @abstractmethod
    async def process(self, msg: Message) -> None:
        ...

    async def send(self, msg: Message) -> None:
        await self.bus.route(msg)
