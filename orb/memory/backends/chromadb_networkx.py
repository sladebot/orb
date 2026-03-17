"""ChromaDB + NetworkX backend for SubgraphStore.

Uses ChromaDB for vector-similarity search and NetworkX for graph traversal.
All ChromaDB calls are blocking, so they are dispatched via run_in_executor
to avoid blocking the event loop.

In-memory mode is used by default (no disk writes, no external services).
Pass persist_path=<directory> to enable persistence.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import uuid

import chromadb
import networkx as nx

from orb.memory.subgraph_store import Fact, SubgraphStore

# One thread is enough; ChromaDB's SQLite layer is not parallelisable anyway.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chroma")

_COLLECTION_NAME = "orb_facts"


def _fact_to_document(fact: Fact) -> str:
    """Serialise a Fact to a searchable document string."""
    return f"{fact.subject} {fact.predicate} {fact.object}"


def _fact_to_metadata(fact: Fact) -> dict[str, Any]:
    """Return ChromaDB-compatible metadata dict (scalar values only)."""
    return {
        "subject": fact.subject,
        "predicate": fact.predicate,
        "object": fact.object,
        "agent_id": fact.agent_id,
        "turn_id": fact.turn_id,
        "confidence": fact.confidence,
        "timestamp": fact.timestamp,
        # Store extra metadata as JSON string to stay within chroma scalar constraints
        "metadata_json": json.dumps(fact.metadata),
    }


def _row_to_fact(id_: str, metadata: dict[str, Any]) -> Fact:
    """Reconstruct a Fact from a ChromaDB metadata row."""
    return Fact(
        id=id_,
        subject=metadata["subject"],
        predicate=metadata["predicate"],
        object=metadata["object"],
        agent_id=metadata["agent_id"],
        turn_id=metadata["turn_id"],
        confidence=float(metadata.get("confidence", 1.0)),
        timestamp=float(metadata.get("timestamp", 0.0)),
        metadata=json.loads(metadata.get("metadata_json", "{}")),
    )


class ChromaDBNetworkXStore(SubgraphStore):
    """Local GraphRAG backend backed by ChromaDB (vector) + NetworkX (graph)."""

    def __init__(self, persist_path: str | None = None, collection_name: str | None = None) -> None:
        if persist_path:
            self._client = chromadb.PersistentClient(path=persist_path)
            # Persistent stores use a stable collection name by default.
            effective_name = collection_name or _COLLECTION_NAME
        else:
            self._client = chromadb.EphemeralClient()
            # Each ephemeral store gets a unique collection so test instances
            # don't share state even when running in the same process.
            effective_name = collection_name or f"{_COLLECTION_NAME}_{uuid.uuid4().hex[:8]}"

        self._collection = self._client.get_or_create_collection(
            name=effective_name,
            # Default embedding function (all-MiniLM-L6-v2) is used automatically.
        )
        self._graph: nx.DiGraph = nx.DiGraph()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking callable in the thread executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))

    # ------------------------------------------------------------------
    # SubgraphStore interface
    # ------------------------------------------------------------------

    async def upsert_fact(self, fact: Fact) -> None:
        doc = _fact_to_document(fact)
        meta = _fact_to_metadata(fact)

        await self._run(
            self._collection.upsert,
            ids=[fact.id],
            documents=[doc],
            metadatas=[meta],
        )

        # Add nodes and a directed edge to the NetworkX graph
        self._graph.add_node(fact.subject)
        self._graph.add_node(fact.object)
        self._graph.add_edge(
            fact.subject,
            fact.object,
            predicate=fact.predicate,
            fact_id=fact.id,
            agent_id=fact.agent_id,
        )

    async def get_facts(self, agent_id: str, *, limit: int = 20) -> list[Fact]:
        result = await self._run(
            self._collection.get,
            where={"agent_id": agent_id},
            limit=limit,
        )

        facts: list[Fact] = []
        for id_, meta in zip(result["ids"], result["metadatas"]):
            facts.append(_row_to_fact(id_, meta))

        # Sort newest-first
        facts.sort(key=lambda f: f.timestamp, reverse=True)
        return facts

    async def get_all_facts(self, *, limit: int = 200) -> list[Fact]:
        result = await self._run(
            self._collection.get,
            limit=limit,
        )

        facts: list[Fact] = []
        for id_, meta in zip(result["ids"], result["metadatas"]):
            facts.append(_row_to_fact(id_, meta))

        facts.sort(key=lambda f: f.timestamp, reverse=True)
        return facts

    async def delete_facts(self, agent_id: str) -> int:
        # Find IDs first
        result = await self._run(
            self._collection.get,
            where={"agent_id": agent_id},
        )
        ids = result["ids"]
        if not ids:
            return 0

        # Collect edges to remove from the graph
        metas = result["metadatas"]
        for meta in metas:
            subj = meta["subject"]
            obj_ = meta["object"]
            pred = meta["predicate"]
            if self._graph.has_edge(subj, obj_):
                edge_data = self._graph[subj][obj_]
                if edge_data.get("predicate") == pred:
                    self._graph.remove_edge(subj, obj_)

        await self._run(self._collection.delete, ids=ids)
        return len(ids)

    async def query(
        self,
        text: str,
        *,
        agent_id: str | None = None,
        limit: int = 10,
    ) -> list[Fact]:
        where = {"agent_id": agent_id} if agent_id is not None else None

        kwargs: dict[str, Any] = dict(
            query_texts=[text],
            n_results=limit,
        )
        if where is not None:
            kwargs["where"] = where

        result = await self._run(self._collection.query, **kwargs)

        facts: list[Fact] = []
        ids_list = result["ids"][0]       # list-of-lists for each query
        metas_list = result["metadatas"][0]

        for id_, meta in zip(ids_list, metas_list):
            fact = _row_to_fact(id_, meta)
            facts.append(fact)

            # Optionally expand via NetworkX neighbours
            subj = meta["subject"]
            if subj in self._graph:
                for neighbour in self._graph.successors(subj):
                    # Avoid adding the same fact again (we'd need the fact_id from the edge)
                    pass  # graph expansion can be enriched in later phases

        return facts

    async def close(self) -> None:
        """No-op for in-memory client; clears graph."""
        self._graph.clear()
