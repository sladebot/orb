from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from aiohttp import web
from json import JSONDecodeError

from orb.runtime import GraphRuntime
from .state import DashboardState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_BUILD_TS = str(int(time.time()))


class DashboardServer:
    """Backend runtime server with API, WebSocket fanout, and dashboard assets."""

    def __init__(
        self,
        state: DashboardState,
        host: str = "0.0.0.0",
        port: int = 8080,
        runtime: GraphRuntime | None = None,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.runtime = runtime or GraphRuntime(state)
        self._app = web.Application()
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None

        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        self._app.router.add_post("/api/inject", self._inject_handler)
        self._app.router.add_post("/api/start", self._start_handler)
        self._app.router.add_post("/api/stop", self._stop_run_handler)
        self._app.router.add_get("/api/run-status", self._run_status_handler)
        self._app.router.add_get("/api/predict-topology", self._predict_topology_handler)
        self._app.router.add_get("/api/models", self._models_handler)
        self._app.router.add_get("/api/topologies", self._topologies_handler)
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR)

    def set_agents(self, agents: dict) -> None:
        """Retained for compatibility; runtime owns live agents."""
        self.runtime._agents = agents

    def set_providers(self, providers: dict, config, model_overrides, tier_override) -> None:
        self.runtime.configure(providers, config, model_overrides, tier_override)

    async def start(self) -> None:
        self.runtime.subscribe(self.broadcast)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("Dashboard server running at http://localhost:%s", self.port)

        # Start topology file watcher for hot-reload
        from orb.topologies import get_watcher
        self._topology_watcher = get_watcher()
        self._topology_watcher.on_reload(self._on_topologies_reloaded)
        self._topology_watcher.start()

    async def _on_topologies_reloaded(self) -> None:
        await self.broadcast(json.dumps({"type": "topologies_reloaded"}))

    async def stop(self) -> None:
        if hasattr(self, "_topology_watcher"):
            self._topology_watcher.stop()
        self.runtime.unsubscribe(self.broadcast)
        await self.runtime.stop()
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, data: str) -> None:
        closed = []
        for ws in self._clients:
            try:
                await ws.send_str(data)
            except (ConnectionResetError, RuntimeError):
                closed.append(ws)
        for ws in closed:
            self._clients.discard(ws)

    async def _index_handler(self, request: web.Request) -> web.Response:
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace("/static/style.css", f"/static/style.css?v={_BUILD_TS}")
        html = html.replace("/static/graph.js", f"/static/graph.js?v={_BUILD_TS}")
        html = html.replace("/static/app.js", f"/static/app.js?v={_BUILD_TS}")
        return web.Response(text=html, content_type="text/html")

    async def _state_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.current_init_event())

    async def _inject_handler(self, request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 1_048_576:
            return web.json_response({"ok": False, "error": "Request too large"}, status=413)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        target_id = body.get("to", "").strip()
        text = body.get("message", "").strip()
        if not target_id:
            return web.json_response({"ok": False, "error": "Missing 'to' field"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "Missing 'message' field"}, status=400)

        status, payload = await self.runtime.inject_message(target_id, text)
        return web.json_response(payload, status=status)

    async def _start_handler(self, request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 1_048_576:
            return web.json_response({"ok": False, "error": "Request too large"}, status=413)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        query = (body.get("query") or "").strip()
        topology = (body.get("topology") or "auto").strip()
        model_pin = (body.get("model") or "auto").strip()
        if not query:
            return web.json_response({"ok": False, "error": "Query must not be empty"}, status=400)
        from orb.topologies import get_loader, normalize_topology_id
        topology = normalize_topology_id(topology)
        valid_topologies = ["auto"] + get_loader().list_ids()
        if topology not in valid_topologies:
            return web.json_response(
                {"ok": False, "error": f"topology must be one of: {', '.join(valid_topologies)}"},
                status=400,
            )

        status, payload = await self.runtime.start_run(
            query,
            topology,
            model_pin=model_pin,
        )
        return web.json_response(payload, status=status)

    async def _stop_run_handler(self, request: web.Request) -> web.Response:
        return web.json_response(await self.runtime.stop_run())

    async def _run_status_handler(self, request: web.Request) -> web.Response:
        return web.json_response({
            "running": self.runtime.running,
            "message_count": self.state.message_count,
        })

    async def _predict_topology_handler(self, request: web.Request) -> web.Response:
        q = request.rel_url.query.get("q", "").strip()
        model = request.rel_url.query.get("model", "auto").strip()
        return web.json_response(await self.runtime.predict_topology(q, model_pin=model))

    async def _models_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.models_payload())

    async def _topologies_handler(self, request: web.Request) -> web.Response:
        from orb.topologies import get_loader

        loader = get_loader()
        topologies = []
        for tid in loader.list_ids():
            topo = loader.get(tid)
            topologies.append({
                "id": tid,
                "label": topo.label,
                "description": topo.description,
                "agents": list(topo.agents.keys()),
            })
        return web.json_response({"topologies": topologies})

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            logger.info("Dashboard client connected (%s total)", len(self._clients))
            try:
                await ws.send_str(json.dumps(self.runtime.current_init_event()))
            except (ConnectionResetError, RuntimeError):
                pass
            async for _msg in ws:
                pass
        finally:
            self._clients.discard(ws)
            logger.info("Dashboard client disconnected (%s total)", len(self._clients))
        return ws
