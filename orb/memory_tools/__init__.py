"""Orb memory tools — persistent memory vault for LLM conversations.

Public API:

    from orb.memory_tools import MemoryConfig, MemoryTools, MarkdownPage

    mt = MemoryTools(MemoryConfig(enabled=True))
    mt.write_entity("llm", "Large language model...")
"""
from __future__ import annotations

from .config import MemoryConfig
from .memory_tools import MemoryTools
from .models import MarkdownPage
from .store import Store

__version__ = "0.1.0"
__all__ = [
    "MemoryConfig",
    "MemoryTools",
    "MarkdownPage",
    "Store",
    "__version__",
]
