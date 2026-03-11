import asyncio

import pytest

from orb.agent.llm_agent import LLMAgent
from orb.agent.types import AgentConfig
from orb.graph.graph import Graph
from orb.llm.client import LLMClient
from orb.llm.types import CompletionRequest, CompletionResponse, ToolCall, ModelTier
from orb.messaging.bus import MessageBus
from orb.messaging.channel import AgentChannel
from orb.messaging.message import Message, MessageType


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


def _build_two_agent_setup(mock_client: MockLLMClient):
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

    agent_a = LLMAgent(config_a, ch_a, bus, providers, model_overrides=overrides)
    agent_b = LLMAgent(config_b, ch_b, bus, providers, model_overrides=overrides)

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
