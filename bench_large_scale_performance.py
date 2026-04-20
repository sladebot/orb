"""Manual large-scale performance checks for shortest path algorithms.

This is intentionally not a pytest test module. Run directly when profiling.

Usage:
  python bench_large_scale_performance.py            # quick smoke mode
  BENCH_MODE=full python bench_large_scale_performance.py  # full benchmark
"""

import os
import random
import sys
import time

from shortest_path import Graph, bfs_shortest_path


def build_sparse_graph(num_nodes: int) -> Graph:
    g = Graph()
    for i in range(1, num_nodes):
        g.add_edge(i, i + 1)
        if i % 10000 == 0 and i + 100000 <= num_nodes:
            g.add_edge(i, i + 100000)
    return g


def run_sparse_benchmark(num_nodes: int, random_trials: int) -> None:
    print("=" * 80)
    print(f"SPARSE GRAPH BENCHMARK: {num_nodes:,} NODES")
    print("=" * 80)

    start_time = time.time()
    g = build_sparse_graph(num_nodes)
    build_time = time.time() - start_time
    print(f"Built in {build_time:.2f}s; nodes={g.num_nodes():,}; edges={g.num_edges():,}")

    for start, end in [(1, 1000), (1, 10000), (1, min(100000, num_nodes)), (1, num_nodes)]:
        start_time = time.time()
        path = bfs_shortest_path(g, start, end)
        search_time = time.time() - start_time
        assert path is not None
        assert path[0] == start and path[-1] == end
        print(f"  {start:,}->{end:,}: {len(path):,} nodes in {search_time:.3f}s")

    for _ in range(random_trials):
        start = random.randint(1, max(1, num_nodes // 2))
        end = random.randint(max(1, num_nodes // 2), num_nodes)
        start_time = time.time()
        path = bfs_shortest_path(g, start, end)
        search_time = time.time() - start_time
        assert path is not None
        assert path[0] == start and path[-1] == end
        print(f"  {start:,}->{end:,}: {len(path):,} nodes in {search_time:.3f}s")

    node_memory = sys.getsizeof(g.adj) / (1024 * 1024)
    print(f"Adjacency list size: ~{node_memory:.1f} MB")


def run_dense_benchmark(num_nodes: int, hub_nodes: int, trials: int) -> None:
    print("\n" + "=" * 80)
    print(f"DENSE REGION BENCHMARK: {num_nodes:,} NODES, {hub_nodes} HUBS")
    print("=" * 80)

    g = Graph()
    for i in range(1, hub_nodes + 1):
        g.add_node(i)

    start_time = time.time()
    for i in range(hub_nodes + 1, num_nodes + 1):
        for hub in random.sample(range(1, hub_nodes + 1), random.randint(2, 3)):
            g.add_edge(i, hub)
        if i > hub_nodes + 1:
            g.add_edge(i, i - 1)
    build_time = time.time() - start_time
    print(f"Built in {build_time:.2f}s; nodes={g.num_nodes():,}; edges={g.num_edges():,}")

    for _ in range(trials):
        start = random.randint(hub_nodes + 1, num_nodes)
        end = random.randint(hub_nodes + 1, num_nodes)
        start_time = time.time()
        path = bfs_shortest_path(g, start, end)
        search_time = time.time() - start_time
        assert path is not None
        assert path[0] == start and path[-1] == end
        print(f"  {start:,}->{end:,}: {len(path)} nodes in {search_time:.3f}s")


def main() -> None:
    mode = os.getenv("BENCH_MODE", "smoke").lower()
    if mode == "full":
        run_sparse_benchmark(num_nodes=10_000_000, random_trials=3)
        run_dense_benchmark(num_nodes=100_000, hub_nodes=100, trials=5)
    else:
        run_sparse_benchmark(num_nodes=100_000, random_trials=1)
        run_dense_benchmark(num_nodes=10_000, hub_nodes=50, trials=2)


if __name__ == "__main__":
    main()
