"""Compatibility shim for the old widget-based TUI.

The original ``OrbTUI`` widget stack (~2500 lines of Textual widgets,
screens, event handlers, and WS/REST plumbing) lived here until it was
superseded by the REPL-stream TUI in :mod:`orb.cli.tui_repl`. The old
implementation was already bypassed on the ``new-tui`` branch and has
been removed.

This module is kept as a thin wrapper so that existing imports
(``from orb.cli.tui import attach_tui``) and test patches
(``patch("orb.cli.tui.attach_tui", ...)``) keep working. All it does is
delegate to :func:`orb.cli.tui_repl.attach_tui_repl`.
"""
from __future__ import annotations


async def attach_tui(
    connect_url: str,
    topology: str = "triad",
    budget: int = 200,
    show_logs: bool = False,
    initial_query: str | None = None,
    exit_after_run: bool = False,
    session_id: str | None = None,
    workdir: str | None = None,
    agent_models: dict[str, str] | None = None,
    approval_required: bool = True,
) -> None:
    """Attach to a running daemon and hand control to the REPL-stream TUI.

    This is the single public entry-point exposed by this module. The
    signature mirrors :func:`orb.cli.tui_repl.attach_tui_repl` so callers
    (notably :mod:`orb.cli.main`) can keep importing from here.
    """
    from orb.cli.tui_repl import attach_tui_repl

    await attach_tui_repl(
        connect_url,
        topology=topology,
        budget=budget,
        show_logs=show_logs,
        initial_query=initial_query,
        exit_after_run=exit_after_run,
        session_id=session_id,
        workdir=workdir,
        agent_models=agent_models,
        approval_required=approval_required,
    )


__all__ = ["attach_tui"]
