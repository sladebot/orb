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
        self.messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        })
        self._trim()

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def _trim(self) -> None:
        if len(self.messages) > self.max_messages:
            # Keep first message (usually the initial task) and trim from the middle
            keep = self.max_messages
            self.messages = [self.messages[0]] + self.messages[-(keep - 1):]

    def clear(self) -> None:
        self.messages.clear()
