from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from .message import Message


class ChannelClosed(Exception):
    pass


class AgentChannel(ABC):
    """Abstract base class for agent communication channels."""

    @abstractmethod
    async def send(self, msg: Message) -> None: ...

    @abstractmethod
    async def receive(self) -> Message: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @property
    @abstractmethod
    def qsize(self) -> int: ...


class InProcessChannel(AgentChannel):
    """In-process channel backed by asyncio.Queue — Go-style channel for agent messaging."""

    def __init__(self, maxsize: int = 32) -> None:
        self._queue: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def send(self, msg: Message) -> None:
        if self._closed:
            raise ChannelClosed("Channel is closed")
        await self._queue.put(msg)

    async def receive(self) -> Message:
        item = await self._queue.get()
        if item is None:
            raise ChannelClosed("Channel is closed")
        return item

    def close(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def full(self) -> bool:
        return self._queue.full()
