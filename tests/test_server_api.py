"""Tests for DashboardServer HTTP endpoints."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from web.server import DashboardServer
from web.state import DashboardState
from orb.llm.types import CompletionResponse, ModelTier, ModelConfig
from tests.test_claude_agent import MockLLMClient
from orb.orchestrator.types import OrchestratorConfig


def _make_server() -> DashboardServer:
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18099)
    mock_client = MockLLMClient([
        CompletionResponse(
            content="",
            model="mock",
            tool_calls=[__import__("orb.llm.types", fromlist=["ToolCall"]).ToolCall(
                id="t1", name="complete_task", input={"result": "done"}
            )],
        )
    ])
    mock_cfg = ModelConfig(tier=ModelTier.CLOUD_LITE, model_id="mock", provider="mock")
    import orb.llm.types as lt
    lt.DEFAULT_MODELS[ModelTier.CLOUD_LITE] = mock_cfg
    lt.DEFAULT_MODELS[ModelTier.CLOUD_FAST] = mock_cfg
    lt.DEFAULT_MODELS[ModelTier.CLOUD_STRONG] = mock_cfg

    config = OrchestratorConfig(budget=50, timeout=10.0)
    server.set_providers(
        providers={"mock": mock_client},
        config=config,
        model_overrides=None,
        tier_override=None,
    )
    return server


@pytest.fixture
async def client(aiohttp_client):
    server = _make_server()
    return await aiohttp_client(server._app)


class TestServerAPI:

    async def test_api_state_returns_init_event(self, client):
        resp = await client.get("/api/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "init"
        assert "agents" in data
        assert "stats" in data

    async def test_api_run_status_not_running_initially(self, client):
        resp = await client.get("/api/run-status")
        assert resp.status == 200
        data = await resp.json()
        assert data["running"] is False

    async def test_api_models_returns_list(self, client):
        resp = await client.get("/api/models")
        assert resp.status == 200
        data = await resp.json()
        assert "models" in data
        assert any(m["id"] == "auto" for m in data["models"])

    async def test_api_start_requires_query(self, client):
        resp = await client.post("/api/start", json={"query": ""})
        assert resp.status == 400

    async def test_api_start_rejects_invalid_topology(self, client):
        resp = await client.post("/api/start", json={
            "query": "hello", "topology": "nonexistent"
        })
        assert resp.status == 400

    async def test_api_inject_no_run_in_progress(self, client):
        resp = await client.post("/api/inject", json={"to": "coder", "message": "hi"})
        assert resp.status == 400

    async def test_api_stop_no_run_returns_error(self, client):
        resp = await client.post("/api/stop")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is False

    async def test_api_start_accepts_valid_request(self, client):
        resp = await client.post("/api/start", json={
            "query": "write hello world",
            "topology": "triangle",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    async def test_api_start_rejects_concurrent_run(self, client):
        # Start first run
        await client.post("/api/start", json={
            "query": "task 1", "topology": "triangle",
        })
        # Immediately try second run — should be rejected
        resp = await client.post("/api/start", json={
            "query": "task 2", "topology": "triangle",
        })
        data = await resp.json()
        assert data["ok"] is False
