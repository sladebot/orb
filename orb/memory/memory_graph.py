from __future__ import annotations

from time import time

from .memory_node import MemoryNode, MemoryEdge


class MemoryGraph:
    """Per-agent graph-structured memory store."""

    def __init__(self) -> None:
        self._nodes: dict[str, MemoryNode] = {}
        self._edges: set[MemoryEdge] = set()
        self._adj: dict[str, set[str]] = {}  # bidirectional adjacency

    def add_node(self, node: MemoryNode) -> None:
        self._nodes[node.id] = node
        self._adj.setdefault(node.id, set())

    def add_edge(self, edge: MemoryEdge) -> None:
        if edge.from_id not in self._nodes:
            raise KeyError(f"Memory node {edge.from_id!r} not found")
        if edge.to_id not in self._nodes:
            raise KeyError(f"Memory node {edge.to_id!r} not found")
        self._edges.add(edge)
        self._adj[edge.from_id].add(edge.to_id)
        self._adj[edge.to_id].add(edge.from_id)

    def get_node(self, node_id: str) -> MemoryNode:
        if node_id not in self._nodes:
            raise KeyError(f"Memory node {node_id!r} not found")
        return self._nodes[node_id]

    def remove_node(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise KeyError(f"Memory node {node_id!r} not found")
        # Remove edges involving this node
        self._edges = {e for e in self._edges if e.from_id != node_id and e.to_id != node_id}
        for neighbor in list(self._adj.get(node_id, [])):
            self._adj[neighbor].discard(node_id)
        del self._adj[node_id]
        del self._nodes[node_id]

    def update_node(self, node_id: str, content: str) -> None:
        node = self.get_node(node_id)
        node.content = content
        node.updated_at = time()

    def get_connected(self, node_id: str, depth: int = 1) -> list[MemoryNode]:
        """BFS traversal returning nodes within `depth` hops."""
        if node_id not in self._nodes:
            raise KeyError(f"Memory node {node_id!r} not found")
        visited: set[str] = {node_id}
        frontier: set[str] = {node_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                for neighbor in self._adj.get(nid, []):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier
        # Exclude the starting node itself
        visited.discard(node_id)
        return [self._nodes[nid] for nid in visited]

    def query_by_type(self, node_type: str) -> list[MemoryNode]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    @property
    def nodes(self) -> list[MemoryNode]:
        return list(self._nodes.values())

    @property
    def edges(self) -> set[MemoryEdge]:
        return set(self._edges)
