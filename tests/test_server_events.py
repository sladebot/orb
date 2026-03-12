"""Tests for DashboardBridge and server WebSocket event emission."""
from __future__ import annotations

import json
import pytest

from orb.messaging.message import Message, MessageType
from web.bridge import DashboardBridge
from web.state import DashboardState


def _make_bridge() -> tuple[DashboardBridge, list[str]]:
    """Return (bridge, captured_broadcasts)."""
    state = DashboardState()
    captured: list[str] = []

    async def broadcast(msg: str) -> None:
        captured.append(msg)

    bridge = DashboardBridge(state, broadcast)
    return bridge, captured


class TestDashboardBridge:

    # ── Setup helpers ────────────────────────────────────────────────────────

    def test_setup_agents_populates_state(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"coordinator": "Coordinator", "coder": "Coder"})
        assert "coordinator" in bridge.state.agents
        assert bridge.state.agents["coder"].role == "Coder"

    def test_setup_edges(self):
        bridge, _ = _make_bridge()
        bridge.setup_edges([("coordinator", "coder"), ("coder", "reviewer")])
        assert len(bridge.state.edges) == 2

    def test_setup_budget(self):
        bridge, _ = _make_bridge()
        bridge.setup_budget(150)
        assert bridge.state.budget == 150
        assert bridge.state.budget_remaining == 150

    # ── on_message_routed ────────────────────────────────────────────────────

    async def test_message_routed_emits_message_event(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"a": "Agent A", "b": "Agent B"})
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="hello")
        await bridge.on_message_routed("routed", msg)

        events = [json.loads(c) for c in captured]
        types = [e["type"] for e in events]
        assert "message" in types

        message_event = next(e for e in events if e["type"] == "message")
        assert message_event["from"] == "a"
        assert message_event["to"] == "b"
        assert message_event["content"] == "hello"

    async def test_message_routed_emits_stats_event(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"a": "A", "b": "B"})
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="x")
        await bridge.on_message_routed("routed", msg)

        events = [json.loads(c) for c in captured]
        stats = next(e for e in events if e["type"] == "stats")
        assert stats["message_count"] == 1

    async def test_message_routed_increments_sender_msg_count(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"a": "A", "b": "B"})
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="x")
        await bridge.on_message_routed("routed", msg)
        assert bridge.state.agents["a"].msg_count == 1

    async def test_message_routed_marks_sender_running(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"a": "A", "b": "B"})
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="x")
        await bridge.on_message_routed("routed", msg)
        assert bridge.state.agents["a"].status == "running"

    async def test_message_content_truncated_to_500(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"a": "A", "b": "B"})
        long_payload = "x" * 1000
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload=long_payload)
        await bridge.on_message_routed("routed", msg)
        events = [json.loads(c) for c in captured]
        message_event = next(e for e in events if e["type"] == "message")
        assert len(message_event["content"]) <= 500

    # ── on_agent_status ──────────────────────────────────────────────────────

    async def test_on_agent_status_broadcasts_event(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"coder": "Coder"})
        await bridge.on_agent_status("coder", "running", "claude-sonnet")

        events = [json.loads(c) for c in captured]
        status_event = next(e for e in events if e["type"] == "agent_status")
        assert status_event["agent"] == "coder"
        assert status_event["status"] == "running"
        assert status_event["model"] == "claude-sonnet"

    async def test_on_agent_status_updates_state(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"coder": "Coder"})
        await bridge.on_agent_status("coder", "completed", "haiku")
        assert bridge.state.agents["coder"].status == "completed"
        assert bridge.state.agents["coder"].model == "haiku"

    async def test_on_agent_status_empty_model_preserves_existing(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"coder": "Coder"})
        bridge.state.agents["coder"].model = "existing-model"
        await bridge.on_agent_status("coder", "running", "")
        # model should be unchanged since empty model was passed
        assert bridge.state.agents["coder"].model == "existing-model"

    async def test_on_agent_heartbeat_updates_state_and_broadcasts(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"coder": "Coder"})
        await bridge.on_agent_heartbeat("coder", {"status": "running", "ts": 123.45})

        assert bridge.state.agents["coder"].last_heartbeat == 123.45
        assert bridge.state.agents["coder"].status == "running"

        events = [json.loads(c) for c in captured]
        heartbeat_event = next(e for e in events if e["type"] == "agent_heartbeat")
        assert heartbeat_event["agent"] == "coder"
        assert heartbeat_event["status"] == "running"
        assert heartbeat_event["ts"] == 123.45

    # ── on_agent_complete ────────────────────────────────────────────────────

    async def test_on_agent_complete_marks_completed(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"reviewer": "Reviewer"})
        await bridge.on_agent_complete("reviewer", "LGTM")
        assert bridge.state.agents["reviewer"].status == "completed"
        assert bridge.state.agents["reviewer"].completed_result == "LGTM"

    async def test_on_agent_complete_broadcasts_complete_event(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"reviewer": "Reviewer"})
        await bridge.on_agent_complete("reviewer", "LGTM")

        events = [json.loads(c) for c in captured]
        complete_event = next(e for e in events if e["type"] == "complete")
        assert complete_event["agent"] == "reviewer"
        assert complete_event["result"] == "LGTM"
        assert complete_event["is_consensus"] is False

    async def test_on_agent_complete_detects_consensus(self):
        bridge, captured = _make_bridge()
        bridge.setup_agents({"reviewer": "Reviewer"})
        await bridge.on_agent_complete("reviewer", "Consensus: task done")

        events = [json.loads(c) for c in captured]
        complete_event = next(e for e in events if e["type"] == "complete")
        assert complete_event["is_consensus"] is True

    # ── DashboardState ───────────────────────────────────────────────────────

    def test_state_reset_clears_agents_and_messages(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"a": "A"})
        bridge.state.message_count = 5
        bridge.state.reset()
        assert bridge.state.agents == {}
        assert bridge.state.message_count == 0
        assert bridge.state.completed is False

    def test_to_init_event_structure(self):
        state = DashboardState()
        state.topology_id = "triangle"
        state.topology_label = "Triad"
        state.agent_neighbors = {"coder": ["reviewer", "tester"]}
        state.agent_positions = {"coder": "implementation hub"}
        state.graph_view = {"rows": [[{"node": "coder"}]], "order": ["coder"]}
        state.agents["coder"] = __import__(
            "web.state", fromlist=["AgentState"]
        ).AgentState(node_id="coder", role="Coder", status="idle")
        event = state.to_init_event()

        assert event["type"] == "init"
        assert "agents" in event
        assert "edges" in event
        assert "messages" in event
        assert "stats" in event
        assert event["plan"]["topology"]["id"] == "triangle"
        assert event["plan"]["neighbors"]["coder"] == ["reviewer", "tester"]
        assert event["plan"]["graph_view"]["order"] == ["coder"]
        agent = event["agents"][0]
        assert agent["id"] == "coder"
        assert agent["role"] == "Coder"
        assert "last_heartbeat" in agent

    async def test_budget_decrements_with_messages(self):
        bridge, _ = _make_bridge()
        bridge.setup_agents({"a": "A", "b": "B"})
        bridge.setup_budget(10)
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="x")
        await bridge.on_message_routed("routed", msg)
        assert bridge.state.budget_remaining == 9
