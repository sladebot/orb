"""Tests for BridgeAgent — background GraphRAG fact merging.

Run:
    python -m pytest tests/test_bridge_agent.py -v
"""

from __future__ import annotations

import asyncio
import uuid
from time import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore
from orb.memory.subgraph_store import Fact
from orb.agent.bridge_agent import BridgeAgent


def _make_fact(
    subject: str = "Alice",
    predicate: str = "knows",
    object_: str = "Bob",
    agent_id: str = "agent-1",
    turn_id: str | None = None,
    timestamp: float | None = None,
) -> Fact:
    return Fact(
        id=uuid.uuid4().hex,
        subject=subject,
        predicate=predicate,
        object=object_,
        agent_id=agent_id,
        turn_id=turn_id or uuid.uuid4().hex[:8],
        timestamp=timestamp if timestamp is not None else time(),
    )


# ---------------------------------------------------------------------------
# 1. merge_once copies facts from source to bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_once_copies_facts():
    """source_store has 3 facts; after merge_once, bridge has all 3."""
    source = ChromaDBNetworkXStore()
    bridge = ChromaDBNetworkXStore()

    facts = [
        _make_fact("Alice", "knows", "Bob", agent_id="a1"),
        _make_fact("Bob", "likes", "Carol", agent_id="a1"),
        _make_fact("Carol", "works_at", "Acme", agent_id="a2"),
    ]
    for f in facts:
        await source.upsert_fact(f)

    agent = BridgeAgent(source_stores=[source], bridge_store=bridge)
    count = await agent.merge_once()

    assert count == 3
    bridge_facts = await bridge.get_all_facts(limit=50)
    assert len(bridge_facts) == 3

    bridge_ids = {f.id for f in bridge_facts}
    for f in facts:
        assert f.id in bridge_ids

    await source.close()
    await bridge.close()


# ---------------------------------------------------------------------------
# 2. merge_once deduplicates identical facts from two source stores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_once_deduplicates_by_spoa():
    """Same (subject, predicate, object, agent_id) from two sources → one copy in bridge."""
    source1 = ChromaDBNetworkXStore()
    source2 = ChromaDBNetworkXStore()
    bridge = ChromaDBNetworkXStore()

    # Same logical fact in both stores (different IDs, same SPOA)
    shared = _make_fact("Alice", "knows", "Bob", agent_id="a1", timestamp=1000.0)
    duplicate = Fact(
        id=uuid.uuid4().hex,  # different id
        subject="Alice",
        predicate="knows",
        object="Bob",
        agent_id="a1",
        turn_id=uuid.uuid4().hex[:8],
        timestamp=1001.0,  # newer — this one should win
    )
    await source1.upsert_fact(shared)
    await source2.upsert_fact(duplicate)

    agent = BridgeAgent(source_stores=[source1, source2], bridge_store=bridge)
    count = await agent.merge_once()

    assert count == 1
    bridge_facts = await bridge.get_all_facts(limit=50)
    assert len(bridge_facts) == 1

    await source1.close()
    await source2.close()
    await bridge.close()


# ---------------------------------------------------------------------------
# 3. last_write_wins: newer timestamp beats older
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_once_last_write_wins():
    """Two facts with same SPO but different timestamps; newer is kept."""
    source1 = ChromaDBNetworkXStore()
    source2 = ChromaDBNetworkXStore()
    bridge = ChromaDBNetworkXStore()

    older = Fact(
        id=uuid.uuid4().hex,
        subject="X",
        predicate="rel",
        object="Y",
        agent_id="ag",
        turn_id="t1",
        timestamp=500.0,
        metadata={"version": "old"},
    )
    newer = Fact(
        id=uuid.uuid4().hex,
        subject="X",
        predicate="rel",
        object="Y",
        agent_id="ag",
        turn_id="t2",
        timestamp=900.0,
        metadata={"version": "new"},
    )
    await source1.upsert_fact(older)
    await source2.upsert_fact(newer)

    agent = BridgeAgent(source_stores=[source1, source2], bridge_store=bridge)
    await agent.merge_once()

    bridge_facts = await bridge.get_all_facts(limit=50)
    assert len(bridge_facts) == 1
    assert bridge_facts[0].timestamp == 900.0
    assert bridge_facts[0].metadata.get("version") == "new"

    await source1.close()
    await source2.close()
    await bridge.close()


# ---------------------------------------------------------------------------
# 4. merge_once returns correct integer count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_once_returns_count():
    """merge_once returns the number of facts upserted into bridge."""
    source = ChromaDBNetworkXStore()
    bridge = ChromaDBNetworkXStore()

    for i in range(5):
        await source.upsert_fact(_make_fact(f"node{i}", "edge", f"node{i+1}", agent_id="ag"))

    agent = BridgeAgent(source_stores=[source], bridge_store=bridge)
    count = await agent.merge_once()

    assert count == 5

    await source.close()
    await bridge.close()


# ---------------------------------------------------------------------------
# 5. start / stop does not hang
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop():
    """start() then stop() completes without hanging."""
    source = ChromaDBNetworkXStore()
    bridge = ChromaDBNetworkXStore()

    agent = BridgeAgent(source_stores=[source], bridge_store=bridge, interval_s=60.0)
    await agent.start()

    # Give the loop one iteration chance
    await asyncio.sleep(0.05)

    async with asyncio.timeout(2.0):
        await agent.stop()

    await source.close()
    await bridge.close()


# ---------------------------------------------------------------------------
# 6. merge_loop runs periodically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_loop_runs_periodically():
    """With interval_s=0.05, merge_once is called at least 2 times in 0.2s."""
    source = ChromaDBNetworkXStore()
    bridge = ChromaDBNetworkXStore()

    call_count = 0
    original_merge_once = BridgeAgent.merge_once

    async def counting_merge_once(self):
        nonlocal call_count
        call_count += 1
        return await original_merge_once(self)

    agent = BridgeAgent(source_stores=[source], bridge_store=bridge, interval_s=0.05)

    with patch.object(BridgeAgent, "merge_once", counting_merge_once):
        await agent.start()
        await asyncio.sleep(0.20)
        await agent.stop()

    assert call_count >= 2, f"Expected at least 2 calls, got {call_count}"

    await source.close()
    await bridge.close()
