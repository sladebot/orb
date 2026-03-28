import pytest

from orb.llm.types import ModelTier, ModelConfig, CompletionResponse, ToolCall
from orb.orchestrator.types import OrchestratorConfig
from orb.runtime.execution_controller import DefaultExecutionController
from orb.topologies import create_orchestrator
from orb.tracing.run_trace import TraceEventKind
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

        mock_with_flow = MockLLMClient([
            # Coordinator: routes to coder, then completes routing duties
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(id="t0", name="send_message", input={"to": "coder", "content": "Write hello world"}),
                    ToolCall(id="t0b", name="complete_task", input={"result": "Task routed to coder"}),
                ],
            ),
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
        orchestrator = create_orchestrator(
            "triad",
            providers={"mock": mock_with_flow},
            config=config,
            model_overrides=overrides,
            trace=False,
        )

        result = await orchestrator.run("Write hello world")

        assert result.success
        assert len(result.completions) == 4
        assert result.completions["coordinator"] == "Task routed to coder"
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
        orchestrator = create_orchestrator(
            "triad",
            providers={"mock": mock},
            config=config,
            model_overrides=overrides,
            trace=True,
        )

        result = await orchestrator.run("Write hello world")

        assert not result.success
        assert result.timed_out
        assert result.error == "timeout"
        assert orchestrator.trace is not None
        final_outcome = next(
            event
            for event in reversed(orchestrator.trace.events)
            if event.kind == TraceEventKind.FINAL_OUTCOME
        )
        assert final_outcome.data["controller_action"] == "timeout"
        assert final_outcome.data["controller_source"] == "timeout"
        assert final_outcome.data["timed_out"] is True

    async def test_budget_exhaustion(self):
        """A routed message budget of zero should fail with a controller outcome."""
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="send_message",
                        input={"to": "coder", "content": "Write hello world"},
                    ),
                ],
            ),
        ])

        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        config = OrchestratorConfig(timeout=2.0, budget=0)
        orchestrator = create_orchestrator(
            "triad",
            providers={"mock": mock},
            config=config,
            model_overrides=overrides,
            trace=True,
        )

        result = await orchestrator.run("Write hello world")

        assert not result.success
        assert not result.timed_out
        assert result.error == "budget_exhausted"
        assert result.message_count == 0
        assert orchestrator.trace is not None
        final_outcome = next(
            event
            for event in reversed(orchestrator.trace.events)
            if event.kind == TraceEventKind.FINAL_OUTCOME
        )
        assert final_outcome.data["controller_action"] == "budget_exhausted"
        assert final_outcome.data["controller_source"] == "route_failure"
        assert final_outcome.data["controller_applied"] is True
        assert final_outcome.data["message_count"] == 0

    async def test_stop_early_controller_finishes_after_first_completion(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t1", name="complete_task", input={"result": "done early"})],
            ),
        ])

        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}
        orchestrator = create_orchestrator(
            "triad",
            providers={"mock": mock},
            config=OrchestratorConfig(timeout=2.0, budget=10),
            model_overrides=overrides,
            trace=True,
            execution_controller=DefaultExecutionController(),
            controller_context={
                "query": "small task",
                "stop_early_allowed": True,
                "stop_early_reason": "First satisfactory completion is enough.",
            },
        )

        result = await orchestrator.run("small task", entry_agent="coordinator")

        assert result.success
        assert not result.timed_out
        assert result.error is None
        assert orchestrator.trace is not None
        final_outcome = next(
            event
            for event in reversed(orchestrator.trace.events)
            if event.kind == TraceEventKind.FINAL_OUTCOME
        )
        assert final_outcome.data["controller_action"] == "stop_early"
        assert final_outcome.data["controller_source"] == "controller"
        assert final_outcome.data["controller_applied"] is True
        assert final_outcome.data["controller_interventions"][0]["action"] == "stop_early"

    async def test_entry_agent_not_found(self):
        mock = MockLLMClient()
        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        config = OrchestratorConfig(entry_agent="nonexistent")
        orchestrator = create_orchestrator(
            "triad",
            providers={"mock": mock},
            config=config,
            model_overrides=overrides,
            trace=False,
        )

        result = await orchestrator.run("test")
        assert not result.success
        assert "not found" in result.error

    async def test_trace_captures_core_run_events(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(id="t0", name="send_message", input={"to": "coder", "content": "Write hello world"}),
                    ToolCall(id="t0b", name="complete_task", input={"result": "Task routed to coder"}),
                ],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(id="t1", name="send_message", input={"to": "reviewer", "content": "code here"}),
                    ToolCall(id="t2", name="send_message", input={"to": "tester", "content": "test this"}),
                    ToolCall(id="t3", name="complete_task", input={"result": "Code written"}),
                ],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t4", name="complete_task", input={"result": "Reviewed"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="t5", name="complete_task", input={"result": "Tested"})],
            ),
        ])

        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
        overrides = {t: mock_model for t in ModelTier}

        orchestrator = create_orchestrator(
            "triad",
            providers={"mock": mock},
            config=OrchestratorConfig(timeout=5.0, budget=50),
            model_overrides=overrides,
            trace=True,
        )

        result = await orchestrator.run("Write hello world")

        assert result.success
        assert orchestrator.trace is not None
        kinds = [event.kind for event in orchestrator.trace.events]
        assert TraceEventKind.TOPOLOGY_CHOICE in kinds
        assert TraceEventKind.AGENT_SPAWN in kinds
        assert TraceEventKind.INITIAL_INJECTION in kinds
        assert TraceEventKind.MESSAGE_ROUTED in kinds
        assert TraceEventKind.STAGE_FINISH in kinds
        assert TraceEventKind.FINAL_OUTCOME in kinds
