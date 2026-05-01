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

import warnings
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
    t.entry_agent = ""
    t.live_text = ""
    # Approval-flow state (task #10). Seeded on every TUI instance whether
    # approvals are enabled or not so the handlers can poke at these
    # attributes unconditionally.
    t.pending_writes = {}
    t.approve_all = False
    t.approval_required = False
    # Streaming state (task #13). Keyed by chain_id; each value is the
    # in-progress Turn widget receiving deltas. Final ``message`` event
    # for a chain pops the entry and finalizes the Turn body.
    t._streaming_turns = {}
    t._finalized_streams = {}
    t.streaming_enabled = False
    # Stub out widget-touching sinks with no-ops.
    t._emit_turn = MagicMock()
    t._emit_block = MagicMock()
    t._emit_whisper = MagicMock()
    t._refresh_chrome = MagicMock()
    # HTTP + async helpers stubbed for approval auto-approve paths.
    t._http_session = MagicMock()
    t._session_url = MagicMock(side_effect=lambda suffix: f"http://test/api/v1{suffix}")
    t._post_approval = MagicMock()
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


# ── approval flow: init ──────────────────────────────────────────────


def test_on_init_parses_approval_required_true():
    """``approval_required`` on the init payload propagates to TUI state.

    Emitted at the top level (see ``web/state.py::to_init_event``), not
    inside the ``session`` block.
    """
    tui = _make_tui()
    tui._handle_init({
        "type": "init",
        "session_id": "s1",
        "workdir": "/w",
        "agents": [],
        "approval_required": True,
    })
    assert tui.approval_required is True


def test_on_init_session_switch_clears_pending_writes_and_approve_all():
    """Approval state is session-scoped; switching sessions drops it."""
    tui = _make_tui()
    tui.session_id = "old-sid"
    tui.pending_writes["req-1"] = {"path": "a.py", "agent": "coder"}
    tui.approve_all = True
    tui._handle_init({
        "type": "init",
        "session_id": "new-sid",  # different → session_changed = True
        "workdir": "/w",
        "agents": [],
    })
    assert tui.pending_writes == {}
    assert tui.approve_all is False


def test_on_init_same_session_rebroadcast_preserves_pending_writes():
    """Server re-broadcasts ``init`` on every run's planning phase. That
    must NOT clobber in-flight approvals — they belong to the session."""
    tui = _make_tui()
    tui.session_id = "sid-1"
    tui.pending_writes["req-1"] = {"path": "a.py", "agent": "coder"}
    tui.approve_all = True
    tui._handle_init({
        "type": "init",
        "session_id": "sid-1",  # same session
        "workdir": "/w",
        "agents": [],
    })
    assert "req-1" in tui.pending_writes
    assert tui.approve_all is True


def test_on_init_approval_required_defaults_false_when_missing():
    tui = _make_tui()
    tui._handle_init({
        "type": "init",
        "session_id": "s1",
        "workdir": "/w",
        "agents": [],
    })
    assert tui.approval_required is False


# ── approval flow: file_write_pending ───────────────────────────────


def test_on_file_write_pending_records_entry_and_emits_warn_block():
    tui = _make_tui()
    tui._handle_file_write_pending({
        "type": "file_write_pending",
        "agent": "coder",
        "request_id": "req-1",
        "path": "src/new.py",
        "content": "print('x')\n",
        "old_content": "",
    })
    # Entry recorded keyed by request_id carrying the payload bits we'll
    # need at action-time (path, agent, content, old_content).
    assert "req-1" in tui.pending_writes
    entry = tui.pending_writes["req-1"]
    assert entry["path"] == "src/new.py"
    assert entry["agent"] == "coder"
    assert entry["content"] == "print('x')\n"
    # A warn-pill block with the accept bar was emitted.
    tui._emit_block.assert_called_once()
    _, kwargs = tui._emit_block.call_args
    assert kwargs.get("pill") == "pending"
    assert kwargs.get("pill_kind") == "warn"
    accept = kwargs.get("accept") or []
    assert any("accept" in a for a in accept)
    assert any("reject" in a for a in accept)


