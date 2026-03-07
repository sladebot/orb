from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AgentState:
    node_id: str
    role: str
    status: str = "idle"
    model: str = ""
    completed_result: str = ""


@dataclass
class EdgeState:
    source: str
    target: str


@dataclass
class MessageRecord:
    id: str
    from_: str
    to: str
    content: str
    model: str
    depth: int
    elapsed: float
    chain_id: str
    msg_type: str


@dataclass
class DashboardState:
    """Snapshot of the full system state for dashboard rendering."""

    agents: dict[str, AgentState] = field(default_factory=dict)
    edges: list[EdgeState] = field(default_factory=list)
    messages: list[MessageRecord] = field(default_factory=list)
    message_count: int = 0
    budget: int = 200
    budget_remaining: int = 200
    start_time: float = field(default_factory=time.time)

    def to_init_event(self) -> dict:
        return {
            "type": "init",
            "agents": [
                {
                    "id": a.node_id,
                    "role": a.role,
                    "status": a.status,
                    "model": a.model,
                }
                for a in self.agents.values()
            ],
            "edges": [{"source": e.source, "target": e.target} for e in self.edges],
            "messages": [
                {
                    "id": m.id,
                    "from": m.from_,
                    "to": m.to,
                    "content": m.content,
                    "model": m.model,
                    "depth": m.depth,
                    "elapsed": m.elapsed,
                    "chain_id": m.chain_id,
                    "type": m.msg_type,
                }
                for m in self.messages
            ],
            "stats": {
                "message_count": self.message_count,
                "budget_remaining": self.budget_remaining,
                "elapsed": time.time() - self.start_time,
            },
        }
