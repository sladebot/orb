from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from uuid import uuid4

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


@dataclass
class ConversationCompaction:
    summary: str
    archived_turns: int
    created_at: float = field(default_factory=time)


@dataclass
class ConversationSession:
    session_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    generation: int = 1
    turns: list[ConversationTurn] = field(default_factory=list)
    compactions: list[ConversationCompaction] = field(default_factory=list)
    agent_carryover: dict[str, list[dict]] = field(default_factory=dict)
    # Absolute path of the workspace the session operates on. Empty means use
    # the daemon's current working directory at run time.
    workdir: str = ""
    # Topology + models pinned to the session after the first run completes.
    # Follow-up v1 session runs reuse these unless the caller explicitly
    # supplies a different topology.
    locked_topology: str = ""
    locked_agent_models: dict[str, str] = field(default_factory=dict)
    locked_model_pin: str = ""
    # Per-session toggle: when True, every agent file write is staged in
    # ``GraphRuntime._pending_approvals`` and broadcast as
    # ``file_write_pending`` until the user approves/rejects via
    # ``POST /api/v1/sessions/{sid}/approvals/{request_id}``. Default False
    # so the existing autonomous path stays zero-overhead.
    approval_required: bool = False
    # Per-session toggle: when True (default), streaming providers invoke
    # the agent's ``on_chunk`` hook and the bridge broadcasts a
    # ``message_delta`` envelope per token. Set to False at session
    # creation to suppress deltas entirely — the final ``message``
    # event still fires for back-compat. See the shared contract with
    # stream-tui/#13 and stream-dashboard/#14 on ``tui-improvements``.
    streaming_enabled: bool = True

    def add_message(self, msg: Message, *, max_turns: int = 500) -> None:
        self._append(ConversationTurn(
            speaker=msg.from_,
            audience=msg.to,
            kind=msg.type.value,
            content=msg.payload,
            timestamp=msg.timestamp,
            chain_id=msg.chain_id,
            depth=msg.depth,
            message_id=msg.id,
        ), max_turns=max_turns)

    def add_completion(self, agent_id: str, result: str, *, max_turns: int = 500) -> None:
        self._append(ConversationTurn(
            speaker=agent_id,
            audience="runtime",
            kind="completion",
            content=result,
        ), max_turns=max_turns)

    def turn_count(self) -> int:
        return len(self.turns)

    def user_turn_count(self) -> int:
        return sum(1 for turn in self.turns if turn.speaker == "user")

    def render_for_model(
        self,
        agent_id: str,
        *,
        max_chars: int | None = None,
    ) -> str:
        lines = [
            "Shared session transcript.",
            f"Current target agent: {agent_id}",
            f"Conversation session: {self.session_id}",
            f"Conversation generation: {self.generation}",
            "Use this transcript as the collaborative conversation context across user and agent turns.",
            "",
        ]

        if self.compactions:
            lines.append("Compacted conversation context:")
            for item in self.compactions[-2:]:
                summary = item.summary.replace("\n", " ").strip()
                if len(summary) > 320:
                    summary = summary[:320] + "…"
                lines.append(f"[summary] {summary}")
            lines.append("")

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

    def render_prior_context(self, *, recent_turns: int = 8) -> str:
        lines: list[str] = []
        if self.compactions:
            lines.append("=== Prior compacted conversation context ===")
            for item in self.compactions[-2:]:
                lines.append(item.summary.strip())
            lines.append("=== End prior compacted context ===")
        if self.turns:
            lines.append("=== Recent conversation turns ===")
            for turn in self.turns[-recent_turns:]:
                content = turn.content.replace("\n", " ").strip()
                if len(content) > 240:
                    content = content[:240] + "…"
                lines.append(f"[{turn.kind}] {turn.speaker} -> {turn.audience}: {content}")
            lines.append("=== End recent conversation turns ===")
        return "\n".join(lines).strip()

    def apply_compaction(self, summary: str, *, preserve_recent_turns: int = 6) -> None:
        archived_turns = max(0, len(self.turns) - preserve_recent_turns)
        self.compactions.append(ConversationCompaction(
            summary=summary,
            archived_turns=archived_turns,
        ))
        self.generation += 1
        if preserve_recent_turns > 0:
            self.turns = self.turns[-preserve_recent_turns:]
        else:
            self.turns = []
        self.updated_at = time()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "generation": self.generation,
            "turns": [asdict(turn) for turn in self.turns],
            "compactions": [asdict(item) for item in self.compactions],
            "agent_carryover": self.agent_carryover,
            "workdir": self.workdir,
            "locked_topology": self.locked_topology,
            "locked_agent_models": dict(self.locked_agent_models),
            "locked_model_pin": self.locked_model_pin,
            "approval_required": bool(self.approval_required),
            "streaming_enabled": bool(self.streaming_enabled),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> ConversationSession:
        session = cls(
            session_id=str(payload.get("session_id") or uuid4().hex),
            created_at=float(payload.get("created_at") or time()),
            updated_at=float(payload.get("updated_at") or time()),
            generation=max(1, int(payload.get("generation") or 1)),
            agent_carryover=dict(payload.get("agent_carryover") or {}),
            workdir=str(payload.get("workdir") or ""),
            locked_topology=str(payload.get("locked_topology") or ""),
            locked_agent_models=dict(payload.get("locked_agent_models") or {}),
            locked_model_pin=str(payload.get("locked_model_pin") or ""),
            approval_required=bool(payload.get("approval_required") or False),
            # Default-on: a missing field in a persisted session (older
            # than the streaming work) should resume with streaming on,
            # not silently disabled. We only honor a literal ``False``.
            streaming_enabled=(
                False if payload.get("streaming_enabled") is False else True
            ),
        )
        session.turns = [
            ConversationTurn(**turn)
            for turn in (payload.get("turns") or [])
            if isinstance(turn, dict)
        ]
        session.compactions = [
            ConversationCompaction(**item)
            for item in (payload.get("compactions") or [])
            if isinstance(item, dict)
        ]
        return session

    @classmethod
    def load(cls, path: Path) -> ConversationSession:
        payload = json.loads(path.read_text())
        return cls.from_dict(payload)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = time()
        path.write_text(json.dumps(self.to_dict(), indent=2))

    def _append(self, turn: ConversationTurn, *, max_turns: int) -> None:
        self.turns.append(turn)
        if len(self.turns) > max_turns:
            self.turns = self.turns[-max_turns:]
        self.updated_at = time()


class RunTranscript:
    """Runtime-owned transcript of user and agent conversation turns."""

    def __init__(self, max_turns: int = 500, session: ConversationSession | None = None) -> None:
        self.max_turns = max_turns
        self.session = session or ConversationSession()

    @property
    def turns(self) -> list[ConversationTurn]:
        return self.session.turns

    def clear(self) -> None:
        self.session.turns.clear()

    def add_message(self, msg: Message) -> None:
        self.session.add_message(msg, max_turns=self.max_turns)

    def add_completion(self, agent_id: str, result: str) -> None:
        self.session.add_completion(agent_id, result, max_turns=self.max_turns)

    def render_for_model(
        self,
        agent_id: str,
        *,
        max_chars: int | None = None,
    ) -> str:
        return self.session.render_for_model(agent_id, max_chars=max_chars)
