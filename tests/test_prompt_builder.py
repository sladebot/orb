from orb.agent.prompt_builder import build_system_prompt


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
