from __future__ import annotations

import asyncio
import json
import logging
import time
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

        # Fields for UI-initiated runs
        self._run_task: asyncio.Task | None = None
        self._providers: dict = {}
        self._config = None
        self._model_overrides = None
        self._tier_override = None

        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        self._app.router.add_post("/api/inject", self._inject_handler)
        self._app.router.add_post("/api/start", self._start_handler)
        self._app.router.add_post("/api/stop", self._stop_run_handler)
        self._app.router.add_get("/api/run-status", self._run_status_handler)
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR)

    def set_agents(self, agents: dict) -> None:
        """Store a reference to live agent objects for direct message injection."""
        self._agents = agents

    def set_providers(self, providers: dict, config, model_overrides, tier_override) -> None:
        """Store provider/config info so the UI can start runs via /api/start."""
        self._providers = providers
        self._config = config
        self._model_overrides = model_overrides
        self._tier_override = tier_override

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Dashboard server running at http://localhost:{self.port}")

    async def stop(self) -> None:
        # Cancel any running orchestrator task
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
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

    async def _start_handler(self, request: web.Request) -> web.Response:
        """POST /api/start — start an orchestrator run from the browser UI."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        query = (body.get("query") or "").strip()
        topology = (body.get("topology") or "triangle").strip()

        if not query:
            return web.json_response({"ok": False, "error": "Query must not be empty"}, status=400)

        if topology not in ("triangle", "dual-review"):
            return web.json_response(
                {"ok": False, "error": "topology must be 'triangle' or 'dual-review'"}, status=400
            )

        if not self._providers:
            return web.json_response(
                {"ok": False, "error": "Server has no providers configured"}, status=500
            )

        # Reject if a run is already in progress
        if self._run_task is not None and not self._run_task.done():
            return web.json_response({"ok": False, "error": "Run already in progress"})

        # Reset state for a fresh run
        self.state.reset()

        # Start orchestrator as a background task
        self._run_task = asyncio.create_task(
            self._run_orchestrator(query, topology)
        )
        self._run_task.add_done_callback(
            lambda t: logger.error("Run task failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )

        return web.json_response({"ok": True})

    async def _stop_run_handler(self, request: web.Request) -> web.Response:
        """POST /api/stop — cancel the currently running orchestrator task."""
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            await self.broadcast(json.dumps({"type": "stopped"}))
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "No run in progress"})

    async def _run_status_handler(self, request: web.Request) -> web.Response:
        """GET /api/run-status — return whether a run is currently active."""
        running = self._run_task is not None and not self._run_task.done()
        return web.json_response({
            "running": running,
            "message_count": self.state.message_count,
        })

    async def _run_orchestrator(self, query: str, topology: str) -> None:
        """Build topology, wire dashboard bridge, and run the orchestrator."""
        from web.bridge import DashboardBridge

        bridge = DashboardBridge(self.state, self.broadcast)

        if topology == "dual-review":
            from orb.topologies.dual_review import create_dual_review
            orchestrator = create_dual_review(
                providers=self._providers,
                config=self._config,
                model_overrides=self._model_overrides,
                trace=False,
                tier_override=self._tier_override,
            )
        else:
            from orb.topologies.triangle import create_triangle
            orchestrator = create_triangle(
                providers=self._providers,
                config=self._config,
                model_overrides=self._model_overrides,
                trace=False,
                tier_override=self._tier_override,
            )

        # Set up bridge with topology info
        agent_roles = {aid: a.config.role for aid, a in orchestrator.agents.items()}
        bridge.setup_agents(agent_roles)
        bridge.setup_edges([(e.a, e.b) for e in orchestrator.bus.graph.edges])
        if self._config:
            bridge.setup_budget(self._config.budget)

        # Broadcast the init event so the UI switches out of launch-panel mode
        init_event = self.state.to_init_event()
        init_event["run_active"] = True
        await self.broadcast(json.dumps(init_event))

        # Wire bridge into bus events
        orchestrator.bus.on_event(bridge.on_message_routed)

        # Wire completion callbacks
        original_on_complete = orchestrator._on_agent_complete

        async def wrapped_on_complete(agent_id, result):
            await bridge.on_agent_complete(agent_id, result)
            await original_on_complete(agent_id, result)

        orchestrator._on_agent_complete = wrapped_on_complete

        # Store agent refs for message injection
        self.set_agents(orchestrator.agents)

        try:
            await orchestrator.run(query)
        except Exception:
            logger.exception("Orchestrator run failed")
        else:
            self.state.completed = True

        # Broadcast final stats
        elapsed = time.time() - self.state.start_time
        await self.broadcast(json.dumps({
            "type": "stats",
            "message_count": self.state.message_count,
            "budget_remaining": self.state.budget_remaining,
            "elapsed": round(elapsed, 2),
        }))

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info(f"Dashboard client connected ({len(self._clients)} total)")

        # Send current state on connect
        try:
            init_event = self.state.to_init_event()
            init_event["run_active"] = self._run_task is not None and not self._run_task.done()
            await ws.send_str(json.dumps(init_event))
        except Exception:
            pass

        try:
            async for msg in ws:
                pass  # We don't expect client messages, just keep connection alive
        finally:
            self._clients.discard(ws)
            logger.info(f"Dashboard client disconnected ({len(self._clients)} total)")

        return ws
