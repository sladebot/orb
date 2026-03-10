import pytest
from shortest_path import Graph, bfs_shortest_path, dijkstra_shortest_path, find_shortest_path


def test_directed_bfs_respects_edge_direction():
    g = Graph(directed=True)
    g.add_edges_from([(1, 2), (2, 3)])
    assert bfs_shortest_path(g, 1, 3) == [1, 2, 3]
    assert bfs_shortest_path(g, 3, 1) is None


def test_directed_dijkstra_respects_edge_direction():
    g = Graph(directed=True)
    g.add_edges_from([(1, 2, 1), (2, 3, 1)])
    assert dijkstra_shortest_path(g, 1, 3) == [1, 2, 3]
    assert dijkstra_shortest_path(g, 3, 1) is None


def test_add_node_allows_zero_length_path():
    g = Graph()
    g.add_node(42)
    assert bfs_shortest_path(g, 42, 42) == [42]
    assert dijkstra_shortest_path(g, 42, 42) == [42]


def test_find_shortest_path_dispatch():
    g1 = Graph()
    g1.add_edges_from([(1, 2), (2, 3)])
    assert find_shortest_path(g1, 1, 3, weighted=False) == [1, 2, 3]

    g2 = Graph()
    g2.add_edges_from([(1, 2, 5), (1, 3, 1), (3, 2, 1)])
    assert find_shortest_path(g2, 1, 2, weighted=True) == [1, 3, 2]


def test_dijkstra_handles_zero_weight_edges():
    g = Graph(directed=True)
    g.add_edges_from([(1, 2, 0), (2, 3, 0), (1, 3, 5)])
    assert dijkstra_shortest_path(g, 1, 3) == [1, 2, 3]


def test_negative_weight_rejected_at_construction():
    """Test that negative weights are rejected when adding edges."""
    g = Graph(directed=True)
    
    # Test add_edge rejects negative weight
    with pytest.raises(ValueError, match="Negative edge weights are not allowed"):
        g.add_edge(1, 2, -5)
    
    # Test add_edges_from rejects negative weight
    with pytest.raises(ValueError, match="Negative edge weights are not allowed"):
        g.add_edges_from([(1, 2, 2), (3, 2, -10)])


def test_dijkstra_validates_corrupted_graph():
    """Test that Dijkstra validates graph even if weights are manually injected."""
    g = Graph(directed=True)
    g.add_edges_from([(1, 2, 2), (1, 3, 5), (2, 4, 1)])
    
    # Manually corrupt the graph with negative weight (simulating external mutation)
    g.adj[3] = [(2, -10)]
    
    # Dijkstra should detect and reject the negative weight
    with pytest.raises(ValueError, match="Graph contains negative edge weight"):
        dijkstra_shortest_path(g, 1, 4)


def test_empty_graph_paths():
    """Test behavior on empty graphs."""
    g = Graph()
    assert bfs_shortest_path(g, 1, 2) is None
    assert dijkstra_shortest_path(g, 1, 2) is None


def test_isolated_nodes():
    """Test paths between isolated nodes."""
    g = Graph()
    g.add_node(1)
    g.add_node(2)
    g.add_edge(3, 4)
    
    assert bfs_shortest_path(g, 1, 2) is None
    assert dijkstra_shortest_path(g, 1, 2) is None
    assert bfs_shortest_path(g, 3, 4) == [3, 4]