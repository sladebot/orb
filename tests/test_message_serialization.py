"""Tests for Message round-trip serialization (Phase 1 of dual-mode spec)."""

from __future__ import annotations

import json

import pytest

from orb.messaging.message import Message, MessageType


class TestMessageToDict:
    def test_basic_fields(self):
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="hello")
        d = msg.to_dict()
        assert d["from_"] == "a"
        assert d["to"] == "b"
        assert d["type"] == "task"
        assert d["payload"] == "hello"

    def test_enum_serialized_as_string(self):
        for mt in MessageType:
            msg = Message(from_="a", to="b", type=mt, payload="x")
            assert msg.to_dict()["type"] == mt.value

    def test_all_fields_present(self):
        msg = Message(
            from_="a", to="b", type=MessageType.RESPONSE, payload="p",
            id="abc123", chain_id="chain1", depth=3,
            context_slice=["ctx1", "ctx2"],
            metadata={"key": "val"},
            timestamp=1000.0,
        )
        d = msg.to_dict()
        assert d["id"] == "abc123"
        assert d["chain_id"] == "chain1"
        assert d["depth"] == 3
        assert d["context_slice"] == ["ctx1", "ctx2"]
        assert d["metadata"] == {"key": "val"}
        assert d["timestamp"] == 1000.0


class TestMessageFromDict:
    def test_basic_round_trip(self):
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="hello")
        d = msg.to_dict()
        restored = Message.from_dict(d)
        assert restored.from_ == msg.from_
        assert restored.to == msg.to
        assert restored.type == msg.type
        assert restored.payload == msg.payload
        assert restored.id == msg.id
        assert restored.chain_id == msg.chain_id

    def test_all_fields_round_trip(self):
        msg = Message(
            from_="agent-1", to="agent-2", type=MessageType.FEEDBACK,
            payload="looks good", id="id1", chain_id="ch1", depth=5,
            context_slice=["a", "b", "c"],
            metadata={"nested": {"deep": True}, "count": 42},
            timestamp=1234567890.123,
        )
        restored = Message.from_dict(msg.to_dict())
        assert restored.from_ == msg.from_
        assert restored.to == msg.to
        assert restored.type == msg.type
        assert restored.payload == msg.payload
        assert restored.id == msg.id
        assert restored.chain_id == msg.chain_id
        assert restored.depth == msg.depth
        assert restored.context_slice == msg.context_slice
        assert restored.metadata == msg.metadata
        assert restored.timestamp == msg.timestamp

    def test_all_message_types(self):
        for mt in MessageType:
            d = {"from_": "a", "to": "b", "type": mt.value, "payload": "x",
                 "id": "i", "chain_id": "c", "depth": 0,
                 "context_slice": [], "metadata": {}, "timestamp": 0.0}
            msg = Message.from_dict(d)
            assert msg.type == mt


class TestMessageJson:
    def test_to_json_is_valid_json(self):
        msg = Message(from_="a", to="b", type=MessageType.SYSTEM, payload="hi")
        raw = msg.to_json()
        parsed = json.loads(raw)
        assert parsed["from_"] == "a"

    def test_json_round_trip(self):
        msg = Message(
            from_="x", to="y", type=MessageType.COMPLETE,
            payload="done", id="id1", chain_id="ch1", depth=2,
            context_slice=["s1"], metadata={"m": 1}, timestamp=99.9,
        )
        restored = Message.from_json(msg.to_json())
        assert restored.from_ == msg.from_
        assert restored.to == msg.to
        assert restored.type == msg.type
        assert restored.payload == msg.payload
        assert restored.id == msg.id
        assert restored.chain_id == msg.chain_id
        assert restored.depth == msg.depth
        assert restored.context_slice == msg.context_slice
        assert restored.metadata == msg.metadata
        assert restored.timestamp == msg.timestamp

    def test_payload_with_newlines_and_special_chars(self):
        """Payloads can contain newlines, quotes, unicode — JSON must handle them."""
        payload = 'line1\nline2\t"quoted"\n\U0001f600'
        msg = Message(from_="a", to="b", type=MessageType.RESPONSE, payload=payload)
        restored = Message.from_json(msg.to_json())
        assert restored.payload == payload

    def test_empty_context_and_metadata(self):
        msg = Message(from_="a", to="b", type=MessageType.TASK, payload="p")
        restored = Message.from_json(msg.to_json())
        assert restored.context_slice == []
        assert restored.metadata == {}


class TestMessageReplyRoundTrip:
    def test_reply_preserves_chain_through_serialization(self):
        original = Message(from_="a", to="b", type=MessageType.TASK, payload="do this")
        reply = original.reply(from_="b", to="a", payload="done")
        restored = Message.from_json(reply.to_json())
        assert restored.chain_id == original.chain_id
        assert restored.depth == original.depth + 1
