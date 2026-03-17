"""SubgraphStore — abstract interface for GraphRAG fact storage.

Defines the Fact dataclass and the SubgraphStore ABC used by all backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from time import time


@dataclass
class Fact:
    """A single subject-predicate-object triple stored in the graph."""

    id: str           # uuid hex
    subject: str
    predicate: str
    object: str
    agent_id: str
    turn_id: str
    confidence: float = 1.0
    timestamp: float = field(default_factory=time)
    metadata: dict = field(default_factory=dict)


class SubgraphStore(ABC):
    """Abstract base for GraphRAG subgraph storage backends."""

    @abstractmethod
    async def upsert_fact(self, fact: Fact) -> None:
        """Insert or update a fact in the store."""
        ...

    @abstractmethod
    async def get_facts(self, agent_id: str, *, limit: int = 20) -> list[Fact]:
        """Return facts for *agent_id*, sorted newest-first, up to *limit*."""
        ...

    @abstractmethod
    async def delete_facts(self, agent_id: str) -> int:
        """Delete all facts for *agent_id*. Returns the number deleted."""
        ...

    @abstractmethod
    async def query(
        self,
        text: str,
        *,
        agent_id: str | None = None,
        limit: int = 10,
    ) -> list[Fact]:
        """Semantic search over facts. Optionally filter by *agent_id*."""
        ...

    @abstractmethod
    async def get_all_facts(self, *, limit: int = 200) -> list[Fact]:
        """Return all facts regardless of agent_id, newest-first, up to *limit*."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release any resources held by this backend."""
        ...
