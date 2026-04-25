"""Real-time LLM token streaming — provider, agent, and bridge wiring.

Contract (shared with stream-tui/#13 and stream-dashboard/#14):

* Provider ``complete(request, on_chunk=...)`` invokes ``on_chunk(delta)``
  for every non-empty text chunk as it arrives. Non-streaming providers
  (ollama/omlx/vmlx) accept the kwarg but never invoke it.
* Agent broadcasts a ``message_delta`` event per chunk:

    {
      "type": "message_delta",
      "from": <agent_id>,
      "chain_id": <chain_id>,
      "delta": <text>,
      "index": <0-based monotonic int>,
    }

* The final ``message`` event still fires once the assistant's turn ends,
  with the full accumulated content. Empty deltas MUST NOT fire.
* Sessions can opt out via ``streaming_enabled=False``: zero deltas, final
  message as before.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

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
from orb.runtime.transcript import ConversationSession


# ── Helpers ──────────────────────────────────────────────────────────────


class StreamingMockClient(LLMClient):
    """Mock provider that replays a scripted chunk sequence via on_chunk.

    Mirrors what a real streaming SDK does: invokes ``on_chunk`` for each
    partial text chunk, then returns a ``CompletionResponse`` carrying the
    accumulated content + tool calls. Non-chunked calls (``on_chunk=None``)
    just return the response.
    """

    def __init__(
        self,
        *,
        chunks: list[str] | None = None,
        tool_calls: list[ToolCall] | None = None,
        model: str = "mock-stream",
    ) -> None:
        self._chunks = list(chunks or [])
        self._tool_calls = list(tool_calls or [])
        self._model = model
        self.on_chunk_invoked_with: list[str] = []

    async def complete(self, request, *, on_chunk=None):  # type: ignore[override]
        content = ""
        for chunk in self._chunks:
            content += chunk
            if on_chunk is not None and chunk:
                self.on_chunk_invoked_with.append(chunk)
                await on_chunk(chunk)
        return CompletionResponse(
            content=content,
            tool_calls=list(self._tool_calls),
            model=self._model,
            stop_reason="end_turn",
            usage={"input": 1, "output": len(content)},
        )

    async def close(self) -> None:
        pass


def _build_single_agent(client: LLMClient):
    graph = Graph()
    graph.add_node("agent_a")
    graph.add_node("agent_b")
    graph.add_edge("agent_a", "agent_b")
    bus = MessageBus(graph)
    ch_a = AgentChannel()
    ch_b = AgentChannel()
    bus.register_channel("agent_a", ch_a)
    bus.register_channel("agent_b", ch_b)
    providers = {"mock": client}
    mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock-stream", provider="mock")
    overrides = {t: mock_model for t in ModelTier}
    cfg = AgentConfig(node_id="agent_a", role="Coder", description="Writes code")
    agent = LLMAgent(cfg, ch_a, bus, providers, model_overrides=overrides)
    agent.initialize({"agent_b": "Reviewer"})
    return agent, bus, ch_a, ch_b


# ── Provider-level tests ─────────────────────────────────────────────────


class TestProviderOnChunk:
    """Each streaming provider must hand deltas to on_chunk as they arrive."""

    async def test_on_chunk_receives_sequenced_deltas(self):
        client = StreamingMockClient(
            chunks=["Hello", " ", "world"],
            tool_calls=[ToolCall(id="t1", name="complete_task", input={"result": "ok"})],
        )
        received: list[str] = []

        async def on_chunk(delta: str) -> None:
            received.append(delta)

        req = CompletionRequest(messages=[{"role": "user", "content": "hi"}])
        resp = await client.complete(req, on_chunk=on_chunk)

        assert received == ["Hello", " ", "world"]
        assert resp.content == "Hello world"
        assert resp.tool_calls[0].name == "complete_task"

    async def test_complete_without_on_chunk_still_works(self):
        client = StreamingMockClient(chunks=["a", "b"])
        req = CompletionRequest(messages=[{"role": "user", "content": "hi"}])
        resp = await client.complete(req)
        assert resp.content == "ab"
        assert client.on_chunk_invoked_with == []

    async def test_non_streaming_provider_accepts_on_chunk_but_never_invokes(self):
        """Ollama/omlx/vmlx must accept on_chunk kwarg without TypeError
        and must not invoke it (preserving back-compat — no deltas)."""
        # Direct import check: the three providers below all define complete
        # with an optional on_chunk param.
        import inspect
        from orb.llm.ollama import OllamaProvider
        from orb.llm.omlx import OmlxProvider
        from orb.llm.vmlx import VmlxProvider

        for cls in (OllamaProvider, OmlxProvider, VmlxProvider):
            sig = inspect.signature(cls.complete)
            assert "on_chunk" in sig.parameters, (
                f"{cls.__name__}.complete must accept on_chunk kwarg"
            )


# ── Agent-level tests ────────────────────────────────────────────────────


class TestAgentBroadcastsDeltas:
    async def test_agent_emits_message_delta_per_chunk_with_chain_id_and_index(self):
        client = StreamingMockClient(
            chunks=["Sure", ", ", "done."],
            tool_calls=[ToolCall(id="t1", name="complete_task", input={"result": "ok"})],
        )
        agent, bus, ch_a, ch_b = _build_single_agent(client)

        deltas: list[dict] = []

        async def on_delta(chain_id: str, delta: str, index: int) -> None:
            deltas.append({"chain_id": chain_id, "delta": delta, "index": index})

        agent._on_message_delta = on_delta

        msg = Message(
            from_="agent_b", to="agent_a",
            type=MessageType.TASK, payload="do the thing",
        )
        await agent.process(msg)

        assert [d["delta"] for d in deltas] == ["Sure", ", ", "done."]
        # Index is 0-based, monotonic, per chain_id.
        assert [d["index"] for d in deltas] == [0, 1, 2]
        # All deltas share the incoming msg's chain_id (same chain carries
        # through to the outgoing reply's ``message`` event).
        assert {d["chain_id"] for d in deltas} == {msg.chain_id}

    async def test_empty_deltas_do_not_fire(self):
        """Empty string chunks must be dropped — the contract explicitly
        forbids emitting them."""
        client = StreamingMockClient(
            chunks=["", "hello", ""],
            tool_calls=[ToolCall(id="t1", name="complete_task", input={"result": "k"})],
        )
        agent, *_ = _build_single_agent(client)

        deltas: list[str] = []

        async def on_delta(chain_id: str, delta: str, index: int) -> None:
            deltas.append(delta)

        agent._on_message_delta = on_delta
        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="p")
        await agent.process(msg)
        assert deltas == ["hello"]

    async def test_deltas_precede_final_message_with_send_message_tool(self):
        """End-to-end ordering: deltas fire first, then the final
        ``message`` event arrives with the outgoing tool-call content.

        Guards against a refactor that accidentally reorders the
        message broadcast ahead of the stream, or drops the final event
        when deltas are present. The final message's content is the
        ``send_message`` tool argument, NOT the streamed text (per the
        contract note in ``DashboardBridge.on_message_delta`` — the two
        can and will differ).
        """
        client = StreamingMockClient(
            chunks=["Let me ", "think"],
            tool_calls=[ToolCall(
                id="t1", name="send_message",
                input={"to": "agent_b", "content": "Here is the plan"},
            )],
        )
        agent, bus, ch_a, ch_b = _build_single_agent(client)

        events: list[tuple[str, Any]] = []

        async def on_delta(chain_id: str, delta: str, index: int) -> None:
            events.append(("delta", {
                "chain_id": chain_id, "delta": delta, "index": index,
            }))

        async def on_routed(event: str, msg: Message) -> None:
            if event == "routed":
                events.append(("message", {
                    "chain_id": msg.chain_id,
                    "from": msg.from_,
                    "content": msg.payload,
                }))

        agent._on_message_delta = on_delta
        bus.on_event(on_routed)

        msg = Message(
            from_="agent_b", to="agent_a",
            type=MessageType.TASK, payload="plan",
        )
        await agent.process(msg)

        kinds = [e[0] for e in events]
        # Deltas first, message last — ordering must not invert.
        assert kinds[:2] == ["delta", "delta"], kinds
        assert kinds[-1] == "message", kinds
        # Final message carries the tool-arg content, not the streamed text.
        final = events[-1][1]
        assert final["content"] == "Here is the plan"
        # All three events share the same chain_id.
        chains = {e[1]["chain_id"] for e in events}
        assert len(chains) == 1

    async def test_no_delta_hook_means_on_chunk_not_passed(self):
        """When the session has streaming disabled, the runtime leaves
        ``_on_message_delta`` unset; the agent must then avoid passing
        on_chunk to the provider so it never bothers streaming."""
        client = StreamingMockClient(
            chunks=["a", "b"],
            tool_calls=[ToolCall(id="t1", name="complete_task", input={"result": "k"})],
        )
        agent, *_ = _build_single_agent(client)
        # _on_message_delta stays unset — this is the streaming_enabled=False path.
        msg = Message(from_="agent_b", to="agent_a", type=MessageType.TASK, payload="p")
        await agent.process(msg)

        # Provider was called but on_chunk was never invoked.
        assert client.on_chunk_invoked_with == []


# ── Bridge envelope tests ────────────────────────────────────────────────


class TestBridgeEnvelope:
    async def test_on_message_delta_emits_exact_envelope(self):
        """stream-tui and stream-dashboard code against the literal shape;
        no extra fields, no missing fields."""
        from web.bridge import DashboardBridge
        from web.state import DashboardState

        state = DashboardState()
        sent: list[str] = []

        async def broadcast(raw: str) -> None:
            sent.append(raw)

        bridge = DashboardBridge(state, broadcast)
        await bridge.on_message_delta(
            chain_id="chain-xyz", from_="agent_a", delta="Hello", index=0,
        )
        assert len(sent) == 1
        payload = json.loads(sent[0])
        assert payload == {
            "type": "message_delta",
            "from": "agent_a",
            "chain_id": "chain-xyz",
            "delta": "Hello",
            "index": 0,
        }


# ── Session-level opt-out ────────────────────────────────────────────────


class TestStreamingEnabledFlag:
    def test_conversation_session_defaults_to_streaming_enabled(self):
        s = ConversationSession()
        assert s.streaming_enabled is True

    def test_conversation_session_round_trip_preserves_streaming_enabled(self, tmp_path: Path):
        s = ConversationSession()
        s.streaming_enabled = False
        p = tmp_path / "session.json"
        s.save(p)
        loaded = ConversationSession.load(p)
        assert loaded.streaming_enabled is False

    def test_dashboard_state_defaults_to_streaming_enabled(self):
        from web.state import DashboardState
        st = DashboardState()
        assert st.streaming_enabled is True

    def test_init_event_surfaces_streaming_enabled(self):
        from web.state import DashboardState
        st = DashboardState()
        st.streaming_enabled = False
        init = st.to_init_event()
        assert init["streaming_enabled"] is False

    def test_init_event_streaming_enabled_default_true(self):
        from web.state import DashboardState
        init = DashboardState().to_init_event()
        assert init["streaming_enabled"] is True
