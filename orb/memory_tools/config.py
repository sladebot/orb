"""Configuration dataclass for memory tools.

Single source of truth for memory-tools options. Defaults leave the feature
disabled — users must opt in before the vault is touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryConfig:
    """Parameters controlling how memory_tools behaves.

    Attributes
        vault_path: file-system location for the persistent vault.
        enabled: when False the tools are effectively no-ops.
        auto_write: when True every read side-effect (e.g. cache refresh)
            triggers an implicit flush (future use — currently ignored because
            Phase 1 is read-only).
        tag_taxonomy: allowed tag values as defined by SCHEMA.md. Pages with
            tags outside this list will be flagged during linting (Phase 4).
    """

    vault_path: str = "~/.orb/vault"
    enabled: bool = False
    auto_write: bool = False
    tag_taxonomy: list[str] = field(default_factory=list)
