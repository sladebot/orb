from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

from orb.messaging.message import Message, MessageType
from .state import DashboardState, AgentState, EdgeState, MessageRecord


# Callback to broadcast JSON to all connected clients
BroadcastFn = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)


class DashboardBridge:
    """Adapter between the tracing system and the web dashboard."""

    def __init__(
        self,
        state: DashboardState,
        broadcast: BroadcastFn,
        persist_state: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self._broadcast = broadcast
        self._persist_state = persist_state or (lambda: None)

    async def _send(self, event: dict) -> None:
        await self._broadcast(json.dumps(event))

    def setup_agents(self, agent_roles: dict[str, str]) -> None:
        """Initialize agent states from the topology."""
        for node_id, role in agent_roles.items():
            self.state.agents[node_id] = AgentState(node_id=node_id, role=role)

    def setup_edges(self, edges: list[tuple[str, str]]) -> None:
        self.state.edges = [EdgeState(source=a, target=b) for a, b in edges]
        neighbors: dict[str, set[str]] = {agent_id: set() for agent_id in self.state.agents}
        for source, target in edges:
            neighbors.setdefault(source, set()).add(target)
            neighbors.setdefault(target, set()).add(source)
        self.state.agent_neighbors = {
            agent_id: sorted(peer_ids)
            for agent_id, peer_ids in neighbors.items()
        }

    def setup_budget(self, budget: int) -> None:
        self.state.budget = budget
        self.state.budget_remaining = budget

    def setup_plan(
        self,
        *,
        query: str,
        topology_id: str,
        topology_label: str,
        topology_description: str,
        agent_complexity: dict[str, int] | None = None,
        agent_models: dict[str, str] | None = None,
        agent_positions: dict[str, str] | None = None,
        graph_view: dict | None = None,
    ) -> None:
        self.state.run_query = query
        self.state.topology_id = topology_id
        self.state.topology_label = topology_label
        self.state.topology_description = topology_description
        self.state.agent_complexity = dict(agent_complexity or {})
        self.state.agent_models = dict(agent_models or {})
        self.state.agent_positions = dict(agent_positions or {})
        self.state.graph_view = dict(graph_view or {})

    async def on_message_routed(self, event: str, msg: Message) -> None:
        """Called by MessageBus event system."""
        elapsed = time.time() - self.state.start_time
        context_slice = list(msg.context_slice) if msg.context_slice else []
        logger.info(
            "dashboard event=message_routed event_name=%s from=%s to=%s type=%s depth=%s elapsed=%.2fs model=%s",
            event,
            msg.from_,
            msg.to,
            msg.type.value,
            msg.depth,
            elapsed,
            msg.metadata.get("model", ""),
        )

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
            context_slice=context_slice,
        )
        self.state.messages.append(record)
        self.state.message_count += 1
        self.state.budget_remaining = max(0, self.state.budget - self.state.message_count)
        self._persist_state()

        # Update agent status and msg_count for sender
        if msg.from_ in self.state.agents:
            agent = self.state.agents[msg.from_]
            if agent.status not in {"completed", "error"}:
                agent.status = "running"
            agent.model = msg.metadata.get("model", agent.model)
            agent.msg_count += 1
            complexity = msg.metadata.get("complexity")
            if complexity is not None:
                agent.complexity = int(complexity)

        # Increment msg_count for receiver
        if msg.to in self.state.agents:
            self.state.agents[msg.to].msg_count += 1

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
            "context_slice": context_slice,
        })

        await self._send({
            "type": "stats",
            "message_count": self.state.message_count,
            "budget_remaining": self.state.budget_remaining,
            "elapsed": round(elapsed, 2),
        })

        # Broadcast updated agent stats for both sender and receiver
        for agent_id in {msg.from_, msg.to}:
            if agent_id in self.state.agents:
                a = self.state.agents[agent_id]
                await self._send({
                    "type": "agent_stats",
                    "agent": agent_id,
                    "msg_count": a.msg_count,
                    "status": a.status,
                    "model": a.model,
                    "complexity": a.complexity,
                })

    async def on_agent_status(self, agent_id: str, status: str, model: str = "") -> None:
        if agent_id in self.state.agents:
            self.state.agents[agent_id].status = status
            if model:
                self.state.agents[agent_id].model = model
        self._persist_state()
        logger.info(
            "dashboard event=agent_status agent=%s status=%s model=%s",
            agent_id,
            status,
            model,
        )

        await self._send({
            "type": "agent_status",
            "agent": agent_id,
            "status": status,
            "model": model,
        })

    async def on_agent_complete(self, agent_id: str, result: str) -> None:
        is_consensus = result.startswith("Consensus:")
        if agent_id in self.state.agents:
            self.state.agents[agent_id].status = "completed"
            self.state.agents[agent_id].completed_result = result
        self._persist_state()
        logger.info(
            "dashboard event=agent_complete agent=%s consensus=%s result_preview=%s",
            agent_id,
            is_consensus,
            result[:160].replace("\n", " "),
        )

        await self._send({
            "type": "complete",
            "agent": agent_id,
            "result": result,
            "is_consensus": is_consensus,
        })

    async def on_message_delta(
        self,
        chain_id: str,
        from_: str,
        delta: str,
        index: int,
    ) -> None:
        """Broadcast a single streaming token chunk.

        Fired by the agent's per-chunk hook (wired in
        ``GraphRuntime._start_run``) for every non-empty delta a
        streaming provider emits. The envelope shape is the shared
        contract with stream-tui/#13 + stream-dashboard/#14 on
        ``tui-improvements`` — keep it exactly:

            {
              "type": "message_delta",
              "from": <agent_id>,
              "chain_id": <chain_id>,
              "delta": <text>,
              "index": <0-based monotonic int per chain_id>,
            }

        The final ``message`` event still fires after the last delta
        with the full accumulated content — deltas do NOT replace it.
        No state is mutated here: a dropped client can resync off the
        next ``message`` event without needing the deltas replayed.
        """
        await self._send({
            "type": "message_delta",
            "from": from_,
            "chain_id": chain_id,
            "delta": delta,
            "index": index,
        })

    async def on_agent_heartbeat(self, agent_id: str, payload: dict) -> None:
        ts = float(payload.get("ts", time.time()))
        status = payload.get("status", "")
        if agent_id in self.state.agents:
            agent = self.state.agents[agent_id]
            agent.last_heartbeat = ts
            if status and agent.status not in {"completed", "error"}:
                agent.status = status
        self._persist_state()
        logger.info(
            "dashboard event=agent_heartbeat agent=%s status=%s ts=%.3f",
            agent_id,
            status,
            ts,
        )

        await self._send({
            "type": "agent_heartbeat",
            "agent": agent_id,
            "ts": ts,
            "status": status,
        })
