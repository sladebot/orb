import json

import pytest

from orb.agent.llm_agent import LLMAgent
from orb.agent.prompt_builder import build_system_prompt
from orb.agent.tools import memory_read_tools, memory_write_tools
from orb.agent.types import AgentConfig
from orb.graph.graph import Graph
from orb.llm.client import LLMClient
from orb.llm.types import CompletionRequest, CompletionResponse, ModelConfig, ModelTier, ToolCall
from orb.messaging.bus import MessageBus
from orb.messaging.channel import AgentChannel
from orb.messaging.message import Message, MessageType


class MockLLMClient(LLMClient):
    def __init__(self, responses: list[CompletionResponse] | None = None):
        self._responses = list(responses or [])
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return CompletionResponse(
            content="",
            model="mock",
            tool_calls=[ToolCall(id="done", name="complete_task", input={"result": "done"})],
        )

    async def close(self) -> None:
        pass


def _agent_with_mock(config: AgentConfig, mock: MockLLMClient):
    graph = Graph()
    graph.add_node(config.node_id)
    graph.add_node("peer")
    graph.add_edge(config.node_id, "peer")
    bus = MessageBus(graph)
    channel = AgentChannel()
    peer_channel = AgentChannel()
    bus.register_channel(config.node_id, channel)
    bus.register_channel("peer", peer_channel)
    model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
    agent = LLMAgent(
        config,
        channel,
        bus,
        {"mock": mock},
        model_overrides={tier: model for tier in ModelTier},
    )
    agent.initialize({"peer": "Peer reviewer"})
    return agent


def _tool_names(agent: LLMAgent) -> set[str]:
    return {tool["name"] for tool in agent._tools}


def test_agent_config_memory_is_opt_in_and_read_only_by_default():
    config = AgentConfig(node_id="agent", role="Researcher", description="Reads memory")

    assert config.enable_memory is False
    assert config.memory_write_enabled is False
    assert config.memory_vault_path == "~/.orb/vault"


def test_memory_tool_schemas_are_model_callable_and_write_tools_are_separate():
    read_names = {tool["name"] for tool in memory_read_tools()}
    write_names = {tool["name"] for tool in memory_write_tools()}

    assert read_names == {
        "memory_read",
        "memory_read_entity",
        "memory_read_tag",
        "memory_list_pages",
    }
    assert write_names == {"memory_write", "memory_write_entity"}
    memory_read = next(tool for tool in memory_read_tools() if tool["name"] == "memory_read")
    assert memory_read["input_schema"]["required"] == ["query"]
    assert "limit" in memory_read["input_schema"]["properties"]


def test_initialize_exposes_memory_tools_only_when_enabled(tmp_path):
    disabled = _agent_with_mock(
        AgentConfig(node_id="agent", role="Researcher", description="Reads memory"),
        MockLLMClient(),
    )
    assert "memory_read" not in _tool_names(disabled)

    read_only = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_vault_path=str(tmp_path / "vault"),
        ),
        MockLLMClient(),
    )
    assert "memory_read" in _tool_names(read_only)
    assert "memory_write" not in _tool_names(read_only)

    writable = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_write_enabled=True,
            memory_vault_path=str(tmp_path / "vault"),
        ),
        MockLLMClient(),
    )
    assert "memory_read" in _tool_names(writable)
    assert "memory_write" in _tool_names(writable)


def test_memory_prompt_guidance_only_renders_when_memory_enabled():
    disabled_prompt = build_system_prompt(
        role="Researcher",
        description="Reads memory",
        neighbors={"peer": "Peer"},
    )
    assert "Persistent Memory Tools" not in disabled_prompt

    read_only_prompt = build_system_prompt(
        role="Researcher",
        description="Reads memory",
        neighbors={"peer": "Peer"},
        enable_memory=True,
    )
    assert "Persistent Memory Tools" in read_only_prompt
    assert "memory_read(query" in read_only_prompt
    assert "read-only" in read_only_prompt.lower()
    assert "memory_write(title" not in read_only_prompt

    writable_prompt = build_system_prompt(
        role="Researcher",
        description="Reads memory",
        neighbors={"peer": "Peer"},
        enable_memory=True,
        memory_write_enabled=True,
    )
    assert "memory_write(title" in writable_prompt
    assert "durable" in writable_prompt.lower()


