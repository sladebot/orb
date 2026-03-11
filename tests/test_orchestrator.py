import pytest

from orb.llm.types import ModelTier, ModelConfig, CompletionResponse, ToolCall
from orb.orchestrator.types import OrchestratorConfig
from orb.topologies.triangle import create_triangle
from tests.test_claude_agent import MockLLMClient


class TestOrchestrator:
    async def test_basic_run_completes(self):
        """All agents immediately call complete_task."""
        mock = MockLLMClient([
            # Each agent will get one message and complete
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t1", name="complete_task", input={"result": "Done"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t2", name="complete_task", input={"result": "Done"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t3", name="complete_task", input={"result": "Done"})],
            ),
        ])

        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        # First agent (coder) completes, but reviewer/tester need messages too
        # Actually coder sends to reviewer and tester, then they complete
        mock_with_flow = MockLLMClient([
            # Coder: sends to reviewer and tester, then completes
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(id="t1", name="send_message", input={"to": "reviewer", "content": "code here"}),
                    ToolCall(id="t2", name="send_message", input={"to": "tester", "content": "test this"}),
                    ToolCall(id="t3", name="complete_task", input={"result": "Code written"}),
                ],
            ),
            # Reviewer completes
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t4", name="complete_task", input={"result": "Reviewed"})],
            ),
            # Tester completes
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t5", name="complete_task", input={"result": "Tested"})],
            ),
        ])

        config = OrchestratorConfig(timeout=5.0, budget=50)
        orchestrator = create_triangle(
            providers={"mock": mock_with_flow},
            config=config,
            model_overrides=overrides,
            trace=False,
        )

        result = await orchestrator.run("Write hello world")

        assert result.success
        assert len(result.completions) == 4
        assert "coordinator" in result.completions
        assert not result.timed_out

    async def test_timeout(self):
        """Agents never complete — should timeout."""
        mock = MockLLMClient([
            # Coder just responds with text, no tool calls — will idle
            CompletionResponse(content="Thinking...", model="mock"),
        ])

        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        config = OrchestratorConfig(timeout=0.5, budget=50)
        orchestrator = create_triangle(
            providers={"mock": mock},
            config=config,
            model_overrides=overrides,
            trace=False,
        )

        result = await orchestrator.run("Write hello world")

        assert result.timed_out

    async def test_entry_agent_not_found(self):
        mock = MockLLMClient()
        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        config = OrchestratorConfig(entry_agent="nonexistent")
        orchestrator = create_triangle(
            providers={"mock": mock},
            config=config,
            model_overrides=overrides,
            trace=False,
        )

        result = await orchestrator.run("test")
        assert not result.success
        assert "not found" in result.error
