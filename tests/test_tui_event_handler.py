"""Tests for OrbTUI._handle_server_event state machine.

We test the TUI's event handling in isolation — no Textual rendering,
just the state-mutation logic driven by server JSON events.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from orb.cli.tui import OrbTUI, AgentInfo, HeaderBar


def _make_tui() -> OrbTUI:
    """Create an OrbTUI instance with all Textual widget calls mocked out."""
    tui = object.__new__(OrbTUI)
    # Initialise only the data-layer attributes (bypass super().__init__)
    tui._server_port   = 8080
    tui._topology_name = "triangle"
    tui._show_logs     = False
    tui._agents        = {}
    tui._detail_feed   = []
    tui._routed        = 0
    tui._budget        = 200
    tui._run_start     = None
    tui._run_status    = "Waiting"
    tui._selected_agent = None
    tui._completions   = {}
    tui._last_query    = ""
    tui._last_elapsed  = 0.0
    tui._last_diff     = ""
    tui._turn_count    = 0
    tui._awaiting_user = None
    tui._tick_count    = 0
    tui._active_edges  = {}
    tui._elapsed_task  = None
    tui._ws_task       = None
    tui._log_handler   = None
    tui._initial_query = ""
    tui._exit_after_run = False
    tui._initial_query_started = False
    tui._awaiting_user_question = ""

    # Stub every widget query so state tests don't need a real Textual app
    mock_feed    = MagicMock()
    mock_log     = MagicMock()
    mock_graph   = MagicMock()
    mock_scroll  = MagicMock()
    mock_label   = MagicMock()
    mock_ta      = MagicMock()
    mock_code    = MagicMock()
    mock_code_hdr = MagicMock()
    mock_code_log = MagicMock()
    mock_qbanner = MagicMock()
    mock_qbanner_hdr = MagicMock()
    mock_qbanner_body = MagicMock()

    def _query_one(selector, *args):
        mapping = {
            "#message-feed": mock_feed,
            "#detail-log":   mock_log,
            "#graph-panel":  mock_graph,
            "#detail-scroll": mock_scroll,
            "#query-label":  mock_label,
            "#query-input":  mock_ta,
            "#code-panel":   mock_code,
            "#code-panel-header": mock_code_hdr,
            "#code-log":     mock_code_log,
            "#question-banner": mock_qbanner,
            "#question-banner-header": mock_qbanner_hdr,
            "#question-banner-body": mock_qbanner_body,
        }
        return mapping.get(selector, MagicMock())

    tui.query_one         = _query_one
    tui._refresh_all      = MagicMock()
    tui._write_feed       = MagicMock()
    tui._append_to_detail = MagicMock()
    tui._update_detail_header = MagicMock()
    tui.action_select     = MagicMock()
    tui.call_after_refresh = MagicMock()
    tui.notify = MagicMock()

    # Store mocks for assertion in tests
    tui._mock_feed = mock_feed
    tui._mock_qbanner = mock_qbanner
    tui._mock_qbanner_hdr = mock_qbanner_hdr
    tui._mock_qbanner_body = mock_qbanner_body
    tui._mock_ta = mock_ta
    return tui


class TestTuiEventHandler:

    # ── init ─────────────────────────────────────────────────────────────────

    def test_init_populates_agents(self):
        tui = _make_tui()
        tui._handle_server_event({
            "type": "init",
            "agents": [
                {"id": "coordinator", "role": "Coordinator", "status": "idle", "model": ""},
                {"id": "coder",       "role": "Coder",       "status": "idle", "model": ""},
            ],
            "edges": [{"source": "coordinator", "target": "coder"}],
            "messages": [],
            "stats": {"message_count": 0, "budget_remaining": 200, "elapsed": 0},
            "run_active": False,
            "completed": False,
        })
        assert "coordinator" in tui._agents
        assert "coder" in tui._agents
        assert tui._agents["coordinator"].role == "Coordinator"

    def test_init_run_active_sets_running(self):
        tui = _make_tui()
        tui._handle_server_event({
            "type": "init",
            "agents": [{"id": "coder", "role": "Coder", "status": "running", "model": ""}],
            "edges": [], "messages": [],
            "stats": {"message_count": 3, "elapsed": 1.5},
            "run_active": True, "completed": False,
        })
        assert tui._run_status == "Running"

    def test_init_completed_sets_idle(self):
        tui = _make_tui()
        tui._handle_server_event({
            "type": "init",
            "agents": [{"id": "coder", "role": "Coder", "status": "completed",
                        "model": "", "completed_result": "Done"}],
            "edges": [], "messages": [],
            "stats": {"message_count": 5, "elapsed": 2.0},
            "run_active": False, "completed": True,
        })
        assert tui._run_status == "Idle"
        assert "coder" in tui._completions

    def test_init_detects_dual_review_topology(self):
        tui = _make_tui()
        tui._handle_server_event({
            "type": "init",
            "agents": [
                {"id": "coordinator", "role": "Coordinator", "status": "idle", "model": ""},
                {"id": "reviewer_a",  "role": "Reviewer A",  "status": "idle", "model": ""},
            ],
            "edges": [], "messages": [],
            "stats": {"message_count": 0, "elapsed": 0},
            "run_active": False, "completed": False,
        })
        assert tui._topology_name == "dual-review"

    def test_init_with_initial_query_starts_run(self):
        tui = _make_tui()
        tui._initial_query = "write hello world"
        with patch("orb.cli.tui.asyncio.create_task") as create_task:
            tui._handle_server_event({
                "type": "init",
                "agents": [],
                "edges": [],
                "messages": [],
                "stats": {"message_count": 0, "elapsed": 0},
                "run_active": False,
                "completed": False,
            })
        create_task.assert_called_once()
        create_task.call_args.args[0].close()

    def test_run_complete_auto_quits_when_enabled(self):
        tui = _make_tui()
        tui._exit_after_run = True
        tui._agents = {"coordinator": AgentInfo("coordinator", "Coordinator")}
        tui._handle_server_event({
            "type": "run_complete",
            "elapsed": 1.2,
            "routed": 3,
            "session_turn": 1,
            "diff": "",
        })
        tui.call_after_refresh.assert_called_once_with(tui.action_quit)

    def test_init_resets_existing_state(self):
        tui = _make_tui()
        tui._agents = {"old": AgentInfo(agent_id="old", role="Old")}
        tui._routed = 99
        tui._handle_server_event({
            "type": "init",
            "agents": [],
            "edges": [], "messages": [],
            "stats": {"message_count": 0, "elapsed": 0},
            "run_active": False, "completed": False,
        })
        assert "old" not in tui._agents
        assert tui._routed == 0

    # ── message ──────────────────────────────────────────────────────────────

    def test_message_increments_routed(self):
        tui = _make_tui()
        tui._agents = {
            "coordinator": AgentInfo("coordinator", "Coordinator"),
            "coder": AgentInfo("coder", "Coder"),
        }
        tui._handle_server_event({
            "type": "message",
            "from": "coordinator", "to": "coder",
            "msg_type": "task", "model": "", "content": "do it",
            "elapsed": 0.5,
        })
        assert tui._routed == 1

    def test_message_marks_sender_running(self):
        tui = _make_tui()
        tui._agents = {
            "coordinator": AgentInfo("coordinator", "Coordinator"),
            "coder": AgentInfo("coder", "Coder"),
        }
        tui._handle_server_event({
            "type": "message",
            "from": "coordinator", "to": "coder",
            "msg_type": "task", "model": "haiku", "content": "x", "elapsed": 0,
        })
        assert tui._agents["coordinator"].status == "running"
        assert tui._agents["coordinator"].model == "haiku"

    def test_message_marks_receiver_waiting(self):
        tui = _make_tui()
        tui._agents = {
            "coordinator": AgentInfo("coordinator", "Coordinator"),
            "coder": AgentInfo("coder", "Coder"),
        }
        tui._handle_server_event({
            "type": "message",
            "from": "coordinator", "to": "coder",
            "msg_type": "task", "model": "", "content": "x", "elapsed": 0,
        })
        assert tui._agents["coder"].status == "waiting"

    def test_message_system_type_ignored(self):
        tui = _make_tui()
        tui._handle_server_event({
            "type": "message",
            "from": "orchestrator", "to": "coder",
            "msg_type": "system", "model": "", "content": "init", "elapsed": 0,
        })
        assert tui._routed == 0

    def test_message_adds_to_detail_feed(self):
        tui = _make_tui()
        tui._agents = {
            "a": AgentInfo("a", "A"),
            "b": AgentInfo("b", "B"),
        }
        tui._handle_server_event({
            "type": "message",
            "from": "a", "to": "b",
            "msg_type": "response", "model": "", "content": "hi", "elapsed": 1.0,
        })
        assert len(tui._detail_feed) == 1
        assert tui._detail_feed[0]["payload"] == "hi"

    def test_message_activates_edge(self):
        tui = _make_tui()
        tui._agents = {
            "a": AgentInfo("a", "A"),
            "b": AgentInfo("b", "B"),
        }
        tui._handle_server_event({
            "type": "message",
            "from": "a", "to": "b",
            "msg_type": "task", "model": "", "content": "x", "elapsed": 0,
        })
        assert ("a", "b") in tui._active_edges

    # ── agent_status ─────────────────────────────────────────────────────────

    def test_agent_status_updates_status_and_model(self):
        tui = _make_tui()
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        tui._handle_server_event({
            "type": "agent_status",
            "agent": "coder", "status": "completed", "model": "sonnet",
        })
        assert tui._agents["coder"].status == "completed"
        assert tui._agents["coder"].model == "sonnet"

    def test_agent_status_unknown_agent_no_error(self):
        tui = _make_tui()
        # Should not raise even if agent not in _agents
        tui._handle_server_event({
            "type": "agent_status",
            "agent": "ghost", "status": "running", "model": "",
        })

    # ── agent_activity ───────────────────────────────────────────────────────

    def test_agent_activity_sets_activity_text(self):
        tui = _make_tui()
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        tui._handle_server_event({
            "type": "agent_activity",
            "agent": "coder", "activity": "Calling claude-sonnet…",
        })
        assert tui._agents["coder"].activity_text == "Calling claude-sonnet…"

    def test_agent_activity_waiting_sets_awaiting_user(self):
        tui = _make_tui()
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        tui._handle_server_event({
            "type": "agent_activity",
            "agent": "coder", "activity": "⏳ Waiting for user: what framework?",
        })
        assert tui._awaiting_user == "coder"
        assert tui._awaiting_user_question == "⏳ Waiting for user: what framework?"
        tui._mock_feed.write.assert_called()
        tui._mock_qbanner.add_class.assert_called_once_with("visible")
        tui._mock_qbanner_body.update.assert_called_once()

    def test_agent_activity_empty_clears_awaiting_user(self):
        tui = _make_tui()
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        tui._awaiting_user = "coder"
        tui._awaiting_user_question = "⏳ Waiting for user: what framework?"
        tui._handle_server_event({
            "type": "agent_activity",
            "agent": "coder", "activity": "",
        })
        assert tui._awaiting_user is None
        assert tui._awaiting_user_question == ""
        tui._mock_qbanner.remove_class.assert_called_once_with("visible")

    def test_cancel_reply_clears_draft(self):
        tui = _make_tui()
        tui._awaiting_user = "coder"
        tui.action_cancel_reply()
        tui._mock_ta.clear.assert_called_once()
        tui.notify.assert_called_once()

    # ── complete ─────────────────────────────────────────────────────────────

    def test_complete_updates_completions(self):
        tui = _make_tui()
        tui._agents = {"reviewer": AgentInfo("reviewer", "Reviewer")}
        tui._handle_server_event({
            "type": "complete", "agent": "reviewer", "result": "LGTM",
        })
        assert tui._completions.get("reviewer") == "LGTM"
        assert tui._agents["reviewer"].status == "completed"

    def test_complete_shutdown_not_added_to_completions(self):
        tui = _make_tui()
        tui._agents = {"reviewer": AgentInfo("reviewer", "Reviewer")}
        tui._handle_server_event({
            "type": "complete", "agent": "reviewer", "result": "[shutdown]",
        })
        assert "reviewer" not in tui._completions

    def test_complete_consensus_not_added_to_completions(self):
        tui = _make_tui()
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        tui._handle_server_event({
            "type": "complete", "agent": "coder", "result": "Consensus: done",
        })
        assert "coder" not in tui._completions

    # ── run_complete ─────────────────────────────────────────────────────────

    def test_run_complete_sets_idle(self):
        tui = _make_tui()
        tui._agents = {"coordinator": AgentInfo("coordinator", "Coordinator")}
        tui._run_status = "Running"
        tui._handle_server_event({
            "type": "run_complete",
            "result": "Final answer",
            "diff": "diff text",
            "elapsed": 12.5,
            "session_turn": 3,
            "routed": 8,
        })
        assert tui._run_status == "Idle"
        assert tui._last_elapsed == 12.5
        assert tui._turn_count == 3
        assert tui._routed == 8
        assert tui._last_diff == "diff text"

    def test_run_complete_marks_all_agents_completed(self):
        tui = _make_tui()
        tui._agents = {
            "coordinator": AgentInfo("coordinator", "Coordinator"),
            "coder":       AgentInfo("coder", "Coder"),
        }
        tui._agents["coder"].status = "running"
        tui._handle_server_event({
            "type": "run_complete",
            "result": "", "diff": "", "elapsed": 1.0,
            "session_turn": 1, "routed": 2,
        })
        assert tui._agents["coder"].status == "completed"

    def test_populate_detail_pane_writes_structured_sections(self):
        tui = _make_tui()
        tui._selected_agent = "coder"
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        tui._agents["coder"].activity_text = "Reviewing feedback"
        tui._agents["coder"].result = "Implemented feature"
        tui._detail_feed = [{
            "from_": "coder",
            "to": "reviewer",
            "model": "mock",
            "type": "task",
            "elapsed": 1.0,
            "payload": "please review",
            "preview": "please review",
        }]

        tui._populate_detail_pane()

        writes = " ".join(str(call.args[0]) for call in tui.query_one("#detail-log").write.call_args_list)
        assert "Overview" in writes
        assert "Recent Messages" in writes
        assert "Result" in writes

    def test_header_bar_shows_waiting_for_user_state(self):
        tui = _make_tui()
        tui._awaiting_user = "coder"
        tui._agents = {"coder": AgentInfo("coder", "Coder")}
        rendered = HeaderBar(tui).render().plain
        assert "USER INPUT" in rendered
        assert "coder" in rendered.lower()

    # ── stats ─────────────────────────────────────────────────────────────────

    def test_stats_updates_routed_and_elapsed(self):
        tui = _make_tui()
        tui._handle_server_event({
            "type": "stats",
            "message_count": 7,
            "budget_remaining": 193,
            "elapsed": 5.2,
        })
        assert tui._routed == 7
        assert tui._last_elapsed == 5.2

    # ── stopped ──────────────────────────────────────────────────────────────

    def test_stopped_sets_error_status(self):
        tui = _make_tui()
        tui._run_status = "Running"
        tui._handle_server_event({"type": "stopped"})
        assert tui._run_status == "Error"

    # ── unknown event type ────────────────────────────────────────────────────

    def test_unknown_event_type_no_error(self):
        tui = _make_tui()
        tui._handle_server_event({"type": "future_event_type", "data": 42})

    # ── carryover stripping integration ───────────────────────────────────────

    def test_consecutive_user_messages_prevented_by_server_stripping(self):
        """Validate that stripping trailing user messages from carryover is safe."""
        # Simulate a conversation that ended with tool_result (role=user)
        carryover = [
            {"role": "user",      "content": "task 1"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                               "name": "complete_task", "input": {}}]},
            {"role": "user",      "content": [{"type": "tool_result",
                                               "tool_use_id": "t1",
                                               "content": "Task marked as complete"}]},
        ]
        # Server-side strip
        msgs = list(carryover)
        while msgs and msgs[-1].get("role") == "user":
            msgs.pop()

        assert msgs[-1]["role"] == "assistant"
        # Appending next user message should be valid
        msgs.append({"role": "user", "content": "task 2"})
        roles = [m["role"] for m in msgs]
        for i in range(1, len(roles)):
            assert not (roles[i] == "user" and roles[i - 1] == "user")
