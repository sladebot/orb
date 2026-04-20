"""Regression tests for security/scoping fixes in the web layer.

Covers six bugs:
  1. Legacy /_git_status_handler leaked the default session's workdir.
  2. fs_read allowed symlink escape via .resolve(strict=False).
  3. fs_files accepted any absolute path (not scoped to home / session workdirs).
  4. WS handler silently fell back to sessions[-1] for unknown session filters.
  5. predict_topology created a session as a side-effect when registry was empty.
  6. _no_cache_middleware skipped /api/ responses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from web.server import DashboardServer
from web.state import DashboardState


@pytest.fixture
async def client(tmp_path: Path):
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18200)
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001
    aiohttp_server = TestServer(server._app)  # noqa: SLF001
    await aiohttp_server.start_server()
    async with TestClient(aiohttp_server) as test_client:
        yield test_client, server
    await aiohttp_server.close()


# ── 1. Legacy _git_status_handler must not leak default session workdir ──


class TestGitStatusHandlerNoDefaultLeak:
    async def test_git_status_without_path_does_not_read_runtime_workdir(self, client, tmp_path):
        test_client, server = client
        # Poison the default session's workdir with a distinctive path.
        sentinel = tmp_path / "sentinel-session-workdir"
        sentinel.mkdir()
        server.runtime._conversation_session.workdir = str(sentinel)  # noqa: SLF001

        # Call the legacy handler directly — there are no registered legacy
        # routes, so we invoke the method with a synthetic request.
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/git/status")
        resp = await server._git_status_handler(req)  # noqa: SLF001
        body = json.loads(resp.body.decode())
        # The response must NOT reference the default session's workdir.
        assert body.get("path", "") != str(sentinel)


# ── 2. fs_read must reject symlink escapes ────────────────────────────────


class TestFsReadSymlinkEscape:
    async def test_fs_read_rejects_symlink_that_escapes_workdir_v1(self, client, tmp_path):
        test_client, _ = client
        workdir = tmp_path / "work"
        workdir.mkdir()
        # Symlink inside workdir pointing outside.
        link = workdir / "escape.txt"
        target = Path("/etc/hosts")
        if not target.exists():
            pytest.skip("no /etc/hosts on this platform")
        os.symlink(str(target), str(link))

        resp = await test_client.get(
            "/api/v1/fs/read",
            params={"workdir": str(workdir), "path": "escape.txt"},
        )
        body = await resp.json()
        assert resp.status >= 400
        assert body["ok"] is False

    async def test_fs_read_rejects_symlink_legacy_handler(self, client, tmp_path):
        test_client, server = client
        workdir = tmp_path / "work2"
        workdir.mkdir()
        link = workdir / "escape.txt"
        target = Path("/etc/hosts")
        if not target.exists():
            pytest.skip("no /etc/hosts on this platform")
        os.symlink(str(target), str(link))

        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request(
            "GET",
            f"/fs/read?workdir={workdir}&path=escape.txt",
        )
        resp = await server._fs_read_handler(req)  # noqa: SLF001
        body = json.loads(resp.body.decode())
        assert body["ok"] is False


# ── 3. fs_files must refuse paths outside home / session workdirs ─────────


class TestFsFilesScopedAccess:
    async def test_fs_files_rejects_paths_outside_home_or_sessions(self, client):
        test_client, _ = client
        # /etc is outside $HOME and outside any registered session workdir.
        resp = await test_client.get("/api/v1/fs/files", params={"path": "/etc"})
        body = await resp.json()
        assert resp.status == 400
        assert body["ok"] is False

    async def test_fs_files_allows_session_workdir(self, client, tmp_path):
        test_client, server = client
        # Create a session whose workdir is tmp_path, then allow listing it
        # even though it's not under $HOME.
        workdir = tmp_path / "allowed"
        workdir.mkdir()
        (workdir / "README.md").write_text("hi")
        resp = await test_client.post("/api/v1/sessions", json={"workdir": str(workdir)})
        assert resp.status == 201
        resp2 = await test_client.get("/api/v1/fs/files", params={"path": str(workdir)})
        body2 = await resp2.json()
        assert resp2.status == 200
        assert body2["ok"] is True


# ── 4. WS handler must not silently swap unknown session_id ───────────────


class TestWsHandlerUnknownSession:
    async def test_ws_rejects_unknown_session_filter(self, client):
        test_client, _ = client
        async with test_client.ws_connect("/api/v1/ws?session_id=does-not-exist") as ws:
            msg = await ws.receive(timeout=5)
            # We expect either an error frame then close, or an immediate close.
            import aiohttp
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                assert payload.get("type") == "error"
                assert payload.get("code") == "SESSION_NOT_FOUND"
                # Next message should be close
                next_msg = await ws.receive(timeout=5)
                assert next_msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
            else:
                assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)


# ── 5. predict_topology must not create a persistent session ─────────────


class TestPredictTopologyNoSessionLeak:
    async def test_predict_topology_does_not_create_session(self, client, monkeypatch):
        test_client, server = client
        # Clear sessions so the old code path would call create_session().
        for sid in list(server.manager._sessions.keys()):  # noqa: SLF001
            server.manager._sessions.pop(sid)  # noqa: SLF001

        before_count = len(server.manager.list_sessions())

        # Stub GraphRuntime.predict_topology so we don't hit real providers.
        from orb.runtime.graph_runtime import GraphRuntime

        async def fake_predict(self, q, model_pin="auto"):
            return {"topology": "triad", "query": q}

        monkeypatch.setattr(GraphRuntime, "predict_topology", fake_predict)

        resp = await test_client.get("/api/v1/predict-topology", params={"q": "hello world"})
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

        after_count = len(server.manager.list_sessions())
        assert after_count == before_count, "predict-topology must not register a session"


# ── 6. _no_cache_middleware must cover /api/ paths ────────────────────────


class TestNoCacheOnApi:
    async def test_api_responses_have_no_cache_headers(self, client):
        test_client, _ = client
        resp = await test_client.get("/api/v1/health")
        assert resp.status == 200
        cc = resp.headers.get("Cache-Control", "")
        assert "no-store" in cc or "no-cache" in cc
