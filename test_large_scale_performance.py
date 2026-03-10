"""
Test performance on extremely large graphs (up to 10M nodes).
"""

import time
import random
import sys
from shortest_path import Graph, bfs_shortest_path, dijkstra_shortest_path


def test_10_million_nodes():
    """Test with 10 million nodes - the stated upper limit."""
    print("=" * 80)
    print("EXTREME SCALE TEST: 10 MILLION NODES")
    print("=" * 80)
    
    g = Graph()
    num_nodes = 10_000_000
    
    print(f"\nBuilding sparse graph with {num_nodes:,} nodes...")
    start_time = time.time()
    
    # Build a very sparse graph to fit in memory
    # Linear chain with occasional long-range connections
    for i in range(1, num_nodes):
        if i % 100000 == 0:
            print(f"  Progress: {i:,} nodes added...")
        
        # Linear connection
        g.add_edge(i, i + 1)
        
        # Add sparse long-range connections
        if i % 10000 == 0 and i + 100000 <= num_nodes:
            g.add_edge(i, i + 100000)
    
    build_time = time.time() - start_time
    print(f"\nGraph construction completed in {build_time:.2f} seconds")
    print(f"Nodes: {g.num_nodes():,}")
    print(f"Edges: {g.num_edges():,}")
    print(f"Average degree: {2 * g.num_edges() / g.num_nodes():.2f}")
    
    # Test BFS on the extreme graph
    print("\n" + "-" * 60)
    print("Testing BFS shortest path...")
    
    test_pairs = [
        (1, 1000),
        (1, 10000),
        (1, 100000),
        (1, 1000000),
        (1, num_nodes)
    ]
    
    for start, end in test_pairs:
        start_time = time.time()
        path = bfs_shortest_path(g, start, end)
        search_time = time.time() - start_time
        
        if path:
            print(f"  Path from {start:,} to {end:,}: {len(path):,} nodes in {search_time:.3f}s")
        else:
            print(f"  No path from {start:,} to {end:,}")
    
    # Memory usage estimate
    print("\n" + "-" * 60)
    print("Memory usage analysis:")
    
    # Rough estimate of memory usage
    node_memory = sys.getsizeof(g.adj) / (1024 * 1024)
    print(f"  Adjacency list size: ~{node_memory:.1f} MB")
    print(f"  Memory per node: ~{node_memory * 1024 * 1024 / g.num_nodes():.1f} bytes")
    
    # Test a random path to ensure robustness
    print("\n" + "-" * 60)
    print("Testing random source/destination pairs...")
    
    for _ in range(3):
        start = random.randint(1, num_nodes // 2)
        end = random.randint(num_nodes // 2, num_nodes)
        
        start_time = time.time()
        path = bfs_shortest_path(g, start, end)
        search_time = time.time() - start_time
        
        if path:
            print(f"  Path from {start:,} to {end:,}: {len(path):,} nodes in {search_time:.3f}s")


def test_dense_region_performance():
    """Test performance on graph with dense regions (worst case for memory)."""
    print("\n" + "=" * 80)
    print("DENSE REGION TEST: 1M nodes with high-degree hubs")
    print("=" * 80)
    
    g = Graph()
    num_nodes = 1_000_000
    hub_nodes = 100  # Number of high-degree hubs
    
    print(f"\nBuilding graph with {hub_nodes} high-degree hubs...")
    start_time = time.time()
    
    # Create hubs
    for i in range(1, hub_nodes + 1):
        g.add_node(i)
    
    # Connect regular nodes to hubs
    for i in range(hub_nodes + 1, num_nodes + 1):
        # Connect to 2-3 random hubs
        num_connections = random.randint(2, 3)
        hubs = random.sample(range(1, hub_nodes + 1), num_connections)
        
        for hub in hubs:
            g.add_edge(i, hub)
        
        # Also create some local structure
        if i > hub_nodes + 1:
            g.add_edge(i, i - 1)
    
    build_time = time.time() - start_time
    print(f"Graph built in {build_time:.2f}s")
    print(f"Nodes: {g.num_nodes():,}, Edges: {g.num_edges():,}")
    
    # Test paths through hubs
    print("\nTesting paths through hub network...")
    
    for _ in range(5):
        start = random.randint(hub_nodes + 1, num_nodes)
        end = random.randint(hub_nodes + 1, num_nodes)
        
        start_time = time.time()
        path = bfs_shortest_path(g, start, end)
        search_time = time.time() - start_time
        
        if path:
            print(f"  Path length: {len(path)} nodes in {search_time:.3f}s")


if __name__ == "__main__":
    print("LARGE SCALE PERFORMANCE TESTS")
    print("Testing shortest path algorithms at extreme scales\n")
    
    # Run the extreme test
    test_10_million_nodes()
    
    # Run dense region test
    test_dense_region_performance()
    
    print("\n" + "=" * 80)
    print("PERFORMANCE TESTS COMPLETED")
    print("=" * 80)