"""BridgeAgent — background task that merges facts from multiple SubgraphStores.

Periodically reads facts from two or more source SubgraphStores and writes
the merged, conflict-resolved facts into a shared "bridge" store.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from orb.memory.subgraph_store import Fact, SubgraphStore

logger = logging.getLogger(__name__)


class BridgeAgent:
    """Background task that merges subgraph facts from multiple source stores into a bridge store.

    Parameters
    ----------
    source_stores:
        One or more SubgraphStore instances to read facts from.
    bridge_store:
        The SubgraphStore instance to write merged facts into.
    interval_s:
        How often (in seconds) the merge loop runs. Default is 30 seconds.
    conflict_resolution:
        Strategy for resolving conflicting facts with the same
        (subject, predicate, object, agent_id) key.
        Currently only ``"last_write_wins"`` is supported, which keeps the
        fact with the highest timestamp.
    """

    def __init__(
        self,
        source_stores: list[SubgraphStore],
        bridge_store: SubgraphStore,
        interval_s: float = 30.0,
        conflict_resolution: Literal["last_write_wins"] = "last_write_wins",
    ) -> None:
        if not source_stores:
            raise ValueError("At least one source store is required")
        if conflict_resolution != "last_write_wins":
            raise ValueError(f"Unsupported conflict_resolution: {conflict_resolution!r}")

        self._source_stores = source_stores
        self._bridge_store = bridge_store
        self._interval_s = interval_s
        self._conflict_resolution = conflict_resolution
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background merge loop."""
        if self._task is not None and not self._task.done():
            logger.warning("BridgeAgent is already running")
            return
        self._task = asyncio.create_task(self._merge_loop(), name="bridge_agent_loop")
        logger.info("BridgeAgent started (interval=%.2fs)", self._interval_s)

    async def stop(self) -> None:
        """Stop the background merge loop and wait for it to finish."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("BridgeAgent stopped")

    # ------------------------------------------------------------------
    # Merge logic
    # ------------------------------------------------------------------

    async def merge_once(self) -> int:
        """Run one merge pass. Returns number of facts upserted into bridge_store."""
        # Collect all facts from all source stores
        all_facts: list[Fact] = []
        for store in self._source_stores:
            try:
                facts = await store.get_all_facts(limit=200)
                all_facts.extend(facts)
            except Exception:
                logger.exception("BridgeAgent: error reading facts from source store %r", store)

        if not all_facts:
            return 0

        # Conflict resolution: last_write_wins on (subject, predicate, object, agent_id) key
        # Keep the fact with the highest timestamp for each key.
        best: dict[tuple[str, str, str, str], Fact] = {}
        for fact in all_facts:
            key = (fact.subject, fact.predicate, fact.object, fact.agent_id)
            existing = best.get(key)
            if existing is None or fact.timestamp > existing.timestamp:
                best[key] = fact

        resolved = list(best.values())

        # Upsert all resolved facts into the bridge store
        for fact in resolved:
            await self._bridge_store.upsert_fact(fact)

        return len(resolved)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _merge_loop(self) -> None:
        """Internal loop — runs merge_once every interval_s seconds."""
        while True:
            try:
                count = await self.merge_once()
                logger.debug("BridgeAgent: merged %d facts", count)
            except Exception:
                logger.exception("BridgeAgent: unhandled error in merge_once; continuing")
            await asyncio.sleep(self._interval_s)
