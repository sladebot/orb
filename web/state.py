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
    msg_count: int = 0


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
    context_slice: list[str] = field(default_factory=list)


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
    completed: bool = False

    def reset(self) -> None:
        """Reset all state back to defaults (called before starting a new run)."""
        self.agents = {}
        self.edges = []
        self.messages = []
        self.message_count = 0
        self.budget_remaining = self.budget
        self.start_time = time.time()
        self.completed = False

    def to_init_event(self) -> dict:
        return {
            "type": "init",
            "completed": self.completed,
            "agents": [
                {
                    "id": a.node_id,
                    "role": a.role,
                    "status": a.status,
                    "model": a.model,
                    "msg_count": a.msg_count,
                    "completed_result": a.completed_result,
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
                    "msg_type": m.msg_type,
                    "context_slice": m.context_slice,
                }
                for m in self.messages
            ],
            "stats": {
                "message_count": self.message_count,
                "budget_remaining": self.budget_remaining,
                "elapsed": time.time() - self.start_time,
            },
        }
