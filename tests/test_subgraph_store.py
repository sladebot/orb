"""Tests for SubgraphStore abstract interface and ChromaDB + NetworkX backend.

Written BEFORE implementation, per CLAUDE.md rule #1.
"""

import asyncio
import uuid
import pytest

from orb.memory.subgraph_store import Fact, SubgraphStore
from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore
from orb.memory.backends.zep_cloud import ZepCloudStore
from orb.memory.factory import SubgraphStoreFactory


def make_fact(subject: str, predicate: str, obj: str, agent_id: str = "agent-a", turn_id: str = "turn-1") -> Fact:
    return Fact(
        id=uuid.uuid4().hex,
        subject=subject,
        predicate=predicate,
        object=obj,
        agent_id=agent_id,
        turn_id=turn_id,
    )


@pytest.fixture
async def store():
    """In-memory ChromaDB store, no disk writes."""
    s = ChromaDBNetworkXStore()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_upsert_and_get_facts(store):
    """Upsert 2 facts for agent 'a', get_facts returns both."""
    fact1 = make_fact("Alice", "writes", "Python code", agent_id="a")
    fact2 = make_fact("Bob", "uses", "NetworkX", agent_id="a")

    await store.upsert_fact(fact1)
    await store.upsert_fact(fact2)

    results = await store.get_facts("a")
    ids = {f.id for f in results}
    assert fact1.id in ids
    assert fact2.id in ids
    assert len(results) == 2


@pytest.mark.asyncio
async def test_get_facts_filters_by_agent(store):
    """Upsert facts for agents 'a' and 'b'; get_facts('a') only returns agent 'a' facts."""
    fact_a = make_fact("Alice", "writes", "Python", agent_id="a")
    fact_b = make_fact("Bob", "uses", "Go", agent_id="b")

    await store.upsert_fact(fact_a)
    await store.upsert_fact(fact_b)

    results_a = await store.get_facts("a")
    assert len(results_a) == 1
    assert results_a[0].id == fact_a.id

    results_b = await store.get_facts("b")
    assert len(results_b) == 1
    assert results_b[0].id == fact_b.id


@pytest.mark.asyncio
async def test_delete_facts(store):
    """Upsert 3 facts, delete them, get_facts returns 0."""
    facts = [make_fact(f"subject{i}", "rel", f"obj{i}", agent_id="del-agent") for i in range(3)]
    for f in facts:
        await store.upsert_fact(f)

    count = await store.delete_facts("del-agent")
    assert count == 3

    remaining = await store.get_facts("del-agent")
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_query_returns_relevant(store):
    """Upsert a fact about Python; query('python programming') should find it."""
    fact = make_fact("Alice", "writes", "Python code", agent_id="query-agent")
    unrelated = make_fact("Bob", "drives", "a car", agent_id="query-agent")

    await store.upsert_fact(fact)
    await store.upsert_fact(unrelated)

    results = await store.query("python programming", agent_id="query-agent", limit=5)
    assert len(results) > 0
    ids = {f.id for f in results}
    assert fact.id in ids


@pytest.mark.asyncio
async def test_factory_chroma():
    """SubgraphStoreFactory.from_config('chroma') returns ChromaDBNetworkXStore."""
    store = SubgraphStoreFactory.from_config("chroma")
    assert isinstance(store, ChromaDBNetworkXStore)
    await store.close()


@pytest.mark.asyncio
async def test_factory_unknown():
    """SubgraphStoreFactory.from_config('unknown') raises ValueError."""
    with pytest.raises(ValueError, match="Unknown backend"):
        SubgraphStoreFactory.from_config("unknown")


@pytest.mark.asyncio
async def test_subgraph_store_is_abstract():
    """SubgraphStore cannot be instantiated directly."""
    with pytest.raises(TypeError):
        SubgraphStore()


@pytest.mark.asyncio
async def test_upsert_is_idempotent(store):
    """Upserting the same fact id twice does not create duplicates."""
    fact = make_fact("Alice", "writes", "Python", agent_id="idem-agent")
    await store.upsert_fact(fact)
    await store.upsert_fact(fact)  # same id, should upsert not duplicate

    results = await store.get_facts("idem-agent")
    assert len(results) == 1


def test_zep_raises_without_api_key():
    """ZepCloudStore(api_key='') raises ValueError."""
    with pytest.raises(ValueError, match="ZEP_API_KEY is required"):
        ZepCloudStore(api_key="")


def test_factory_zep_raises_without_key():
    """SubgraphStoreFactory.from_config('zep', api_key='') raises ValueError."""
    with pytest.raises(ValueError, match="ZEP_API_KEY is required"):
        SubgraphStoreFactory.from_config("zep", api_key="")
