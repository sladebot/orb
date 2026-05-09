from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryConfig:
    """Configuration for the persistent memory vault.

    Default: disabled — memory tools are off until the user opts in.
    """
    vault_path: str = "~/.orb/vault"
    enabled: bool = False
    auto_write: bool = False
    tag_taxonomy: list[str] = field(default_factory=list)
