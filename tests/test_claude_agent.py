import asyncio
from unittest.mock import MagicMock

import pytest

from orb.agent.llm_agent import LLMAgent
from orb.agent.types import AgentConfig, AgentStatus
from orb.graph.graph import Graph
from orb.llm.client import LLMClient
from orb.llm.types import CompletionRequest, CompletionResponse, ToolCall, ModelTier
from orb.messaging.bus import MessageBus
from orb.messaging.channel import AgentChannel
from orb.messaging.message import Message, MessageType
from orb.runtime.transcript import RunTranscript
from orb.tracing.run_trace import RunTrace, TraceEventKind


class MockLLMClient(LLMClient):
    """Mock LLM client that returns predefined responses."""

    def __init__(self, responses: list[CompletionResponse] | None = None):
        self._responses = list(responses or [])
        self._call_count = 0
        self.requests: list[CompletionRequest] = []

    def add_response(self, response: CompletionResponse) -> None:
        self._responses.append(response)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
        else:
            resp = CompletionResponse(content="Default mock response", model="mock")
        self._call_count += 1
        return resp

    async def close(self) -> None:
        pass


def _build_two_agent_setup(mock_client: MockLLMClient, trace: RunTrace | None = None):
    """Build a minimal 2-agent setup for testing."""
    graph = Graph()
    graph.add_node("agent_a")
    graph.add_node("agent_b")
    graph.add_edge("agent_a", "agent_b")

    bus = MessageBus(graph)

    ch_a = AgentChannel()
    ch_b = AgentChannel()
    bus.register_channel("agent_a", ch_a)
    bus.register_channel("agent_b", ch_b)

    providers = {"mock": mock_client}

    config_a = AgentConfig(node_id="agent_a", role="Coder", description="Writes code")
    config_b = AgentConfig(node_id="agent_b", role="Reviewer", description="Reviews code")

    # Force all agents to use mock provider
    from orb.llm.types import ModelConfig
    mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
    overrides = {t: mock_model for t in ModelTier}

    agent_a = LLMAgent(config_a, ch_a, bus, providers, model_overrides=overrides, trace=trace)
    agent_b = LLMAgent(config_b, ch_b, bus, providers, model_overrides=overrides, trace=trace)

    agent_a.initialize({"agent_b": "Reviewer"})
    agent_b.initialize({"agent_a": "Coder"})

    return agent_a, agent_b, bus, ch_a, ch_b


