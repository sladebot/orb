from unittest.mock import AsyncMock, MagicMock

import pytest

from orb.llm.types import ModelConfig, ModelTier
from orb.topologies.factory import create_orchestrator


def _mock_providers():
    mock = MagicMock()
    mock.chat = AsyncMock(return_value=MagicMock(content="ok", tool_calls=[]))
    return {"mock": mock}


class TestCreateOrchestrator:
    def test_triad_agents(self):
        orch = create_orchestrator(
            "triad",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        assert set(orch.agents.keys()) == {
            "coordinator", "coder", "reviewer", "tester"
        }

    def test_triad_graph_edges(self):
        orch = create_orchestrator(
            "triad",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        graph = orch.bus.graph
        assert graph.has_edge("coordinator", "coder")
        assert graph.has_edge("coder", "reviewer")
        assert graph.has_edge("coder", "tester")
        assert graph.has_edge("reviewer", "tester")

    def test_triad_entry_agent(self):
        orch = create_orchestrator(
            "triad",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        assert orch.config.entry_agent == "coordinator"

    def test_dual_review_agents(self):
        orch = create_orchestrator(
            "dual-review",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        assert set(orch.agents.keys()) == {
            "coordinator", "coder", "reviewer_a", "reviewer_b", "tester"
        }

    def test_dual_review_graph_edges(self):
        orch = create_orchestrator(
            "dual-review",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        graph = orch.bus.graph
        assert graph.has_edge("coder", "reviewer_a")
        assert graph.has_edge("coder", "reviewer_b")
        assert graph.has_edge("reviewer_a", "reviewer_b")

    def test_hierarchy_agents(self):
        orch = create_orchestrator(
            "hierarchy",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        assert set(orch.agents.keys()) == {
            "coordinator", "researcher", "coder", "reviewer", "tester"
        }

    def test_hierarchy_graph_edges(self):
        orch = create_orchestrator(
            "hierarchy",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        graph = orch.bus.graph
        assert graph.has_edge("coordinator", "researcher")
        assert graph.has_edge("researcher", "coder")
        assert graph.has_edge("coder", "reviewer")
        assert graph.has_edge("coder", "tester")

    def test_unknown_topology_raises(self):
        with pytest.raises(ValueError, match="Unknown topology"):
            create_orchestrator("nonexistent", _mock_providers())

    def test_agents_have_system_prompt(self):
        orch = create_orchestrator(
            "triad",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        for agent in orch.agents.values():
            assert agent._system_prompt

    def test_agents_have_tools(self):
        orch = create_orchestrator(
            "triad",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        for agent in orch.agents.values():
            tool_names = {tool["name"] for tool in agent._tools}
            assert "send_message" in tool_names
            assert "complete_task" in tool_names

    def test_filesystem_agents_have_file_tools(self):
        orch = create_orchestrator(
            "triad",
            _mock_providers(),
            model_overrides={t: ModelConfig(ModelTier.LOCAL_SMALL, "mock", "mock") for t in ModelTier},
            trace=False,
        )
        coder_tools = {t["name"] for t in orch.agents["coder"]._tools}
        assert "write_file" in coder_tools
        assert "read_file" in coder_tools

        coord_tools = {t["name"] for t in orch.agents["coordinator"]._tools}
        assert "write_file" not in coord_tools
