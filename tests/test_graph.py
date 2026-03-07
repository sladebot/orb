import pytest

from orb.graph import Graph, Edge


class TestGraph:
    def test_add_node(self):
        g = Graph()
        g.add_node("a", data={"role": "coder"})
        assert g.has_node("a")
        assert g.get_node_data("a") == {"role": "coder"}

    def test_add_edge(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        assert g.has_edge("a", "b")
        assert g.has_edge("b", "a")  # undirected

    def test_add_edge_missing_node(self):
        g = Graph()
        g.add_node("a")
        with pytest.raises(KeyError):
            g.add_edge("a", "b")

    def test_get_neighbors(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        assert g.get_neighbors("a") == {"b", "c"}
        assert g.get_neighbors("b") == {"a"}

    def test_get_neighbors_missing_node(self):
        g = Graph()
        with pytest.raises(KeyError):
            g.get_neighbors("x")

    def test_remove_node(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        g.remove_node("a")
        assert not g.has_node("a")
        assert not g.has_edge("a", "b")
        assert g.get_neighbors("b") == set()

    def test_remove_edge(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        g.remove_edge("a", "b")
        assert not g.has_edge("a", "b")
        assert not g.has_edge("b", "a")

    def test_remove_edge_missing(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        with pytest.raises(KeyError):
            g.remove_edge("a", "b")

    def test_nodes_property(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        assert g.nodes == {"a", "b"}

    def test_edges_property(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        edges = g.edges
        assert Edge("a", "b") in edges

    def test_edge_equality(self):
        assert Edge("a", "b") == Edge("b", "a")
        assert Edge("a", "b") != Edge("a", "c")

    def test_triangle(self):
        g = Graph()
        for n in ["coder", "reviewer", "tester"]:
            g.add_node(n)
        g.add_edge("coder", "reviewer")
        g.add_edge("coder", "tester")
        g.add_edge("reviewer", "tester")

        assert g.get_neighbors("coder") == {"reviewer", "tester"}
        assert g.get_neighbors("reviewer") == {"coder", "tester"}
        assert g.get_neighbors("tester") == {"coder", "reviewer"}
        assert len(g.edges) == 3
