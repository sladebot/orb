"""Tests for graph storage functionality."""

import os
import tempfile
from shortest_path import Graph, bfs_shortest_path, dijkstra_shortest_path
from graph_storage import GraphStorage, save_graph, load_graph


def test_json_storage_undirected():
    """Test saving and loading an undirected graph in JSON format."""
    g = Graph(directed=False)
    g.add_edges_from([(1, 2, 1.5), (2, 3, 2.0), (3, 4, 1.0)])
    
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        filepath = f.name
    
    try:
        save_graph(g, filepath, format='json')
        loaded = load_graph(filepath)
        
        assert loaded.directed == g.directed
        assert loaded.num_nodes() == g.num_nodes()
        assert loaded.num_edges() == g.num_edges()
        assert bfs_shortest_path(loaded, 1, 4) == [1, 2, 3, 4]
    finally:
        os.unlink(filepath)


def test_json_storage_directed():
    """Test saving and loading a directed graph in JSON format."""
    g = Graph(directed=True)
    g.add_edges_from([(1, 2, 1.5), (2, 3, 2.0), (3, 1, 1.0)])
    
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        filepath = f.name
    
    try:
        save_graph(g, filepath, format='json')
        loaded = load_graph(filepath)
        
        assert loaded.directed == g.directed
        assert loaded.num_nodes() == g.num_nodes()
        assert loaded.num_edges() == g.num_edges()
        assert bfs_shortest_path(loaded, 1, 3) == [1, 2, 3]
        assert bfs_shortest_path(loaded, 3, 2) == [3, 1, 2]
    finally:
        os.unlink(filepath)


def test_pickle_storage():
    """Test saving and loading a graph in pickle format."""
    g = Graph(directed=False)
    g.add_edges_from([(1, 2, 1.5), (2, 3, 2.0), (1, 3, 5.0)])
    
    with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as f:
        filepath = f.name
    
    try:
        save_graph(g, filepath, format='pickle')
        loaded = load_graph(filepath)
        
        assert loaded.directed == g.directed
        assert loaded.num_nodes() == g.num_nodes()
        assert loaded.num_edges() == g.num_edges()
        assert dijkstra_shortest_path(loaded, 1, 3) == [1, 2, 3]
    finally:
        os.unlink(filepath)


def test_binary_storage():
    """Test saving and loading a graph in binary format."""
    g = Graph(directed=False)
    g.add_edges_from([(1, 2, 1.5), (2, 3, 2.0), (3, 4, 1.0), (1, 4, 10.0)])
    
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        filepath = f.name
    
    try:
        save_graph(g, filepath, format='binary')
        loaded = load_graph(filepath)
        
        assert loaded.directed == g.directed
        assert loaded.num_nodes() == g.num_nodes()
        assert loaded.num_edges() == g.num_edges()
        assert dijkstra_shortest_path(loaded, 1, 4) == [1, 2, 3, 4]
    finally:
        os.unlink(filepath)


def test_format_inference():
    """Test automatic format inference from file extension."""
    g = Graph()
    g.add_edge(1, 2, 1.0)
    
    # Test JSON inference
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        json_path = f.name
    try:
        save_graph(g, json_path)  # Default is json
        loaded = load_graph(json_path)  # Should infer json
        assert loaded.num_edges() == 1
    finally:
        os.unlink(json_path)


def test_invalid_format():
    """Test error handling for invalid formats."""
    g = Graph()
    g.add_edge(1, 2)
    
    try:
        save_graph(g, 'test.xyz', format='xyz')
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Unsupported format" in str(e)


def test_large_graph_storage():
    """Test storage with a moderately large graph."""
    g = Graph(directed=False)
    
    # Create a graph with 1000 nodes in a chain
    for i in range(1000):
        if i < 999:
            g.add_edge(i, i + 1, 1.0)
    
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        filepath = f.name
    
    try:
        save_graph(g, filepath, format='binary')
        loaded = load_graph(filepath)
        
        assert loaded.num_nodes() == 1000
        assert loaded.num_edges() == 999
        path = bfs_shortest_path(loaded, 0, 999)
        assert len(path) == 1000
    finally:
        os.unlink(filepath)


if __name__ == '__main__':
    test_json_storage_undirected()
    test_json_storage_directed()
    test_pickle_storage()
    test_binary_storage()
    test_format_inference()
    test_invalid_format()
    test_large_graph_storage()
    print('All graph storage tests passed!')