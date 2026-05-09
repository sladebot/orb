"""HTTP tests for the multi-tenant v1 API.

Covers the session lifecycle routes (create/list/get/delete) and the
run-control routes (start/stop/inject/state) end-to-end through the
aiohttp test client. Run execution is mocked at the runtime level so
these tests stay fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from web.api_v1 import MAX_JSON_BODY_BYTES
from web.server import DashboardServer
from web.state import DashboardState


@pytest.fixture
async def client(tmp_path: Path):
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18100)
    # Point the default session's session file at tmp so we don't pollute the repo
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001
    aiohttp_server = TestServer(server._app)  # noqa: SLF001
    await aiohttp_server.start_server()
    async with TestClient(aiohttp_server) as test_client:
        yield test_client, server
    await aiohttp_server.close()


class TestHealth:
    async def test_health_reports_counts(self, client):
        test_client, server = client
        resp = await test_client.get("/api/v1/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["code"] == "HEALTHY"
        assert "active_sessions" in data["data"]


class TestSessionLifecycle:
    async def test_list_sessions_includes_default(self, client):
        test_client, server = client
        resp = await test_client.get("/api/v1/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["data"]["total"] >= 1

    async def test_create_session_returns_summary(self, client, tmp_path: Path):
        test_client, server = client
        workdir = str(tmp_path)
        resp = await test_client.post("/api/v1/sessions", json={"workdir": workdir})
        assert resp.status == 201
        data = await resp.json()
        assert data["ok"] is True
        assert data["code"] == "SESSION_CREATED"
        assert data["data"]["workdir"] == workdir

    async def test_oversized_json_returns_v1_error_envelope(self, client):
        test_client, _ = client
        body = json.dumps({"query": "x" * MAX_JSON_BODY_BYTES})
        resp = await test_client.post(
            "/api/v1/sessions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 413
        data = await resp.json()
        assert data["ok"] is False
        assert data["code"] == "REQUEST_TOO_LARGE"

    async def test_create_session_rejects_invalid_workdir(self, client):
        test_client, _ = client
        resp = await test_client.post("/api/v1/sessions", json={"workdir": "/nope/does/not/exist/xyz"})
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False
        assert data["code"] == "INVALID_WORKDIR"

    async def test_get_session_info_by_id(self, client):
        test_client, _ = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]

        resp = await test_client.get(f"/api/v1/sessions/{session_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data["data"]["session_id"] == session_id

    async def test_get_session_unknown_returns_404(self, client):
        test_client, _ = client
        resp = await test_client.get("/api/v1/sessions/nope")
        assert resp.status == 404
        data = await resp.json()
        assert data["code"] == "SESSION_NOT_FOUND"

    async def test_delete_session_removes_from_registry(self, client):
        test_client, server = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]

        resp = await test_client.delete(f"/api/v1/sessions/{session_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data["code"] == "SESSION_DELETED"
        assert server.manager.get_session(session_id) is None

    async def test_delete_unknown_session_returns_404(self, client):
        test_client, _ = client
        resp = await test_client.delete("/api/v1/sessions/nope")
        assert resp.status == 404

    async def test_create_session_defaults_streaming_enabled_true(self, client):
        """Streaming is on by default — callers who don't opt out get deltas.

        Contract with stream-tui/#13 and stream-dashboard/#14 (shared on
        ``tui-improvements``): init payload MUST carry
        ``streaming_enabled: true`` so clients can decide whether to paint
        active-turn bubbles with streaming state or fall back to the
        one-shot ``message`` event path.
        """
        test_client, server = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        runtime = server.manager.get_session(session_id)
        assert runtime._conversation_session.streaming_enabled is True  # noqa: SLF001
        init = runtime.current_init_event(session_id=session_id)
        assert init["streaming_enabled"] is True

    async def test_create_session_honors_explicit_streaming_false(self, client):
        """Only a literal ``false`` disables streaming.

        Strict check mirrors ``approval_required`` (inverted): the field is
        default-on, so we disable ONLY when the request explicitly sends
        ``streaming_enabled: false``. Strings like ``"false"`` or missing
        fields must not flip the flag off.
        """
        test_client, server = client
        resp = await test_client.post(
            "/api/v1/sessions", json={"streaming_enabled": False},
        )
        assert resp.status == 201
        session_id = (await resp.json())["data"]["session_id"]
        runtime = server.manager.get_session(session_id)
        assert runtime._conversation_session.streaming_enabled is False  # noqa: SLF001
        init = runtime.current_init_event(session_id=session_id)
        assert init["streaming_enabled"] is False

    async def test_create_session_ignores_nonbool_streaming_values(self, client):
        """A stringy ``"false"`` must NOT disable streaming — strict True check."""
        test_client, server = client
        resp = await test_client.post(
            "/api/v1/sessions", json={"streaming_enabled": "false"},
        )
        session_id = (await resp.json())["data"]["session_id"]
        runtime = server.manager.get_session(session_id)
        # "false" (string) isn't a real bool-false, so we keep the default (True).
        assert runtime._conversation_session.streaming_enabled is True  # noqa: SLF001


class TestRunControl:
    async def test_start_run_against_missing_session_404s(self, client):
        test_client, _ = client
        resp = await test_client.post("/api/v1/sessions/nope/runs", json={"query": "hi"})
        assert resp.status == 404

    async def test_start_run_empty_query_is_400(self, client):
        test_client, _ = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        resp = await test_client.post(f"/api/v1/sessions/{session_id}/runs", json={})
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "QUERY_EMPTY"

    async def test_start_run_delegates_to_runtime(self, client):
        test_client, server = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        runtime = server.manager.get_session(session_id)

        async def fake_start_run(*args, **kwargs):
            return 202, {"ok": True, "init": {"type": "init"}, "session_turn": 1}

        with patch.object(runtime, "start_run", side_effect=fake_start_run) as start_run:
            resp = await test_client.post(
                f"/api/v1/sessions/{session_id}/runs",
                json={"query": "hello", "topology": "auto"},
            )
        assert resp.status == 202
        data = await resp.json()
        assert data["ok"] is True
        assert data["code"] == "RUN_STARTED"
        assert "eval_mode" not in start_run.call_args.kwargs

    async def test_start_run_passes_eval_mode_to_runtime(self, client):
        test_client, server = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        runtime = server.manager.get_session(session_id)

        async def fake_start_run(*args, **kwargs):
            return 202, {"ok": True, "init": {"type": "init"}, "session_turn": 1}

        with patch.object(runtime, "start_run", side_effect=fake_start_run) as start_run:
            resp = await test_client.post(
                f"/api/v1/sessions/{session_id}/runs",
                json={"query": "hello", "topology": "auto", "eval_mode": True},
            )
        assert resp.status == 202
        assert start_run.call_args.kwargs["eval_mode"] is True

    async def test_start_run_while_one_is_in_flight_returns_409(self, client):
        """Starting a second run while one is in flight must map to HTTP 409
        with envelope code RUN_IN_PROGRESS — not the previous 200 {ok:false}.

        Mirrors the stop_run → NO_RUN_IN_FLIGHT treatment at api_v1.py.
        """
        test_client, server = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        runtime = server.manager.get_session(session_id)

        async def fake_start_run(*args, **kwargs):
            # Mirror the runtime's internal contract when a run is in flight:
            # it returns (200, {"ok": False, "error": "Run already in progress"}).
            return 200, {"ok": False, "error": "Run already in progress"}

        with patch.object(runtime, "start_run", side_effect=fake_start_run):
            resp = await test_client.post(
                f"/api/v1/sessions/{session_id}/runs",
                json={"query": "hello", "topology": "auto"},
            )
        assert resp.status == 409
        data = await resp.json()
        assert data["ok"] is False
        assert data["code"] == "RUN_IN_PROGRESS"

    async def test_stop_run_when_no_run_in_flight_is_409(self, client):
        test_client, _ = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        resp = await test_client.post(f"/api/v1/sessions/{session_id}/runs/stop")
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "NO_RUN_IN_FLIGHT"

    async def test_inject_requires_message(self, client):
        test_client, _ = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]
        resp = await test_client.post(
            f"/api/v1/sessions/{session_id}/runs/inject",
            json={"to": "coordinator"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "MESSAGE_EMPTY"


class TestSessionState:
    async def test_state_endpoint_returns_init_payload(self, client):
        test_client, server = client
        create = await (await test_client.post("/api/v1/sessions", json={})).json()
        session_id = create["data"]["session_id"]

        resp = await test_client.get(f"/api/v1/sessions/{session_id}/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["data"]["type"] == "init"
        # The init payload carries an explicit run_state — value depends
        # on what's in the dashboard snapshot for this session (a fresh
        # session reports `idle`; a hydrated one may surface whatever
        # state was last persisted). Assert only that it's a valid enum.
        assert data["data"]["run_state"] in {
            "idle", "planning", "running", "stopping", "completed", "errored",
        }