def test_on_file_write_pending_auto_approves_when_approve_all_set():
    """With ``approve_all`` latched the handler must schedule an approve
    POST and skip emitting a warn block (no pill-flash for the user)."""
    tui = _make_tui()
    tui.approve_all = True
    tui.session_id = "sid-1"
    scheduled: list = []
    tui._schedule_approval_post = MagicMock(
        side_effect=lambda request_id, action, **kw: scheduled.append((request_id, action, kw))
    )

    tui._handle_file_write_pending({
        "agent": "coder",
        "request_id": "req-auto",
        "path": "src/a.py",
        "content": "body",
    })
    # Pending entry still recorded so the eventual file_write (approved)
    # can locate it for pill → applied.
    assert "req-auto" in tui.pending_writes
    tui._emit_block.assert_not_called()
    assert scheduled == [("req-auto", "approve", {})]


# ── approval flow: file_write_rejected ──────────────────────────────


def test_on_file_write_rejected_updates_block_and_drops_pending():
    tui = _make_tui()
    fake_block = MagicMock()
    tui.pending_writes["req-1"] = {
        "path": "src/x.py", "agent": "coder",
        "content": "c", "old_content": "",
        "block": fake_block,
    }
    tui._handle_file_write_rejected({
        "agent": "coder",
        "request_id": "req-1",
        "path": "src/x.py",
        "reason": "user rejected",
    })
    assert "req-1" not in tui.pending_writes
    fake_block.set_status.assert_called_once_with("rejected", "err")


def test_on_file_write_rejected_unknown_request_is_noop():
    """Rejection for a request_id we don't know (edge case: block already
    replaced by a file_write, or teardown-initiated reject after state wipe)
    must not crash."""
    tui = _make_tui()
    # No entry for req-ghost.
    tui._handle_file_write_rejected({
        "agent": "coder",
        "request_id": "req-ghost",
        "path": "src/x.py",
        "reason": "timeout",
    })
    # Silent — just ensure no exception.
    assert tui.pending_writes == {}


# ── approval flow: file_write resolves pending ──────────────────────


def test_on_file_write_updates_pending_block_to_applied():
    tui = _make_tui()
    fake_block = MagicMock()
    tui.pending_writes["req-1"] = {
        "path": "src/x.py", "agent": "coder",
        "content": "c", "old_content": "",
        "block": fake_block,
    }
    tui._handle_file_write({
        "type": "file_write",
        "agent": "coder",
        "path": "src/x.py",
        "old_content": "",
        "content": "c",
    })
    # Pending entry cleared, block flipped to applied.
    assert "req-1" not in tui.pending_writes
    fake_block.set_status.assert_called_once_with("applied", "ok")
    # file_changes still records the write (rail display parity).
    assert "src/x.py" in tui.file_changes
    # _emit_block NOT called a second time — the existing block was updated in place.
    tui._emit_block.assert_not_called()


def test_on_file_write_resolves_pending_block_by_request_id_before_path():
    tui = _make_tui()
    old_block = MagicMock()
    new_block = MagicMock()
    tui.pending_writes["req-old"] = {
        "path": "src/x.py", "agent": "coder",
        "content": "old", "old_content": "",
        "block": old_block,
    }
    tui.pending_writes["req-new"] = {
        "path": "src/x.py", "agent": "coder",
        "content": "new", "old_content": "",
        "block": new_block,
    }

    tui._handle_file_write({
        "type": "file_write",
        "agent": "coder",
        "request_id": "req-new",
        "path": "src/x.py",
        "old_content": "",
        "content": "new",
    })

    assert "req-old" in tui.pending_writes
    assert "req-new" not in tui.pending_writes
    old_block.set_status.assert_not_called()
    new_block.set_status.assert_called_once_with("applied", "ok")


