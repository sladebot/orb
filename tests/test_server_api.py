"""Tests for DashboardServer HTTP endpoints."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from web.server import DashboardServer
from web.state import DashboardState
from orb.llm.types import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_MODEL,
    ANTHROPIC_SONNET_MODEL,
)
from orb.runtime import GraphRuntime
from orb.runtime import graph_runtime as runtime_mod
from orb.llm.types import CompletionResponse, ModelTier, ModelConfig
from tests.test_claude_agent import MockLLMClient
from orb.orchestrator.types import OrchestratorConfig


def _all_anthropic_models_enabled():
    return {
        "anthropic": {
            "enabled": True,
            "models": {
                ANTHROPIC_HAIKU_MODEL: {"enabled": True},
                ANTHROPIC_SONNET_MODEL: {"enabled": True},
                ANTHROPIC_OPUS_MODEL: {"enabled": True},
            },
        },
    }


def _openai_and_ollama_enabled():
    return {
        "anthropic": {
            "enabled": False,
            "models": {
                ANTHROPIC_HAIKU_MODEL: {"enabled": False},
                ANTHROPIC_SONNET_MODEL: {"enabled": False},
                ANTHROPIC_OPUS_MODEL: {"enabled": False},
            },
        },
        "openai-codex": {
            "enabled": True,
            "models": {},
            "default_models": {
                "cloud_lite": "gpt-5.4",
                "cloud_fast": "gpt-5.4",
                "cloud_strong": "gpt-5.4",
            },
        },
        "ollama": {
            "enabled": True,
            "models": {
                "qwen3.5:9b": {"enabled": True},
                "qwen3.5:27b": {"enabled": True},
            },
            "default_models": {
                "local_small": "qwen3.5:9b",
                "local_medium": "qwen3.5:27b",
                "local_large": "qwen3.5:27b",
            },
        },
    }


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
async def client():
    server = _make_server()
    test_server = TestServer(server._app)
    test_client = TestClient(test_server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


class TestServerAPI:

    async def test_api_state_returns_init_event(self, client):
        resp = await client.get("/api/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "init"
        assert "agents" in data
        assert "stats" in data
        assert "session_id" in data
        assert "session_generation" in data

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
            "topology": "triad",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    async def test_api_start_rejects_concurrent_run(self, client):
        await client.post("/api/start", json={
            "query": "task 1", "topology": "triad",
        })
        resp = await client.post("/api/start", json={
            "query": "task 2", "topology": "triad",
        })
        data = await resp.json()
        assert data["ok"] is False



class TestModelAllocation:
    def test_models_payload_includes_openai_nano_and_mini(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: {
            "openai-codex": {
                "enabled": True,
                "catalog": [
                    {"id": "gpt-5.4-nano", "label": "GPT-5.4 Nano", "local": False},
                    {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini", "local": False},
                    {"id": "gpt-5.4", "label": "GPT-5.4", "local": False},
                ],
                "default_models": {
                    "cloud_lite": "gpt-5.4-nano",
                    "cloud_fast": "gpt-5.4-mini",
                    "cloud_strong": "gpt-5.4",
                },
            }
        } if key == "providers" else None)
        runtime = GraphRuntime()
        runtime._providers = {"openai-codex": object()}  # noqa: SLF001

        models = runtime.models_payload()["models"]
        entries = {(item["provider"], item["id"]) for item in models}

        assert ("openai-codex", "gpt-5.4-nano") in entries
        assert ("openai-codex", "gpt-5.4-mini") in entries
        assert ("openai-codex", "gpt-5.4") in entries

    def test_triad_balances_openai_and_ollama_by_complexity_when_both_are_enabled(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: _openai_and_ollama_enabled() if key == "providers" else None)
        runtime = GraphRuntime()
        runtime._providers = {"openai-codex": object(), "ollama": object()}  # noqa: SLF001

        model_map = runtime._build_agent_model_map(  # noqa: SLF001
            complexity=40,
            topology_id="triad",
            agent_complexity={
                "coordinator": 30,
                "coder": 40,
                "reviewer": 30,
                "tester": 35,
            },
        )

        assert model_map["coordinator"].provider == "ollama"
        assert model_map["coordinator"].model_id == "qwen3.5:9b"
        assert model_map["coder"].provider == "openai-codex"
        assert model_map["reviewer"].provider == "openai-codex"
        assert model_map["tester"].provider == "ollama"
        assert model_map["tester"].model_id == "qwen3.5:27b"

    def test_triad_prefers_stronger_models_for_coder_and_reviewer(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: _all_anthropic_models_enabled() if key == "providers" else None)
        runtime = GraphRuntime()
        runtime._providers = {"anthropic": object()}  # noqa: SLF001

        model_map = runtime._build_agent_model_map(  # noqa: SLF001
            complexity=40,
            topology_id="triad",
            agent_complexity={
                "coordinator": 30,
                "coder": 40,
                "reviewer": 30,
                "tester": 35,
            },
        )

        assert model_map["coordinator"].model_id == ANTHROPIC_HAIKU_MODEL
        assert model_map["coder"].model_id == ANTHROPIC_SONNET_MODEL
        assert model_map["reviewer"].model_id == ANTHROPIC_SONNET_MODEL
        assert model_map["tester"].model_id == ANTHROPIC_HAIKU_MODEL

    def test_hierarchy_prefers_stronger_models_for_research_and_code_roles(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: _all_anthropic_models_enabled() if key == "providers" else None)
        runtime = GraphRuntime()
        runtime._providers = {"anthropic": object()}  # noqa: SLF001

        model_map = runtime._build_agent_model_map(  # noqa: SLF001
            complexity=45,
            topology_id="hierarchy",
            agent_complexity={
                "coordinator": 25,
                "researcher": 45,
                "coder": 45,
                "reviewer": 40,
                "tester": 30,
            },
        )

        assert model_map["coordinator"].model_id == ANTHROPIC_HAIKU_MODEL
        assert model_map["researcher"].model_id == ANTHROPIC_SONNET_MODEL
        assert model_map["coder"].model_id == ANTHROPIC_SONNET_MODEL
        assert model_map["reviewer"].model_id == ANTHROPIC_SONNET_MODEL

    @pytest.mark.asyncio
    async def test_llm_model_allocator_can_override_heuristic_assignments(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: _all_anthropic_models_enabled() if key == "providers" else None)
        runtime = GraphRuntime()
        allocator = MockLLMClient([
            CompletionResponse(
                content=json.dumps({
                    "assignments": {
                        "coordinator": {
                            "provider": "anthropic",
                            "model": ANTHROPIC_HAIKU_MODEL,
                            "reason": "routing only",
                        },
                        "coder": {
                            "provider": "anthropic",
                            "model": ANTHROPIC_OPUS_MODEL,
                            "reason": "hard implementation",
                        },
                        "reviewer": {
                            "provider": "anthropic",
                            "model": ANTHROPIC_SONNET_MODEL,
                            "reason": "strong review",
                        },
                        "tester": {
                            "provider": "anthropic",
                            "model": ANTHROPIC_HAIKU_MODEL,
                            "reason": "light validation",
                        },
                    }
                }),
                model=ANTHROPIC_SONNET_MODEL,
            )
        ])
        runtime._providers = {"anthropic": allocator}  # noqa: SLF001
        heuristic = runtime._build_agent_model_map(  # noqa: SLF001
            complexity=40,
            topology_id="triad",
            agent_complexity={
                "coordinator": 30,
                "coder": 40,
                "reviewer": 30,
                "tester": 35,
            },
        )

        assigned, reasons = await runtime._llm_assign_agent_models(  # noqa: SLF001
            "build a production ios app",
            "triad",
            40,
            {"coordinator": 30, "coder": 40, "reviewer": 30, "tester": 35},
            heuristic,
        )

        assert assigned["coder"].model_id == ANTHROPIC_OPUS_MODEL
        assert reasons["coder"] == "hard implementation"

    @pytest.mark.asyncio
    async def test_predict_topology_uses_llm_allocator_for_agent_models(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: _all_anthropic_models_enabled() if key == "providers" else None)
        runtime = GraphRuntime()
        predictor = MockLLMClient([
            CompletionResponse(
                content=json.dumps({
                    "complexity": 40,
                    "reason": "Compact implementation task",
                    "topology": "triad",
                    "agent_complexity": {
                        "coordinator": 30,
                        "coder": 40,
                        "reviewer": 30,
                        "tester": 35,
                    },
                    "assignments": {
                        "coordinator": {"provider": "anthropic", "model": ANTHROPIC_HAIKU_MODEL, "reason": "routing"},
                        "coder": {"provider": "anthropic", "model": ANTHROPIC_OPUS_MODEL, "reason": "implementation"},
                        "reviewer": {"provider": "anthropic", "model": ANTHROPIC_SONNET_MODEL, "reason": "review"},
                        "tester": {"provider": "anthropic", "model": ANTHROPIC_HAIKU_MODEL, "reason": "validation"},
                    }
                }),
                model=ANTHROPIC_SONNET_MODEL,
            ),
        ])
        runtime._providers = {"anthropic": predictor}  # noqa: SLF001

        predicted = await runtime.predict_topology("build a mobile app")

        assert predicted["agent_models"]["coder"] == ANTHROPIC_OPUS_MODEL
