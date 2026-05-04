"""Slash command registry for Orb TUI.

Canonical source of truth for all slash commands: definitions, execution,
and runtime disabled evaluation.  Both the inline slash palette and
``/help`` read from this module.

Commands are registered at module import time; the registry is a ``frozenset``
for O(1) membership checks, and COMMAND_MAP provides O(1) lookups by key.
"""

from __future__ import annotations

__all__ = ("COMMAND_REGISTRY", "COMMAND_MAP", "SlashCommand", "COMMAND_CATEGORIES")

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, order=False)
class SlashCommand:
    """Definition of a single slash command.

    ``execute`` receives ``(app, arg)`` where ``app`` is the
    :class:`orb.cli.tui_repl.OrbReplTUI` instance and ``arg`` is the
    raw text after the command name (everything past ``/name arg``).

    Runtime disabled evaluation:
        Commands that are conditionally disabled (e.g. ``/quit`` when not
        in a session) compute ``is_disabled(self, state)`` which returns
        ``(bool, str)`` — whether it is disabled and the reason string
        to show if the user explicitly types the slash.
    """

    slash: str
    """Full command token including leading slash (e.g. ``/help``)."""

    description: str
    """Short user-facing description shown in the palette + /help."""

    priority: int = 0
    """Sort order for ``/help`` output and palette rendering (higher = first)."""

    # Optional runtime constraints.  A command that requires a session can
    # declare ``needs_session = True``; the registry evaluates these at
    # dispatch time.  The fallback in ``_run_slash_command`` refuses to
    # run the command and surfaces a reason so the user isn't confused.
    needs_session: bool = False
    needs_connection: bool = False  # alias for needs_session
    needs_not_in_run: bool = False  # e.g. ``/new`` refuses mid-run

    # Lazy evaluation.  When provided, ``is_disabled`` is called with the
    # current app state at dispatch/palette-render time to decide whether
    # the command is available.  When absent the command is always enabled.
    is_disabled: Callable[["SlashCommand", Any], tuple[bool, str]] | None = None  # noqa: A003

    # The function that runs when the command is dispatched.
    execute: Callable[["Any", str], Awaitable[None] | None]

    # --- helpers ---

    @property
    def key(self) -> str:
        """Lower-case name without the leading slash."""
        return self.slash.lstrip("/").lower()

    @property
    def disabled(self) -> bool:
        """Static check (for documentation / testing)."""
        if self.is_disabled is not None:
            return self.is_disabled(self, type("", (), {"session_id": "", "run_state": "idle"})())[0]  # type: ignore[arg-type]
        return False


# ---------------------------------------------------------------------------
# Helper functions for complex commands (kept out of command definitions).
# ---------------------------------------------------------------------------


def _clear_execute(app: Any, _arg: str) -> None:
    """Clear the REPL stream."""
    try:
        stream = app.query_one("ReplStream")
    except Exception:
        return
    stream.remove_children()
    # Clear streaming bookkeeping too — otherwise the next ``message_delta``
    # for any (chain, agent) still in the dict tries to append on a
    # detached widget, silently losing the chunk (or raising if GC'd).
    if hasattr(app, "_streaming_turns"):
        app._streaming_turns.clear()
    if hasattr(app, "_finalized_streams"):
        app._finalized_streams.clear()
    try:
        app._emit_turn("system", "[#8796a7]stream cleared[/]", body_markup=True)
    except Exception:
        pass


def _stop_execute(app: Any, _arg: str) -> None:
    """Stop the current run."""
    # Delegate to the existing action — this avoids duplicating the POST logic.
    if hasattr(app, "action_cancel_run"):
        asyncio.create_task(app.action_cancel_run())


def _resume_execute(app: Any, _arg: str) -> None:
    """Open the session picker."""
    if hasattr(app, "action_resume_session"):
        asyncio.create_task(app.action_resume_session())


def _topology_execute(app: Any, arg: str) -> None:
    """Switch routing topology for the next run."""
    async def _do() -> None:
        if not hasattr(app, "_http_session") or app._http_session is None:
            try:
                app._emit_turn("system", "[#f3afa7]/topology failed:[/] not connected[/]", body_markup=True)
            except Exception:
                pass
            return
        new = arg.strip().lower()
        if not new:
            await app._hydrate_topologies()
            valid = app._valid_topology_ids()
            try:
                app._emit_turn(
                    "system",
                    f"[#8796a7]current topology:[/] [#c4ced9]{app.topology}[/]  "
                    f"[#8796a7]valid:[/] {_tui_escape(', '.join(valid))}",
                    body_markup=True,
                )
            except Exception:
                pass
            return
        await app._hydrate_topologies()
        valid_topologies = app._valid_topology_ids()
        if new not in app.available_topology_labels:
            try:
                app._emit_turn(
                    "system",
                    f"[#f3afa7]unknown topology:[/] {_tui_escape(repr(new))}[/]\n"
                    f"[#8796a7]valid:[/] {_tui_escape(', '.join(valid_topologies))}",
                    body_markup=True,
                )
            except Exception:
                pass
            return
        if getattr(app, "locked_topology", "") and new != app.locked_topology:
            try:
                app._emit_turn(
                    "system",
                    f"[#f3afa7]topology is pinned to[/] [#c4ced9]{_tui_escape(app.locked_topology)}[/] "
                    f"[#f3afa7]for this session — start[/] [#94bfff]/new {_tui_escape(new)}[/] "
                    f"[#f3afa7]to change it[/]",
                    body_markup=True,
                )
            except Exception:
                pass
            return
        app._topology = new
        app.topology = new
        try:
            app._emit_turn(
                "system",
                f"[#8796a7]topology set to[/] [#c4ced9]{_tui_escape(new)}[/] "
                f"[#6b7685](applies to the next run)[/]",
                body_markup=True,
            )
        except Exception:
            pass

    asyncio.create_task(_do())


