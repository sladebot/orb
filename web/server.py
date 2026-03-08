from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from aiohttp import web

from .state import DashboardState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class DashboardServer:
    """aiohttp-based WebSocket server for the live dashboard."""

    def __init__(self, state: DashboardState, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._app = web.Application()
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._agents: dict = {}  # live agent objects keyed by agent_id

        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        self._app.router.add_post("/api/inject", self._inject_handler)
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR)

    def set_agents(self, agents: dict) -> None:
        """Store a reference to live agent objects for direct message injection."""
        self._agents = agents

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Dashboard server running at http://localhost:{self.port}")

    async def stop(self) -> None:
        # Close all WebSocket connections
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, data: str) -> None:
        """Send data to all connected WebSocket clients."""
        closed = []
        for ws in self._clients:
            try:
                await ws.send_str(data)
            except (ConnectionResetError, Exception):
                closed.append(ws)
        for ws in closed:
            self._clients.discard(ws)

    async def _index_handler(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _state_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.to_init_event())

    async def _inject_handler(self, request: web.Request) -> web.Response:
        """POST /api/inject — send a message directly to an agent's channel."""
        from orb.messaging.message import Message, MessageType

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        target_id = body.get("to", "").strip()
        text = body.get("message", "").strip()

        if not target_id:
            return web.json_response({"ok": False, "error": "Missing 'to' field"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "Missing 'message' field"}, status=400)

        agent = self._agents.get(target_id)
        if agent is None:
            return web.json_response(
                {"ok": False, "error": f"Unknown agent: {target_id}"}, status=404
            )

        msg = Message(
            from_="user",
            to=target_id,
            type=MessageType.TASK,
            payload=text,
        )

        try:
            await agent.channel.send(msg)
        except Exception as exc:
            logger.exception("Failed to inject message")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

        return web.json_response({"ok": True})

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info(f"Dashboard client connected ({len(self._clients)} total)")

        # Send current state on connect
        try:
            await ws.send_str(json.dumps(self.state.to_init_event()))
        except Exception:
            pass

        try:
            async for msg in ws:
                pass  # We don't expect client messages, just keep connection alive
        finally:
            self._clients.discard(ws)
            logger.info(f"Dashboard client disconnected ({len(self._clients)} total)")

        return ws
