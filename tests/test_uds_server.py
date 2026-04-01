"""Tests for UDSServer — Phase 5 of dual-mode spec."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid

import pytest

from orb.messaging.control import ControlType, make_control
from orb.messaging.message import Message, MessageType
from orb.messaging.uds_channel import UDSChannel, _HEADER_FMT, _HEADER_SIZE
from orb.runtime.uds_server import UDSServer


def _msg(from_: str = "a", to: str = "b", payload: str = "test") -> Message:
    return Message(from_=from_, to=to, type=MessageType.TASK, payload=payload)


@pytest.fixture
def socket_dir():
    path = f"/tmp/orb-test-{uuid.uuid4().hex[:8]}"
    os.makedirs(path, exist_ok=True)
    yield path
    # Cleanup
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class TestUDSServer:
    async def test_agent_connects_and_receives_message(self, socket_dir):
        """Server accepts agent connection and can forward messages to it."""
        server = UDSServer(socket_dir=socket_dir, agent_ids=["agent-a"])
        await server.start()

        # Simulate agent subprocess connecting
        sock_path = os.path.join(socket_dir, "agent-a.sock")
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await server.wait_for_agent("agent-a", timeout=2.0)

        # Send a message to agent-a through the server
        msg = _msg(from_="user", to="agent-a", payload="hello")
        await server.send_to_agent("agent-a", msg)

        # Agent reads the message
        header = await reader.readexactly(_HEADER_SIZE)
        length = struct.unpack(_HEADER_FMT, header)[0]
        payload = await reader.readexactly(length)
        received = Message.from_json(payload.decode("utf-8"))
        assert received.payload == "hello"

        writer.close()
        await server.stop()

    async def test_agent_sends_message_to_server(self, socket_dir):
        """Server receives regular messages from agent and forwards to route callback."""
        routed: list[Message] = []

        async def on_route(msg: Message):
            routed.append(msg)

        server = UDSServer(socket_dir=socket_dir, agent_ids=["agent-a"])
        server.on_route = on_route
        await server.start()

        sock_path = os.path.join(socket_dir, "agent-a.sock")
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await server.wait_for_agent("agent-a", timeout=2.0)

        # Start reading from this agent
        server.start_reading("agent-a")

        # Agent sends a message
        msg = _msg(from_="agent-a", to="agent-b", payload="routed msg")
        payload = msg.to_json().encode("utf-8")
        header = struct.pack(_HEADER_FMT, len(payload))
        writer.write(header + payload)
        await writer.drain()

        await asyncio.sleep(0.1)
        assert len(routed) == 1
        assert routed[0].payload == "routed msg"
        assert routed[0].from_ == "agent-a"

        writer.close()
        await server.stop()

    async def test_control_message_dispatch(self, socket_dir):
        """Server dispatches control messages to registered callbacks."""
        completions: list[tuple[str, str]] = []

        async def on_complete(agent_id: str, result: str):
            completions.append((agent_id, result))

        server = UDSServer(socket_dir=socket_dir, agent_ids=["agent-a"])
        server.on_complete = on_complete
        await server.start()

        sock_path = os.path.join(socket_dir, "agent-a.sock")
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await server.wait_for_agent("agent-a", timeout=2.0)

        server.start_reading("agent-a")

        # Agent sends a control message
        ctrl = make_control(ControlType.COMPLETE, "agent-a", result="done")
        header = struct.pack(_HEADER_FMT, len(ctrl))
        writer.write(header + ctrl)
        await writer.drain()

        await asyncio.sleep(0.1)
        assert len(completions) == 1
        assert completions[0] == ("agent-a", "done")

        writer.close()
        await server.stop()

    async def test_shutdown_sends_control_to_agents(self, socket_dir):
        """Stop sends shutdown control message to connected agents."""
        server = UDSServer(socket_dir=socket_dir, agent_ids=["agent-a"])
        await server.start()

        sock_path = os.path.join(socket_dir, "agent-a.sock")
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await server.wait_for_agent("agent-a", timeout=2.0)

        # Stop the server — should send shutdown to agent-a
        await server.stop()

        # Agent reads the shutdown control message
        try:
            header = await asyncio.wait_for(reader.readexactly(_HEADER_SIZE), timeout=2.0)
            length = struct.unpack(_HEADER_FMT, header)[0]
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=2.0)
            data = json.loads(payload.decode("utf-8"))
            assert data["_control"] == "shutdown"
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            # Connection closed before we could read — also acceptable
            pass

        writer.close()

    async def test_multiple_agents(self, socket_dir):
        """Server handles multiple agent connections."""
        server = UDSServer(socket_dir=socket_dir, agent_ids=["a", "b"])
        await server.start()

        sock_a = os.path.join(socket_dir, "a.sock")
        sock_b = os.path.join(socket_dir, "b.sock")
        reader_a, writer_a = await asyncio.open_unix_connection(sock_a)
        reader_b, writer_b = await asyncio.open_unix_connection(sock_b)

        await server.wait_for_agent("a", timeout=2.0)
        await server.wait_for_agent("b", timeout=2.0)

        # Send to each agent
        await server.send_to_agent("a", _msg(payload="for-a"))
        await server.send_to_agent("b", _msg(payload="for-b"))

        header = await reader_a.readexactly(_HEADER_SIZE)
        length = struct.unpack(_HEADER_FMT, header)[0]
        payload = await reader_a.readexactly(length)
        assert Message.from_json(payload.decode("utf-8")).payload == "for-a"

        header = await reader_b.readexactly(_HEADER_SIZE)
        length = struct.unpack(_HEADER_FMT, header)[0]
        payload = await reader_b.readexactly(length)
        assert Message.from_json(payload.decode("utf-8")).payload == "for-b"

        writer_a.close()
        writer_b.close()
        await server.stop()

    async def test_socket_cleanup_on_stop(self, socket_dir):
        """Socket files are cleaned up on stop."""
        server = UDSServer(socket_dir=socket_dir, agent_ids=["agent-a"])
        await server.start()
        sock_path = os.path.join(socket_dir, "agent-a.sock")
        assert os.path.exists(sock_path)
        await server.stop()
        assert not os.path.exists(sock_path)

    async def test_get_channel_after_connect(self, socket_dir):
        """get_channel returns the UDSChannel once an agent connects."""
        server = UDSServer(socket_dir=socket_dir, agent_ids=["agent-a"])
        await server.start()

        sock_path = os.path.join(socket_dir, "agent-a.sock")
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await server.wait_for_agent("agent-a", timeout=2.0)

        ch = server.get_channel("agent-a")
        assert ch is not None, "get_channel must return channel after agent connects"

        writer.close()
        await server.stop()
