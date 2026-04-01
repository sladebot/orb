"""UDS (Unix Domain Socket) channel implementation — Phase 3 of dual-mode spec.

Framing protocol: [4-byte uint32 big-endian length][UTF-8 JSON payload]

Both regular Message frames and control frames (_control field present) share
the same framing. receive() detects SHUTDOWN control frames and raises
ChannelClosed so the agent run loop exits cleanly.
"""

from __future__ import annotations

import asyncio
import json
import struct
import logging

from .channel import AgentChannel, ChannelClosed
from .message import Message

logger = logging.getLogger(__name__)

_HEADER_FMT = "!I"  # network byte order, unsigned 32-bit int
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


class UDSChannel(AgentChannel):
    """Agent channel over a Unix domain socket using length-prefixed JSON framing."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    def from_connection(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> UDSChannel:
        return cls(reader, writer)

    @classmethod
    async def connect(cls, socket_path: str) -> UDSChannel:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        return cls(reader, writer)

    async def send(self, msg: Message) -> None:
        if self._closed:
            raise ChannelClosed("Channel is closed")
        payload = msg.to_json().encode("utf-8")
        header = struct.pack(_HEADER_FMT, len(payload))
        try:
            self._writer.write(header + payload)
            await self._writer.drain()
        except (ConnectionError, OSError):
            self._closed = True
            raise ChannelClosed("Connection lost during send")

    async def receive(self) -> Message:
        try:
            header = await self._reader.readexactly(_HEADER_SIZE)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._closed = True
            raise ChannelClosed("Connection closed")

        length = struct.unpack(_HEADER_FMT, header)[0]

        try:
            payload = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._closed = True
            raise ChannelClosed("Connection closed during payload read")

        raw = payload.decode("utf-8")
        parsed = json.loads(raw)
        if "_control" in parsed:
            # SHUTDOWN tells this agent to exit; any other control type is noise.
            self._closed = True
            raise ChannelClosed("Received SHUTDOWN from orchestrator")
        return Message.from_dict(parsed)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def qsize(self) -> int:
        return 0