def test_on_file_write_without_pending_renders_as_before():
    """Non-approval flow (approval_required=False or hook-disabled agent)
    must keep emitting a fresh ToolBlock like before."""
    tui = _make_tui()
    tui._handle_file_write({
        "type": "file_write",
        "agent": "coder",
        "path": "src/untracked.py",
        "old_content": "",
        "content": "body",
    })
    assert "src/untracked.py" in tui.file_changes
    tui._emit_block.assert_called_once()


# ── ToolBlock post-hoc updates ───────────────────────────────────────


# ── Streaming: Turn mutability + message_delta dispatch (task #13) ──


def test_turn_append_mutates_body_and_re_renders():
    """``Turn.append(delta)`` extends the body in-place and reruns the
    markup builder so the on-screen widget reflects the new content
    without remounting. This is the foundation of token streaming."""
    from orb.cli.tui_repl import Turn

    turn = Turn("coder", 0.0, "", model="sonnet")
    captured: list[str] = []
    # Replace ``update`` so we can observe re-renders.
    turn.update = lambda text: captured.append(text)
    turn.append("Hello, ")
    turn.append("world!")
    assert turn.body == "Hello, world!"
    assert captured, "append must call update() so the widget redraws"
    # Final render contains the latest body verbatim somewhere in the
    # markup (it's wrapped in style tags; we just check the substring).
    assert "Hello, world!" in captured[-1]


def test_turn_set_body_replaces_content_and_re_renders():
    """The final ``message`` event closes the stream by replacing the
    Turn body wholesale (e.g. when WS dropped some deltas and we want
    the canonical full content)."""
    from orb.cli.tui_repl import Turn

    turn = Turn("coder", 0.0, "partial", model="sonnet")
    captured: list[str] = []
    turn.update = lambda text: captured.append(text)
    turn.set_body("the full canonical body", model="sonnet-4.6")
    assert turn.body == "the full canonical body"
    assert turn.model == "sonnet-4.6"
    assert "the full canonical body" in captured[-1]


def test_handle_message_delta_mounts_new_turn_when_chain_id_unseen():
    """First delta for a (chain_id, from) pair mounts a fresh Turn into
    the stream and stores it under ``_streaming_turns[(chain_id, from)]``.
    The key is a tuple — two different agents on the same chain_id are
    independent streams (per the #12 contract: monotonic per agent)."""
    tui = _make_tui()
    fake_stream = MagicMock()
    tui.query_one = MagicMock(return_value=fake_stream)
    tui._handle_message_delta({
        "type": "message_delta",
        "from": "coder",
        "chain_id": "chain-1",
        "delta": "Hel",
        "index": 0,
    })
    # A turn was mounted into the stream.
    fake_stream.mount.assert_called_once()
    mounted = fake_stream.mount.call_args[0][0]
    # The mounted widget is the one we tracked under the tuple key.
    assert tui._streaming_turns.get(("chain-1", "coder")) is mounted
    # Body is the first delta (append happened after mount).
    assert mounted.body == "Hel"


def test_handle_message_delta_appends_to_existing_turn():
    """Subsequent deltas for the same (chain_id, from) append onto the
    existing Turn instead of mounting a new one."""
    from orb.cli.tui_repl import Turn

    tui = _make_tui()
    existing = Turn("coder", 0.0, "Hel", model="")
    # Capture re-renders so we can verify ``append`` ran without
    # touching real Textual update plumbing.
    existing.update = MagicMock()
    tui._streaming_turns[("chain-1", "coder")] = existing
    fake_stream = MagicMock()
    tui.query_one = MagicMock(return_value=fake_stream)

    tui._handle_message_delta({
        "type": "message_delta",
        "from": "coder",
        "chain_id": "chain-1",
        "delta": "lo, world",
        "index": 1,
    })

    # No new Turn mounted — the existing one was reused.
    fake_stream.mount.assert_not_called()
    assert existing.body == "Hello, world"
    existing.update.assert_called()  # at least one re-render fired


