"""Tests for TopologySchema GraphRAG extensions (clusters + bridge)
and GraphRAGConfig helper.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from orb.topologies.schema import TopologiesFileSchema, TopologySchema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_topology_data(**overrides) -> dict:
    """Return a minimal valid topology data dict with optional overrides applied."""
    base = {
        "id": "test-graphrag",
        "label": "Test GraphRAG",
        "description": "Test topology with clusters",
        "entry_agent": "agent1",
        "agents": {
            "agent1": {"role": "Worker", "description": "Does work"},
            "agent2": {"role": "Reviewer", "description": "Reviews work"},
            "agent3": {"role": "Tester", "description": "Tests work"},
        },
        "edges": [],
        "workflow_steps": ["Step 1"],
        "completion_rules": {},
    }
    base.update(overrides)
    return base


def _wrap(topo_data: dict) -> dict:
    return {"version": "1.0", "topologies": {"test-graphrag": topo_data}}


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestTopologySchemaGraphRAG:

    def test_topology_with_clusters_valid(self):
        """Parse YAML with clusters block — no validation errors."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {
                    "agents": ["agent1", "agent2"],
                    "store_backend": "chroma",
                },
                "cluster_b": {
                    "agents": ["agent3"],
                    "store_backend": "chroma",
                },
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]
        assert "cluster_a" in topo.clusters
        assert "cluster_b" in topo.clusters
        assert topo.clusters["cluster_a"].agents == ["agent1", "agent2"]
        assert topo.clusters["cluster_b"].store_backend == "chroma"

    def test_topology_cluster_agent_not_in_agents_raises(self):
        """Cluster references an agent not in topology.agents → ValidationError."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {
                    "agents": ["agent1", "ghost_agent"],
                    "store_backend": "chroma",
                },
            }
        ))
        with pytest.raises((ValueError, ValidationError), match="ghost_agent"):
            TopologiesFileSchema.model_validate(data)

    def test_topology_bridge_source_not_in_clusters_raises(self):
        """bridge.source_clusters references an unknown cluster → ValidationError."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
            },
            bridge={
                "interval_s": 30.0,
                "source_clusters": ["cluster_a", "nonexistent_cluster"],
                "target_cluster": "cluster_a",
                "conflict_resolution": "last_write_wins",
            }
        ))
        with pytest.raises((ValueError, ValidationError), match="nonexistent_cluster"):
            TopologiesFileSchema.model_validate(data)

    def test_topology_bridge_target_not_in_clusters_raises(self):
        """bridge.target_cluster references an unknown cluster → ValidationError."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
                "cluster_b": {"agents": ["agent2"], "store_backend": "chroma"},
            },
            bridge={
                "interval_s": 30.0,
                "source_clusters": ["cluster_a"],
                "target_cluster": "missing_bridge_store",
                "conflict_resolution": "last_write_wins",
            }
        ))
        with pytest.raises((ValueError, ValidationError), match="missing_bridge_store"):
            TopologiesFileSchema.model_validate(data)

    def test_topology_no_clusters_defaults_empty(self):
        """Topology without clusters key → schema.clusters == {}."""
        data = _wrap(_minimal_topology_data())
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]
        assert topo.clusters == {}
        assert topo.bridge is None

    def test_topology_with_bridge_valid(self):
        """Topology with valid clusters + bridge parses without errors."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
                "cluster_b": {"agents": ["agent2"], "store_backend": "chroma"},
                "bridge_store": {"agents": ["agent3"], "store_backend": "chroma"},
            },
            bridge={
                "interval_s": 15.0,
                "source_clusters": ["cluster_a", "cluster_b"],
                "target_cluster": "bridge_store",
                "conflict_resolution": "last_write_wins",
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]
        assert topo.bridge is not None
        assert topo.bridge.interval_s == 15.0
        assert topo.bridge.source_clusters == ["cluster_a", "cluster_b"]
        assert topo.bridge.target_cluster == "bridge_store"

    def test_cluster_schema_defaults(self):
        """ClusterSchema defaults: store_backend='chroma', store_kwargs={}."""
        from orb.topologies.schema import ClusterSchema
        c = ClusterSchema(agents=["a", "b"])
        assert c.store_backend == "chroma"
        assert c.store_kwargs == {}

    def test_bridge_schema_defaults(self):
        """BridgeSchema defaults: interval_s=30.0, conflict_resolution='last_write_wins'."""
        from orb.topologies.schema import BridgeSchema
        b = BridgeSchema(source_clusters=["c1"], target_cluster="c2")
        assert b.interval_s == 30.0
        assert b.conflict_resolution == "last_write_wins"


