"""Tests for shortest path algorithms."""

import random
import time

from shortest_path import Graph, bfs_shortest_path, dijkstra_shortest_path


def test_basic_correctness():
    print("=" * 60)
    print("Testing Basic Correctness")
    print("=" * 60)

    g = Graph()
    g.add_edges_from([(1, 2), (2, 3), (3, 4)])
    assert bfs_shortest_path(g, 1, 4) == [1, 2, 3, 4]

    g2 = Graph()
    g2.add_edges_from([
        (1, 2), (2, 5),
        (1, 3), (3, 4), (4, 5),
    ])
    assert bfs_shortest_path(g2, 1, 5) == [1, 2, 5]

    gw = Graph(directed=False)
    gw.add_edges_from([
        (1, 2, 10), (2, 3, 1),
        (1, 3, 5),
    ])
    assert dijkstra_shortest_path(gw, 1, 3) == [1, 3]

    g_disc = Graph()
    g_disc.add_edges_from([(1, 2), (3, 4)])
    assert bfs_shortest_path(g_disc, 1, 4) is None

    assert bfs_shortest_path(g, 2, 2) == [2]
    assert bfs_shortest_path(g, 1, 999) is None
    assert bfs_shortest_path(g, 999, 1) is None


def test_error_handling():
    print("\n" + "=" * 60)
    print("Testing Error Handling")
    print("=" * 60)

    g = Graph()
    try:
        g.add_edge(1, 2, -1.0)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    g = Graph()
    try:
        g.add_edges_from([(1,)])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    try:
        g.add_edges_from([(1, 2, 3, 4)])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_sparse_graph_performance():
    print("\n" + "=" * 60)
    print("Testing Sparse Graph Performance")
    print("=" * 60)

    size = 10_000
    g = Graph()

    start_time = time.time()
    for i in range(1, size):
        g.add_edge(i, i + 1)
        if i % 1000 == 0 and i + 5000 <= size:
            g.add_edge(i, i + 5000)

    build_time = time.time() - start_time
    print(f"  Graph built in {build_time:.3f}s")
    assert g.num_nodes() == size
    assert g.num_edges() >= size - 1

    start_time = time.time()
    path = bfs_shortest_path(g, 1, size)
    bfs_time = time.time() - start_time

    assert path is not None
    assert path[0] == 1 and path[-1] == size
    assert len(path) <= size
    print(f"  BFS found path of length {len(path)} in {bfs_time:.3f}s")


def test_early_termination():
    print("\n" + "=" * 60)
    print("Testing Early Termination")
    print("=" * 60)

    g = Graph()
    for i in range(1, 1001):
        g.add_edge(i, i + 1)
    g.add_edge(1, 1001)

    start_time = time.time()
    path = bfs_shortest_path(g, 1, 1001)
    search_time = time.time() - start_time

    assert path == [1, 1001]
    print(f"✓ Early termination test passed (found in {search_time:.6f}s)")


def run_all_tests():
    print("SHORTEST PATH ALGORITHM TEST SUITE")
    print("=" * 60)
    print()

    test_basic_correctness()
    test_error_handling()
    test_sparse_graph_performance()
    test_early_termination()

    print("\n" + "=" * 60)
    print("ALL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    run_all_tests()
