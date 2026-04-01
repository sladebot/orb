"""Tests for AgentProcess and control messages — Phase 4 of dual-mode spec."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid
from unittest.mock import MagicMock, patch

import pytest

from orb.messaging.control import ControlType, is_control, parse_control, make_control
from orb.messaging.message import Message, MessageType
from orb.agent.types import AgentConfig, TopologyContext


# ── Control message unit tests ──


class TestControlMessages:
    def test_make_control_complete(self):
        raw = make_control(ControlType.COMPLETE, "reviewer", result="LGTM")
        d = json.loads(raw)
        assert d["_control"] == "complete"
        assert d["agent_id"] == "reviewer"
        assert d["result"] == "LGTM"

    def test_make_control_activity(self):
        raw = make_control(ControlType.ACTIVITY, "coder", text="Writing file...")
        d = json.loads(raw)
        assert d["_control"] == "activity"
        assert d["text"] == "Writing file..."

    def test_make_control_heartbeat(self):
        raw = make_control(ControlType.HEARTBEAT, "coder", payload={"tokens": 150})
        d = json.loads(raw)
        assert d["_control"] == "heartbeat"
        assert d["payload"] == {"tokens": 150}

    def test_make_control_shutdown(self):
        raw = make_control(ControlType.SHUTDOWN, "coder")
        d = json.loads(raw)
        assert d["_control"] == "shutdown"

    def test_is_control(self):
        assert is_control({"_control": "complete", "agent_id": "a"})
        assert not is_control({"from_": "a", "to": "b"})

    def test_parse_control(self):
        d = {"_control": "activity", "agent_id": "coder", "text": "busy"}
        ctrl_type, agent_id, extra = parse_control(d)
        assert ctrl_type == ControlType.ACTIVITY
        assert agent_id == "coder"
        assert extra == {"text": "busy"}

    def test_all_control_types_round_trip(self):
        for ct in ControlType:
            raw = make_control(ct, "agent-1", data="test")
            d = json.loads(raw)
            assert is_control(d)
            parsed_type, agent_id, extra = parse_control(d)
            assert parsed_type == ct
            assert agent_id == "agent-1"


# ── AgentConfig serialization ──


class TestAgentConfigSerialization:
    def test_basic_round_trip(self):
        config = AgentConfig(node_id="coder", role="Coder", description="Writes code")
        d = config.to_dict()
        restored = AgentConfig.from_dict(d)
        assert restored.node_id == config.node_id
        assert restored.role == config.role
        assert restored.description == config.description
        assert restored.base_complexity == config.base_complexity
        assert restored.max_history == config.max_history
        assert restored.enable_filesystem == config.enable_filesystem

    def test_with_pinned_model(self):
        from orb.llm.types import ModelConfig, ModelTier
        pm = ModelConfig(tier=ModelTier.CLOUD_FAST, model_id="claude-3-sonnet", provider="anthropic")
        config = AgentConfig(
            node_id="coder", role="Coder", description="Writes code",
            pinned_model=pm,
        )
        d = config.to_dict()
        assert "pinned_model" in d
        restored = AgentConfig.from_dict(d)
        assert restored.pinned_model is not None
        assert restored.pinned_model.model_id == "claude-3-sonnet"
        assert restored.pinned_model.provider == "anthropic"

    def test_with_sandbox(self, tmp_path):
        from orb.sandbox.sandbox import Sandbox
        sandbox = Sandbox(str(tmp_path))
        config = AgentConfig(
            node_id="coder", role="Coder", description="Writes code",
            enable_filesystem=True, sandbox=sandbox,
        )
        d = config.to_dict()
        assert "sandbox_root" in d
        restored = AgentConfig.from_dict(d)
        assert restored.sandbox is not None
        assert str(restored.sandbox.root) == str(tmp_path)

    def test_without_optional_fields(self):
        d = {"node_id": "a", "role": "R", "description": "D"}
        config = AgentConfig.from_dict(d)
        assert config.base_complexity == 50
        assert config.max_history == 20
        assert config.pinned_model is None
        assert config.sandbox is None


# ── TopologyContext serialization ──


class TestTopologyContextSerialization:
    def test_round_trip(self):
        ctx = TopologyContext(
            topology_id="pair",
            topology_label="Pair Programming",
            node_id="coder",
            role="Coder",
            direct_neighbors={"reviewer": "Reviewer"},
            graph_edges=[("coder", "reviewer")],
            node_roles={"coder": "Coder", "reviewer": "Reviewer"},
            workflow_steps=["Write code", "Review code"],
            completion_rules=["Both agents agree"],
        )
        d = ctx.to_dict()
        restored = TopologyContext.from_dict(d)
        assert restored.topology_id == ctx.topology_id
        assert restored.topology_label == ctx.topology_label
        assert restored.node_id == ctx.node_id
        assert restored.role == ctx.role
        assert restored.direct_neighbors == ctx.direct_neighbors
        assert restored.graph_edges == ctx.graph_edges
        assert restored.node_roles == ctx.node_roles
        assert restored.workflow_steps == ctx.workflow_steps
        assert restored.completion_rules == ctx.completion_rules

    def test_graph_edges_are_tuples(self):
        ctx = TopologyContext(
            topology_id="t", topology_label="T", node_id="a", role="R",
            direct_neighbors={}, graph_edges=[("a", "b"), ("b", "c")],
            node_roles={}, workflow_steps=[], completion_rules=[],
        )
        d = ctx.to_dict()
        # Serialized as lists
        assert d["graph_edges"] == [["a", "b"], ["b", "c"]]
        # Restored as tuples
        restored = TopologyContext.from_dict(d)
        assert all(isinstance(e, tuple) for e in restored.graph_edges)


# ── AgentProcess socket ownership ──


class TestAgentProcessSocketOwnership:
    """AgentProcess must NOT create or delete sockets — UDSServer owns them."""

    @pytest.mark.asyncio
    async def test_start_does_not_create_socket(self, tmp_path):
        """AgentProcess.start() must not create a socket file."""
        from orb.runtime.agent_process import AgentProcess

        config = AgentConfig(node_id="coder", role="Coder", description="Writes code")
        proc = AgentProcess(config=config, topology_context=None, socket_dir=str(tmp_path))

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.is_alive.return_value = True

        with patch("multiprocessing.Process", return_value=fake_proc):
            await proc.start()

        # No socket file should have been created by AgentProcess
        sock_path = tmp_path / "coder.sock"
        assert not sock_path.exists(), (
            "AgentProcess.start() must not create the socket — UDSServer owns it"
        )

    @pytest.mark.asyncio
    async def test_start_does_not_delete_existing_socket(self, tmp_path):
        """AgentProcess.start() must not delete a socket that UDSServer created."""
        from orb.runtime.agent_process import AgentProcess

        # Simulate UDSServer having created the socket already
        sock_path = tmp_path / "coder.sock"
        sock_path.touch()

        config = AgentConfig(node_id="coder", role="Coder", description="Writes code")
        proc = AgentProcess(config=config, topology_context=None, socket_dir=str(tmp_path))

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.is_alive.return_value = True

        with patch("multiprocessing.Process", return_value=fake_proc):
            await proc.start()

        assert sock_path.exists(), (
            "AgentProcess.start() must not delete UDSServer's socket"
        )

    @pytest.mark.asyncio
    async def test_bootstrap_contains_correct_socket_path(self, tmp_path):
        """Subprocess bootstrap must reference the UDSServer socket path."""
        from orb.runtime.agent_process import AgentProcess

        config = AgentConfig(node_id="coder", role="Coder", description="Writes code")
        proc = AgentProcess(config=config, topology_context=None, socket_dir=str(tmp_path))

        captured_args = {}

        def fake_process_init(*pargs, target=None, args=(), name=None, daemon=None):
            captured_args["bootstrap"] = json.loads(args[0])
            m = MagicMock()
            m.pid = 99
            m.is_alive.return_value = True
            return m

        with patch("multiprocessing.Process", side_effect=fake_process_init):
            await proc.start()

        expected_path = str(tmp_path / "coder.sock")
        assert captured_args["bootstrap"]["socket_path"] == expected_path

    @pytest.mark.asyncio
    async def test_stop_terminates_process(self, tmp_path):
        """AgentProcess.stop() waits for the subprocess to exit."""
        from orb.runtime.agent_process import AgentProcess

        config = AgentConfig(node_id="coder", role="Coder", description="Writes code")
        proc = AgentProcess(config=config, topology_context=None, socket_dir=str(tmp_path))

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        # is_alive returns True before join, False after
        fake_proc.is_alive.side_effect = [True, False]

        with patch("multiprocessing.Process", return_value=fake_proc):
            await proc.start()

        await proc.stop(timeout=1.0)
        fake_proc.join.assert_called_once()
