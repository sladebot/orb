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
        neighbor_ids: list[str] | None = None,
        focus_chain_id: str = "",
        max_turns: int = 40,
        max_chars: int = 6000,
    ) -> str:
        neighbors = set(neighbor_ids or [])
        selected: list[ConversationTurn] = []
        for turn in self.turns:
            if turn.chain_id and focus_chain_id and turn.chain_id == focus_chain_id:
                selected.append(turn)
                continue
            if turn.speaker == "user" or turn.audience == "user":
                selected.append(turn)
                continue
            if turn.speaker == agent_id or turn.audience == agent_id:
                selected.append(turn)
                continue
            if (
                (turn.speaker == agent_id and turn.audience in neighbors)
                or (turn.audience == agent_id and turn.speaker in neighbors)
            ):
                selected.append(turn)
                continue
            if turn.kind == "completion" and turn.speaker in neighbors:
                selected.append(turn)

        if not selected:
            selected = list(self.turns)

        recent = selected[-max_turns:]
        omitted = selected[:-max_turns]
        lines = [
            "Shared session transcript.",
            f"Current target agent: {agent_id}",
            "Use this transcript as the collaborative conversation context across user and agent turns.",
            "",
        ]
        if omitted:
            lines.extend(self._compact_turns(omitted))
            lines.append("")

        for turn in recent:
            speaker = turn.speaker
            audience = turn.audience
            kind = turn.kind
            content = turn.content.replace("\n", " ").strip()
            if len(content) > 220:
                content = content[:220] + "…"
            lines.append(f"[{kind}] {speaker} -> {audience}: {content}")

        rendered = "\n".join(lines).strip()
        if len(rendered) <= max_chars:
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

    def _compact_turns(self, turns: list[ConversationTurn]) -> list[str]:
        counts: dict[str, int] = {}
        highlights: list[str] = []
        for turn in turns[-8:]:
            counts[turn.kind] = counts.get(turn.kind, 0) + 1
            content = turn.content.replace("\n", " ").strip()
            if len(content) > 140:
                content = content[:140] + "…"
            highlights.append(f"[{turn.kind}] {turn.speaker} -> {turn.audience}: {content}")

        count_str = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
        lines = [f"Compacted earlier transcript turns ({len(turns)} omitted; {count_str})."]
        lines.extend(highlights)
        return lines
