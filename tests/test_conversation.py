"""Tests for ConversationHistory — carryover safety and trimming."""
from __future__ import annotations

import pytest

from orb.agent.conversation import ConversationHistory


class TestConversationHistory:

    # ── Basic operations ─────────────────────────────────────────────────────

    def test_add_user_and_assistant(self):
        h = ConversationHistory()
        h.add_user("hello")
        h.add_assistant("hi")
        msgs = h.get_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi"}

    def test_add_tool_result_creates_user_message(self):
        h = ConversationHistory()
        h.add_assistant([{"type": "tool_use", "id": "t1", "name": "foo", "input": {}}])
        h.add_tool_result("t1", "result text")
        msgs = h.get_messages()
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"][0]["type"] == "tool_result"

    def test_consecutive_tool_results_merged_into_one_message(self):
        h = ConversationHistory()
        h.add_assistant([
            {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
        ])
        h.add_tool_result("t1", "r1")
        h.add_tool_result("t2", "r2")
        # Both results should be in the same user message
        msgs = h.get_messages()
        last = msgs[-1]
        assert last["role"] == "user"
        assert len(last["content"]) == 2

    # ── Carryover safety ─────────────────────────────────────────────────────

    def test_after_complete_task_history_ends_with_user(self):
        """Simulates a real agent conversation ending with complete_task tool result."""
        h = ConversationHistory()
        h.add_user("write a function")
        h.add_assistant([{"type": "tool_use", "id": "t1", "name": "complete_task",
                          "input": {"result": "Done"}}])
        h.add_tool_result("t1", "Task marked as complete")
        assert h.messages[-1]["role"] == "user"  # the bug condition

    def test_carryover_with_trailing_user_stripped_allows_add_user(self):
        """Stripping trailing user messages from carryover prevents consecutive user messages."""
        h = ConversationHistory()
        h.add_user("task 1")
        h.add_assistant([{"type": "tool_use", "id": "t1", "name": "complete_task",
                          "input": {"result": "Done"}}])
        h.add_tool_result("t1", "Task marked as complete")

        # Simulate server-side stripping before restoring carryover
        msgs = list(h.messages)
        while msgs and msgs[-1].get("role") == "user":
            msgs.pop()

        # Restore to a new agent's history
        h2 = ConversationHistory()
        h2.messages = msgs

        # This should now be safe — last msg is assistant, not user
        assert h2.messages[-1]["role"] == "assistant"

        # Adding next task must not create consecutive user messages
        h2.add_user("task 2")
        roles = [m["role"] for m in h2.messages]
        for i in range(1, len(roles)):
            assert not (roles[i] == "user" and roles[i - 1] == "user"), \
                f"Consecutive user messages at positions {i-1},{i}: {roles}"

    def test_carryover_strip_removes_dangling_tool_use(self):
        """After stripping, an assistant message with tool_use (no tool_result) must also be removed.

        This is the root cause of the 'unexpected tool_use_id in tool_result blocks' 400 error:
        stripping only the trailing tool_result leaves the preceding assistant tool_use dangling.
        The server strips backward until it reaches a clean assistant-text-only endpoint.
        """
        h = ConversationHistory()
        h.add_user("write a file")
        h.add_assistant([{"type": "tool_use", "id": "t1", "name": "write_file",
                          "input": {"path": "a.py", "content": "x"}}])
        h.add_tool_result("t1", "File written")
        h.add_assistant([{"type": "tool_use", "id": "t2", "name": "complete_task",
                          "input": {"result": "Done"}}])
        h.add_tool_result("t2", "Task marked as complete")

        # Simulate the server's new backward-stripping logic
        msgs = list(h.messages)
        while msgs:
            last = msgs[-1]
            role = last.get("role")
            content = last.get("content", "")
            if role == "user":
                msgs.pop()
                continue
            if role == "assistant" and isinstance(content, list) and any(
                b.get("type") == "tool_use" for b in content
            ):
                msgs.pop()
                continue
            break

        # All messages were tool_use/tool_result — carryover is empty (agent starts fresh)
        assert msgs == []

    def test_carryover_strip_preserves_text_assistant(self):
        """If there is a clean text-only assistant message, the strip stops there."""
        h = ConversationHistory()
        h.add_user("task")
        h.add_assistant("I'll help you with that.")  # text-only — clean endpoint
        h.add_user("thanks")  # plain text follow-up (not tool_result)
        h.add_assistant([{"type": "tool_use", "id": "t1", "name": "complete_task",
                          "input": {"result": "Done"}}])
        h.add_tool_result("t1", "Task marked as complete")

        msgs = list(h.messages)
        while msgs:
            last = msgs[-1]
            role = last.get("role")
            content = last.get("content", "")
            if role == "user":
                msgs.pop()
                continue
            if role == "assistant" and isinstance(content, list) and any(
                b.get("type") == "tool_use" for b in content
            ):
                msgs.pop()
                continue
            break

        # Stops at the text-only assistant message
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "I'll help you with that."

    def test_carryover_strip_allows_safe_add_user(self):
        """After the new backward-strip, adding a new task never creates an invalid conversation."""
        h = ConversationHistory()
        h.add_user("task 1")
        h.add_assistant([{"type": "tool_use", "id": "t1", "name": "write_file",
                          "input": {"path": "x.py", "content": ""}}])
        h.add_tool_result("t1", "File written")
        h.add_assistant([{"type": "tool_use", "id": "t2", "name": "complete_task",
                          "input": {"result": "Done"}}])
        h.add_tool_result("t2", "Task marked as complete")

        msgs = list(h.messages)
        while msgs:
            last = msgs[-1]
            role = last.get("role")
            content = last.get("content", "")
            if role == "user":
                msgs.pop()
                continue
            if role == "assistant" and isinstance(content, list) and any(
                b.get("type") == "tool_use" for b in content
            ):
                msgs.pop()
                continue
            break

        h2 = ConversationHistory()
        if msgs:
            h2.messages = msgs
        h2.add_user("task 2")

        # Must not have consecutive user messages
        roles = [m["role"] for m in h2.messages]
        for i in range(1, len(roles)):
            assert not (roles[i] == "user" and roles[i - 1] == "user"), \
                f"Consecutive user messages at {i-1},{i}: {roles}"

        # Must not have tool_result without preceding tool_use
        for i, m in enumerate(h2.messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
                    assert i > 0, "tool_result at position 0 has no preceding message"
                    prev = h2.messages[i - 1]
                    assert prev.get("role") == "assistant", \
                        f"tool_result at {i} not preceded by assistant message"
                    prev_content = prev.get("content", [])
                    assert isinstance(prev_content, list) and any(
                        b.get("type") == "tool_use" for b in prev_content
                    ), f"tool_result at {i} preceded by assistant with no tool_use"

    # ── Trimming ─────────────────────────────────────────────────────────────

    def test_trim_keeps_at_most_max_messages(self):
        h = ConversationHistory(max_messages=6)
        for i in range(10):
            if i % 2 == 0:
                h.add_user(f"u{i}")
            else:
                h.add_assistant(f"a{i}")
        assert len(h.messages) <= 6

    def test_trim_never_starts_with_tool_result(self):
        """After trim, the first message must not be a tool_result user block."""
        h = ConversationHistory(max_messages=4)
        h.add_user("task")
        for i in range(5):
            h.add_assistant([{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}])
            h.add_tool_result(f"t{i}", f"result {i}")
        first = h.messages[0]
        is_tool_result = (
            isinstance(first.get("content"), list)
            and any(b.get("type") == "tool_result" for b in first["content"])
        )
        assert not is_tool_result, "History must not start with a tool_result"

    def test_trim_preserves_first_message(self):
        h = ConversationHistory(max_messages=4)
        h.add_user("ORIGINAL TASK")
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            if role == "user":
                h.add_user(f"msg {i}")
            else:
                h.add_assistant(f"msg {i}")
        assert h.messages[0]["content"] == "ORIGINAL TASK"
