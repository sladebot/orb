from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OrchestratorConfig:
    timeout: float = 600.0
    budget: int = 200
    max_depth: int = 10
    max_cooldown: int = 5
    entry_agent: str = "coder"
    synthesis_agent: str | None = None  # stays alive until all workers complete


@dataclass
class RunResult:
    success: bool
    completions: dict[str, str] = field(default_factory=dict)  # {agent_id: result}
    message_count: int = 0
    timed_out: bool = False
    error: str | None = None
    sandbox_dir: str | None = None  # path used during the run (already cleaned up)
