"""GraphRAGConfig — runtime GraphRAG configuration resolved from a TopologySchema."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orb.agent.bridge_agent import BridgeAgent
    from orb.memory.subgraph_store import SubgraphStore
    from orb.topologies.schema import TopologySchema


@dataclass
class GraphRAGConfig:
    """Runtime GraphRAG configuration resolved from a TopologySchema.

    Attributes
    ----------
    cluster_stores:
        Mapping from cluster name to the SubgraphStore instance for that cluster.
    agent_cluster_map:
        Mapping from agent_id to the name of the cluster it belongs to.
    bridge_agent:
        A BridgeAgent instance if the topology defines a bridge, otherwise None.
    """

    cluster_stores: dict[str, "SubgraphStore"] = field(default_factory=dict)
    agent_cluster_map: dict[str, str] = field(default_factory=dict)
    bridge_agent: "BridgeAgent | None" = None

    @classmethod
    def from_topology(
        cls,
        topology: "TopologySchema",
        auto_persist: bool = True,
    ) -> "GraphRAGConfig":
        """Build SubgraphStores and BridgeAgent from topology clusters/bridge config.

        Parameters
        ----------
        topology:
            The parsed topology definition.
        auto_persist:
            When True (default) and no ``persist_base`` or cluster ``persist_path``
            is set, stores default to ``~/.orb/chroma/<topology.id>/<cluster_name>``
            so facts survive between sessions (Option B behaviour).
            Set to False to force ephemeral stores regardless of topology config.
        """
        if not topology.clusters:
            return cls(cluster_stores={}, agent_cluster_map={}, bridge_agent=None)

        from orb.memory.factory import SubgraphStoreFactory

        # 1. Build one SubgraphStore per cluster
        cluster_stores: dict[str, SubgraphStore] = {}
        for cluster_name, cluster_schema in topology.clusters.items():
            # Resolve persist_path priority:
            #   1. Explicit cluster persist_path
            #   2. Topology persist_base + cluster_name
            #   3. Auto-default: ~/.orb/chroma/<topology.id>/<cluster_name> (if auto_persist)
            #   4. None → ephemeral
            persist_path: str | None = cluster_schema.persist_path
            if persist_path is None and topology.persist_base:
                persist_path = f"{topology.persist_base}/{cluster_name}"
            if persist_path is None and auto_persist:
                persist_path = f"~/.orb/chroma/{topology.id}/{cluster_name}"
            if persist_path:
                persist_path = os.path.expanduser(persist_path)

            cluster_stores[cluster_name] = SubgraphStoreFactory.from_config(
                cluster_schema.store_backend,
                persist_path=persist_path,
                **cluster_schema.store_kwargs,
            )

        # 2. Build agent → cluster map by inverting the cluster.agents lists
        agent_cluster_map: dict[str, str] = {}
        for cluster_name, cluster_schema in topology.clusters.items():
            for agent_id in cluster_schema.agents:
                agent_cluster_map[agent_id] = cluster_name

        # 3. Build BridgeAgent if bridge config is present
        bridge_agent: BridgeAgent | None = None
        if topology.bridge is not None:
            from orb.agent.bridge_agent import BridgeAgent

            source_stores = [
                cluster_stores[c] for c in topology.bridge.source_clusters
            ]
            bridge_store = cluster_stores[topology.bridge.target_cluster]
            bridge_agent = BridgeAgent(
                source_stores=source_stores,
                bridge_store=bridge_store,
                interval_s=topology.bridge.interval_s,
                conflict_resolution=topology.bridge.conflict_resolution,  # type: ignore[arg-type]
            )

        return cls(
            cluster_stores=cluster_stores,
            agent_cluster_map=agent_cluster_map,
            bridge_agent=bridge_agent,
        )
