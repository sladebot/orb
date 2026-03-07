from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Awaitable

from orb.messaging.message import Message, MessageType
from .state import DashboardState, AgentState, EdgeState, MessageRecord


# Callback to broadcast JSON to all connected clients
BroadcastFn = Callable[[str], Awaitable[None]]


class DashboardBridge:
    """Adapter between the tracing system and the web dashboard."""

    def __init__(self, state: DashboardState, broadcast: BroadcastFn) -> None:
        self.state = state
        self._broadcast = broadcast

    async def _send(self, event: dict) -> None:
        await self._broadcast(json.dumps(event))

    def setup_agents(self, agent_roles: dict[str, str]) -> None:
        """Initialize agent states from the topology."""
        for node_id, role in agent_roles.items():
            self.state.agents[node_id] = AgentState(node_id=node_id, role=role)

    def setup_edges(self, edges: list[tuple[str, str]]) -> None:
        self.state.edges = [EdgeState(source=a, target=b) for a, b in edges]

    def setup_budget(self, budget: int) -> None:
        self.state.budget = budget
        self.state.budget_remaining = budget

    async def on_message_routed(self, event: str, msg: Message) -> None:
        """Called by MessageBus event system."""
        elapsed = time.time() - self.state.start_time

        record = MessageRecord(
            id=msg.id,
            from_=msg.from_,
            to=msg.to,
            content=msg.payload[:500],
            model=msg.metadata.get("model", ""),
            depth=msg.depth,
            elapsed=elapsed,
            chain_id=msg.chain_id,
            msg_type=msg.type.value,
        )
        self.state.messages.append(record)
        self.state.message_count += 1
        self.state.budget_remaining = max(0, self.state.budget - self.state.message_count)

        # Update agent status
        if msg.from_ in self.state.agents:
            agent = self.state.agents[msg.from_]
            agent.status = "running"
            agent.model = msg.metadata.get("model", agent.model)

        await self._send({
            "type": "message",
            "from": msg.from_,
            "to": msg.to,
            "content": msg.payload[:500],
            "model": msg.metadata.get("model", ""),
            "depth": msg.depth,
            "elapsed": round(elapsed, 2),
            "chain_id": msg.chain_id,
            "msg_type": msg.type.value,
        })

        await self._send({
            "type": "stats",
            "message_count": self.state.message_count,
            "budget_remaining": self.state.budget_remaining,
            "elapsed": round(elapsed, 2),
        })

    async def on_agent_status(self, agent_id: str, status: str, model: str = "") -> None:
        if agent_id in self.state.agents:
            self.state.agents[agent_id].status = status
            if model:
                self.state.agents[agent_id].model = model

        await self._send({
            "type": "agent_status",
            "agent": agent_id,
            "status": status,
            "model": model,
        })

    async def on_agent_complete(self, agent_id: str, result: str) -> None:
        if agent_id in self.state.agents:
            self.state.agents[agent_id].status = "completed"
            self.state.agents[agent_id].completed_result = result

        await self._send({
            "type": "complete",
            "agent": agent_id,
            "result": result[:1000],
        })
