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

        def _tool_use_ids(slice_: list) -> set[str]:
            ids: set[str] = set()
            for m in slice_:
                if m["role"] == "assistant":
                    c = m.get("content", [])
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                ids.add(b.get("id", ""))
            return ids

        def _has_orphan(slice_: list, valid_ids: set[str]) -> bool:
            for m in slice_:
                if m["role"] == "user":
                    c = m.get("content", [])
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                if b.get("tool_use_id", "") not in valid_ids:
                                    return True
            return False

        # Walk forward until we find a start position where:
        # 1. The first message is a plain user message (not tool_result)
        # 2. No tool_result in the resulting slice is orphaned
        start = len(tail)  # fallback: drop everything except messages[0]
        for i, m in enumerate(tail):
            if m["role"] != "user":
                continue
            content = m.get("content", "")
            is_tool_result = isinstance(content, list) and any(
                b.get("type") == "tool_result" for b in content
            )
            if is_tool_result:
                continue
            # Candidate start — verify no orphaned tool_results in tail[i:]
            candidate = tail[i:]
            if not _has_orphan(candidate, _tool_use_ids(candidate)):
                start = i
                break

        self.messages = [self.messages[0]] + tail[start:]

    def clear(self) -> None:
        self.messages.clear()
