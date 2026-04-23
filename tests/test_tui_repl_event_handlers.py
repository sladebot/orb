"""Unit tests for ``OrbReplTUI`` server-event handlers.

These tests avoid mounting the Textual app. We construct the class
with ``__new__`` and seed only the attributes the handlers touch,
then stub out the emission / chrome-refresh sinks so we can verify
pure state transitions.

Note: the handlers are defined as ``_on_*`` in the source but
Textual's ``App`` metaclass rewrites them to ``_handle_*`` at class
build time (the ``_on_*`` namespace is reserved for Textual hooks).
We call the runtime names here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_tui():
    from orb.cli.tui_repl import OrbReplTUI

    t = OrbReplTUI.__new__(OrbReplTUI)
    t.agents = {}
    t.agent_order = []
    t.edges = []
    t.graph_rows = []
    t.file_changes = {}
    t.plan_items = []
    t.scope_paths = []
    t.message_count = 0
    t.budget_remaining = 200
    t._budget = 200
    t.elapsed = 0.0
    t.session_id = ""
    t.workdir = ""
    t.topology = "auto"
    t.live_text = ""
    # Stub out widget-touching sinks with no-ops.
    t._emit_turn = MagicMock()
    t._emit_block = MagicMock()
    t._refresh_chrome = MagicMock()
    # ``_on_init`` calls ``self.query_one(ReplStream).remove_children()``
    # and then ``self._render_message`` which calls ``_emit_turn``.
    # Patch ``query_one`` to return a dummy stream-like object.
    stream = MagicMock()
    stream.remove_children = MagicMock()
    t.query_one = MagicMock(return_value=stream)
    return t


# ── _on_init ──────────────────────────────────────────────────────────


def test_on_init_populates_runtime_state():
    tui = _make_tui()
    payload = {
        "type": "init",
        "session_id": "abc-123-def",
        "workdir": "/tmp/project",
        "plan": {"topology": {"id": "triad"}},
        "agents": [
            {"id": "coordinator", "role": "coordinator", "status": "idle", "model": "opus"},
            {"id": "coder",       "role": "coder",       "status": "running", "model": "sonnet"},
            {"id": "reviewer",    "role": "reviewer",    "status": "idle", "model": "haiku"},
        ],
        "stats": {"message_count": 4, "elapsed": 1.25},
        "messages": [],
    }

    tui._handle_init(payload)

    assert tui.session_id == "abc-123-def"
    assert tui.workdir == "/tmp/project"
    assert tui.topology == "triad"
    assert tui.agent_order == ["coordinator", "coder", "reviewer"]
    assert tui.agents["coordinator"] == {"role": "coordinator", "status": "idle", "model": "opus"}
    assert tui.agents["coder"]["status"] == "running"
    assert tui.agents["reviewer"]["model"] == "haiku"
    assert tui.message_count == 4
    assert tui.elapsed == pytest.approx(1.25)
    assert tui.budget_remaining == 196  # 200 − 4
    tui._refresh_chrome.assert_called()


def test_on_init_skips_agents_with_missing_id():
    tui = _make_tui()
    tui._handle_init({
        "type": "init",
        "session_id": "s1",
        "workdir": "/w",
        "agents": [
            {"id": "", "role": "coder", "status": "idle"},
            {"id": "coder", "role": "coder", "status": "idle"},
        ],
    })
    assert tui.agent_order == ["coder"]
    assert "" not in tui.agents


def test_on_init_preserves_existing_session_when_payload_empty():
    tui = _make_tui()
    tui.session_id = "keepme"
    tui.workdir = "/old"
    tui._handle_init({"type": "init", "agents": []})
    # Missing keys should not clobber existing values.
    assert tui.session_id == "keepme"
    assert tui.workdir == "/old"


# ── _on_agent_status ──────────────────────────────────────────────────


def test_on_agent_status_updates_existing_agent():
    tui = _make_tui()
    tui.agents = {"coder": {"role": "coder", "status": "idle", "model": ""}}
    tui.agent_order = ["coder"]

    tui._handle_agent_status({"type": "agent_status", "agent": "coder", "status": "running"})

    assert tui.agents["coder"]["status"] == "running"
    tui._refresh_chrome.assert_called()


def test_on_agent_status_also_updates_model_when_present():
    tui = _make_tui()
    tui.agents = {"coder": {"role": "coder", "status": "idle", "model": ""}}
    tui._handle_agent_status({"agent": "coder", "status": "running", "model": "opus"})
    assert tui.agents["coder"]["model"] == "opus"


def test_on_agent_status_ignores_unknown_agent():
    tui = _make_tui()
    tui.agents = {"coder": {"role": "coder", "status": "idle", "model": ""}}
    tui._handle_agent_status({"agent": "ghost", "status": "running"})
    assert tui.agents == {"coder": {"role": "coder", "status": "idle", "model": ""}}


# ── _on_agent_activity ────────────────────────────────────────────────


def test_on_agent_activity_waiting_uses_full_content_when_present():
    tui = _make_tui()
    tui.agents = {"coordinator": {"role": "coordinator", "status": "waiting", "model": ""}}
    tui._handle_agent_activity({
        "agent": "coordinator",
        "activity": "⏳ Waiting for user: quick summary",
        "details": {"full_content": "The complete multi-line prompt body"},
    })
    # live_text reflects the waiting status.
    assert "waiting" in tui.live_text.lower()
    # Turn was emitted with the *full_content*, not the short snippet.
    args, kwargs = tui._emit_turn.call_args
    rendered = args[1]
    assert "The complete multi-line prompt body" in rendered
    assert "quick summary" not in rendered


def test_on_agent_activity_waiting_strips_prefix_when_no_full_content():
    tui = _make_tui()
    tui.agents = {"coordinator": {"role": "coordinator", "status": "waiting", "model": ""}}
    tui._handle_agent_activity({
        "agent": "coordinator",
        "activity": "⏳ Waiting for user: fall-back question",
        "details": {},
    })
    args, _ = tui._emit_turn.call_args
    assert "fall-back question" in args[1]
    # Prefix should be stripped.
    assert "Waiting for user" not in args[1]


def test_on_agent_activity_non_waiting_emits_whisper_and_sets_live_text():
    """Intermediate agent activities surface as compact whispers (not
    full conversation turns) so the stream distinguishes internal
    progress from the user/agent conversation. live_text still feeds
    the status strip.
    """
    tui = _make_tui()
    tui._emit_whisper = MagicMock()
    tui._handle_agent_activity({"agent": "coder", "activity": "editing file.py"})
    assert "editing file.py" in tui.live_text
    tui._emit_whisper.assert_called_once()
    args, _ = tui._emit_whisper.call_args
    assert args[0] == "coder"
    assert "editing file.py" in args[1]


# ── _on_file_write ────────────────────────────────────────────────────


def test_on_file_write_records_change_keyed_by_path_with_agent():
    tui = _make_tui()
    tui._handle_file_write({
        "path": "src/foo.py",
        "agent": "coder",
        "old_content": "a\nb\n",
        "content": "a\nb\nc\nd\n",
    })
    assert "src/foo.py" in tui.file_changes
    entry = tui.file_changes["src/foo.py"]
    assert entry["agent"] == "coder"
    # 4 new lines vs 2 old lines → added≈2, removed=0.
    assert entry["added"] == 2
    assert entry["removed"] == 0
    tui._emit_block.assert_called_once()


def test_on_file_write_ignores_empty_path():
    tui = _make_tui()
    tui._handle_file_write({"path": "", "agent": "coder", "content": "anything"})
    assert tui.file_changes == {}
    tui._emit_block.assert_not_called()


def test_on_file_write_handles_shrinking_file():
    tui = _make_tui()
    tui._handle_file_write({
        "path": "old.py",
        "agent": "reviewer",
        "old_content": "1\n2\n3\n4\n",
        "content": "1\n2\n",
    })
    entry = tui.file_changes["old.py"]
    assert entry["removed"] == 2
    assert entry["added"] == 0
    assert entry["agent"] == "reviewer"


# ── _on_plan_step ─────────────────────────────────────────────────────


def test_on_plan_step_appends_as_now_and_demotes_previous_now_to_done():
    tui = _make_tui()
    tui._handle_plan_step({"title": "design schema"})
    tui._handle_plan_step({"title": "write migration"})
    tui._handle_plan_step({"title": "run tests"})

    assert tui.plan_items == [
        ("done", "design schema"),
        ("done", "write migration"),
        ("now", "run tests"),
    ]


def test_on_plan_step_emits_turn_only_when_detail_present():
    tui = _make_tui()
    tui._handle_plan_step({"title": "step"})
    tui._emit_turn.assert_not_called()

    tui._handle_plan_step({"title": "step 2", "detail": "more info"})
    tui._emit_turn.assert_called_once()


def test_on_plan_step_uses_default_title_when_missing():
    tui = _make_tui()
    tui._handle_plan_step({})
    assert tui.plan_items == [("now", "Planning update")]


# ── _on_run_complete ──────────────────────────────────────────────────


def test_on_run_complete_updates_live_text_to_done():
    tui = _make_tui()
    tui._exit_after_run = False
    tui._handle_run_complete({
        "agent": "coordinator",
        "elapsed": 12.5,
        "result": "All green.",
    })
    assert tui.live_text == "done"
    tui._refresh_chrome.assert_called()
    tui._emit_turn.assert_called_once()


# ── plan progress meter ───────────────────────────────────────────────


def test_plan_progress_zero_of_zero():
    from orb.cli.tui_repl import _plan_progress

    done, total, meter = _plan_progress([])
    assert (done, total) == (0, 0)
    # All cells empty when nothing has been planned yet.
    assert meter == "▱▱▱▱▱▱"


def test_plan_progress_half_filled():
    from orb.cli.tui_repl import _plan_progress

    items = [
        ("done", "a"),
        ("done", "b"),
        ("done", "c"),
        ("now",  "d"),
        ("todo", "e"),
        ("todo", "f"),
    ]
    done, total, meter = _plan_progress(items)
    assert (done, total) == (3, 6)
    # 3/6 → 3 of 6 cells filled.
    assert meter == "▰▰▰▱▱▱"


def test_plan_progress_fully_filled():
    from orb.cli.tui_repl import _plan_progress

    items = [("done", f"step {i}") for i in range(6)]
    done, total, meter = _plan_progress(items)
    assert (done, total) == (6, 6)
    assert meter == "▰▰▰▰▰▰"


# ── agents rail: current-agent accent ────────────────────────────────


def test_context_rail_accents_running_agent():
    """The row for a running agent gets the ▎ accent marker and background tint."""
    from orb.cli.tui_repl import ContextRail

    tui = _make_tui()
    tui.agents = {
        "coordinator": {"role": "coordinator", "status": "idle", "model": ""},
        "coder":       {"role": "coder",       "status": "running", "model": ""},
        "reviewer":    {"role": "reviewer",    "status": "idle", "model": ""},
    }
    tui.agent_order = ["coordinator", "coder", "reviewer"]

    rail = ContextRail.__new__(ContextRail)
    rail.state = tui
    captured: dict[str, str] = {}
    rail.update = lambda text: captured.setdefault("text", text)
    rail.refresh_content()

    text = captured["text"]
    # Split into lines to reason about per-agent rows.
    lines = text.splitlines()
    coder_line = next(ln for ln in lines if "Coder" in ln)
    coord_line = next(ln for ln in lines if "Coordinator" in ln)
    # Running agent gets the accent marker and the on-colour background.
    assert "▎" in coder_line
    assert "on rgb(20,30,45)" in coder_line
    # Idle agents do NOT get the accent marker.
    assert "▎" not in coord_line
    assert "on rgb(20,30,45)" not in coord_line


# ── scope tracking ───────────────────────────────────────────────────


def test_track_scope_mentions_records_two_mentions_in_order():
    tui = _make_tui()
    # Use the real method, not a stub.
    from orb.cli.tui_repl import OrbReplTUI

    OrbReplTUI._track_scope_mentions(tui, "please read @app.py and compare to @tests/test_app.py")
    assert tui.scope_paths == ["app.py", "tests/test_app.py"]


def test_track_scope_mentions_dedups_repeats():
    tui = _make_tui()
    from orb.cli.tui_repl import OrbReplTUI

    OrbReplTUI._track_scope_mentions(tui, "@app.py take a look")
    OrbReplTUI._track_scope_mentions(tui, "now compare @app.py with @tests/unit.py")
    # app.py recorded once, tests/unit.py appended after.
    assert tui.scope_paths == ["app.py", "tests/unit.py"]


# ── TOPOLOGY / SCOPE sections render end-to-end ───────────────────────


def test_context_rail_renders_topology_and_scope_sections_for_triad():
    from orb.cli.tui_repl import ContextRail

    tui = _make_tui()
    tui.topology = "triad"
    tui.agents = {
        "coordinator": {"role": "coordinator", "status": "running", "model": ""},
        "coder":       {"role": "coder",       "status": "idle",    "model": ""},
        "reviewer":    {"role": "reviewer",    "status": "idle",    "model": ""},
        "tester":      {"role": "tester",      "status": "idle",    "model": ""},
    }
    tui.agent_order = ["coordinator", "coder", "reviewer", "tester"]
    tui.scope_paths = ["app.py", "tests/test_app.py"]

    rail = ContextRail.__new__(ContextRail)
    rail.state = tui
    captured: dict[str, str] = {}
    rail.update = lambda text: captured.setdefault("text", text)
    rail.refresh_content()
    text = captured["text"]

    assert "AGENTS" in text
    assert "TOPOLOGY" in text
    assert "SCOPE" in text
    # Triad graph includes the coordintr and coder boxes.
    assert "coordntr" in text
    assert "coder" in text
    # Scope rows include each @ path + the "add with @" hint.
    assert "app.py" in text
    assert "tests/test_app.py" in text
    assert "add with @" in text
