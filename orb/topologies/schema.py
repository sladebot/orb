from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class OrbSchemaModel(BaseModel):
    model_config = {"protected_namespaces": ()}


class ClusterSchema(OrbSchemaModel):
    """A named group of agents that share a SubgraphStore."""

    agents: list[str]
    store_backend: str = "chroma"
    store_kwargs: dict = Field(default_factory=dict)


class BridgeSchema(OrbSchemaModel):
    """Configuration for a BridgeAgent that merges cluster stores."""

    interval_s: float = 30.0
    source_clusters: list[str]
    target_cluster: str
    conflict_resolution: str = "last_write_wins"


class ModelSelectionSchema(OrbSchemaModel):
    """Optional model selection hints for multi-provider scenarios."""

    prefer_provider: str | None = None
    prefer_different_provider_than: str | None = None
    fallback_providers: list[str] = Field(default_factory=list)


class AgentSchema(OrbSchemaModel):
    """Schema for a single agent definition within a topology."""

    role: str
    description: str
    category: str = "worker"
    position_label: str | None = None
    base_complexity: int = 50
    max_history: int = 20
    enable_filesystem: bool = False
    suppress_context_guidelines: bool = False
    model_selection: ModelSelectionSchema | None = None


class GraphViewSchema(OrbSchemaModel):
    """Graph visualization hints for the TUI/dashboard."""

    rows: list[list[dict[str, Any]]]
    order: list[str]


class SelectionHintsSchema(OrbSchemaModel):
    """Optional hints used by daemon-side topology recommendation."""

    ideal_for: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    min_complexity: int = 0
    max_complexity: int = 100


class TopologySchema(OrbSchemaModel):
    """Schema for a complete topology definition."""

    id: str
    label: str
    description: str
    entry_agent: str
    synthesis_agent: str | None = None
    agents: dict[str, AgentSchema]
    edges: list[tuple[str, str] | list[str]]
    workflow_steps: list[str]
    completion_rules: dict[str, list[str]]
    graph_view: GraphViewSchema | None = None
    selection_hints: SelectionHintsSchema | None = None
    clusters: dict[str, ClusterSchema] = Field(default_factory=dict)
    bridge: BridgeSchema | None = None

    @field_validator("edges", mode="before")
    @classmethod
    def normalize_edges(cls, v: Any) -> list[tuple[str, str]]:
        return [tuple(e) if isinstance(e, list) else e for e in v]

    @model_validator(mode="after")
    def validate_references(self) -> "TopologySchema":
        agent_ids = set(self.agents.keys())

        if self.entry_agent not in agent_ids:
            raise ValueError(
                f"entry_agent '{self.entry_agent}' not found in agents: "
                f"{sorted(agent_ids)}"
            )

        if self.synthesis_agent and self.synthesis_agent not in agent_ids:
            raise ValueError(
                f"synthesis_agent '{self.synthesis_agent}' not found in agents: "
                f"{sorted(agent_ids)}"
            )

        for a, b in self.edges:
            if a not in agent_ids:
                raise ValueError(
                    f"Edge references unknown agent '{a}'. "
                    f"Known agents: {sorted(agent_ids)}"
                )
            if b not in agent_ids:
                raise ValueError(
                    f"Edge references unknown agent '{b}'. "
                    f"Known agents: {sorted(agent_ids)}"
                )

        for agent_id in self.completion_rules:
            if agent_id not in agent_ids:
                raise ValueError(
                    f"completion_rules references unknown agent '{agent_id}'. "
                    f"Known agents: {sorted(agent_ids)}"
                )

        # Validate cluster agent references
        for cluster_name, cluster in self.clusters.items():
            for agent_id in cluster.agents:
                if agent_id not in agent_ids:
                    raise ValueError(
                        f"Cluster '{cluster_name}' references unknown agent '{agent_id}'. "
                        f"Known agents: {sorted(agent_ids)}"
                    )

        # Validate bridge references
        if self.bridge is not None:
            cluster_names = set(self.clusters.keys())
            for src in self.bridge.source_clusters:
                if src not in cluster_names:
                    raise ValueError(
                        f"bridge.source_clusters references unknown cluster '{src}'. "
                        f"Known clusters: {sorted(cluster_names)}"
                    )
            if self.bridge.target_cluster not in cluster_names:
                raise ValueError(
                    f"bridge.target_cluster references unknown cluster "
                    f"'{self.bridge.target_cluster}'. "
                    f"Known clusters: {sorted(cluster_names)}"
                )

        return self


class TopologiesFileSchema(OrbSchemaModel):
    """Root schema for a topologies YAML file."""

    version: str = "1.0"
    topologies: dict[str, TopologySchema]

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        supported = ("1.0",)
        if v not in supported:
            raise ValueError(
                f"Unsupported schema version '{v}'. Supported: {supported}"
            )
        return v
