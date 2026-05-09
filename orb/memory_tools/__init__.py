"""Orb memory tools — Read Engine (Phase 1).

Exports the public API for reading the Orb memory vault.

Usage
    >>> from orb.memory_tools import MemoryTools, MemoryConfig
    >>> mt = MemoryTools(MemoryConfig(enabled=True))
    >>> mt.read("python")
"""
from __future__ import annotations

from .config import MemoryConfig
from .memory_tools import MemoryTools

__version__ = "0.1.0"
__all__ = ["MemoryTools", "MemoryConfig", "__version__"]
