"""Scale / performance tests for the GraphRAG infrastructure.

These tests do NOT use any real LLM or network calls — they exercise the
local ChromaDBNetworkXStore and BridgeAgent infrastructure at scale.

Targets
-------
- p99 upsert latency  < 50 ms
- p99 query latency   < 100 ms
- BridgeAgent.merge_once() correct under concurrent load (no deadlock)
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

_IN_CI = os.getenv("CI") == "true"

from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore
from orb.memory.subgraph_store import Fact
from orb.agent.bridge_agent import BridgeAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fact(agent_id: str = "agent0", i: int = 0) -> Fact:
    return Fact(
        id=uuid.uuid4().hex,
        subject=f"subject_{i}",
        predicate="relates_to",
        object=f"object_{i}",
        agent_id=agent_id,
        turn_id=uuid.uuid4().hex,
        confidence=1.0,
    )


async def _populate_store(store: ChromaDBNetworkXStore, n_facts: int, agent_id: str = "agent0") -> None:
    for i in range(n_facts):
        await store.upsert_fact(_make_fact(agent_id=agent_id, i=i))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScaleGraphRAG:

    @pytest.mark.asyncio
    @pytest.mark.skipif(_IN_CI, reason="ChromaDB latency unreliable on CI runners")
    async def test_20_agents_fact_write_latency(self):
        """Create 20 stores, write 100 facts to each (2000 total).
        Assert p99 write latency < 50ms.

        Notes
        -----
        A single-thread ChromaDB executor is shared across all stores in the
        same process.  We warm up the embedding model with a handful of writes
        before collecting latency samples so that one-time initialisation costs
        (model download, JIT) do not inflate the p99.
        """
        n_stores = 20
        n_facts = 100
        n_warmup = 5  # writes to discard per store during model warm-up

        stores = [ChromaDBNetworkXStore() for _ in range(n_stores)]

        # Warm-up pass — not measured
        for store_idx, store in enumerate(stores):
            for i in range(n_warmup):
                await store.upsert_fact(_make_fact(agent_id=f"warmup_{store_idx}", i=i))

        latencies: list[float] = []
        for store_idx, store in enumerate(stores):
            agent_id = f"agent_{store_idx}"
            for i in range(n_facts):
                fact = _make_fact(agent_id=agent_id, i=i)
                t0 = time.perf_counter()
                await store.upsert_fact(fact)
                latencies.append((time.perf_counter() - t0) * 1000)  # ms

        latencies.sort()
        p99_idx = int(len(latencies) * 0.99)
        p99_ms = latencies[p99_idx]

        assert p99_ms < 50.0, (
            f"p99 write latency {p99_ms:.2f}ms exceeds 50ms target. "
            f"Total facts: {len(latencies)}, median: {latencies[len(latencies)//2]:.2f}ms"
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(_IN_CI, reason="ChromaDB latency unreliable on CI runners")
    async def test_20_agents_query_latency(self):
        """20 stores, 100 facts each, run 20 queries (one per store).
        Assert p99 query latency < 100ms."""
        n_stores = 20
        n_facts = 100

        stores = [ChromaDBNetworkXStore() for _ in range(n_stores)]

        # Populate all stores
        for store_idx, store in enumerate(stores):
            await _populate_store(store, n_facts, agent_id=f"agent_{store_idx}")

        # One query per store
        latencies: list[float] = []
        for store_idx, store in enumerate(stores):
            t0 = time.perf_counter()
            await store.query("subject relates to object", limit=10)
            latencies.append((time.perf_counter() - t0) * 1000)  # ms

        latencies.sort()
        p99_idx = max(0, int(len(latencies) * 0.99) - 1)
        p99_ms = latencies[p99_idx]

        assert p99_ms < 100.0, (
            f"p99 query latency {p99_ms:.2f}ms exceeds 100ms target. "
            f"Queries: {len(latencies)}, median: {latencies[len(latencies)//2]:.2f}ms"
        )

    @pytest.mark.asyncio
    async def test_bridge_agent_3_clusters_merge(self):
        """3 source stores each with 50 facts, 1 bridge store.
        After merge_once(), bridge store has >= 50 facts (dedup may reduce)."""
        n_source = 3
        n_facts = 50

        source_stores = [ChromaDBNetworkXStore() for _ in range(n_source)]
        bridge_store = ChromaDBNetworkXStore()

        # Populate source stores with distinct agent_ids and facts
        for src_idx, store in enumerate(source_stores):
            for i in range(n_facts):
                await store.upsert_fact(_make_fact(agent_id=f"cluster_{src_idx}_agent", i=i))

        bridge = BridgeAgent(
            source_stores=source_stores,
            bridge_store=bridge_store,
            interval_s=999.0,  # Don't auto-run
        )
        merged_count = await bridge.merge_once()

        # Should have merged at least n_facts (since all facts are distinct per cluster)
        assert merged_count >= n_facts, (
            f"Expected at least {n_facts} merged facts, got {merged_count}"
        )

        # Bridge store should also contain facts
        all_facts = await bridge_store.get_all_facts(limit=1000)
        assert len(all_facts) >= n_facts, (
            f"Expected at least {n_facts} facts in bridge store, got {len(all_facts)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(_IN_CI, reason="ChromaDB too slow on CI runners (>60s timeout)")
    async def test_no_deadlock_concurrent_upserts(self):
        """20 asyncio tasks each writing 50 facts to the SAME store concurrently.
        All must complete without hanging (uses asyncio.wait_for timeout)."""
        store = ChromaDBNetworkXStore()
        n_tasks = 20
        n_facts_per_task = 50

        async def write_facts(task_id: int) -> int:
            for i in range(n_facts_per_task):
                await store.upsert_fact(_make_fact(agent_id=f"task_{task_id}", i=i))
            return n_facts_per_task

        async def run_all():
            tasks = [write_facts(t) for t in range(n_tasks)]
            results = await asyncio.gather(*tasks)
            return results

        # 60-second timeout should be very generous; deadlock would hang forever
        results = await asyncio.wait_for(run_all(), timeout=60.0)

        assert len(results) == n_tasks
        assert all(r == n_facts_per_task for r in results)

        # Verify facts are in the store
        total_facts = await store.get_all_facts(limit=10000)
        # Due to deduplication (same subject/predicate/object across tasks),
        # the actual count may be lower, but should have at least n_facts_per_task
        assert len(total_facts) >= n_facts_per_task