def _mount_widget_for_test(turn) -> None:
    """Helper: simulate a mounted widget for ``_schedule_flush`` to take
    the timer path. Replaces ``is_mounted`` and ``set_timer`` with mocks
    that mirror Textual's mounted-app behavior without requiring a
    running ``App`` instance.
    """
    type(turn).is_mounted = property(lambda self: True)  # type: ignore[attr-defined]
    turn.set_timer = MagicMock(return_value=MagicMock(name="FakeTimer"))


def test_turn_append_is_o1_per_chunk():
    """``Turn.append`` must be O(1) per call when the widget is
    mounted: append a chunk, set the dirty flag, schedule a render
    via ``set_timer``. The expensive markup rebuild is deferred to
    the debounce timer.

    Regression guard for the post-streaming review's perf finding:
    pre-debounce, 1000 chunks against a body of size N produced O(N²)
    work because every ``append`` rebuilt the full markup. With the
    chunk-list + debounce, each ``append`` is constant work and the
    rebuild happens at most once per ``_STREAM_FLUSH_MS`` window.
    """
    from orb.cli.tui_repl import Turn

    turn = Turn("coder", 0.0, "")
    rebuild_calls = 0
    real_rebuild = turn._rebuild_turn_markup

    def _counting_rebuild():
        nonlocal rebuild_calls
        rebuild_calls += 1
        real_rebuild()
    turn._rebuild_turn_markup = _counting_rebuild
    _mount_widget_for_test(turn)

    rebuild_calls = 0  # reset after constructor's call
    for i in range(100):
        turn.append(f"chunk-{i} ")

    expected = "".join(f"chunk-{i} " for i in range(100))
    assert turn.body == expected
    # Crucial assertion: NO synchronous rebuild during the 100 appends.
    assert rebuild_calls == 0, (
        f"append() should defer rebuild to a debounced flush, but "
        f"_rebuild_turn_markup was called {rebuild_calls} times "
        f"during 100 appends"
    )
    # set_timer scheduled exactly once — subsequent appends in the
    # same window short-circuit on ``_pending_flush``.
    assert turn.set_timer.call_count == 1
    # Manually fire the deferred flush — rebuild happens once.
    turn._flush_streamed_render()
    assert rebuild_calls == 1
    # Cleanup — remove the patched class-level property so it doesn't
    # leak into other tests.
    delattr(type(turn), "is_mounted")


def test_turn_append_unmounted_falls_back_synchronously_no_coroutine_warning():
    """Unmounted Turn (e.g. a bare ``Turn(...)`` in a unit test): the
    debounce guard must skip ``set_timer`` entirely so Textual never
    constructs the ``Timer._run_timer`` coroutine. That coroutine,
    if created, leaks ``RuntimeWarning: coroutine ... was never
    awaited`` and pollutes warning-strict CI.

    Verifies: append on unmounted widget → sync rebuild, NO call to
    ``set_timer``, no RuntimeWarning emitted.
    """
    from orb.cli.tui_repl import Turn

    turn = Turn("coder", 0.0, "")
    # Don't mount; ``is_mounted`` defaults to whatever Textual returns
    # for an un-added widget (typically False / missing).
    turn.set_timer = MagicMock(name="should_not_be_called")
    rebuild_calls = 0
    real_rebuild = turn._rebuild_turn_markup

    def _counting_rebuild():
        nonlocal rebuild_calls
        rebuild_calls += 1
        real_rebuild()
    turn._rebuild_turn_markup = _counting_rebuild
    rebuild_calls = 0  # reset after constructor

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        turn.append("first chunk ")
        turn.append("second chunk")

    # Synchronous fallback: each append rebuilds immediately (no
    # event loop to debounce against). And critically, set_timer was
    # never invoked — so no coroutine was created.
    assert rebuild_calls == 2
    turn.set_timer.assert_not_called()
    assert turn.body == "first chunk second chunk"


