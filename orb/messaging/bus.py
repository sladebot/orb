from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from ..graph.graph import Graph
from .channel import AgentChannel
from .message import Message
from .middleware import HopCounter, BudgetTracker, CooldownTracker


class RoutingError(Exception):
    pass


# Event callback type: called with (event_name, message)
EventCallback = Callable[[str, Message], Awaitable[None] | None]


class MessageBus:
    """Routes messages along graph edges with middleware checks."""

    def __init__(
        self,
        graph: Graph,
        max_depth: int = 10,
        budget: int = 200,
        max_cooldown: int = 5,
    ) -> None:
        self.graph = graph
        self._channels: dict[str, AgentChannel] = {}
        self._hop_counter = HopCounter(max_depth)
        self._budget = BudgetTracker(budget)
        self._cooldown = CooldownTracker(max_cooldown)
        self._listeners: list[EventCallback] = []

    def register_channel(self, node_id: str, channel: AgentChannel) -> None:
        self._channels[node_id] = channel

    def on_event(self, callback: EventCallback) -> None:
        self._listeners.append(callback)

    async def _emit(self, event: str, msg: Message) -> None:
        for cb in self._listeners:
            result = cb(event, msg)
            if asyncio.iscoroutine(result):
                await result

    async def route(self, msg: Message) -> None:
        # Validate edge exists
        if not self.graph.has_edge(msg.from_, msg.to):
            raise RoutingError(
                f"No edge between {msg.from_!r} and {msg.to!r}"
            )

        # Validate destination channel exists
        if msg.to not in self._channels:
            raise RoutingError(f"No channel registered for {msg.to!r}")

        # Run middleware checks
        self._hop_counter.check(msg)
        self._budget.check(msg)
        self._cooldown.check(msg)

        # Deliver
        self._budget.increment()
        self._cooldown.increment(msg)
        await self._channels[msg.to].send(msg)
        await self._emit("routed", msg)

    @property
    def budget_remaining(self) -> int:
        return self._budget.remaining

    @property
    def message_count(self) -> int:
        return self._budget.count
