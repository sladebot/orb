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
        assert data["data"]["run_state"] == "idle"

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

        with patch.object(runtime, "start_run", side_effect=fake_start_run):
            resp = await test_client.post(
                f"/api/v1/sessions/{session_id}/runs",
                json={"query": "hello", "topology": "auto"},
            )
        assert resp.status == 202
        data = await resp.json()
        assert data["ok"] is True
        assert data["code"] == "RUN_STARTED"

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
