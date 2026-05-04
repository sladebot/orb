"""TUI command registry and fuzzy matching.

This module defines the canonical command registry for the TUI repl,
providing a single source of truth for slash commands, help text,
and the Peek palette.  Commands can declare a ``state`` dependency
that controls whether they are enabled or disabled depending on the
current topological state of the session.
"""

import logging
import sys
from typing import Optional

from orb.cli.config import SessionState, TopologyConfig
from orb.topology import SessionState

logger = logging.getLogger(__name__)


class SlashCommand:
    """A single TUI slash command entry.
    
    Attributes:
        slash: The command name (without the leading slash).
        handler: The callable that processes the command arguments.
        description: Human-readable description for the help system.
        state: Optional topological-state dependency for disabling.
        is_disabled: Callback to determine if the command is disabled
            in a given session state.
    """

    def __init__(
        self,
        slash: str,
        handler,
        description: str,
        state: Optional[TopologyConfig] = None,
        is_disabled=None,
    ) -> None:
        self.slash = slash
        self.handler = handler
        self.description = description
        self.state = state
        self.is_disabled = is_disabled or (lambda _: (False, ""))


# ====================================================================
# Command definitions
# ====================================================================

COMMAND_MAP: dict[str, SlashCommand] = {}
COMMAND_REGISTRY: list[SlashCommand] = []


def _register(cmd: SlashCommand) -> None:
    """Register a command in both the map and the ordered registry."""
    COMMAND_MAP[cmd.slash] = cmd
    COMMAND_REGISTRY.append(cmd)


# Register all slash commands here...


def fuzzy_filter(query: str) -> list[SlashCommand]:
    """Simple fuzzy matcher for slash commands.
    
    Returns commands where the slash name starts with or contains
    the query string (case-insensitive).
    """
    if not query:
        return []
    query_lower = query.lower()
    matches = []
    for cmd in COMMAND_REGISTRY:
        slash_name = cmd.slash.lstrip("/").lower()
        if slash_name.startswith(query_lower) or query_lower in slash_name:
            matches.append(cmd)
    # Sort by relevance (exact matches first, then prefix matches)
    matches.sort(key=lambda c: (0 if c.slash.lstrip("/").lower() == query_lower else 1, c.slash))
    return matches
