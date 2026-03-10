from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConversationHistory:
    """Per-agent message history manager."""

    max_messages: int = 20
    messages: list[dict] = field(default_factory=list)

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})
        self._trim()

    def add_assistant(self, content: str | list[dict]) -> None:
        self.messages.append({"role": "assistant", "content": content})
        self._trim()

    def add_tool_result(self, tool_use_id: str, content: str) -> None:
        # Anthropic requires all tool_results from one LLM turn to be in a
        # single user message.  Append to the existing tool_result message if
        # the last message is already one; otherwise start a new one.
        last = self.messages[-1] if self.messages else None
        if (
            last
            and last["role"] == "user"
            and isinstance(last.get("content"), list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            })
        else:
            self.messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
                ],
            })
        self._trim()

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def _trim(self) -> None:
        if len(self.messages) <= self.max_messages:
            return
        keep = self.max_messages
        tail = self.messages[-(keep - 1):]
        # Find a safe start point: a plain user message (not a tool_result).
        # Dropping mid-pair would leave orphaned tool_result blocks referencing
        # tool_use IDs no longer present, causing Anthropic 400 errors.
        start = 0
        for i, m in enumerate(tail):
            if m["role"] == "user":
                content = m.get("content", "")
                is_tool_result = isinstance(content, list) and any(
                    b.get("type") == "tool_result" for b in content
                )
                if not is_tool_result:
                    start = i
                    break
        self.messages = [self.messages[0]] + tail[start:]

    def clear(self) -> None:
        self.messages.clear()
