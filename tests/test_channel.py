import asyncio

import pytest

from orb.messaging.channel import AgentChannel, ChannelClosed
from orb.messaging.message import Message, MessageType


def _msg(payload: str = "test") -> Message:
    return Message(from_="a", to="b", type=MessageType.TASK, payload=payload)


class TestAgentChannel:
    async def test_send_receive(self):
        ch = AgentChannel()
        msg = _msg("hello")
        await ch.send(msg)
        received = await ch.receive()
        assert received.payload == "hello"

    async def test_fifo_order(self):
        ch = AgentChannel()
        for i in range(5):
            await ch.send(_msg(f"msg-{i}"))
        for i in range(5):
            received = await ch.receive()
            assert received.payload == f"msg-{i}"

    async def test_close_unblocks_receive(self):
        ch = AgentChannel()

        async def close_later():
            await asyncio.sleep(0.05)
            ch.close()

        asyncio.create_task(close_later())
        with pytest.raises(ChannelClosed):
            await ch.receive()

    async def test_send_after_close(self):
        ch = AgentChannel()
        ch.close()
        with pytest.raises(ChannelClosed):
            await ch.send(_msg())

    async def test_backpressure(self):
        ch = AgentChannel(maxsize=2)
        await ch.send(_msg("1"))
        await ch.send(_msg("2"))
        assert ch.full

        # Third send should block; verify with timeout
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ch.send(_msg("3")), timeout=0.1)

    async def test_qsize(self):
        ch = AgentChannel()
        assert ch.qsize == 0
        await ch.send(_msg())
        assert ch.qsize == 1
        await ch.receive()
        assert ch.qsize == 0
