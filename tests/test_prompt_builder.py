from orb.agent.prompt_builder import build_system_prompt
from orb.agent.types import TopologyContext


class TestPromptBuilder:
    def test_basic_prompt(self):
        prompt = build_system_prompt(
            role="Coder",
            description="You write code.",
            neighbors={"reviewer": "Reviewer", "tester": "Tester"},
        )
        assert "Coder" in prompt
        assert "You write code." in prompt
        assert "reviewer" in prompt
        assert "tester" in prompt
        assert "send_message" in prompt
        assert "complete_task" in prompt

    def test_includes_all_neighbors(self):
        prompt = build_system_prompt(
            role="Reviewer",
            description="You review code.",
            neighbors={"coder": "Coder", "tester": "Tester"},
        )
        assert "coder" in prompt
        assert "tester" in prompt

    def test_includes_runtime_topology_context(self):
        prompt = build_system_prompt(
            role="Coder",
            description="You write code.",
            neighbors={"reviewer": "Reviewer", "tester": "Tester"},
            topology=TopologyContext(
                topology_id="triangle",
                topology_label="Triad",
                node_id="coder",
                role="Coder",
                direct_neighbors={"coordinator": "Coordinator", "reviewer": "Reviewer", "tester": "Tester"},
                graph_edges=[("coder", "reviewer"), ("coder", "tester"), ("coordinator", "coder"), ("reviewer", "tester")],
                node_roles={
                    "coordinator": "Coordinator",
                    "coder": "Coder",
                    "reviewer": "Reviewer",
                    "tester": "Tester",
                },
                workflow_steps=["Coder implements", "Reviewer reviews", "Tester validates"],
                completion_rules=["Send work to reviewer", "Send work to tester before completion"],
            ),
        )
        assert "Runtime Topology Context" in prompt
        assert "Triad" in prompt
        assert "coder" in prompt
        assert "coordinator" in prompt
        assert "Graph Edges" in prompt
        assert "Send work to reviewer" in prompt
