from __future__ import annotations

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