# ---------------------------------------------------------------------------
# GraphRAGConfig tests
# ---------------------------------------------------------------------------

class TestGraphRAGConfig:

    def test_graphrag_config_from_empty_topology(self):
        """Topology with no clusters → GraphRAGConfig with empty maps, bridge_agent=None."""
        data = _wrap(_minimal_topology_data())
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        config = GraphRAGConfig.from_topology(topo)

        assert config.cluster_stores == {}
        assert config.agent_cluster_map == {}
        assert config.bridge_agent is None

    def test_graphrag_config_from_topology_with_clusters(self, tmp_path):
        """Topology with 2 clusters, 3 agents total → cluster_stores has 2 entries,
        agent_cluster_map maps all 3 agents correctly."""
        data = _wrap(_minimal_topology_data(
            persist_base=str(tmp_path),
            clusters={
                "cluster_a": {"agents": ["agent1", "agent2"], "store_backend": "chroma"},
                "cluster_b": {"agents": ["agent3"], "store_backend": "chroma"},
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        config = GraphRAGConfig.from_topology(topo)

        assert len(config.cluster_stores) == 2
        assert "cluster_a" in config.cluster_stores
        assert "cluster_b" in config.cluster_stores

        assert config.agent_cluster_map["agent1"] == "cluster_a"
        assert config.agent_cluster_map["agent2"] == "cluster_a"
        assert config.agent_cluster_map["agent3"] == "cluster_b"
        assert config.bridge_agent is None

    def test_graphrag_config_with_bridge(self, tmp_path):
        """Topology with 2 clusters + bridge → bridge_agent is not None,
        source_stores are correct SubgraphStore instances."""
        data = _wrap(_minimal_topology_data(
            persist_base=str(tmp_path),
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
                "cluster_b": {"agents": ["agent2"], "store_backend": "chroma"},
                "bridge_store": {"agents": ["agent3"], "store_backend": "chroma"},
            },
            bridge={
                "interval_s": 10.0,
                "source_clusters": ["cluster_a", "cluster_b"],
                "target_cluster": "bridge_store",
                "conflict_resolution": "last_write_wins",
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.agent.bridge_agent import BridgeAgent
        from orb.memory.subgraph_store import SubgraphStore

        config = GraphRAGConfig.from_topology(topo)

        assert config.bridge_agent is not None
        assert isinstance(config.bridge_agent, BridgeAgent)

        # All cluster stores should be SubgraphStore instances
        for name, store in config.cluster_stores.items():
            assert isinstance(store, SubgraphStore), f"Store '{name}' is not a SubgraphStore"

        # The bridge agent's source stores are the cluster_a and cluster_b stores
        assert config.bridge_agent._bridge_store is config.cluster_stores["bridge_store"]
        # Source stores correspond to cluster_a and cluster_b
        source_store_ids = {id(s) for s in config.bridge_agent._source_stores}
        expected_ids = {
            id(config.cluster_stores["cluster_a"]),
            id(config.cluster_stores["cluster_b"]),
        }
        assert source_store_ids == expected_ids

    def test_graphrag_config_many_agents_cluster_mapping(self, tmp_path):
        """Two clusters with 3 agents each → agent_cluster_map maps all 6 correctly."""
        agents = {f"agent{i}": {"role": f"Agent{i}", "description": "D"} for i in range(1, 7)}
        topo_data = _minimal_topology_data(persist_base=str(tmp_path))
        topo_data["agents"] = agents
        topo_data["clusters"] = {
            "cluster_x": {
                "agents": ["agent1", "agent2", "agent3"],
                "store_backend": "chroma",
            },
            "cluster_y": {
                "agents": ["agent4", "agent5", "agent6"],
                "store_backend": "chroma",
            },
        }
        schema = TopologiesFileSchema.model_validate(_wrap(topo_data))
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        config = GraphRAGConfig.from_topology(topo)

        assert len(config.cluster_stores) == 2
        for i in range(1, 4):
            assert config.agent_cluster_map[f"agent{i}"] == "cluster_x"
        for i in range(4, 7):
            assert config.agent_cluster_map[f"agent{i}"] == "cluster_y"


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------

class TestGraphRAGPersistence:

    def test_cluster_persist_path_field(self):
        """ClusterSchema accepts persist_path as a first-class field."""
        from orb.topologies.schema import ClusterSchema
        cluster = ClusterSchema(agents=["a1"], persist_path="/tmp/test-chroma")
        assert cluster.persist_path == "/tmp/test-chroma"

    def test_cluster_persist_path_defaults_none(self):
        """ClusterSchema.persist_path defaults to None (ephemeral)."""
        from orb.topologies.schema import ClusterSchema
        cluster = ClusterSchema(agents=["a1"])
        assert cluster.persist_path is None

    def test_topology_persist_base_field(self):
        """TopologySchema accepts persist_base as a first-class field."""
        data = _wrap(_minimal_topology_data(persist_base="~/.orb/chroma"))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]
        assert topo.persist_base == "~/.orb/chroma"

    def test_topology_persist_base_defaults_none(self):
        """TopologySchema.persist_base defaults to None."""
        data = _wrap(_minimal_topology_data())
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]
        assert topo.persist_base is None

    def test_from_topology_uses_cluster_persist_path(self, tmp_path):
        """GraphRAGConfig.from_topology passes cluster persist_path to the store."""
        persist_dir = str(tmp_path / "chroma")
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {
                    "agents": ["agent1"],
                    "store_backend": "chroma",
                    "persist_path": persist_dir,
                },
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo)
        store = config.cluster_stores["cluster_a"]
        assert isinstance(store, ChromaDBNetworkXStore)
        assert store._persist_path == persist_dir

    def test_from_topology_uses_persist_base_for_clusters(self, tmp_path):
        """persist_base on topology auto-sets per-cluster paths as <base>/<cluster_name>."""
        base = str(tmp_path / "chroma")
        data = _wrap(_minimal_topology_data(
            persist_base=base,
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
                "cluster_b": {"agents": ["agent2"], "store_backend": "chroma"},
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo)
        for name in ("cluster_a", "cluster_b"):
            store = config.cluster_stores[name]
            assert isinstance(store, ChromaDBNetworkXStore)
            assert store._persist_path == f"{base}/{name}"

    def test_from_topology_cluster_persist_path_overrides_base(self, tmp_path):
        """Explicit cluster persist_path takes precedence over topology persist_base."""
        base = str(tmp_path / "base")
        override = str(tmp_path / "override")
        data = _wrap(_minimal_topology_data(
            persist_base=base,
            clusters={
                "cluster_a": {
                    "agents": ["agent1"],
                    "store_backend": "chroma",
                    "persist_path": override,
                },
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo)
        store = config.cluster_stores["cluster_a"]
        assert store._persist_path == override

    def test_from_topology_tilde_expanded_in_persist_path(self, tmp_path):
        """~ in persist_path is expanded to the home directory."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {
                    "agents": ["agent1"],
                    "store_backend": "chroma",
                    "persist_path": "~/.orb/chroma",
                },
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore
        import os

        config = GraphRAGConfig.from_topology(topo)
        store = config.cluster_stores["cluster_a"]
        assert store._persist_path == os.path.expanduser("~/.orb/chroma")

    def test_no_persist_path_gives_ephemeral_store(self):
        """auto_persist=False with no persist_path → store._persist_path is None (ephemeral)."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo, auto_persist=False)
        store = config.cluster_stores["cluster_a"]
        assert isinstance(store, ChromaDBNetworkXStore)
        assert store._persist_path is None

    def test_auto_default_persist_path_uses_topology_id(self, tmp_path, monkeypatch):
        """When no persist_base or persist_path set, default is ~/.orb/chroma/<topology_id>/<cluster>."""
        import os
        # Redirect home so we don't write to real ~/.orb during tests
        monkeypatch.setenv("HOME", str(tmp_path))

        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo, auto_persist=True)
        store = config.cluster_stores["cluster_a"]
        assert isinstance(store, ChromaDBNetworkXStore)
        expected = os.path.join(str(tmp_path), ".orb", "chroma", topo.id, "cluster_a")
        assert store._persist_path == expected

    def test_auto_persist_false_gives_ephemeral(self):
        """auto_persist=False → stores remain ephemeral even without explicit persist_path."""
        data = _wrap(_minimal_topology_data(
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo, auto_persist=False)
        store = config.cluster_stores["cluster_a"]
        assert store._persist_path is None

    def test_explicit_persist_base_overrides_auto_default(self, tmp_path):
        """Explicit persist_base on topology takes precedence over auto-default."""
        base = str(tmp_path / "explicit")
        data = _wrap(_minimal_topology_data(
            persist_base=base,
            clusters={
                "cluster_a": {"agents": ["agent1"], "store_backend": "chroma"},
            }
        ))
        schema = TopologiesFileSchema.model_validate(data)
        topo = schema.topologies["test-graphrag"]

        from orb.memory.graphrag_config import GraphRAGConfig
        from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

        config = GraphRAGConfig.from_topology(topo, auto_persist=True)
        store = config.cluster_stores["cluster_a"]
        assert store._persist_path == f"{base}/cluster_a"
