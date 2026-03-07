from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OrchestratorConfig:
    timeout: float = 120.0
    budget: int = 200
    max_depth: int = 10
    max_cooldown: int = 5
    entry_agent: str = "coder"


@dataclass
class RunResult:
    success: bool
    completions: dict[str, str] = field(default_factory=dict)  # {agent_id: result}
    message_count: int = 0
    timed_out: bool = False
    error: str | None = None
