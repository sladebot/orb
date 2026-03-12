from __future__ import annotations

from typing import Any

from .types import NodeId, Edge


class Graph:
    """Generic undirected graph via adjacency dict."""

    def __init__(self) -> None:
        self._nodes: dict[NodeId, Any] = {}
        self._adj: dict[NodeId, set[NodeId]] = {}

    def add_node(self, node_id: NodeId, data: Any = None) -> None:
        self._nodes[node_id] = data
        self._adj.setdefault(node_id, set())

    def remove_node(self, node_id: NodeId) -> None:
        if node_id not in self._nodes:
            raise KeyError(f"Node {node_id!r} not found")
        for neighbor in list(self._adj[node_id]):
            self._adj[neighbor].discard(node_id)
        del self._adj[node_id]
        del self._nodes[node_id]

    def add_edge(self, a: NodeId, b: NodeId) -> None:
        if a not in self._nodes:
            raise KeyError(f"Node {a!r} not found")
        if b not in self._nodes:
            raise KeyError(f"Node {b!r} not found")
        self._adj[a].add(b)
        self._adj[b].add(a)

    def remove_edge(self, a: NodeId, b: NodeId) -> None:
        if not self.has_edge(a, b):
            raise KeyError(f"Edge ({a!r}, {b!r}) not found")
        self._adj[a].discard(b)
        self._adj[b].discard(a)

    def has_node(self, node_id: NodeId) -> bool:
        return node_id in self._nodes

    def has_edge(self, a: NodeId, b: NodeId) -> bool:
        return a in self._adj and b in self._adj[a]

    def get_neighbors(self, node_id: NodeId) -> set[NodeId]:
        if node_id not in self._adj:
            raise KeyError(f"Node {node_id!r} not found")
        return set(self._adj[node_id])

    def get_node_data(self, node_id: NodeId) -> Any:
        if node_id not in self._nodes:
            raise KeyError(f"Node {node_id!r} not found")
        return self._nodes[node_id]

    @property
    def nodes(self) -> set[NodeId]:
        return set(self._nodes.keys())

    @property
    def edges(self) -> set[Edge]:
        seen: set[Edge] = set()
        for a, neighbors in self._adj.items():
            for b in neighbors:
                seen.add(Edge(a, b))
        return seen
