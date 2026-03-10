"""
Example usage of shortest path algorithms for large-scale graphs.

This example shows how to efficiently handle graphs with millions of nodes
using the implemented BFS and Dijkstra algorithms.
"""

from shortest_path import Graph, bfs_shortest_path, dijkstra_shortest_path, find_shortest_path
import time
import random


def example_1_social_network():
    """Example: Finding connections in a large social network."""
    print("Example 1: Social Network Graph")
    print("-" * 40)
    
    # Create a social network with 1 million users
    social_graph = Graph()
    num_users = 1_000_000
    
    print(f"Building social network with {num_users:,} users...")
    start_time = time.time()
    
    # Each user has connections (friends)
    # Using a power-law distribution (more realistic for social networks)
    for user in range(1, num_users + 1):
        # Number of connections follows a power law
        num_connections = min(int(random.paretovariate(1.5)), 1000)
        
        for _ in range(num_connections):
            friend = random.randint(1, num_users)
            if friend != user:
                social_graph.add_edge(user, friend)
        
        if user % 100_000 == 0:
            print(f"  Added {user:,} users...")
    
    build_time = time.time() - start_time
    print(f"Network built in {build_time:.2f} seconds")
    print(f"Total connections: {social_graph.num_edges():,}")
    
    # Find shortest path between two random users
    user1 = random.randint(1, num_users)
    user2 = random.randint(1, num_users)
    
    print(f"\nFinding shortest path between user {user1} and user {user2}...")
    start_time = time.time()
    path = bfs_shortest_path(social_graph, user1, user2)
    search_time = time.time() - start_time
    
    if path:
        print(f"Path found in {search_time:.3f} seconds")
        print(f"Degrees of separation: {len(path) - 1}")
        print(f"Path: {' -> '.join(map(str, path[:5]))}{'...' if len(path) > 5 else ''}")
    else:
        print(f"No connection found between users (searched in {search_time:.3f} seconds)")


def example_2_transport_network():
    """Example: Finding optimal routes in a transportation network."""
    print("\n\nExample 2: Transportation Network")
    print("-" * 40)
    
    # Create a transport network with weighted edges (distances)
    transport = Graph()
    num_cities = 100_000
    
    print(f"Building transport network with {num_cities:,} cities...")
    start_time = time.time()
    
    # Create a hub-and-spoke model with some direct connections
    num_hubs = 100
    hubs = list(range(1, num_hubs + 1))
    
    # Connect hubs to each other
    for i in range(len(hubs)):
        for j in range(i + 1, min(i + 10, len(hubs))):
            distance = random.uniform(100, 1000)  # km
            transport.add_edge(hubs[i], hubs[j], distance)
    
    # Connect cities to nearby hubs
    for city in range(num_hubs + 1, num_cities + 1):
        # Connect to 1-3 nearby hubs
        nearby_hubs = random.sample(hubs, random.randint(1, 3))
        for hub in nearby_hubs:
            distance = random.uniform(10, 200)  # km
            transport.add_edge(city, hub, distance)
        
        # Some direct connections between nearby cities
        if random.random() < 0.1:  # 10% chance
            nearby_city = city + random.randint(1, 100)
            if nearby_city <= num_cities:
                distance = random.uniform(5, 50)  # km
                transport.add_edge(city, nearby_city, distance)
    
    build_time = time.time() - start_time
    print(f"Network built in {build_time:.2f} seconds")
    print(f"Total routes: {transport.num_edges():,}")
    
    # Find shortest path between two cities
    start_city = random.randint(1, num_cities)
    end_city = random.randint(1, num_cities)
    
    print(f"\nFinding optimal route from city {start_city} to city {end_city}...")
    start_time = time.time()
    path = dijkstra_shortest_path(transport, start_city, end_city)
    search_time = time.time() - start_time
    
    if path:
        print(f"Route found in {search_time:.3f} seconds")
        print(f"Number of stops: {len(path) - 1}")
        print(f"Route: {' -> '.join(map(str, path[:5]))}{'...' if len(path) > 5 else ''}")
        
        # Calculate total distance
        total_distance = 0
        for i in range(len(path) - 1):
            for neighbor, weight in transport.get_neighbors(path[i]):
                if neighbor == path[i + 1]:
                    total_distance += weight
                    break
        print(f"Total distance: {total_distance:.1f} km")
    else:
        print(f"No route found between cities (searched in {search_time:.3f} seconds)")


def example_3_web_graph():
    """Example: Web page ranking and crawling paths."""
    print("\n\nExample 3: Web Graph (Page Links)")
    print("-" * 40)
    
    # Create a web graph with pages linking to each other
    web = Graph(directed=True)
    num_pages = 500_000
    
    print(f"Building web graph with {num_pages:,} pages...")
    start_time = time.time()
    
    # Popular pages (will have many incoming links)
    popular_pages = list(range(1, 1001))
    
    for page in range(1, num_pages + 1):
        # Number of outgoing links (follows power law)
        num_links = min(int(random.paretovariate(2)), 100)
        
        for _ in range(num_links):
            # Higher chance to link to popular pages
            if random.random() < 0.3:  # 30% chance to link to popular page
                target = random.choice(popular_pages)
            else:
                target = random.randint(1, num_pages)
            
            if target != page:
                web.add_edge(page, target)
        
        if page % 100_000 == 0:
            print(f"  Added {page:,} pages...")
    
    build_time = time.time() - start_time
    print(f"Web graph built in {build_time:.2f} seconds")
    print(f"Total links: {web.num_edges():,}")
    
    # Find crawl path from a page to a popular page
    start_page = random.randint(1001, num_pages)
    target_page = random.choice(popular_pages)
    
    print(f"\nFinding crawl path from page {start_page} to popular page {target_page}...")
    start_time = time.time()
    path = bfs_shortest_path(web, start_page, target_page)
    search_time = time.time() - start_time
    
    if path:
        print(f"Path found in {search_time:.3f} seconds")
        print(f"Number of clicks: {len(path) - 1}")
        print(f"Path: {' -> '.join(map(str, path))}")
    else:
        print(f"No path found (searched in {search_time:.3f} seconds)")


def main():
    """Run all examples."""
    print("=" * 60)
    print("LARGE-SCALE GRAPH SHORTEST PATH EXAMPLES")
    print("=" * 60)
    print()
    
    # Set random seed for reproducibility
    random.seed(42)
    
    # Run examples
    example_1_social_network()
    example_2_transport_network()
    example_3_web_graph()
    
    print("\n" + "=" * 60)
    print("Examples completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()