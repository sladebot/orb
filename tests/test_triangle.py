from orb.llm.types import ModelTier, ModelConfig
from orb.topologies.triangle import create_triangle

# Reuse mock from test_claude_agent
from tests.test_claude_agent import MockLLMClient


class TestTriangle:
    def test_triangle_topology(self):
        mock = MockLLMClient()
        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        orchestrator = create_triangle(
            providers={"mock": mock},
            model_overrides=overrides,
            trace=False,
        )

        # Verify 3 agents
        assert len(orchestrator.agents) == 3
        assert "coder" in orchestrator.agents
        assert "reviewer" in orchestrator.agents
        assert "tester" in orchestrator.agents

        # Verify fully connected
        graph = orchestrator.bus.graph
        assert graph.has_edge("coder", "reviewer")
        assert graph.has_edge("coder", "tester")
        assert graph.has_edge("reviewer", "tester")

    def test_triangle_agents_initialized(self):
        mock = MockLLMClient()
        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        orchestrator = create_triangle(
            providers={"mock": mock},
            model_overrides=overrides,
            trace=False,
        )

        # Verify agents have tools and system prompts
        for agent in orchestrator.agents.values():
            assert agent._system_prompt  # noqa: SLF001
            assert len(agent._tools) == 2  # noqa: SLF001
