"""Orchestrator-side UDS server — Phase 5 of dual-mode spec.

Creates per-agent Unix domain sockets, accepts connections from agent
subprocesses, and bridges messages between agents and the MessageBus.
Control messages are dispatched to orchestrator callbacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from typing import Callable, Awaitable

from ..messaging.control import ControlType, is_control, parse_control, make_control
from ..messaging.message import Message
from ..messaging.uds_channel import UDSChannel, _HEADER_FMT, _HEADER_SIZE

logger = logging.getLogger(__name__)

CompletionCallback = Callable[[str, str], Awaitable[None] | None]
ActivityCallback = Callable[[str, str], Awaitable[None] | None]
HeartbeatCallback = Callable[[str, dict], Awaitable[None] | None]
RouteCallback = Callable[[Message], Awaitable[None]]


class UDSServer:
    """Manages per-agent UDS sockets for the orchestrator side of process mode."""

    def __init__(self, socket_dir: str, agent_ids: list[str]) -> None:
        self._socket_dir = socket_dir
        self._agent_ids = agent_ids

        # Per-agent state
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._channels: dict[str, UDSChannel] = {}
        self._connected_events: dict[str, asyncio.Event] = {
            aid: asyncio.Event() for aid in agent_ids
        }
        self._read_tasks: dict[str, asyncio.Task] = {}

        # Callbacks
        self.on_complete: CompletionCallback | None = None
        self.on_activity: ActivityCallback | None = None
        self.on_heartbeat: HeartbeatCallback | None = None
        self.on_route: RouteCallback | None = None

    async def start(self) -> None:
        """Start per-agent UDS servers, ready to accept connections."""
        os.makedirs(self._socket_dir, exist_ok=True)

        for agent_id in self._agent_ids:
            sock_path = self._socket_path(agent_id)
            if os.path.exists(sock_path):
                os.unlink(sock_path)

            def make_handler(aid: str):
                async def handler(reader, writer):
                    self._channels[aid] = UDSChannel(reader, writer)
                    self._connected_events[aid].set()
                    logger.info(f"Agent {aid} connected over UDS")
                return handler

            server = await asyncio.start_unix_server(
                make_handler(agent_id), path=sock_path
            )
            os.chmod(sock_path, 0o600)
            self._servers[agent_id] = server

        logger.info(f"UDS server started for {len(self._agent_ids)} agents in {self._socket_dir}")

    async def wait_for_agent(self, agent_id: str, timeout: float = 30.0) -> None:
        """Wait for a specific agent to connect."""
        await asyncio.wait_for(self._connected_events[agent_id].wait(), timeout=timeout)

    async def wait_for_all(self, timeout: float = 30.0) -> None:
        """Wait for all agents to connect."""
        await asyncio.gather(
            *(self.wait_for_agent(aid, timeout=timeout) for aid in self._agent_ids)
        )

    def start_reading(self, agent_id: str) -> None:
        """Start reading messages from a connected agent."""
        if agent_id in self._read_tasks:
            return
        self._read_tasks[agent_id] = asyncio.create_task(
            self._read_loop(agent_id), name=f"uds-read-{agent_id}"
        )

    def start_reading_all(self) -> None:
        """Start reading from all connected agents."""
        for aid in self._agent_ids:
            if aid in self._channels:
                self.start_reading(aid)

    async def send_to_agent(self, agent_id: str, msg: Message) -> None:
        """Send a message to a specific agent."""
        channel = self._channels.get(agent_id)
        if channel is None:
            raise RuntimeError(f"Agent {agent_id} not connected")
        await channel.send(msg)

    def get_channel(self, agent_id: str) -> UDSChannel | None:
        return self._channels.get(agent_id)

    async def _read_loop(self, agent_id: str) -> None:
        """Read frames from an agent, dispatch control vs regular messages."""
        channel = self._channels.get(agent_id)
        if channel is None:
            return

        reader = channel._reader
        try:
            while True:
                header = await reader.readexactly(_HEADER_SIZE)
                length = struct.unpack(_HEADER_FMT, header)[0]
                payload_bytes = await reader.readexactly(length)
                data = json.loads(payload_bytes.decode("utf-8"))

                if is_control(data):
                    await self._dispatch_control(data)
                else:
                    msg = Message.from_dict(data)
                    if self.on_route:
                        await self.on_route(msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            logger.info(f"Agent {agent_id} UDS read loop ended (connection closed)")
        except asyncio.CancelledError:
            pass

    async def _dispatch_control(self, data: dict) -> None:
        ctrl_type, agent_id, extra = parse_control(data)
        if ctrl_type == ControlType.COMPLETE and self.on_complete:
            result = extra.get("result", "")
            cb = self.on_complete(agent_id, result)
            if asyncio.iscoroutine(cb):
                await cb
        elif ctrl_type == ControlType.ACTIVITY and self.on_activity:
            text = extra.get("text", "")
            cb = self.on_activity(agent_id, text)
            if asyncio.iscoroutine(cb):
                await cb
        elif ctrl_type == ControlType.HEARTBEAT and self.on_heartbeat:
            payload = extra.get("payload", {})
            cb = self.on_heartbeat(agent_id, payload)
            if asyncio.iscoroutine(cb):
                await cb

    async def stop(self) -> None:
        """Send shutdown to all agents, cancel read loops, close servers, clean up sockets."""
        # Send shutdown control to each connected agent
        for agent_id, channel in self._channels.items():
            if not channel.closed:
                try:
                    ctrl = make_control(ControlType.SHUTDOWN, agent_id)
                    header = struct.pack(_HEADER_FMT, len(ctrl))
                    channel._writer.write(header + ctrl)
                    await channel._writer.drain()
                except Exception:
                    pass

        # Cancel read tasks
        for task in self._read_tasks.values():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._read_tasks.clear()

        # Close channels
        for channel in self._channels.values():
            if not channel.closed:
                channel.close()
        self._channels.clear()

        # Close servers and clean up sockets
        for agent_id, server in self._servers.items():
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            sock_path = self._socket_path(agent_id)
            if os.path.exists(sock_path):
                os.unlink(sock_path)
        self._servers.clear()

    def _socket_path(self, agent_id: str) -> str:
        return os.path.join(self._socket_dir, f"{agent_id}.sock")
