"""Tests for graph-augmented request context helpers.

Written BEFORE implementation, per CLAUDE.md rule #1.
"""

from __future__ import annotations

import asyncio
import uuid
import pytest

from orb.memory.subgraph_store import Fact
from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore


def make_fact(
    subject: str,
    predicate: str,
    obj: str,
    agent_id: str = "agent-a",
    turn_id: str = "turn-1",
    confidence: float = 1.0,
) -> Fact:
    return Fact(
        id=uuid.uuid4().hex,
        subject=subject,
        predicate=predicate,
        object=obj,
        agent_id=agent_id,
        turn_id=turn_id,
        confidence=confidence,
    )


@pytest.fixture
async def store():
    """In-memory ChromaDB store, no disk writes."""
    s = ChromaDBNetworkXStore()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# build_graph_context_block tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_graph_context_block_empty_store(store):
    """Empty store → returns empty string."""
    from orb.agent.request_context import build_graph_context_block

    result = await build_graph_context_block(store, "agent-a", "some query")
    assert result == ""


@pytest.mark.asyncio
async def test_build_graph_context_block_with_facts(store):
    """Store has 2 facts for agent 'a'; output contains predicate strings."""
    from orb.agent.request_context import build_graph_context_block

    fact1 = make_fact("Alice", "writes", "Python code", agent_id="a")
    fact2 = make_fact("Bob", "uses", "NetworkX", agent_id="a")

    await store.upsert_fact(fact1)
    await store.upsert_fact(fact2)

    result = await build_graph_context_block(store, "a", "Python code", limit=10)

    assert result != ""
    assert "Graph-retrieved context (agent: a):" in result
    # Both predicates should appear somewhere in the output
    assert "writes" in result or "uses" in result
    # Header with agent id
    assert "a" in result


@pytest.mark.asyncio
async def test_build_graph_context_block_format(store):
    """Output format matches '- [{predicate}] {subject} → {object}  (confidence: X.XX)'."""
    from orb.agent.request_context import build_graph_context_block

    fact = make_fact("Alice", "writes", "Python code", agent_id="fmt-agent", confidence=0.85)
    await store.upsert_fact(fact)

    result = await build_graph_context_block(store, "fmt-agent", "Alice writes", limit=5)

    assert "writes" in result
    assert "Alice" in result
    assert "Python code" in result
    assert "0.85" in result
    assert "→" in result


# ---------------------------------------------------------------------------
# build_request_messages_with_graph tests
# ---------------------------------------------------------------------------


def test_build_request_messages_with_graph_only():
    """graph_context provided, no transcript → injected into first user message."""
    from orb.agent.request_context import build_request_messages_with_graph

    messages = [{"role": "user", "content": "Hello, agent"}]
    graph_context = "Graph-retrieved context (agent: a):\n- [writes] Alice → Python code  (confidence: 1.00)"

    result = build_request_messages_with_graph(messages, graph_context)

    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert graph_context in result[0]["content"]
    assert "Hello, agent" in result[0]["content"]


def test_build_request_messages_with_transcript_only():
    """No graph_context, transcript provided → behaves like existing build_request_messages."""
    from orb.agent.request_context import build_request_messages_with_graph, build_request_messages

    messages = [{"role": "user", "content": "Do the task"}]
    transcript = "Agent X said: hello"

    result_new = build_request_messages_with_graph(messages, "", transcript)
    result_old = build_request_messages(messages, transcript)

    assert result_new == result_old


def test_build_request_messages_with_both():
    """Both graph_context and transcript provided → combined block injected."""
    from orb.agent.request_context import build_request_messages_with_graph

    messages = [{"role": "user", "content": "Do the task"}]
    graph_context = "Graph-retrieved context (agent: a):\n- [writes] Alice → Python  (confidence: 1.00)"
    transcript = "Agent X said: hello"

    result = build_request_messages_with_graph(messages, graph_context, transcript)

    assert len(result) == 1
    content = result[0]["content"]
    assert graph_context in content
    assert transcript in content
    assert "--- Session context ---" in content
    assert "Do the task" in content


def test_build_request_messages_with_neither():
    """Neither graph_context nor transcript → messages unchanged."""
    from orb.agent.request_context import build_request_messages_with_graph

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    result = build_request_messages_with_graph(messages, "", "")

    # Should be equal content-wise but deep copies
    assert len(result) == len(messages)
    assert result[0]["content"] == messages[0]["content"]
    assert result[1]["content"] == messages[1]["content"]


def test_build_request_messages_with_neither_no_messages():
    """Empty messages list with neither context → returns empty list."""
    from orb.agent.request_context import build_request_messages_with_graph

    result = build_request_messages_with_graph([], "", "")
    assert result == []


def test_build_request_messages_prepends_when_no_user_first():
    """If first message is not user role, graph context is prepended as new user turn."""
    from orb.agent.request_context import build_request_messages_with_graph

    messages = [{"role": "assistant", "content": "I am ready"}]
    graph_context = "Graph-retrieved context (agent: x):\n- [knows] X → Y  (confidence: 0.90)"

    result = build_request_messages_with_graph(messages, graph_context)

    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert graph_context in result[0]["content"]
    assert result[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Deduplication test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deduplication(store):
    """Same fact returned by both query and get_facts → appears only once in output."""
    from orb.agent.request_context import build_graph_context_block

    fact = make_fact("Alice", "writes", "Python code", agent_id="dup-agent")
    await store.upsert_fact(fact)

    result = await build_graph_context_block(store, "dup-agent", "Alice writes Python", limit=10)

    # Count how many times the fact's subject appears as a line entry
    lines = [line for line in result.splitlines() if "Alice" in line and "→" in line]
    assert len(lines) == 1, f"Expected 1 occurrence of the fact, got {len(lines)}: {lines}"
