"""Control message types for UDS process mode — Phase 4 of dual-mode spec.

Control messages share the same UDS framing as regular messages but are
distinguished by a top-level ``_control`` field. They replace direct
Python callbacks (on_complete, on_activity, on_heartbeat) in process mode.
"""

from __future__ import annotations

import json
from enum import Enum


class ControlType(Enum):
    COMPLETE = "complete"
    ACTIVITY = "activity"
    HEARTBEAT = "heartbeat"
    SHUTDOWN = "shutdown"


def make_control(type: ControlType, agent_id: str, **kwargs) -> bytes:
    """Encode a control message as UTF-8 JSON bytes."""
    payload = {"_control": type.value, "agent_id": agent_id, **kwargs}
    return json.dumps(payload).encode("utf-8")


def is_control(data: dict) -> bool:
    return "_control" in data


def parse_control(data: dict) -> tuple[ControlType, str, dict]:
    """Parse a control message dict into (type, agent_id, extra_fields)."""
    ctrl = ControlType(data["_control"])
    agent_id = data["agent_id"]
    extra = {k: v for k, v in data.items() if k not in ("_control", "agent_id")}
    return ctrl, agent_id, extra
