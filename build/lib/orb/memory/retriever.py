from __future__ import annotations

from .memory_graph import MemoryGraph
from .memory_node import MemoryNode


def get_relevant_context(
    memory: MemoryGraph,
    current_node_id: str,
    depth: int = 2,
    max_nodes: int = 10,
) -> list[MemoryNode]:
    """Retrieve memory nodes relevant to the current task node via graph traversal."""
    connected = memory.get_connected(current_node_id, depth=depth)
    # Sort by most recently updated, limit count
    connected.sort(key=lambda n: n.updated_at, reverse=True)
    return connected[:max_nodes]
