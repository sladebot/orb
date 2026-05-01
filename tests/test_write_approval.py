"""Tests for the staged file-write approval pipeline.

The runtime can be configured per-session to require explicit user
approval before any agent file write hits disk. The contract spans:

* ``ConversationSession.approval_required`` (persisted alongside the
  ``locked_*`` fields).
* ``DashboardState.approval_required`` mirrored from the session and
  surfaced in ``to_init_event``.
* ``GraphRuntime.request_write_approval`` / ``resolve_approval`` for
  the broadcast + future-resolution dance.
* ``LLMAgent._on_write_request`` hook gating ``_handle_write_file``.
* ``POST /api/v1/sessions/{sid}/approvals/{request_id}`` HTTP endpoint.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aiohttp.test_utils import TestClient, TestServer

from orb.agent.llm_agent import LLMAgent
from orb.agent.types import AgentConfig
from orb.graph.graph import Graph
from orb.llm.client import LLMClient
from orb.llm.types import (
    CompletionRequest,
    CompletionResponse,
    ModelConfig,
    ModelTier,
    ToolCall,
)
from orb.messaging.bus import MessageBus
from orb.messaging.channel import AgentChannel
from orb.messaging.message import Message, MessageType
from orb.runtime.graph_runtime import GraphRuntime
from orb.runtime.transcript import ConversationSession
from web.server import DashboardServer
from web.state import DashboardState


class _MockLLMClient(LLMClient):
    def __init__(self, responses: list[CompletionResponse]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
        else:
            resp = CompletionResponse(content="", model="mock")
        self._idx += 1
        return resp

    async def close(self) -> None:
        pass


def _make_agent() -> LLMAgent:
    graph = Graph()
    graph.add_node("agent_a")
    graph.add_node("agent_b")
    graph.add_edge("agent_a", "agent_b")
    bus = MessageBus(graph)
    ch_a = AgentChannel()
    ch_b = AgentChannel()
    bus.register_channel("agent_a", ch_a)
    bus.register_channel("agent_b", ch_b)

    mock_client = _MockLLMClient([
        CompletionResponse(
            content="",
            model="mock",
            tool_calls=[
                ToolCall(id="tc1", name="write_file", input={"path": "app.py", "content": "print('hi')"})
            ],
        ),
        CompletionResponse(
            content="",
            model="mock",
            tool_calls=[ToolCall(id="tc2", name="complete_task", input={"result": "Done"})],
        ),
    ])
    config_a = AgentConfig(node_id="agent_a", role="Coder", description="x")
    mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")
    overrides = {t: mock_model for t in ModelTier}
    agent = LLMAgent(config_a, ch_a, bus, {"mock": mock_client}, model_overrides=overrides)
    agent.initialize({"agent_b": "Reviewer"})
    sandbox = MagicMock()
    sandbox.list_directory = MagicMock(return_value="")
    sandbox.read_file = MagicMock(side_effect=FileNotFoundError("missing"))
    sandbox.write_file = MagicMock(return_value="wrote app.py")
    agent.config.sandbox = sandbox
    return agent


# ── ConversationSession ───────────────────────────────────────────────


class TestConversationSessionApprovalRoundtrip:
    def test_round_trip_preserves_approval_required(self, tmp_path: Path) -> None:
        session = ConversationSession(approval_required=True)
        path = tmp_path / "session.json"
        session.save(path)
        loaded = ConversationSession.load(path)
        assert loaded.approval_required is True

    def test_round_trip_default_false(self, tmp_path: Path) -> None:
        session = ConversationSession()
        path = tmp_path / "session.json"
        session.save(path)
        loaded = ConversationSession.load(path)
        assert loaded.approval_required is False


# ── DashboardState init event ─────────────────────────────────────────


class TestInitEventApprovalFlag:
    def test_init_event_surfaces_approval_required(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        ConversationSession(approval_required=True).save(path)
        runtime = GraphRuntime(session_path=path)
        event = runtime.current_init_event()
        assert event.get("approval_required") is True

    def test_init_event_default_false(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        ConversationSession().save(path)
        runtime = GraphRuntime(session_path=path)
        event = runtime.current_init_event()
        assert event.get("approval_required") is False


# ── request_write_approval / resolve_approval ─────────────────────────


@pytest.mark.asyncio
class TestApprovalPipeline:
    async def test_request_broadcasts_pending_and_awaits_future(self, tmp_path: Path) -> None:
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        runtime._conversation_session.approval_required = True

        events: list[str] = []

        async def sub(payload: str) -> None:
            events.append(payload)

        runtime.subscribe(sub)

        async def driver() -> tuple[bool, str]:
            return await runtime.request_write_approval(
                "agent_a", "app.py", "print('hi')", ""
            )

        task = asyncio.create_task(driver())

        # Wait for the request to register + broadcast.
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        assert runtime._pending_approvals, "request did not register"
        request_id = next(iter(runtime._pending_approvals))

        pending_events = [
            json.loads(e) for e in events
            if json.loads(e).get("type") == "file_write_pending"
        ]
        assert pending_events, "no file_write_pending broadcast"
        ev = pending_events[0]
        assert ev["agent"] == "agent_a"
        assert ev["request_id"] == request_id
        assert ev["path"] == "app.py"
        assert ev["content"] == "print('hi')"
        assert ev["old_content"] == ""

        # Resolve with edit.
        status, payload = await runtime.resolve_approval(
            request_id, "approve", edited_content="print('edited')", reason=None
        )
        assert status == 200
        assert payload["code"] == "APPROVAL_RESOLVED"
        approved, effective = await task
        assert approved is True
        assert effective == "print('edited')"
        assert request_id not in runtime._pending_approvals

    async def test_resolve_unknown_request_returns_404(self, tmp_path: Path) -> None:
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        status, payload = await runtime.resolve_approval(
            "nope", "approve", edited_content=None, reason=None
        )
        assert status == 404
        assert payload["code"] == "APPROVAL_UNKNOWN"

    async def test_resolve_reject_broadcasts_rejected_and_unblocks(self, tmp_path: Path) -> None:
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        runtime._conversation_session.approval_required = True

        events: list[dict] = []

        async def sub(payload: str) -> None:
            try:
                events.append(json.loads(payload))
            except (TypeError, ValueError):
                pass

        runtime.subscribe(sub)

        task = asyncio.create_task(
            runtime.request_write_approval("agent_a", "app.py", "BAD", "OLD")
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        request_id = next(iter(runtime._pending_approvals))

        status, _ = await runtime.resolve_approval(
            request_id, "reject", edited_content=None, reason="too risky"
        )
        assert status == 200
        approved, effective = await task
        assert approved is False
        assert effective == ""

        rejected = [e for e in events if e.get("type") == "file_write_rejected"]
        assert rejected
        assert rejected[0]["request_id"] == request_id
        assert rejected[0]["reason"] == "too risky"
        assert rejected[0]["agent"] == "agent_a"
        assert rejected[0]["path"] == "app.py"

    async def test_resolve_approve_default_uses_proposed_content(self, tmp_path: Path) -> None:
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        task = asyncio.create_task(
            runtime.request_write_approval("a", "x.py", "PROPOSED", "")
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        rid = next(iter(runtime._pending_approvals))
        status, _ = await runtime.resolve_approval(rid, "approve", edited_content=None, reason=None)
        assert status == 200
        approved, effective = await task
        assert approved is True
        assert effective == "PROPOSED"

    async def test_resolve_approve_with_empty_string_edit_treated_as_edit(
        self, tmp_path: Path
    ) -> None:
        """Empty string is a meaningful edit (user wiped the file)."""
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        task = asyncio.create_task(
            runtime.request_write_approval("a", "x.py", "PROPOSED", "")
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        rid = next(iter(runtime._pending_approvals))
        status, _ = await runtime.resolve_approval(rid, "approve", edited_content="", reason=None)
        assert status == 200
        approved, effective = await task
        assert approved is True
        assert effective == ""

    async def test_resolve_invalid_action_400s(self, tmp_path: Path) -> None:
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        task = asyncio.create_task(
            runtime.request_write_approval("a", "x.py", "P", "")
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        rid = next(iter(runtime._pending_approvals))
        status, payload = await runtime.resolve_approval(
            rid, "weird", edited_content=None, reason=None
        )
        assert status == 400
        assert payload["code"] == "INVALID_ACTION"
        # The pending entry must still be there so the user can retry.
        assert rid in runtime._pending_approvals
        # Clean up the dangling task so pytest doesn't warn.
        await runtime.resolve_approval(rid, "reject", edited_content=None, reason=None)
        await task

    async def test_stop_run_auto_rejects_pending_approvals(self, tmp_path: Path) -> None:
        runtime = GraphRuntime(session_path=tmp_path / "s.json")
        runtime._conversation_session.approval_required = True
        # Force the FSM into a state where stop_run actually does work.
        runtime._fsm.fire("start_run_begin")
        runtime._fsm.fire("orchestrator_task_created")

        async def driver() -> tuple[bool, str]:
            return await runtime.request_write_approval("a", "x.py", "P", "")

        task = asyncio.create_task(driver())
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break

        # No real run task is registered, but stop_run should still walk
        # the pending-approvals map and reject everyone.
        runtime._reject_all_pending_approvals("run stopped")  # noqa: SLF001
        approved, effective = await task
        assert approved is False
        assert effective == ""
        assert not runtime._pending_approvals


# ── LLMAgent gating ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLLMAgentApprovalHook:
    async def test_no_hook_passthrough_writes_to_sandbox(self) -> None:
        agent = _make_agent()
        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="go")
        await agent.process(msg)
        # Without _on_write_request the sandbox is hit normally.
        agent.config.sandbox.write_file.assert_called_once_with("app.py", "print('hi')")

    async def test_approved_write_uses_effective_content(self) -> None:
        agent = _make_agent()
        seen: list[tuple[str, str, str, str]] = []

        async def hook(agent_id: str, path: str, content: str, old_content: str):
            seen.append((agent_id, path, content, old_content))
            return (True, "EDITED CONTENT")

        agent._on_write_request = hook
        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="go")
        await agent.process(msg)

        assert seen == [("agent_a", "app.py", "print('hi')", "")]
        # The sandbox got the *edited* content, not the proposed content.
        agent.config.sandbox.write_file.assert_called_once_with("app.py", "EDITED CONTENT")

    async def test_file_write_callback_accepts_legacy_four_arg_signature(self) -> None:
        agent = _make_agent()
        fired: list[tuple[str, str, str, str]] = []

        def legacy_cb(agent_id: str, path: str, content: str, old_content: str) -> None:
            fired.append((agent_id, path, content, old_content))

        agent._on_file_write = legacy_cb
        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="go")
        await agent.process(msg)

        assert fired == [("agent_a", "app.py", "print('hi')", "")]

    async def test_rejected_write_skips_sandbox_and_records_tool_result(self) -> None:
        agent = _make_agent()

        async def hook(*_args, **_kwargs):
            return (False, "")

        agent._on_write_request = hook
        # Capture _on_file_write to assert it does NOT fire on reject.
        fired: list = []
        agent._on_file_write = lambda *a, **kw: fired.append(a)

        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="go")
        await agent.process(msg)

        agent.config.sandbox.write_file.assert_not_called()
        assert fired == []
        # The LLM must see a tool_result for tc1 — otherwise the next
        # turn 400s on a dangling tool_use block.
        # Inspect the conversation history for a tool_result message.
        msgs = agent._conversation.messages
        tool_results = [
            block
            for m in msgs
            for block in (m.get("content") if isinstance(m.get("content"), list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
            and block.get("tool_use_id") == "tc1"
        ]
        assert tool_results, "expected a tool_result for the rejected write"


# ── HTTP endpoint ────────────────────────────────────────────────────


@pytest.fixture
async def api_client(tmp_path: Path):
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18102)
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001
    aiohttp_server = TestServer(server._app)  # noqa: SLF001
    await aiohttp_server.start_server()
    async with TestClient(aiohttp_server) as test_client:
        yield test_client, server
    await aiohttp_server.close()


class TestApprovalEndpoint:
    async def test_unknown_request_id_404s(self, api_client) -> None:
        test_client, _ = api_client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        sid = create["data"]["session_id"]
        resp = await test_client.post(
            f"/api/v1/sessions/{sid}/approvals/no-such-id",
            json={"action": "approve"},
        )
        assert resp.status == 404
        data = await resp.json()
        assert data["code"] == "APPROVAL_UNKNOWN"

    async def test_invalid_action_400s(self, api_client) -> None:
        test_client, server = api_client
        create = await (await test_client.post(
            "/api/v1/sessions", json={"approval_required": True}
        )).json()
        sid = create["data"]["session_id"]
        runtime = server.manager.get_session(sid)

        task = asyncio.create_task(
            runtime.request_write_approval("a", "x.py", "P", "")
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        rid = next(iter(runtime._pending_approvals))
        resp = await test_client.post(
            f"/api/v1/sessions/{sid}/approvals/{rid}",
            json={"action": "noop"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "INVALID_ACTION"
        # Clean up
        await runtime.resolve_approval(rid, "reject", edited_content=None, reason=None)
        await task

    async def test_invalid_body_400s(self, api_client) -> None:
        test_client, _ = api_client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        sid = create["data"]["session_id"]
        resp = await test_client.post(
            f"/api/v1/sessions/{sid}/approvals/abc",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "INVALID_BODY"

    async def test_create_session_with_approval_required(self, api_client) -> None:
        test_client, server = api_client
        resp = await test_client.post(
            "/api/v1/sessions", json={"approval_required": True}
        )
        assert resp.status == 201
        data = await resp.json()
        sid = data["data"]["session_id"]
        runtime = server.manager.get_session(sid)
        assert runtime._conversation_session.approval_required is True  # noqa: SLF001

    async def test_approve_resolves_pending_request(self, api_client) -> None:
        test_client, server = api_client
        create = await (await test_client.post(
            "/api/v1/sessions", json={"approval_required": True}
        )).json()
        sid = create["data"]["session_id"]
        runtime = server.manager.get_session(sid)

        task = asyncio.create_task(
            runtime.request_write_approval("agent_a", "x.py", "P", "")
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if runtime._pending_approvals:
                break
        rid = next(iter(runtime._pending_approvals))

        resp = await test_client.post(
            f"/api/v1/sessions/{sid}/approvals/{rid}",
            json={"action": "approve", "edited_content": "FINAL"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["code"] == "APPROVAL_RESOLVED"
        assert data["data"]["request_id"] == rid
        assert data["data"]["action"] == "approve"
        approved, effective = await task
        assert approved is True
        assert effective == "FINAL"