@pytest.mark.asyncio
async def test_memory_read_tool_call_returns_structured_results(tmp_path):
    vault = tmp_path / "vault"
    agent = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_vault_path=str(vault),
        ),
        MockLLMClient(),
    )
    # Seed the agent's enabled vault through the backend API.
    agent._memory_tools.write_with_sources(
        "orb-memory-tools",
        "Agents can search persistent [[Orb]] memory using tool calls.",
        ["test-session"],
        page_type="concept",
    )

    await agent._handle_memory_read("tc1", {"query": "persistent memory", "limit": 3})

    results = agent._conversation.get_messages()[-1]["content"][0]["content"]
    assert "orb-memory-tools" in results
    assert "persistent" in results
    assert "content" not in results.lower()


@pytest.mark.asyncio
async def test_memory_write_tool_call_requires_write_gate(tmp_path):
    read_only = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_vault_path=str(tmp_path / "vault-ro"),
        ),
        MockLLMClient(),
    )

    await read_only._handle_memory_write(
        "tc1",
        {"title": "blocked", "content": "Should not persist", "page_type": "concept"},
    )

    result = read_only._conversation.get_messages()[-1]["content"][0]["content"]
    assert "disabled" in result.lower()
    assert not (tmp_path / "vault-ro" / "wiki" / "concept" / "blocked.md").exists()

    writable = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_write_enabled=True,
            memory_vault_path=str(tmp_path / "vault-rw"),
        ),
        MockLLMClient(),
    )

    await writable._handle_memory_write(
        "tc2",
        {
            "title": "orb-agent-memory",
            "content": "Agents can write durable memory when explicitly enabled.",
            "page_type": "concept",
            "tags": ["software-engineering"],
            "sources": ["test-session"],
        },
    )

    result = writable._conversation.get_messages()[-1]["content"][0]["content"]
    assert "orb-agent-memory" in result
    assert (tmp_path / "vault-rw" / "wiki" / "concept" / "orb-agent-memory.md").exists()


@pytest.mark.asyncio
async def test_memory_write_rejects_path_traversal_titles(tmp_path):
    vault = tmp_path / "vault"
    agent = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_write_enabled=True,
            memory_vault_path=str(vault),
        ),
        MockLLMClient(),
    )

    await agent._handle_memory_write(
        "tc1",
        {
            "title": "../../outside",
            "content": "Prompt injection should not escape the vault wiki directory.",
            "page_type": "concept",
        },
    )

    raw_result = agent._conversation.get_messages()[-1]["content"][0]["content"]
    result = json.loads(raw_result)
    assert result["ok"] is False
    assert "unsafe" in result["error"]
    assert not (tmp_path / "outside.md").exists()
    assert not (vault / "outside.md").exists()


@pytest.mark.asyncio
async def test_memory_write_entity_rejects_path_traversal_entities(tmp_path):
    vault = tmp_path / "vault"
    agent = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_write_enabled=True,
            memory_vault_path=str(vault),
        ),
        MockLLMClient(),
    )

    await agent._handle_memory_write_entity(
        "tc1",
        {
            "entity": "../outside-entity",
            "content": "Entity names are also model-controlled memory page titles.",
        },
    )

    raw_result = agent._conversation.get_messages()[-1]["content"][0]["content"]
    result = json.loads(raw_result)
    assert result["ok"] is False
    assert "unsafe" in result["error"]
    assert not (tmp_path / "outside-entity.md").exists()


@pytest.mark.asyncio
async def test_process_dispatches_memory_tools_without_completing(tmp_path):
    mock = MockLLMClient([
        CompletionResponse(
            content="",
            model="mock",
            tool_calls=[ToolCall(id="tc1", name="memory_list_pages", input={"limit": 5})],
        ),
        CompletionResponse(
            content="",
            model="mock",
            tool_calls=[ToolCall(id="tc2", name="complete_task", input={"result": "done"})],
        ),
    ])
    agent = _agent_with_mock(
        AgentConfig(
            node_id="agent",
            role="Researcher",
            description="Reads memory",
            enable_memory=True,
            memory_vault_path=str(tmp_path / "vault"),
        ),
        mock,
    )

    msg = Message(from_="peer", to="agent", type=MessageType.TASK, payload="Check memory")
    await agent.process(msg)

    assert len(mock.requests) == 2
    assert agent.status.value == "completed"
