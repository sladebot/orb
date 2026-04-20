"""Multi-tenant v1 HTTP + WebSocket API for Orb.

Everything under ``/api/v1/sessions/{session_id}/...`` routes to a
specific :class:`~orb.runtime.graph_runtime.GraphRuntime` registered in
the :class:`~orb.runtime.manager.RuntimeManager`. The old top-level
``/api/*`` routes continue to work against a default session for the
dashboard + TUI during the transition; v1 is the surface external
harnesses (hermes, openclaw, etc.) should target.

Every v1 response uses the standard envelope:

    {"ok": bool, "code": "UPPER_SNAKE", "error"?: str, "data"?: dict}

Error responses set ``ok: false`` and include ``code`` + ``error`` but
never ``data``; success responses set ``ok: true`` with ``code`` and
``data``. HTTP status codes mirror the envelope for 400/404/409 cases.
"""

from __future__ import annotations

import asyncio
import logging
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from orb.runtime.manager import RuntimeManager
    from orb.runtime.graph_runtime import GraphRuntime

logger = logging.getLogger(__name__)


# ── Envelope helpers ─────────────────────────────────────────────────────


def ok(code: str, data: dict | None = None, *, status: int = 200) -> web.Response:
    return web.json_response(
        {"ok": True, "code": code, "data": data or {}},
        status=status,
    )


def err(code: str, message: str, *, status: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "code": code, "error": message},
        status=status,
    )


# ── Session lookup ───────────────────────────────────────────────────────


def _get_session(manager: "RuntimeManager", session_id: str) -> "GraphRuntime | None":
    return manager.get_session(session_id)


def _session_summary(runtime: "GraphRuntime") -> dict:
    cs = runtime._conversation_session  # noqa: SLF001
    return {
        "session_id": cs.session_id,
        "generation": cs.generation,
        "workdir": cs.workdir,
        "run_state": runtime.run_state.value,
        "turn": cs.user_turn_count(),
        "locked_topology": cs.locked_topology,
    }


# ── Routes ───────────────────────────────────────────────────────────────


