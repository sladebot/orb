"""TUI and dashboard must handle the same runtime events (CLAUDE.md parity rule).

The dashboard and TUI both attach to the daemon's WebSocket broadcast fanout.
If the runtime adds a new event type, both handlers must learn to react to it
or one of the surfaces goes silently out of sync.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
TUI_PATH = REPO / "orb" / "cli" / "tui.py"
BRIDGE_PATH = REPO / "web" / "bridge.py"
RUNTIME_PATH = REPO / "orb" / "runtime" / "graph_runtime.py"
APP_JS_PATH = REPO / "web" / "static" / "app.js"


def _event_types_broadcast_by_runtime() -> set[str]:
    """Find every `"type": "..."` literal in the runtime broadcasts + bridge."""
    types: set[str] = set()
    for path in (RUNTIME_PATH, BRIDGE_PATH):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # Look for dict literals with a "type": "..." entry.
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant) and key.value == "type"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                    ):
                        types.add(value.value)
    return types


def _tui_handled_event_types() -> set[str]:
    """Find every `t == "..."` branch in the TUI's event dispatcher."""
    tree = ast.parse(TUI_PATH.read_text())
    handled: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            left = node.left
            right = node.comparators[0]
            if (
                isinstance(left, ast.Name) and left.id == "t"
                and isinstance(right, ast.Constant) and isinstance(right.value, str)
            ):
                handled.add(right.value)
    return handled


# Event types that exist as runtime broadcasts but are intentionally ignored by the
# TUI (e.g. dashboard-only affordances). Keep this list tight and documented.
TUI_IGNORED_EVENTS: set[str] = {
    "error",                # dashboard-only SESSION_NOT_FOUND redirect (see app.js)
    "system_prompts",       # dashboard debug pane only
    "git_status",           # dashboard git pill only
    "chat_user_message",    # rendered via `message` on TUI side
    "chat_final",           # rendered via `complete` / `run_complete`
    "chat_assistant_message",  # rendered via `message`
    "catalog_refresh",      # dashboard catalog pane only
}


def test_tui_handles_every_runtime_event_type():
    """If runtime broadcasts a new `type`, the TUI dispatcher must handle it."""
    broadcast = _event_types_broadcast_by_runtime()
    handled = _tui_handled_event_types()

    # Filter out the explicitly-dashboard-only events.
    required = broadcast - TUI_IGNORED_EVENTS
    missing = required - handled

    assert not missing, (
        f"TUI is missing handlers for broadcast event types: {sorted(missing)}.\n"
        "Either add a handler in orb/cli/tui.py::_handle_server_event, or "
        "add the type to TUI_IGNORED_EVENTS with a comment explaining why "
        "it is intentionally dashboard-only."
    )


def test_tui_reconnect_receives_fresh_init_via_ws_handler():
    """The v1 WS handler sends current_init_event on every new connection.

    Regression guard: the init-event-on-connect behavior is what lets the TUI
    re-sync after a reconnect. If this line moves or is deleted, the parity
    rule in CLAUDE.md is violated.
    """
    api_v1_path = REPO / "web" / "api_v1.py"
    src = api_v1_path.read_text()
    assert "current_init_event" in src, (
        "web/api_v1.py no longer sends current_init_event on WS connect — "
        "TUI reconnect will now show stale state until an event fires."
    )