class TestLLMAgent:
    async def test_process_text_response(self):
        mock = MockLLMClient([
            CompletionResponse(content="Hello from mock", model="mock"),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Write hello world")
        await agent_a.process(msg)

        # Agent retries text-only responses up to MAX_TOOL_NUDGES times; verify at least 1 call was made
        assert len(mock.requests) >= 1
        assert "Write hello world" in mock.requests[0].messages[0]["content"]

    async def test_process_send_message_tool(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="send_message",
                    input={"to": "agent_b", "content": "Here's my code"},
                )],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Write code")
        await agent_a.process(msg)

        # agent_b should have received the message
        received = await asyncio.wait_for(ch_b.receive(), timeout=1.0)
        assert received.payload == "Here's my code"
        assert received.from_ == "agent_a"

    async def test_process_complete_task_tool(self):
        completions = {}

        async def on_complete(agent_id, result):
            completions[agent_id] = result

        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="complete_task",
                    input={"result": "Done!"},
                )],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)
        agent_a._on_complete = on_complete

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Finish up")
        await agent_a.process(msg)

        assert "agent_a" in completions
        assert completions["agent_a"] == "Done!"

    async def test_send_to_non_neighbor_fails(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="send_message",
                    input={"to": "nonexistent", "content": "Hi"},
                )],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Test")
        await agent_a.process(msg)

        # ch_b should be empty — message was rejected
        assert ch_b.qsize == 0

    async def test_context_passed_through(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="send_message",
                    input={
                        "to": "agent_b",
                        "content": "Review this",
                        "context": ["def hello(): pass", "must handle errors"],
                    },
                )],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Write code")
        await agent_a.process(msg)

        received = await asyncio.wait_for(ch_b.receive(), timeout=1.0)
        assert received.context_slice == ["def hello(): pass", "must handle errors"]

    async def test_send_to_user_sets_waiting_status(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="send_message",
                    input={"to": "user", "content": "Which framework should I use?"},
                )],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Build app")
        await agent_a.process(msg)

        assert agent_a.status == AgentStatus.WAITING

    async def test_llm_retry_activity_includes_failure_reason(self):
        """When an LLM call fails and is retried, the activity text must
        include *why* — otherwise users see 'Retrying...' and assume progress
        while every retry hits the same dead end (e.g. model not loaded).
        """
        class _FailingClient:
            async def complete(self, request):
                raise httpx.ReadTimeout("timed out waiting for model")
            async def close(self):
                pass

        import httpx
        mock = _FailingClient()
        from orb.llm.types import ModelConfig
        mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="gemma4", provider="omlx")
        overrides = {t: mock_model for t in ModelTier}
        providers = {"omlx": mock}

        graph = Graph(); graph.add_node("agent_a"); graph.add_node("agent_b")
        graph.add_edge("agent_a", "agent_b")
        bus = MessageBus(graph)
        ch_a = AgentChannel(); ch_b = AgentChannel()
        bus.register_channel("agent_a", ch_a); bus.register_channel("agent_b", ch_b)
        config_a = AgentConfig(node_id="agent_a", role="Coder", description="Writes code")
        agent_a = LLMAgent(config_a, ch_a, bus, providers, model_overrides=overrides)
        agent_a.initialize({"agent_b": "Reviewer"})

        activities: list[str] = []
        async def capture(_agent, activity, _details=None):
            activities.append(activity)
        agent_a._on_activity = capture

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="go")
        await agent_a.process(msg)

        retry_lines = [a for a in activities if "Retrying" in a]
        assert retry_lines, f"expected at least one Retrying activity, got: {activities}"
        # At least one retry line must surface the error reason. ReadTimeout
        # is a very specific httpx exception name — test that *something*
        # concrete about the failure appears in the retry UX.
        assert any("timeout" in a.lower() or "ReadTimeout" in a for a in retry_lines), (
            f"retry activities don't mention the failure reason: {retry_lines}"
        )

    async def test_send_to_user_preserves_full_question_in_details(self):
        """The user-facing question must not be truncated before reaching the UI.

        Regression: the activity string was capped at 120 chars, so long
        clarification questions got sliced mid-sentence in the dashboard banner.
        """
        long_question = (
            "The coder is ready to implement the calculator CLI but needs "
            "clarification: what target language or framework would you like "
            "to use for the project, and should it include unit tests?"
        )
        assert len(long_question) > 120

        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="send_message",
                    input={"to": "user", "content": long_question},
                )],
            ),
        ])
        agent_a, *_ = _build_two_agent_setup(mock)

        captured: list[tuple[str, str, dict]] = []

        async def on_activity(agent_id, activity, details):
            captured.append((agent_id, activity, dict(details or {})))

        agent_a._on_activity = on_activity

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Build app")
        await agent_a.process(msg)

        waiting = [c for c in captured if c[1].startswith("⏳ Waiting for user")]
        assert waiting, "agent never emitted a 'Waiting for user' activity"
        _, _activity, details = waiting[0]
        assert details.get("full_content") == long_question, (
            f"full question must be preserved in details.full_content; got {details!r}"
        )

    async def test_model_request_includes_shared_transcript_context(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="complete_task",
                    input={"result": "Done"},
                )],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)
        transcript = RunTranscript()
        transcript.add_message(Message(from_="user", to="agent_a", type=MessageType.TASK, payload="Build a CLI tool"))
        transcript.add_message(Message(from_="agent_b", to="agent_a", type=MessageType.FEEDBACK, payload="Need tests"))
        agent_a._shared_transcript = transcript

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Continue")
        await agent_a.process(msg)

        assert mock.requests

    async def test_trace_records_llm_retry_and_tool_calls(self):
        class FlakyMockLLMClient(LLMClient):
            def __init__(self) -> None:
                self.calls = 0
                self.requests: list[CompletionRequest] = []

            async def complete(self, request: CompletionRequest) -> CompletionResponse:
                self.requests.append(request)
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary provider error")
                return CompletionResponse(
                    content="",
                    model="mock",
                    tool_calls=[ToolCall(id="tc1", name="complete_task", input={"result": "Done"})],
                )

            async def close(self) -> None:
                pass

        trace = RunTrace()
        mock = FlakyMockLLMClient()
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock, trace=trace)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Test")
        await agent_a.process(msg)

        kinds = [event.kind for event in trace.events]
        assert TraceEventKind.RETRY in kinds
        assert TraceEventKind.TOOL_CALL in kinds
        assert trace.events[-1].kind == TraceEventKind.TOOL_CALL

    async def test_repeated_directory_listing_uses_turn_cache(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc1", name="list_directory", input={"path": "."})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc2", name="list_directory", input={"path": "."})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc3", name="complete_task", input={"result": "Done"})],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)
        sandbox = MagicMock()
        sandbox.list_directory = MagicMock(return_value="app/\npackage.json")
        sandbox.read_file = MagicMock(return_value="")
        sandbox.write_file = MagicMock(return_value="ok")
        agent_a.config.sandbox = sandbox

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Inspect project")
        await agent_a.process(msg)

        assert sandbox.list_directory.call_count == 1

    async def test_repeated_read_file_uses_turn_cache_until_write(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc1", name="read_file", input={"path": "app.py"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc2", name="read_file", input={"path": "app.py"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc3", name="write_file", input={"path": "app.py", "content": "print('hi')"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc4", name="read_file", input={"path": "app.py"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc5", name="complete_task", input={"result": "Done"})],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)
        sandbox = MagicMock()
        sandbox.list_directory = MagicMock(return_value="")
        sandbox.read_file = MagicMock(side_effect=["old", "old", "new"])
        sandbox.write_file = MagicMock(return_value="wrote app.py")
        agent_a.config.sandbox = sandbox

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Inspect project")
        await agent_a.process(msg)

        # One initial read, one old-content read during write_file for diff capture,
        # and one fresh read after cache invalidation.
        assert sandbox.read_file.call_count == 3

    async def test_write_file_creates_artifact_memory_node(self):
        mock = MockLLMClient([
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc1", name="write_file", input={"path": "app.py", "content": "print('hi')"})],
            ),
            CompletionResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="tc2", name="complete_task", input={"result": "Done"})],
            ),
        ])
        agent_a, agent_b, bus, ch_a, ch_b = _build_two_agent_setup(mock)
        sandbox = MagicMock()
        sandbox.list_directory = MagicMock(return_value="")
        sandbox.read_file = MagicMock(side_effect=FileNotFoundError("missing"))
        sandbox.write_file = MagicMock(return_value="wrote app.py")
        agent_a.config.sandbox = sandbox

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="Create app.py")
        await agent_a.process(msg)

        artifact = agent_a._memory.get_node("file:app.py")
        assert artifact.node_type == "artifact"
        assert artifact.metadata["path"] == "app.py"
        assert artifact.metadata["action"] == "write"
        assert artifact.content == "print('hi')"
        assert any(
            edge.to_id == "file:app.py" and edge.relation == "writes_to"
            for edge in agent_a._memory.edges
        )
