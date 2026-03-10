"""
Graph storage module for persisting graphs to disk.

Supports saving and loading graph structures with:
- Node and edge data
- Directed/undirected graphs
- Edge weights
- Multiple storage formats (JSON, pickle, custom binary)
"""

import json
import pickle
import struct
from pathlib import Path
from typing import Optional, Union
from shortest_path import Graph


class GraphStorage:
    """Handles persistence of Graph objects to disk."""
    
    @staticmethod
    def save_json(graph: Graph, filepath: Union[str, Path]) -> None:
        """Save graph to JSON format (human-readable but larger)."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "directed": graph.directed,
            "nodes": list(graph.adj.keys()),
            "edges": []
        }
        
        seen_edges = set()
        for node, neighbors in graph.adj.items():
            for neighbor, weight in neighbors:
                if graph.directed:
                    data["edges"].append({
                        "from": node,
                        "to": neighbor,
                        "weight": weight
                    })
                else:
                    edge_key = tuple(sorted((node, neighbor)))
                    if edge_key not in seen_edges:
                        data["edges"].append({
                            "from": node,
                            "to": neighbor,
                            "weight": weight
                        })
                        seen_edges.add(edge_key)
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
    
    @staticmethod
    def load_json(filepath: Union[str, Path]) -> Graph:
        """Load graph from JSON format."""
        filepath = Path(filepath)
        with open(filepath, 'r') as f:
            data = json.load(f)
        
        graph = Graph(directed=data["directed"])
        
        for node in data["nodes"]:
            graph.add_node(node)
        
        for edge in data["edges"]:
            graph.add_edge(edge["from"], edge["to"], edge["weight"])
        
        return graph
    
    @staticmethod
    def save_pickle(graph: Graph, filepath: Union[str, Path]) -> None:
        """Save graph using pickle (fast, compact, Python-specific)."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'wb') as f:
            pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)
    
    @staticmethod
    def load_pickle(filepath: Union[str, Path]) -> Graph:
        """Load graph from pickle format."""
        filepath = Path(filepath)
        with open(filepath, 'rb') as f:
            return pickle.load(f)
    
    @staticmethod
    def save_binary(graph: Graph, filepath: Union[str, Path]) -> None:
        """Save graph in custom binary format (most compact for large graphs)."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        nodes = sorted(graph.adj.keys())
        edges = []
        seen_edges = set()
        
        for node, neighbors in graph.adj.items():
            for neighbor, weight in neighbors:
                if graph.directed:
                    edges.append((node, neighbor, weight))
                else:
                    edge_key = tuple(sorted((node, neighbor)))
                    if edge_key not in seen_edges:
                        edges.append((node, neighbor, weight))
                        seen_edges.add(edge_key)
        
        with open(filepath, 'wb') as f:
            f.write(struct.pack('?', graph.directed))
            f.write(struct.pack('I', len(nodes)))
            f.write(struct.pack('I', len(edges)))
            
            for node in nodes:
                f.write(struct.pack('i', node))
            
            for from_node, to_node, weight in edges:
                f.write(struct.pack('i', from_node))
                f.write(struct.pack('i', to_node))
                f.write(struct.pack('d', weight))
    
    @staticmethod
    def load_binary(filepath: Union[str, Path]) -> Graph:
        """Load graph from custom binary format."""
        filepath = Path(filepath)
        with open(filepath, 'rb') as f:
            directed = struct.unpack('?', f.read(1))[0]
            num_nodes = struct.unpack('I', f.read(4))[0]
            num_edges = struct.unpack('I', f.read(4))[0]
            
            graph = Graph(directed=directed)
            
            for _ in range(num_nodes):
                node = struct.unpack('i', f.read(4))[0]
                graph.add_node(node)
            
            for _ in range(num_edges):
                from_node = struct.unpack('i', f.read(4))[0]
                to_node = struct.unpack('i', f.read(4))[0]
                weight = struct.unpack('d', f.read(8))[0]
                graph.add_edge(from_node, to_node, weight)
        
        return graph
    
    @staticmethod
    def save(graph: Graph, filepath: Union[str, Path], format: str = 'json') -> None:
        """Save graph to disk in specified format."""
        if format == 'json':
            GraphStorage.save_json(graph, filepath)
        elif format == 'pickle':
            GraphStorage.save_pickle(graph, filepath)
        elif format == 'binary':
            GraphStorage.save_binary(graph, filepath)
        else:
            raise ValueError(f"Unsupported format: {format}. Use 'json', 'pickle', or 'binary'.")
    
    @staticmethod
    def load(filepath: Union[str, Path], format: Optional[str] = None) -> Graph:
        """Load graph from disk."""
        filepath = Path(filepath)
        
        if format is None:
            ext = filepath.suffix.lower()
            if ext == '.json':
                format = 'json'
            elif ext in ['.pkl', '.pickle']:
                format = 'pickle'
            elif ext in ['.bin', '.dat']:
                format = 'binary'
            else:
                raise ValueError(f"Cannot infer format from extension: {ext}")
        
        if format == 'json':
            return GraphStorage.load_json(filepath)
        elif format == 'pickle':
            return GraphStorage.load_pickle(filepath)
        elif format == 'binary':
            return GraphStorage.load_binary(filepath)
        else:
            raise ValueError(f"Unsupported format: {format}. Use 'json', 'pickle', or 'binary'.")


def save_graph(graph: Graph, filepath: Union[str, Path], format: str = 'json') -> None:
    """Save a graph to disk. Convenience wrapper for GraphStorage.save()."""
    GraphStorage.save(graph, filepath, format)


def load_graph(filepath: Union[str, Path], format: Optional[str] = None) -> Graph:
    """Load a graph from disk. Convenience wrapper for GraphStorage.load()."""
    return GraphStorage.load(filepath, format)


if __name__ == "__main__":
    g = Graph()
    g.add_edges_from([(1, 2, 1.5), (2, 3, 2.0), (3, 4, 1.0), (1, 4, 3.5)])
    
    save_graph(g, "test_graph.json", format="json")
    save_graph(g, "test_graph.pkl", format="pickle")
    save_graph(g, "test_graph.bin", format="binary")
    
    g_json = load_graph("test_graph.json")
    g_pickle = load_graph("test_graph.pkl")
    g_binary = load_graph("test_graph.bin")
    
    print("Graph saved and loaded successfully in all formats!")