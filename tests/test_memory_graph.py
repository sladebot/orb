import pytest

from orb.memory import MemoryGraph, MemoryNode, MemoryEdge


class TestMemoryGraph:
    def test_add_and_get_node(self):
        mg = MemoryGraph()
        node = MemoryNode(id="n1", content="hello", node_type="text")
        mg.add_node(node)
        assert mg.get_node("n1").content == "hello"

    def test_get_missing_node(self):
        mg = MemoryGraph()
        with pytest.raises(KeyError):
            mg.get_node("missing")

    def test_add_edge(self):
        mg = MemoryGraph()
        mg.add_node(MemoryNode(id="n1", content="a", node_type="text"))
        mg.add_node(MemoryNode(id="n2", content="b", node_type="text"))
        mg.add_edge(MemoryEdge(from_id="n1", to_id="n2", relation="related_to"))
        assert len(mg.edges) == 1

    def test_add_edge_missing_node(self):
        mg = MemoryGraph()
        mg.add_node(MemoryNode(id="n1", content="a", node_type="text"))
        with pytest.raises(KeyError):
            mg.add_edge(MemoryEdge(from_id="n1", to_id="missing", relation="r"))

    def test_get_connected(self):
        mg = MemoryGraph()
        mg.add_node(MemoryNode(id="n1", content="root", node_type="text"))
        mg.add_node(MemoryNode(id="n2", content="child", node_type="text"))
        mg.add_node(MemoryNode(id="n3", content="grandchild", node_type="text"))
        mg.add_edge(MemoryEdge(from_id="n1", to_id="n2", relation="derived_from"))
        mg.add_edge(MemoryEdge(from_id="n2", to_id="n3", relation="derived_from"))

        # Depth 1: only direct neighbors
        connected = mg.get_connected("n1", depth=1)
        assert len(connected) == 1
        assert connected[0].id == "n2"

        # Depth 2: includes grandchild
        connected = mg.get_connected("n1", depth=2)
        assert len(connected) == 2

    def test_remove_node(self):
        mg = MemoryGraph()
        mg.add_node(MemoryNode(id="n1", content="a", node_type="text"))
        mg.add_node(MemoryNode(id="n2", content="b", node_type="text"))
        mg.add_edge(MemoryEdge(from_id="n1", to_id="n2", relation="r"))
        mg.remove_node("n1")
        assert len(mg.nodes) == 1
        assert len(mg.edges) == 0

    def test_update_node(self):
        mg = MemoryGraph()
        mg.add_node(MemoryNode(id="n1", content="old", node_type="text"))
        mg.update_node("n1", "new")
        assert mg.get_node("n1").content == "new"

    def test_query_by_type(self):
        mg = MemoryGraph()
        mg.add_node(MemoryNode(id="n1", content="code", node_type="code"))
        mg.add_node(MemoryNode(id="n2", content="review", node_type="review"))
        mg.add_node(MemoryNode(id="n3", content="more code", node_type="code"))
        results = mg.query_by_type("code")
        assert len(results) == 2