def test_set_body_cancels_pending_streaming_flush():
    """``set_body`` (used on terminal-message finalization) replaces the
    body wholesale and should cancel any in-flight debounce timer —
    otherwise a late flush would re-render the same content twice."""
    from orb.cli.tui_repl import Turn

    turn = Turn("coder", 0.0, "")
    _mount_widget_for_test(turn)
    turn.append("hello")
    # ``_pending_flush`` should now be a Timer mock (we're "mounted").
    assert turn._pending_flush is not None
    turn.set_body("final body")
    assert turn._pending_flush is None
    assert turn._dirty is False
    assert turn.body == "final body"
    # Also verify the pending Timer mock had ``stop`` invoked so the
    # real Textual timer would actually be cancelled.
    delattr(type(turn), "is_mounted")


def test_handle_message_delta_keys_per_agent_so_two_agents_get_separate_turns():
    """#12 contract: monotonic ``index`` is per-agent-per-chain. Two
    agents on the same ``chain_id`` each emit their own 0..N sequence
    and must render into independent Turns. If we keyed by ``chain_id``
    alone, agent B's deltas would collapse onto agent A's Turn."""
    tui = _make_tui()
    fake_stream = MagicMock()
    tui.query_one = MagicMock(return_value=fake_stream)

    tui._handle_message_delta({
        "type": "message_delta", "from": "coder",
        "chain_id": "shared", "delta": "A1", "index": 0,
    })
    tui._handle_message_delta({
        "type": "message_delta", "from": "reviewer",
        "chain_id": "shared", "delta": "B1", "index": 0,
    })
    tui._handle_message_delta({
        "type": "message_delta", "from": "coder",
        "chain_id": "shared", "delta": "A2", "index": 1,
    })
    tui._handle_message_delta({
        "type": "message_delta", "from": "reviewer",
        "chain_id": "shared", "delta": "B2", "index": 1,
    })

    # Two separate Turns mounted, one per (chain_id, from).
    assert fake_stream.mount.call_count == 2
    coder_turn = tui._streaming_turns[("shared", "coder")]
    reviewer_turn = tui._streaming_turns[("shared", "reviewer")]
    assert coder_turn is not reviewer_turn
    assert coder_turn.body == "A1A2"
    assert reviewer_turn.body == "B1B2"


def test_handle_message_delta_ignores_missing_chain_id_or_delta():
    """A malformed delta event (missing chain_id or empty delta) is a
    no-op. Defensive — protects against bus replay edge cases."""
    tui = _make_tui()
    fake_stream = MagicMock()
    tui.query_one = MagicMock(return_value=fake_stream)
    tui._handle_message_delta({
        "type": "message_delta", "from": "coder", "delta": "x", "index": 0,
    })
    tui._handle_message_delta({
        "type": "message_delta", "from": "coder", "chain_id": "c", "delta": "", "index": 0,
    })
    fake_stream.mount.assert_not_called()
    assert tui._streaming_turns == {}


def test_handle_message_finalizes_streaming_turn_when_chain_id_matches():
    """The terminal ``message`` event closes the stream — it finds the
    matching streaming Turn (keyed by ``(chain_id, from)``), pops it
    from ``_streaming_turns``, and leaves the accumulated streamed
    body in place. It must NOT call ``set_body`` to overwrite — the
    final ``message.content`` is the ``send_message`` tool arg, not
    the streamed assistant text (the bridge truncates it to 500 chars
    too) — and must NOT call ``_emit_turn`` which would mount a
    duplicate Turn."""
    from orb.cli.tui_repl import Turn

    tui = _make_tui()
    turn = Turn("coder", 0.0, "Hel", model="")
    turn.update = MagicMock()
    turn.set_body = MagicMock()
    tui._streaming_turns[("chain-1", "coder")] = turn

    tui._handle_message({
        "type": "message",
        "from": "coder",
        "content": "Hello, world!",
        "model": "sonnet",
        "chain_id": "chain-1",
    })

    # Streamed body preserved — no set_body call.
    turn.set_body.assert_not_called()
    # Streaming entry was popped.
    assert ("chain-1", "coder") not in tui._streaming_turns
    # Non-streaming render path (which mounts a new Turn) was bypassed.
    tui._emit_turn.assert_not_called()
    # Counters still tick.
    assert tui.message_count == 1


