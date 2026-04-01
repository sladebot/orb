from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from time import time


class MessageType(Enum):
    SYSTEM = "system"
    TASK = "task"
    RESPONSE = "response"
    FEEDBACK = "feedback"
    COMPLETE = "complete"


@dataclass
class Message:
    from_: str
    to: str
    type: MessageType
    payload: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    chain_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    depth: int = 0
    context_slice: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time)

    def to_dict(self) -> dict:
        return {
            "from_": self.from_,
            "to": self.to,
            "type": self.type.value,
            "payload": self.payload,
            "id": self.id,
            "chain_id": self.chain_id,
            "depth": self.depth,
            "context_slice": self.context_slice,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Message:
        return cls(
            from_=d["from_"],
            to=d["to"],
            type=MessageType(d["type"]),
            payload=d["payload"],
            id=d.get("id") or uuid.uuid4().hex[:12],
            chain_id=d.get("chain_id") or uuid.uuid4().hex[:12],
            depth=d.get("depth", 0),
            context_slice=d.get("context_slice") or [],
            metadata=d.get("metadata") or {},
            timestamp=d.get("timestamp") or time(),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, raw: str) -> Message:
        return cls.from_dict(json.loads(raw))

    def reply(self, from_: str, to: str, payload: str, type: MessageType = MessageType.RESPONSE, context_slice: list[str] | None = None) -> Message:
        return Message(
            from_=from_,
            to=to,
            type=type,
            payload=payload,
            chain_id=self.chain_id,
            depth=self.depth + 1,
            context_slice=context_slice or [],
        )
