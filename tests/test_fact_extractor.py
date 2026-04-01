"""Tests for LLM-based FactExtractor — written BEFORE implementation per CLAUDE.md rule #1."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orb.agent.fact_extractor import FactExtractor, LLM_EXTRACTION_CONFIDENCE
from orb.llm.client import LLMClient
from orb.llm.types import CompletionResponse
from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore


def _make_response(json_payload: str) -> CompletionResponse:
    return CompletionResponse(content=json_payload, tool_calls=[], model="test-model")


def _make_providers(codex_response=None, codex_raises=None,
                    anthropic_response=None, anthropic_raises=None):
    from orb.llm.types import OPENAI_CODEX_PROVIDER, ANTHROPIC_PROVIDER
    providers = {}
    if codex_response is not None or codex_raises is not None:
        codex = AsyncMock(spec=LLMClient)
        if codex_raises:
            codex.complete.side_effect = codex_raises
        else:
            codex.complete.return_value = _make_response(codex_response)
        providers[OPENAI_CODEX_PROVIDER] = codex
    if anthropic_response is not None or anthropic_raises is not None:
        ant = AsyncMock(spec=LLMClient)
        if anthropic_raises:
            ant.complete.side_effect = anthropic_raises
        else:
            ant.complete.return_value = _make_response(anthropic_response)
        providers[ANTHROPIC_PROVIDER] = ant
    return providers


@pytest.mark.asyncio
async def test_llm_extraction_returns_facts():
    store = ChromaDBNetworkXStore()
    payload = json.dumps([
        {"subject": "Alice", "predicate": "wrote", "object": "tests"},
        {"subject": "Bob", "predicate": "reviewed", "object": "PR #42"},
    ])
    providers = _make_providers(codex_response=payload)
    extractor = FactExtractor(store, "agent-a", providers)
    facts = await extractor.extract_and_store("turn-1", "Alice wrote tests. Bob reviewed PR #42.")
    assert len(facts) == 2
    predicates = {f.predicate for f in facts}
    assert predicates == {"wrote", "reviewed"}
    for f in facts:
        assert f.confidence == pytest.approx(LLM_EXTRACTION_CONFIDENCE)
        assert f.metadata["extraction_method"] == "llm"
        assert f.agent_id == "agent-a"
        assert f.turn_id == "turn-1"
    stored = await store.get_facts("agent-a")
    stored_ids = {f.id for f in stored}
    for fact in facts:
        assert fact.id in stored_ids
    await store.close()


@pytest.mark.asyncio
async def test_codex_fallback_to_sonnet():
    store = ChromaDBNetworkXStore()
    payload = json.dumps([{"subject": "Alice", "predicate": "wrote", "object": "code"}])
    providers = _make_providers(
        codex_raises=RuntimeError("codex down"),
        anthropic_response=payload,
    )
    extractor = FactExtractor(store, "agent-b", providers)
    facts = await extractor.extract_and_store("turn-2", "Alice wrote code.")
    assert len(facts) == 1
    assert facts[0].predicate == "wrote"
    from orb.llm.types import OPENAI_CODEX_PROVIDER, ANTHROPIC_PROVIDER
    providers[OPENAI_CODEX_PROVIDER].complete.assert_called_once()
    providers[ANTHROPIC_PROVIDER].complete.assert_called_once()
    await store.close()


@pytest.mark.asyncio
async def test_both_providers_fail_raises():
    store = ChromaDBNetworkXStore()
    providers = _make_providers(
        codex_raises=RuntimeError("codex down"),
        anthropic_raises=RuntimeError("anthropic down"),
    )
    extractor = FactExtractor(store, "agent-c", providers)
    with pytest.raises(RuntimeError, match="Fact extraction failed"):
        await extractor.extract_and_store("turn-3", "some content")
    await store.close()


@pytest.mark.asyncio
async def test_malformed_json_raises():
    store = ChromaDBNetworkXStore()
    providers = _make_providers(codex_response="not valid json at all")
    extractor = FactExtractor(store, "agent-d", providers)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await extractor.extract_and_store("turn-4", "some content")
    await store.close()


@pytest.mark.asyncio
async def test_empty_json_array_returns_no_facts():
    store = ChromaDBNetworkXStore()
    providers = _make_providers(codex_response="[]")
    extractor = FactExtractor(store, "agent-e", providers)
    facts = await extractor.extract_and_store("turn-5", "some content")
    assert facts == []
    stored = await store.get_facts("agent-e")
    assert stored == []
    await store.close()


@pytest.mark.asyncio
async def test_partial_triple_skipped():
    store = ChromaDBNetworkXStore()
    payload = json.dumps([{"subject": "x"}])
    providers = _make_providers(codex_response=payload)
    extractor = FactExtractor(store, "agent-f", providers)
    facts = await extractor.extract_and_store("turn-6", "some content")
    assert facts == []
    stored = await store.get_facts("agent-f")
    assert stored == []
    await store.close()


@pytest.mark.asyncio
async def test_anthropic_only_provider_no_codex():
    """When only Anthropic provider is present (no Codex), Anthropic is used directly."""
    store = ChromaDBNetworkXStore()
    payload = json.dumps([{"subject": "Alice", "predicate": "wrote", "object": "tests"}])
    providers = _make_providers(anthropic_response=payload)
    extractor = FactExtractor(store, "agent-g", providers)

    facts = await extractor.extract_and_store("turn-7", "Alice wrote tests.")

    assert len(facts) == 1
    assert facts[0].predicate == "wrote"

    await store.close()


@pytest.mark.asyncio
async def test_set_subgraph_store_passes_providers():
    from orb.agent.llm_agent import LLMAgent
    from orb.agent.types import AgentConfig
    from orb.graph.graph import Graph
    from orb.messaging.bus import MessageBus
    from orb.messaging.channel import InProcessChannel
    from orb.llm.types import ModelConfig, ModelTier

    graph = Graph()
    graph.add_node("agent-test")
    bus = MessageBus(graph)
    channel = InProcessChannel()
    bus.register_channel("agent-test", channel)
    config = AgentConfig(node_id="agent-test", role="Tester", description="Test agent")
    mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")

    class _StubClient(LLMClient):
        async def complete(self, req): raise NotImplementedError
        async def close(self): pass

    providers = {"mock": _StubClient()}
    agent = LLMAgent(
        config, channel, bus, providers,
        model_overrides={t: mock_model for t in ModelTier},
    )
    assert agent._fact_extractor is None
    store = ChromaDBNetworkXStore()
    agent.set_subgraph_store(store)
    assert agent._fact_extractor is not None
    assert agent._fact_extractor._providers is providers
    assert agent._subgraph_store is store
    await store.close()


@pytest.mark.asyncio
async def test_non_list_json_raises():
    """LLM returns a JSON object (not array); RuntimeError is raised."""
    store = ChromaDBNetworkXStore()
    providers = _make_providers(codex_response='{"subject": "x", "predicate": "y", "object": "z"}')
    extractor = FactExtractor(store, "agent-h", providers)

    with pytest.raises(RuntimeError, match="invalid JSON"):
        await extractor.extract_and_store("turn-8", "some content")

    await store.close()
