"""
Shortest path algorithms for large sparse graphs in Python.

Supports:
- Unweighted graphs: BFS, O(V + E)
- Weighted graphs with non-negative weights: Dijkstra with min-heap, O(E log V)

Notes:
- This implementation is intended for sparse graphs.
- For extremely large graphs (millions of nodes), practical limits depend heavily on
  available RAM and graph density. Python object overhead can be substantial.
- Path reconstruction stores one parent per discovered node, so memory usage grows
  with the explored search space.
"""

import heapq
from collections import deque
from typing import Dict, Iterable, Iterator, List, Optional, Tuple


class Graph:
    """Adjacency-list graph representation optimized for sparse graphs."""

    def __init__(self, directed: bool = False):
        self.adj: Dict[int, List[Tuple[int, float]]] = {}
        self.directed = directed

    def add_node(self, node: int) -> None:
        if node not in self.adj:
            self.adj[node] = []

    def add_edge(self, u: int, v: int, weight: float = 1.0, directed: Optional[bool] = None) -> None:
        if weight < 0:
            raise ValueError("Negative edge weights are not allowed")

        if directed is None:
            directed = self.directed

        self.add_node(u)
        self.add_node(v)

        self.adj[u].append((v, float(weight)))
        if not directed:
            self.adj[v].append((u, float(weight)))

    def add_edges_from(self, edges: Iterable[Tuple], directed: Optional[bool] = None) -> None:
        if directed is None:
            directed = self.directed

        for edge in edges:
            if len(edge) == 2:
                u, v = edge
                self.add_edge(u, v, weight=1.0, directed=directed)
            elif len(edge) == 3:
                u, v, w = edge
                self.add_edge(u, v, weight=w, directed=directed)
            else:
                raise ValueError("Each edge must be a 2-tuple (u, v) or 3-tuple (u, v, weight)")

    def iter_neighbors(self, node: int) -> Iterator[Tuple[int, float]]:
        """Yield neighbors of a node. Treat yielded data as read-only graph state."""
        yield from self.adj.get(node, ())

    def get_neighbors(self, node: int) -> List[Tuple[int, float]]:
        """Return a shallow copy of a node's neighbors for compatibility with callers."""
        return list(self.adj.get(node, ()))

    def has_node(self, node: int) -> bool:
        return node in self.adj

    def num_nodes(self) -> int:
        return len(self.adj)

    def num_edges(self) -> int:
        total = sum(len(neighbors) for neighbors in self.adj.values())
        return total if self.directed else total // 2

    def validate_non_negative_weights(self) -> None:
        """Validate that all edge weights in the graph are non-negative."""
        for node, neighbors in self.adj.items():
            for neighbor, weight in neighbors:
                if weight < 0:
                    raise ValueError(f"Graph contains negative edge weight ({node} -> {neighbor}: {weight})")


def bfs_shortest_path(graph: Graph, start: int, end: int) -> Optional[List[int]]:
    """Return the shortest path in an unweighted graph, or None if unreachable."""
    if not graph.has_node(start) or not graph.has_node(end):
        return None
    if start == end:
        return [start]

    visited = {start}
    queue = deque([start])
    parent: Dict[int, Optional[int]] = {start: None}

    while queue:
        current = queue.popleft()

        for neighbor, _ in graph.iter_neighbors(current):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parent[neighbor] = current
            if neighbor == end:
                return _reconstruct_path(parent, start, end)
            queue.append(neighbor)

    return None


def dijkstra_shortest_path(graph: Graph, start: int, end: int) -> Optional[List[int]]:
    """Return the shortest path in a non-negative weighted graph, or None if unreachable.

    Raises ValueError if the graph contains negative edge weights.
    """
    graph.validate_non_negative_weights()

    if not graph.has_node(start) or not graph.has_node(end):
        return None
    if start == end:
        return [start]

    heap: List[Tuple[float, int]] = [(0.0, start)]
    distances: Dict[int, float] = {start: 0.0}
    parent: Dict[int, Optional[int]] = {start: None}
    visited = set()

    while heap:
        current_dist, current = heapq.heappop(heap)

        if current in visited:
            continue
        visited.add(current)

        if current == end:
            return _reconstruct_path(parent, start, end)

        for neighbor, weight in graph.iter_neighbors(current):
            if neighbor in visited:
                continue

            new_dist = current_dist + weight
            if new_dist < distances.get(neighbor, float("inf")):
                distances[neighbor] = new_dist
                parent[neighbor] = current
                heapq.heappush(heap, (new_dist, neighbor))

    return None


def _reconstruct_path(parent: Dict[int, Optional[int]], start: int, end: int) -> Optional[List[int]]:
    path = []
    current: Optional[int] = end

    while current is not None:
        path.append(current)
        if current == start:
            break
        current = parent.get(current)

    path.reverse()
    return path if path and path[0] == start else None


def find_shortest_path(graph: Graph, start: int, end: int, weighted: bool = False) -> Optional[List[int]]:
    """Dispatch to BFS or Dijkstra based on whether weights should be considered."""
    return dijkstra_shortest_path(graph, start, end) if weighted else bfs_shortest_path(graph, start, end)


if __name__ == "__main__":
    g = Graph()
    g.add_edges_from([(1, 2), (1, 3), (2, 4), (4, 5)])
    print("BFS:", bfs_shortest_path(g, 1, 5))

    gw = Graph(directed=True)
    gw.add_edges_from([(1, 2, 4), (1, 3, 2), (3, 4, 1), (4, 5, 8)])
    print("Dijkstra:", dijkstra_shortest_path(gw, 1, 5))
