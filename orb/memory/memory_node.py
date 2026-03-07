from __future__ import annotations

from dataclasses import dataclass, field
from time import time


@dataclass
class MemoryNode:
    id: str
    content: str
    node_type: str  # e.g. "code", "requirement", "review", "test_result", "constraint"
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)


@dataclass(frozen=True)
class MemoryEdge:
    from_id: str
    to_id: str
    relation: str  # e.g. "derived_from", "related_to", "supersedes"
