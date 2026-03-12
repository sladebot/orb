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

        # Verify coordinator + 3 worker agents
        assert len(orchestrator.agents) == 4
        assert "coordinator" in orchestrator.agents
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
            tool_names = {tool["name"] for tool in agent._tools}  # noqa: SLF001
            assert "send_message" in tool_names
            assert "complete_task" in tool_names

        coder_prompt = orchestrator.agents["coder"]._system_prompt  # noqa: SLF001
        assert "Runtime Topology Context" in coder_prompt
        assert "Triad" in coder_prompt
        assert "reviewer" in coder_prompt
        assert "tester" in coder_prompt
