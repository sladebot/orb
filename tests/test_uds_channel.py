"""Tests for UDS channel — Phase 3 of dual-mode spec."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from orb.messaging.message import Message, MessageType
from orb.messaging.uds_channel import UDSChannel
from orb.messaging.channel import ChannelClosed


def _msg(payload: str = "test", from_: str = "a", to: str = "b") -> Message:
    return Message(from_=from_, to=to, type=MessageType.TASK, payload=payload)


@pytest.fixture
def socket_path():
    path = f"/tmp/orb-test-{uuid.uuid4().hex[:8]}.sock"
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def channel_pair(socket_path):
    """Create a connected server/client UDS channel pair."""
    server_channel: UDSChannel | None = None
    connected = asyncio.Event()

    async def on_connect(reader, writer):
        nonlocal server_channel
        server_channel = UDSChannel.from_connection(reader, writer)
        connected.set()

    server = await asyncio.start_unix_server(on_connect, path=socket_path)
    client_channel = await UDSChannel.connect(socket_path)
    await asyncio.wait_for(connected.wait(), timeout=2.0)
    assert server_channel is not None

    yield client_channel, server_channel

    # Teardown — close channels then server, with timeout to avoid hangs
    for ch in (client_channel, server_channel):
        if not ch.closed:
            ch.close()
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=1.0)
    except asyncio.TimeoutError:
        pass


class TestUDSChannel:
    async def test_send_receive(self, channel_pair):
        client, server = channel_pair
        msg = _msg("hello")
        await client.send(msg)
        received = await server.receive()
        assert received.payload == "hello"
        assert received.from_ == "a"
        assert received.to == "b"

    async def test_bidirectional(self, channel_pair):
        client, server = channel_pair
        await client.send(_msg("ping"))
        received = await server.receive()
        assert received.payload == "ping"

        await server.send(_msg("pong", from_="b", to="a"))
        received = await client.receive()
        assert received.payload == "pong"

    async def test_fifo_order(self, channel_pair):
        client, server = channel_pair
        for i in range(10):
            await client.send(_msg(f"msg-{i}"))
        for i in range(10):
            received = await server.receive()
            assert received.payload == f"msg-{i}"

    async def test_all_fields_preserved(self, channel_pair):
        client, server = channel_pair
        msg = Message(
            from_="agent-1", to="agent-2", type=MessageType.FEEDBACK,
            payload="looks good", id="id1", chain_id="ch1", depth=5,
            context_slice=["a", "b"], metadata={"key": "val"},
            timestamp=1234567890.123,
        )
        await client.send(msg)
        received = await server.receive()
        assert received.from_ == msg.from_
        assert received.to == msg.to
        assert received.type == msg.type
        assert received.payload == msg.payload
        assert received.id == msg.id
        assert received.chain_id == msg.chain_id
        assert received.depth == msg.depth
        assert received.context_slice == msg.context_slice
        assert received.metadata == msg.metadata
        assert received.timestamp == msg.timestamp

    async def test_close_raises_channel_closed_on_receive(self, channel_pair):
        client, server = channel_pair
        client.close()
        with pytest.raises(ChannelClosed):
            await asyncio.wait_for(server.receive(), timeout=2.0)

    async def test_send_after_close_raises(self, channel_pair):
        client, server = channel_pair
        client.close()
        with pytest.raises(ChannelClosed):
            await client.send(_msg())

    async def test_closed_property(self, channel_pair):
        client, server = channel_pair
        assert not client.closed
        client.close()
        assert client.closed

    async def test_large_payload(self, channel_pair):
        client, server = channel_pair
        big_payload = "x" * 100_000
        await client.send(_msg(big_payload))
        received = await server.receive()
        assert received.payload == big_payload

    async def test_payload_with_special_chars(self, channel_pair):
        client, server = channel_pair
        payload = 'line1\nline2\t"quoted"\n\U0001f600'
        await client.send(_msg(payload))
        received = await server.receive()
        assert received.payload == payload

    async def test_qsize_always_zero(self, channel_pair):
        client, server = channel_pair
        assert client.qsize == 0