def test_late_delta_after_finalize_is_dropped():
    """If a delta arrives AFTER the terminal ``message`` for the same
    (chain_id, from), the handler must ignore it instead of mounting a
    fresh Turn or appending to a popped reference. Catches the
    out-of-order / replay scenario flagged in the post-streaming review.
    """
    from orb.cli.tui_repl import Turn

    tui = _make_tui()
    fake_stream = MagicMock()
    tui.query_one = MagicMock(return_value=fake_stream)

    # Stream a delta, finalize, then send a late delta.
    tui._handle_message_delta({
        "type": "message_delta",
        "from": "coder",
        "chain_id": "chain-late",
        "delta": "first",
        "index": 0,
    })
    assert ("chain-late", "coder") in tui._streaming_turns
    tui._handle_message({
        "type": "message",
        "from": "coder",
        "chain_id": "chain-late",
        "content": "first done",
    })
    assert ("chain-late", "coder") not in tui._streaming_turns
    assert "coder" in tui._finalized_streams.get("chain-late", set())

    # Reset the mount counter so the next assertion is unambiguous.
    fake_stream.mount.reset_mock()

    # Late delta — should be dropped silently, NO new Turn mounted.
    tui._handle_message_delta({
        "type": "message_delta",
        "from": "coder",
        "chain_id": "chain-late",
        "delta": "late",
        "index": 1,
    })
    fake_stream.mount.assert_not_called()
    assert ("chain-late", "coder") not in tui._streaming_turns


def test_run_complete_clears_streaming_bookkeeping():
    """``run_complete`` is the natural lifecycle boundary — clear out
    any orphaned ``_streaming_turns`` (whose terminal message never
    arrived) and the ``_finalized_streams`` tombstone set so a long
    session doesn't accumulate dead references / unbounded sets.
    """
    tui = _make_tui()
    tui._emit_turn = MagicMock()
    tui._refresh_chrome = MagicMock()
    tui._exit_after_run = False  # control the post-cleanup branch
    tui._live_started = None
    # Seed both — one un-closed stream, one tombstoned.
    fake_turn = MagicMock()
    tui._streaming_turns[("chain-orphan", "coder")] = fake_turn
    tui._finalized_streams.setdefault("chain-done", set()).add("reviewer")

    tui._handle_run_complete({
        "type": "run_complete",
        "agent": "coordinator",
        "elapsed": 1.0,
        "result": "all good",
    })

    assert tui._streaming_turns == {}
    assert tui._finalized_streams == {}


def test_handle_message_falls_back_to_emit_when_no_streaming_turn():
    """Providers that don't stream emit only a final ``message`` — the
    handler must keep the original ``_emit_turn`` path so those
    responses still render."""
    tui = _make_tui()
    tui._handle_message({
        "type": "message",
        "from": "coder",
        "content": "no stream here",
        "model": "haiku",
    })
    tui._emit_turn.assert_called_once()
    assert tui.message_count == 1


def test_handle_init_records_streaming_enabled_flag():
    """``init.streaming_enabled`` is hydrated onto the TUI so callers
    can read whether this session streams. Purely informational."""
    tui = _make_tui()
    tui._handle_init({
        "type": "init",
        "session_id": "s",
        "streaming_enabled": True,
        "agents": [],
    })
    assert tui.streaming_enabled is True


def test_tool_block_set_status_updates_rendered_pill():
    from orb.cli.tui_repl import ToolBlock

    block = ToolBlock(
        glyph="±",
        label="edit_file",
        meta="src/x.py",
        pill="pending",
        pill_kind="warn",
        body="",
        agent="coder",
        accept=["y/accept", "n/reject"],
    )
    # Capture render calls so we can inspect the rewritten content.
    captured: list[str] = []
    block.update = lambda text: captured.append(text)
    block.set_status("applied", "ok")
    assert captured, "set_status must trigger a re-render via update()"
    new_markup = captured[-1]
    assert "applied" in new_markup
    assert "pending" not in new_markup
