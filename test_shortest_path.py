"""Tests for shortest path algorithms."""

import time
import random
from shortest_path import Graph, bfs_shortest_path, dijkstra_shortest_path


def test_basic_correctness():
    print("=" * 60)
    print("Testing Basic Correctness")
    print("=" * 60)

    # Test 1: Simple path
    g = Graph()
    g.add_edges_from([(1, 2), (2, 3), (3, 4)])
    assert bfs_shortest_path(g, 1, 4) == [1, 2, 3, 4]
    print("✓ Simple path test passed")

    # Test 2: Multiple paths (should find shortest)
    g2 = Graph()
    g2.add_edges_from([
        (1, 2), (2, 5),  # Path 1: 1->2->5 (2 hops)
        (1, 3), (3, 4), (4, 5)  # Path 2: 1->3->4->5 (3 hops)
    ])
    assert bfs_shortest_path(g2, 1, 5) == [1, 2, 5]
    print("✓ Multiple paths test passed")

    # Test 3: Weighted graph with different optimal path
    gw = Graph(directed=False)
    gw.add_edges_from([
        (1, 2, 10), (2, 3, 1),  # Path 1: 1->2->3 (weight 11)
        (1, 3, 5)  # Path 2: 1->3 (weight 5)
    ])
    assert dijkstra_shortest_path(gw, 1, 3) == [1, 3]
    print("✓ Weighted graph test passed")

    # Test 4: Disconnected graph
    g_disc = Graph()
    g_disc.add_edges_from([(1, 2), (3, 4)])
    assert bfs_shortest_path(g_disc, 1, 4) is None
    print("✓ Disconnected graph test passed")

    # Test 5: Self-loop
    assert bfs_shortest_path(g, 2, 2) == [2]
    print("✓ Self-loop test passed")

    # Test 6: Non-existent nodes
    assert bfs_shortest_path(g, 1, 999) is None
    assert bfs_shortest_path(g, 999, 1) is None
    print("✓ Non-existent nodes test passed")


def test_error_handling():
    print("\n" + "=" * 60)
    print("Testing Error Handling")
    print("=" * 60)

    # Test negative weights
    g = Graph()
    try:
        g.add_edge(1, 2, -1.0)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        print("✓ Negative weight rejection in add_edge passed")

    # Test invalid edge tuples
    g = Graph()
    try:
        g.add_edges_from([(1,)])
        assert False, "Should have raised ValueError"
    except ValueError:
        print("✓ Invalid edge tuple rejection passed")

    try:
        g.add_edges_from([(1, 2, 3, 4)])
        assert False, "Should have raised ValueError"
    except ValueError:
        print("✓ Too-long edge tuple rejection passed")


def test_sparse_graph_performance():
    print("\n" + "=" * 60)
    print("Testing Sparse Graph Performance")
    print("=" * 60)

    sizes = [10_000, 100_000]

    for size in sizes:
        print(f"\nTesting with {size:,} nodes...")

        # Create a sparse graph
        g = Graph()

        start_time = time.time()

        # Linear chain + some shortcuts
        for i in range(1, size):
            g.add_edge(i, i + 1)
            if i % 1000 == 0 and i + 5000 <= size:
                g.add_edge(i, i + 5000)

        build_time = time.time() - start_time
        print(f"  Graph built in {build_time:.3f}s")
        print(f"  Nodes: {g.num_nodes():,}, Edges: {g.num_edges():,}")

        # Test BFS
        start_time = time.time()
        path = bfs_shortest_path(g, 1, size)
        bfs_time = time.time() - start_time

        if path:
            print(f"  BFS: Found path of length {len(path)} in {bfs_time:.3f}s")


def test_early_termination():
    print("\n" + "=" * 60)
    print("Testing Early Termination")
    print("=" * 60)

    # Create a graph where early termination matters
    g = Graph()
    # Create a long chain
    for i in range(1, 1001):
        g.add_edge(i, i + 1)
    # Add direct shortcut from 1 to 1001
    g.add_edge(1, 1001)

    start_time = time.time()
    path = bfs_shortest_path(g, 1, 1001)
    search_time = time.time() - start_time

    assert path == [1, 1001], f"Expected direct path, got {path}"
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