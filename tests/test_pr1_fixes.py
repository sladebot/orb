import pytest

from orb.topologies.schema import AgentSchema, TopologiesFileSchema


class TestPositionHint:
    def test_position_hint_defaults_to_none(self):
        agent = AgentSchema(role="Worker", description="Does work")
        assert agent.position_hint is None

    def test_position_hint_set_in_schema(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "Test",
                    "description": "A test",
                    "entry_agent": "a",
                    "agents": {
                        "a": {
                            "role": "Architect",
                            "description": "Designs systems",
                            "position_hint": "implementation hub",
                        },
                    },
                    "edges": [],
                    "workflow_steps": [],
                    "completion_rules": {},
                }
            },
        }
        schema = TopologiesFileSchema.model_validate(data)
        assert schema.topologies["test"].agents["a"].position_hint == "implementation hub"


class TestGraphViewInBuiltins:
    def test_triangle_has_graph_view(self):
        from orb.topologies.loader import TopologyLoader

        loader = TopologyLoader()
        loader.load()
        topo = loader.get("triangle")
        assert topo.graph_view is not None
        assert len(topo.graph_view.rows) > 0
        assert "coordinator" in topo.graph_view.order
        assert "coder" in topo.graph_view.order

    def test_dual_review_has_graph_view(self):
        from orb.topologies.loader import TopologyLoader

        loader = TopologyLoader()
        loader.load()
        topo = loader.get("dual-review")
        assert topo.graph_view is not None
        assert len(topo.graph_view.rows) > 0
        assert "reviewer_a" in topo.graph_view.order
        assert "reviewer_b" in topo.graph_view.order


class TestTopologyMetaUsesPositionHint:
    def test_position_hint_in_triangle_builtins(self):
        from orb.topologies.loader import TopologyLoader

        loader = TopologyLoader()
        loader.load()
        topo = loader.get("triangle")
        assert topo.agents["coordinator"].position_hint == "entry router"
        assert topo.agents["coder"].position_hint == "implementation hub"
        assert topo.agents["reviewer"].position_hint == "quality edge"
        assert topo.agents["tester"].position_hint == "validation edge"

    def test_custom_agent_uses_position_hint(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        user_yaml = """\
version: "1.0"
topologies:
  custom:
    id: custom
    label: Custom
    description: A custom topology
    entry_agent: planner
    agents:
      planner:
        role: Planner
        description: Plans work
        position_hint: orchestration hub
      architect:
        role: Architect
        description: Designs systems
        position_hint: design node
    edges:
      - [planner, architect]
    workflow_steps: []
    completion_rules: {}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(user_yaml)
            tmp = Path(f.name)

        try:
            with patch("orb.topologies.loader.USER_TOPOLOGIES_PATH", tmp):
                from orb.topologies.loader import TopologyLoader as TL
                loader = TL()
                loader.load()
                topo = loader.get("custom")
                assert topo.agents["planner"].position_hint == "orchestration hub"
                assert topo.agents["architect"].position_hint == "design node"
        finally:
            tmp.unlink()

    def test_agent_without_hint_falls_back_to_role(self):
        from orb.topologies.schema import AgentSchema

        agent = AgentSchema(role="Architect", description="D")
        assert agent.position_hint is None


class TestSingletonReset:
    def test_loader_reset(self):
        from orb.topologies.loader import get_loader, reset_loader

        loader1 = get_loader()
        reset_loader()
        loader2 = get_loader()
        assert loader1 is not loader2

    def test_watcher_reset(self):
        from orb.topologies.watcher import get_watcher, reset_watcher

        watcher1 = get_watcher()
        reset_watcher()
        watcher2 = get_watcher()
        assert watcher1 is not watcher2


class TestResolveModelSelectionNoHardcode:
    def test_resolve_uses_config_maps(self):
        """_resolve_model_selection should derive models from llm/types.py
        config maps, not hardcoded strings."""
        from unittest.mock import MagicMock

        from orb.llm.types import DEFAULT_MODELS, ModelTier
        from orb.topologies.factory import _resolve_model_selection
        from orb.topologies.schema import ModelSelectionSchema

        selection = ModelSelectionSchema(prefer_provider="anthropic")
        providers = {"anthropic": MagicMock()}
        result = _resolve_model_selection(selection, providers, {})
        assert result is not None
        assert result.provider == "anthropic"
        expected = DEFAULT_MODELS[ModelTier.CLOUD_STRONG]
        assert result.model_id == expected.model_id