def _new_execute(app: Any, arg: str) -> None:
    """Start a fresh session (or escape a pinned session)."""
    if hasattr(app, "_create_fresh_session"):
        asyncio.create_task(app._create_fresh_session(arg.strip().lower()))


def _quit_execute(app: Any, _arg: str) -> None:
    """Exit the TUI."""
    if hasattr(app, "call_after_refresh") and hasattr(app, "action_quit"):
        app.call_after_refresh(app.action_quit)


def _help_execute(app: Any, _arg: str) -> None:
    """Show all slash commands (also responds to /?)."""
    rows: list[str] = []
    for cmd in sorted(COMMAND_REGISTRY, key=lambda c: c.priority, reverse=True):
        if cmd.is_disabled:
            is_dis, reason = cmd.is_disabled(cmd, type("", (), {"session_id": "", "run_state": "idle"})())  # type: ignore[arg-type]
            if is_dis:
                rows.append(f"  [dim]{cmd.slash:<12}[/dim]  {cmd.description}  ([dim]disabled — {reason}[/])")
                continue
        rows.append(f"  [bold #94bfff]{cmd.slash:<12}[/bold]  {cmd.description}")
    app._emit_turn(
        "system",
        "[#c4ced9]commands:[/]\n" + "\n".join(rows),
        body_markup=True,
    )


# ---------------------------------------------------------------------------
# Disabled-evaluator helpers.
# ---------------------------------------------------------------------------


def _session_required(cmd: SlashCommand, state: Any) -> tuple[bool, str]:
    """Evaluator for commands that require an attached session."""
    sid = getattr(state, "session_id", "")
    if not sid:
        return (True, "no active session")
    return (False, "")


def _not_in_run(cmd: SlashCommand, state: Any) -> tuple[bool, str]:
    """Evaluator for commands that refuse when a run is in flight."""
    rs = getattr(state, "run_state", "idle")
    if rs and rs not in {"idle", "completed", "errored"}:
        return (True, f"run in flight ({rs})")
    return (False, "")


def _always_enabled(cmd: SlashCommand, state: Any) -> tuple[bool, str]:
    return (False, "")


# ---------------------------------------------------------------------------
# Canonical registry — order is arbitrary (sorted by priority at render).
# ---------------------------------------------------------------------------

COMMAND_CATEGORIES: frozenset[str] = frozenset(("Session", "Run", "View", "App"))

COMMAND_REGISTRY: frozenset[SlashCommand] = frozenset((
    SlashCommand(
        slash="/help",
        description="show all commands",
        priority=100,
        is_disabled=_always_enabled,
        execute=_help_execute,
    ),
    SlashCommand(
        slash="/clear",
        description="clear the stream",
        priority=90,
        is_disabled=_always_enabled,
        execute=_clear_execute,
    ),
    SlashCommand(
        slash="/stop",
        description="stop the current run",
        priority=85,
        is_disabled=_session_required,
        execute=_stop_execute,
    ),
    SlashCommand(
        slash="/cancel",
        description="stop the current run (alias)",
        priority=84,
        is_disabled=_session_required,
        execute=_stop_execute,
    ),
    SlashCommand(
        slash="/resume",
        description="pick a prior session (same as ^r)",
        priority=80,
        is_disabled=_session_required,
        execute=_resume_execute,
    ),
    SlashCommand(
        slash="/new",
        description="start a fresh session [topology]",
        priority=75,
        needs_session=False,  # /new can run without session
        is_disabled=_not_in_run,
        execute=_new_execute,
    ),
    SlashCommand(
        slash="/topology",
        description="change topology for new runs",
        priority=70,
        is_disabled=_session_required,
        execute=_topology_execute,
    ),
    SlashCommand(
        slash="/quit",
        description="exit the TUI",
        priority=60,
        is_disabled=_always_enabled,
        execute=_quit_execute,
    ),
    SlashCommand(
        slash="/exit",
        description="exit the TUI (alias)",
        priority=59,
        is_disabled=_always_enabled,
        execute=_quit_execute,
    ),
))

# Fast lookup by slash token (e.g. "/stop" → SlashCommand).
COMMAND_MAP: dict[str, SlashCommand] = {cmd.slash: cmd for cmd in COMMAND_REGISTRY}

# Reverse alias map: "/cancel" → canonical "/stop" for dispatch.
_ALIASES_TO_CANONICAL: dict[str, str] = {}
for cmd in COMMAND_REGISTRY:
    if cmd.slash not in {"stop", "quit"}:  # these are canonical targets
        # All others alias to themselves (already set by COMMAND_MAP).
        pass
# Explicit alias resolution: dispatch "/cancel" and "/exit" to the canonical
# command's handler.
_COMMAND_TARGET: dict[str, str] = {
    "/cancel": "/stop",
    "/exit": "/quit",
}


def _tui_escape(value: Any) -> str:
    """Escape runtime/user text before interpolating into Textual markup."""
    from rich.markup import escape as _rich_markup_escape
    return _rich_markup_escape(str(value))