def register_v1_routes(app: web.Application, manager: "RuntimeManager", server: Any) -> None:
    """Attach every v1 route to the aiohttp app.

    ``server`` is the enclosing :class:`DashboardServer`, needed only for
    the WebSocket client registry — v1 reuses the same broadcast fanout
    path as the legacy routes during the transition.
    """

    # ---- Health ----

    async def health(request: web.Request) -> web.Response:
        return ok("HEALTHY", {
            "active_sessions": len(manager.list_sessions()),
            "active_runs": manager.active_session_count(),
        })

    # ---- Sessions ----

    async def create_session(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return err("INVALID_BODY", "Request body must be a JSON object", status=400)
        workdir = (body.get("workdir") or "").strip() or None
        if workdir:
            path = Path(workdir).expanduser()
            if not path.exists() or not path.is_dir():
                return err("INVALID_WORKDIR", f"Workdir does not exist or is not a directory: {path}", status=400)
            workdir = str(path.resolve())
        runtime = manager.create_session(workdir=workdir)
        return ok("SESSION_CREATED", _session_summary(runtime), status=201)

    async def list_sessions(request: web.Request) -> web.Response:
        sessions = [_session_summary(rt) for rt in manager.list_sessions()]
        return ok("SESSIONS_LISTED", {
            "sessions": sessions,
            "total": len(sessions),
        })

    async def get_session_info(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        return ok("SESSION_FETCHED", _session_summary(runtime))

    async def delete_session(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        deleted = manager.delete_session(session_id)
        if not deleted:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        return ok("SESSION_DELETED", {"session_id": session_id})

    # ---- Runs within a session ----

    async def start_run(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return err("INVALID_BODY", "Request body must be a JSON object", status=400)
        query = (body.get("query") or "").strip()
        if not query:
            return err("QUERY_EMPTY", "query must not be empty", status=400)
        topology = (body.get("topology") or "auto").strip()
        model_pin = (body.get("model") or body.get("model_pin") or "auto").strip()
        raw_agent_models = body.get("agent_models") or {}
        if not isinstance(raw_agent_models, dict):
            return err("INVALID_AGENT_MODELS", "agent_models must be an object", status=400)
        agent_models: dict[str, str] = {
            str(r).strip(): str(m).strip()
            for r, m in raw_agent_models.items()
            if str(r).strip() and str(m).strip()
        } or None
        workdir = (body.get("workdir") or "").strip() or None

        status_code, payload = await runtime.start_run(
            query,
            topology,
            model_pin=model_pin,
            agent_models=agent_models,
            workdir=workdir,
        )
        if not payload.get("ok"):
            return err("RUN_START_FAILED", payload.get("error") or "Failed to start run", status=status_code or 500)
        return ok("RUN_STARTED", {
            "session_id": session_id,
            "run_state": runtime.run_state.value,
            "init": payload.get("init"),
            "session_turn": payload.get("session_turn"),
        }, status=202)

    async def stop_run(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        payload = await runtime.stop_run()
        if not payload.get("ok"):
            return err("NO_RUN_IN_FLIGHT", payload.get("error") or "No run in progress", status=409)
        return ok("RUN_STOP_REQUESTED", {
            "session_id": session_id,
            "run_state": runtime.run_state.value,
        })

    async def inject_message(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return err("INVALID_BODY", "Request body must be a JSON object", status=400)
        target = (body.get("to") or "").strip()
        message = (body.get("message") or "").strip()
        if not message:
            return err("MESSAGE_EMPTY", "message must not be empty", status=400)
        if not target:
            return err("TARGET_MISSING", "target agent id must be provided in 'to'", status=400)
        status_code, payload = await runtime.inject_message(target, message)
        if not payload.get("ok"):
            code = "INJECT_FAILED"
            if "no run" in str(payload.get("error", "")).lower():
                code = "NO_RUN_IN_FLIGHT"
            return err(code, payload.get("error") or "Inject failed", status=status_code or 400)
        return ok("MESSAGE_INJECTED", {"session_id": session_id, "target": target})

    # ---- State ----

    async def session_state(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        init = runtime.current_init_event(session_id=session_id)
        return ok("STATE_FETCHED", init)

    # ---- WebSocket ----

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        """Single multiplexed socket.

        ``?session_id=X`` optionally filters to one session's events;
        without the param the client receives every session's broadcasts
        and must filter client-side.
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        filter_session = request.rel_url.query.get("session_id") or None
        server._clients[ws] = filter_session  # noqa: SLF001
        try:
            # Snapshot of the requested session (or the most recent one)
            target_session: "GraphRuntime | None" = None
            if filter_session:
                target_session = manager.get_session(filter_session)
            if target_session is None:
                sessions = manager.list_sessions()
                target_session = sessions[-1] if sessions else None
            if target_session is not None:
                init = target_session.current_init_event(
                    session_id=target_session._conversation_session.session_id  # noqa: SLF001
                )
                init["session_id"] = target_session._conversation_session.session_id  # noqa: SLF001
                import json as _json
                await ws.send_str(_json.dumps(init))
            async for _msg in ws:  # client sends are ignored in v1
                pass
        finally:
            server._clients.pop(ws, None)  # noqa: SLF001
        return ws

    # ---- Route registration ----

    app.router.add_get("/api/v1/health", health)
    app.router.add_post("/api/v1/sessions", create_session)
    app.router.add_get("/api/v1/sessions", list_sessions)
    app.router.add_get("/api/v1/sessions/{session_id}", get_session_info)
    app.router.add_delete("/api/v1/sessions/{session_id}", delete_session)
    app.router.add_post("/api/v1/sessions/{session_id}/runs", start_run)
    app.router.add_post("/api/v1/sessions/{session_id}/runs/stop", stop_run)
    app.router.add_post("/api/v1/sessions/{session_id}/runs/inject", inject_message)
    app.router.add_get("/api/v1/sessions/{session_id}/state", session_state)
    app.router.add_get("/api/v1/ws", ws_handler)
