from __future__ import annotations

from dataclasses import dataclass, field
from time import time

from orb.messaging.message import Message


@dataclass
class ConversationTurn:
    speaker: str
    audience: str
    kind: str
    content: str
    timestamp: float = field(default_factory=time)
    chain_id: str = ""
    depth: int = 0
    message_id: str = ""


class RunTranscript:
    """Runtime-owned transcript of user and agent conversation turns."""

    def __init__(self, max_turns: int = 500) -> None:
        self.max_turns = max_turns
        self.turns: list[ConversationTurn] = []

    def clear(self) -> None:
        self.turns.clear()

    def add_message(self, msg: Message) -> None:
        self._append(ConversationTurn(
            speaker=msg.from_,
            audience=msg.to,
            kind=msg.type.value,
            content=msg.payload,
            timestamp=msg.timestamp,
            chain_id=msg.chain_id,
            depth=msg.depth,
            message_id=msg.id,
        ))

    def add_completion(self, agent_id: str, result: str) -> None:
        self._append(ConversationTurn(
            speaker=agent_id,
            audience="runtime",
            kind="completion",
            content=result,
        ))

    def render_for_model(
        self,
        agent_id: str,
        *,
        max_chars: int | None = None,
    ) -> str:
        lines = [
            "Shared session transcript.",
            f"Current target agent: {agent_id}",
            "Use this transcript as the collaborative conversation context across user and agent turns.",
            "",
        ]

        for turn in self.turns:
            speaker = turn.speaker
            audience = turn.audience
            kind = turn.kind
            content = turn.content.replace("\n", " ").strip()
            if len(content) > 220:
                content = content[:220] + "…"
            lines.append(f"[{kind}] {speaker} -> {audience}: {content}")

        rendered = "\n".join(lines).strip()
        if max_chars is None or len(rendered) <= max_chars:
            return rendered

        clipped = rendered[-max_chars:]
        newline = clipped.find("\n")
        if newline != -1:
            clipped = clipped[newline + 1:]
        return "Shared session transcript (truncated to recent turns).\n" + clipped

    def _append(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]
