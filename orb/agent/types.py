from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.types import ModelConfig
    from ..sandbox.sandbox import Sandbox


class AgentStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class AgentConfig:
    node_id: str
    role: str
    description: str
    base_complexity: int = 50
    max_history: int = 20
    pinned_model: ModelConfig | None = None  # bypasses tier selection when set
    enable_filesystem: bool = False          # give agent read/write/run tools
    sandbox: "Sandbox | None" = None        # shared sandbox for this run
    suppress_context_guidelines: bool = False  # omit generic context-sharing hints from system prompt

    def to_dict(self) -> dict:
        d: dict = {
            "node_id": self.node_id,
            "role": self.role,
            "description": self.description,
            "base_complexity": self.base_complexity,
            "max_history": self.max_history,
            "enable_filesystem": self.enable_filesystem,
            "suppress_context_guidelines": self.suppress_context_guidelines,
        }
        if self.pinned_model is not None:
            d["pinned_model"] = {
                "tier": self.pinned_model.tier.value,
                "model_id": self.pinned_model.model_id,
                "provider": self.pinned_model.provider,
            }
        # sandbox is reconstructed from root path in subprocess
        if self.sandbox is not None:
            d["sandbox_root"] = str(self.sandbox.root)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> AgentConfig:
        pinned = None
        if "pinned_model" in d:
            from ..llm.types import ModelConfig, ModelTier
            pm = d["pinned_model"]
            pinned = ModelConfig(
                tier=ModelTier(pm["tier"]),
                model_id=pm["model_id"],
                provider=pm["provider"],
            )
        sandbox = None
        if "sandbox_root" in d:
            from ..sandbox.sandbox import Sandbox
            sandbox = Sandbox(d["sandbox_root"])
        return cls(
            node_id=d["node_id"],
            role=d["role"],
            description=d["description"],
            base_complexity=d.get("base_complexity", 50),
            max_history=d.get("max_history", 20),
            pinned_model=pinned,
            enable_filesystem=d.get("enable_filesystem", False),
            sandbox=sandbox,
            suppress_context_guidelines=d.get("suppress_context_guidelines", False),
        )


@dataclass
class TopologyContext:
    topology_id: str
    topology_label: str
    node_id: str
    role: str
    direct_neighbors: dict[str, str]
    graph_edges: list[tuple[str, str]]
    node_roles: dict[str, str]
    workflow_steps: list[str]
    completion_rules: list[str]

    def to_dict(self) -> dict:
        return {
            "topology_id": self.topology_id,
            "topology_label": self.topology_label,
            "node_id": self.node_id,
            "role": self.role,
            "direct_neighbors": self.direct_neighbors,
            "graph_edges": [list(e) for e in self.graph_edges],
            "node_roles": self.node_roles,
            "workflow_steps": self.workflow_steps,
            "completion_rules": self.completion_rules,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TopologyContext:
        return cls(
            topology_id=d["topology_id"],
            topology_label=d["topology_label"],
            node_id=d["node_id"],
            role=d["role"],
            direct_neighbors=d["direct_neighbors"],
            graph_edges=[tuple(e) for e in d["graph_edges"]],
            node_roles=d["node_roles"],
            workflow_steps=d["workflow_steps"],
            completion_rules=d["completion_rules"],
        )
