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


@web.middleware
async def _no_cache_middleware(request: web.Request, handler):
    response = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
        self._app = web.Application(middlewares=[_no_cache_middleware])
        self._clients: dict[web.WebSocketResponse, str | None] = {}
        self._runner: web.AppRunner | None = None

        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        self._app.router.add_post("/api/inject", self._inject_handler)
        self._app.router.add_post("/api/start", self._start_handler)
        self._app.router.add_post("/api/session/new", self._new_session_handler)
        self._app.router.add_post("/api/stop", self._stop_run_handler)
        self._app.router.add_get("/api/run-status", self._run_status_handler)
        self._app.router.add_get("/api/predict-topology", self._predict_topology_handler)
        self._app.router.add_get("/api/models", self._models_handler)
        self._app.router.add_get("/api/settings", self._settings_get_handler)
        self._app.router.add_get("/api/topologies", self._topologies_handler)
        self._app.router.add_get("/api/admin/traces/sessions", self._trace_sessions_handler)
        self._app.router.add_get("/api/admin/traces/session/{session_id}", self._trace_session_runs_handler)
        self._app.router.add_get("/api/admin/traces/run/{run_id}", self._trace_run_handler)
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR)

    def set_agents(self, agents: dict) -> None:
        """Retained for compatibility; runtime owns live agents."""
        self.runtime._agents = agents

    def set_providers(self, providers: dict, config, model_overrides, tier_override, mode: str = "single") -> None:
        self.runtime.configure(providers, config, model_overrides, tier_override, mode=mode)

    async def start(self) -> None:
        self.runtime.subscribe(self.broadcast)
        await self.runtime.refresh_provider_catalogs()
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
        current_session_id = self.runtime.state.session_id or None
        for ws, session_id in self._clients.items():
            if session_id and session_id != current_session_id:
                continue
            try:
                await ws.send_str(data)
            except (ConnectionResetError, RuntimeError):
                closed.append(ws)
        for ws in closed:
            self._clients.pop(ws, None)

    async def _index_handler(self, request: web.Request) -> web.Response:
        build_ts = str(max(
            int((STATIC_DIR / "style.css").stat().st_mtime),
            int((STATIC_DIR / "graph.js").stat().st_mtime),
            int((STATIC_DIR / "app.js").stat().st_mtime),
            int((STATIC_DIR / "index.html").stat().st_mtime),
        ))
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace("/static/style.css", f"/static/style.css?v={build_ts}")
        html = html.replace("/static/graph.js", f"/static/graph.js?v={build_ts}")
        html = html.replace("/static/app.js", f"/static/app.js?v={build_ts}")
        response = web.Response(text=html, content_type="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    async def _state_handler(self, request: web.Request) -> web.Response:
        session_id = request.rel_url.query.get("session", "").strip() or None
        return web.json_response(self.runtime.current_init_event(session_id=session_id))

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

    async def _new_session_handler(self, request: web.Request) -> web.Response:
        status, payload = await self.runtime.new_session()
        return web.json_response(payload, status=status)

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

    async def _settings_get_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.settings_payload())

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

    async def _trace_sessions_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.list_trace_sessions())

    async def _trace_session_runs_handler(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"].strip()
        if not session_id:
            return web.json_response({"error": "missing session id"}, status=400)
        return web.json_response(self.runtime.list_session_traces(session_id))

    async def _trace_run_handler(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"].strip()
        if not run_id:
            return web.json_response({"error": "missing run id"}, status=400)
        payload = self.runtime.get_trace_payload(run_id)
        if payload is None:
            return web.json_response({"error": "trace not found"}, status=404)
        return web.json_response(payload)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        session_id = request.rel_url.query.get("session", "").strip() or None
        self._clients[ws] = session_id
        try:
            logger.info("Dashboard client connected (%s total)", len(self._clients))
            try:
                await ws.send_str(json.dumps(self.runtime.current_init_event(session_id=session_id)))
            except (ConnectionResetError, RuntimeError):
                pass
            async for _msg in ws:
                pass
        finally:
            self._clients.pop(ws, None)
            logger.info("Dashboard client disconnected (%s total)", len(self._clients))
        return ws
