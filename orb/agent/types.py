from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.types import ModelConfig
    from ..sandbox.sandbox import Sandbox


class AgentStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class AgentConfig:
    node_id: str
    role: str
    description: str
    base_complexity: int = 50
    max_history: int = 20
    pinned_model: ModelConfig | None = None  # bypasses tier selection when set
    enable_filesystem: bool = False          # give agent read/write/run tools
    sandbox: "Sandbox | None" = None        # shared sandbox for this run
