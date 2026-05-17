"""Tests for DashboardServer HTTP endpoints."""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from web.server import DashboardServer
from web.state import DashboardState
from web.api_v1 import _DEFAULT_VAULT_ROOT
from orb.llm.types import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_MODEL,
    ANTHROPIC_SONNET_MODEL,
)
from orb.runtime import GraphRuntime
from orb.runtime import graph_runtime as runtime_mod
from orb.runtime.topology_classifier import TopologyClassification
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
            "default_models": {
                "cloud_lite": ANTHROPIC_HAIKU_MODEL,
                "cloud_fast": ANTHROPIC_SONNET_MODEL,
                "cloud_strong": ANTHROPIC_OPUS_MODEL,
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
    server._app["_test_server_ref"] = server
    mock_client = MockLLMClient([
        CompletionResponse(
            content=json.dumps({
                "task_type": "coding",
                "summary": "Compact implementation task",
                "complexity": 30,
                "reason": "Small coding task",
                "topology": "triad",
                "candidates": [{"topology": "triad", "score": 0.9, "reason": "best fit"}],
                "escalation_allowed": False,
                "stop_early_allowed": True,
                "escalation_reason": "",
                "stop_early_reason": "A compact topology should be enough.",
            }),
            model="mock",
        ),
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

    async def _default_session_id(self, client) -> str:
        """Pull the session_id of the default session the server spins up."""
        resp = await client.get("/api/v1/sessions")
        data = (await resp.json())["data"]
        return data["sessions"][0]["session_id"]

    async def test_api_state_returns_init_event(self, client):
        sid = await self._default_session_id(client)
        resp = await client.get(f"/api/v1/sessions/{sid}/state")
        assert resp.status == 200
        env = await resp.json()
        data = env["data"]
        assert data["type"] == "init"
        assert "agents" in data
        assert "stats" in data
        assert "session_id" in data
        assert "session_generation" in data

    async def test_api_run_status_not_running_initially(self, client):
        sid = await self._default_session_id(client)
        resp = await client.get(f"/api/v1/sessions/{sid}")
        assert resp.status == 200
        env = await resp.json()
        assert env["data"]["run_state"] == "idle"

    async def test_api_models_returns_list(self, client):
        resp = await client.get("/api/v1/models")
        assert resp.status == 200
        env = await resp.json()
        models = env["data"]["models"]
        assert any(m["id"] == "auto" for m in models)

    async def test_api_start_requires_query(self, client):
        sid = await self._default_session_id(client)
        resp = await client.post(f"/api/v1/sessions/{sid}/runs", json={"query": ""})
        assert resp.status == 400
        env = await resp.json()
        assert env["code"] == "QUERY_EMPTY"

    async def test_api_start_rejects_invalid_topology(self, client):
        sid = await self._default_session_id(client)
        resp = await client.post(
            f"/api/v1/sessions/{sid}/runs",
            json={"query": "hello", "topology": "nonexistent"},
        )
        assert resp.status in {400, 500}
        env = await resp.json()
        assert env["ok"] is False

    async def test_api_inject_no_run_in_progress(self, client):
        sid = await self._default_session_id(client)
        resp = await client.post(
            f"/api/v1/sessions/{sid}/runs/inject",
            json={"to": "coder", "message": "hi"},
        )
        assert resp.status in {400, 409}
        env = await resp.json()
        assert env["ok"] is False

    async def test_api_stop_no_run_returns_error(self, client):
        sid = await self._default_session_id(client)
        resp = await client.post(f"/api/v1/sessions/{sid}/runs/stop")
        assert resp.status == 409
        env = await resp.json()
        assert env["ok"] is False
        assert env["code"] == "NO_RUN_IN_FLIGHT"

    async def test_api_start_accepts_valid_request(self, client):
        sid = await self._default_session_id(client)
        resp = await client.post(
            f"/api/v1/sessions/{sid}/runs",
            json={"query": "write hello world", "topology": "triad"},
        )
        assert resp.status == 202
        env = await resp.json()
        assert env["ok"] is True
        init = env["data"]["init"]
        assert init["type"] == "init"
        # Init snapshot surfaces the FSM state directly — in-flight means
        # planning | running | stopping (the three IN_FLIGHT_STATES).
        assert init["run_state"] in {"planning", "running", "stopping"}
        assert init["plan"]["topology"]["id"] == "triad"
        assert init["plan"]["query"] == "write hello world"

    async def test_api_start_rejects_concurrent_run(self, client):
        sid = await self._default_session_id(client)
        await client.post(
            f"/api/v1/sessions/{sid}/runs",
            json={"query": "task 1", "topology": "triad"},
        )
        resp = await client.post(
            f"/api/v1/sessions/{sid}/runs",
            json={"query": "task 2", "topology": "triad"},
        )
        env = await resp.json()
        assert env["ok"] is False

    async def test_index_handler_returns_cache_busted_html(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert "/static/app.js?v=" in body
        assert "/static/style.css?v=" in body
        assert "/static/graph.js?v=" in body


    async def test_memory_dashboard_route_returns_cache_busted_html(self, client):
        resp = await client.get("/memory")
        assert resp.status == 200
        body = await resp.text()
        assert "Memory Overview" in body
        assert "/static/style.css?v=" in body
        assert "/static/memory.js?v=" in body

    async def test_memory_overview_api_reports_vault_counts(self, client, tmp_path):
        # The vault must be under _DEFAULT_VAULT_ROOT to pass the security
        # check added for GH issue #30.  Create it there.
        vault = _DEFAULT_VAULT_ROOT / "test_security_counts"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "wiki" / "entity").mkdir(parents=True, exist_ok=True)
        (vault / "wiki" / "concept").mkdir(parents=True, exist_ok=True)
        (vault / "memories").mkdir(parents=True, exist_ok=True)
        (vault / "raw" / "articles").mkdir(parents=True, exist_ok=True)
        (vault / "wiki" / "entity" / "orb.md").write_text("---\ntitle: Orb\ntype: entity\ntags: [project, agent]\n---\n# Orb\n[[Memory]]\n", encoding="utf-8")
        (vault / "wiki" / "concept" / "memory.md").write_text("---\ntitle: Memory\ntype: concept\ntags: [agent]\n---\n# Memory\n", encoding="utf-8")
        (vault / "memories" / "old.md").write_text("archived", encoding="utf-8")
        (vault / "raw" / "articles" / "source.md").write_text("raw", encoding="utf-8")
        resp = await client.get(f"/api/v1/memory/overview?vault_path={vault}")
        assert resp.status == 200
        env = await resp.json()
        assert env["ok"] is True
        data = env["data"]
        assert data["vault_path"] == str(vault.resolve())
        assert data["wiki_pages"] == 2
        assert data["memories"] == 1
        assert data["raw_items"] == 1
        assert data["page_types"] == {"concept": 1, "entity": 1}
        assert data["top_tags"][0] == {"tag": "agent", "count": 2}

    async def test_memory_overview_api_handles_missing_vault(self, client):
        # Must be under _DEFAULT_VAULT_ROOT to pass the security check.
        missing = _DEFAULT_VAULT_ROOT / "nonexistent_subdir"
        resp = await client.get(f"/api/v1/memory/overview?vault_path={missing}")
        assert resp.status == 200
        env = await resp.json()
        assert env["data"]["exists"] is False
        assert env["data"]["wiki_pages"] == 0

    async def test_memory_overview_api_rejects_path_traversal(self, client):
        """Path traversal via vault_path must be rejected (GH issue #30)."""
        # /etc is outside the allowed root (~/.orb/vault)
        resp = await client.get("/api/v1/memory/overview?vault_path=/etc")
        assert resp.status == 403
        env = await resp.json()
        assert env["ok"] is False
        assert env["code"] == "PATH_TRAVERSAL"

    async def test_memory_overview_api_rejects_parent_directory_traversal(self, client):
        """Traversing above the default vault root must be rejected."""
        resp = await client.get("/api/v1/memory/overview?vault_path=/var/tmp/../../etc")
        assert resp.status == 403
        env = await resp.json()
        assert env["ok"] is False
        assert env["code"] == "PATH_TRAVERSAL"

    async def test_memory_overview_api_allows_valid_subdirectories(self, client):
        """Valid subdirectories of the default vault root should work normally."""
        sub = _DEFAULT_VAULT_ROOT / "test_security_subdir"
        sub.mkdir(parents=True, exist_ok=True)
        try:
            resp = await client.get(f"/api/v1/memory/overview?vault_path={sub}")
            # The endpoint should return 200 with ok=True (subdir is a valid vault path)
            assert resp.status == 200
            env = await resp.json()
            assert env["ok"] is True
            assert env["data"]["vault_path"] == str(sub.resolve())
        finally:
            sub.rmdir()

    async def test_index_handler_does_not_block_event_loop(self, client):
        """Index handler must offload file I/O; a slow stat() must not starve other requests."""
        import threading
        import time as _time
        from pathlib import Path as _Path
        from unittest.mock import patch

        real_stat = _Path.stat
        lock = threading.Lock()
        active_stats = 0
        max_active_stats = 0

        def slow_stat(self, *args, **kwargs):
            nonlocal active_stats, max_active_stats
            # Only slow down the files touched by the index handler.
            if self.name in ("style.css", "graph.js", "app.js", "index.html"):
                with lock:
                    active_stats += 1
                    max_active_stats = max(max_active_stats, active_stats)
                try:
                    _time.sleep(0.05)
                finally:
                    with lock:
                        active_stats -= 1
            return real_stat(self, *args, **kwargs)

        with patch.object(_Path, "stat", slow_stat):
            start = _time.monotonic()
            results = await asyncio.gather(client.get("/"), client.get("/"))
            elapsed = _time.monotonic() - start

        assert all(r.status == 200 for r in results)
        assert max_active_stats >= 2, (
            f"index handler did not overlap file stat calls; elapsed={elapsed:.3f}s"
        )

    async def test_git_status_does_not_block_event_loop(self, client, tmp_path):
        """/api/v1/git/status shells out to git N times; must not stall the loop."""
        import time as _time
        import subprocess as _sub
        import threading
        from unittest.mock import patch

        workdir = tmp_path / "repo"
        workdir.mkdir()
        # Initialize a real git repo so _git_status makes all 5 git calls
        # (otherwise it short-circuits after rev-parse --is-inside-work-tree).
        _sub.run(["git", "init", "-q"], cwd=str(workdir), check=True, timeout=10)
        _sub.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "--allow-empty", "-m", "x", "-q"],
                 cwd=str(workdir), check=True, timeout=10)
        lock = threading.Lock()
        active_git_calls = 0
        max_active_git_calls = 0

        def slow_run(args, *a, **kw):
            nonlocal active_git_calls, max_active_git_calls
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                with lock:
                    active_git_calls += 1
                    max_active_git_calls = max(max_active_git_calls, active_git_calls)
                try:
                    _time.sleep(0.05)
                    cmd = list(args[1:])
                    if cmd == ["rev-parse", "--is-inside-work-tree"]:
                        return _sub.CompletedProcess(args, 0, stdout="true\n", stderr="")
                    if cmd == ["rev-parse", "--abbrev-ref", "HEAD"]:
                        return _sub.CompletedProcess(args, 0, stdout="feature/git-status\n", stderr="")
                    if cmd == ["remote", "get-url", "origin"]:
                        return _sub.CompletedProcess(args, 0, stdout="git@github.com:acme/demo.git\n", stderr="")
                    if cmd == ["status", "--porcelain"]:
                        return _sub.CompletedProcess(args, 0, stdout=" M web/server.py\n", stderr="")
                    if cmd == ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]:
                        return _sub.CompletedProcess(args, 0, stdout="1 2\n", stderr="")
                    return _sub.CompletedProcess(args, 1, stdout="", stderr="unexpected git command")
                finally:
                    with lock:
                        active_git_calls -= 1
            return _sub.CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(_sub, "run", slow_run):
            start = _time.monotonic()
            results = await asyncio.gather(
                client.get("/api/v1/git/status", params={"path": str(workdir)}),
                client.get("/api/v1/git/status", params={"path": str(workdir)}),
            )
            elapsed = _time.monotonic() - start

        # The git calls are fully faked so the assertion is not coupled to
        # local git process startup cost on slower CI/macOS runners. If the
        # handler runs git inline on the event loop, no two git calls can be
        # active at once; offloading with asyncio.to_thread lets parallel
        # requests overlap in the thread pool.
        assert all(r.status == 200 for r in results)
        assert max_active_git_calls >= 2, (
            f"git_status did not overlap git calls; elapsed={elapsed:.3f}s"
        )

    async def test_trace_admin_endpoints_return_session_and_run_data(self, client):
        sid = await self._default_session_id(client)

        runtime = client.server.app["_test_server_ref"].runtime  # type: ignore[index]
        trace = runtime._last_trace = __import__("orb.tracing", fromlist=["RunTrace"]).RunTrace(session_id=sid)  # noqa: SLF001
        trace.record_topology_choice("triad", reason="test")
        trace.record_final_outcome(success=True, result="done")
        runtime._persist_run_trace()  # noqa: SLF001

        sessions_resp = await client.get("/api/v1/traces/sessions")
        assert sessions_resp.status == 200
        sessions_env = await sessions_resp.json()
        assert any(item["session_id"] == sid for item in sessions_env["data"]["sessions"])

        runs_resp = await client.get(f"/api/v1/traces/sessions/{sid}")
        assert runs_resp.status == 200
        runs_env = await runs_resp.json()
        assert runs_env["data"]["runs"][0]["session_id"] == sid

        run_id = runs_env["data"]["runs"][0]["run_id"]
        trace_resp = await client.get(f"/api/v1/traces/runs/{run_id}")
        assert trace_resp.status == 200
        trace_env = await trace_resp.json()
        assert trace_env["data"]["summary"]["run_id"] == run_id



class TestModelAllocation:
    def test_models_payload_includes_openai_nano_and_mini(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: {
            "openai-codex": {
                "enabled": True,
                "catalog": [
                    {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini", "local": False},
                    {"id": "gpt-5.4", "label": "GPT-5.4", "local": False},
                ],
                "default_models": {
                    "cloud_lite": "gpt-5.4-mini",
                    "cloud_fast": "gpt-5.4-mini",
                    "cloud_strong": "gpt-5.4",
                },
            }
        } if key == "providers" else None)
        runtime = GraphRuntime()
        runtime._providers = {"openai-codex": object()}  # noqa: SLF001

        models = runtime.models_payload()["models"]
        entries = {(item["provider"], item["id"]) for item in models}

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
                    "task_type": "coding",
                    "summary": "Compact implementation task",
                    "complexity": 40,
                    "reason": "Compact implementation task",
                    "topology": "triad",
                    "candidates": [{"topology": "triad", "score": 0.95, "reason": "best fit"}],
                    "escalation_allowed": False,
                    "stop_early_allowed": True,
                    "escalation_reason": "",
                    "stop_early_reason": "Triad should be sufficient for this task.",
                }),
                model=ANTHROPIC_HAIKU_MODEL,
            ),
            CompletionResponse(
                content=json.dumps({
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
        assert predicted["task_type"] == "coding"
        assert predicted["classifier_model"] == ANTHROPIC_HAIKU_MODEL
        assert predicted["classifier_provider"] == "anthropic"
        assert predicted["signals"]["word_count"] >= 4
        assert predicted["signals"]["requested_topology"] == "auto"
        assert predicted["stop_early_allowed"] is True
        assert predicted["stop_early_reason"] == "Triad should be sufficient for this task."

    @pytest.mark.asyncio
    async def test_predict_topology_uses_openai_provider(self, monkeypatch):
        """openai provider (API key) should be used for topology prediction, not fall back to default."""
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: None)
        runtime = GraphRuntime()
        predictor = MockLLMClient([
            CompletionResponse(
                content=json.dumps({
                    "task_type": "coding",
                    "summary": "Compact implementation task",
                    "complexity": 40,
                    "reason": "Compact implementation task",
                    "topology": "triad",
                    "candidates": [],
                    "escalation_allowed": False,
                    "stop_early_allowed": True,
                    "escalation_reason": "",
                    "stop_early_reason": "No escalation is needed.",
                }),
                model="gpt-4o",
            ),
            CompletionResponse(
                content=json.dumps({"assignments": {}}),
                model="gpt-4o",
            ),
        ])
        runtime._providers = {"openai": predictor}  # noqa: SLF001

        predicted = await runtime.predict_topology("build a mobile app")

        assert predicted.get("topology") == "triad"
        assert predicted.get("classifier_model") == "gpt-4o"
        assert predicted.get("classifier_provider") == "openai"
        assert predicted["candidates"][0]["topology"] == "triad"
        assert predicted["stop_early_reason"] == "No escalation is needed."

    @pytest.mark.asyncio
    async def test_predict_topology_errors_when_no_provider_is_available(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: None)
        runtime = GraphRuntime()
        runtime._providers = {}  # noqa: SLF001

        with pytest.raises(RuntimeError, match="No providers available for task classification"):
            await runtime.predict_topology("build a mobile app")

    @pytest.mark.asyncio
    async def test_predict_topology_accepts_custom_classifier(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: None)
        runtime = GraphRuntime()
        runtime._providers = {}  # noqa: SLF001

        class FakeClassifier:
            async def classify(self, *, query, requested_topology, model_pin, topologies):
                topo = topologies["triad"]
                return TopologyClassification(
                    topology_id="triad",
                    label=topo.label,
                    description=topo.description,
                    task_type="coding",
                    summary="Custom classifier result",
                    complexity=33,
                    reason=f"custom:{query}",
                    escalation_allowed=True,
                    stop_early_allowed=False,
                    escalation_reason="Escalate if the task grows beyond the initial decomposition.",
                    stop_early_reason="",
                    requested_topology=requested_topology,
                    classifier_model="orb-lite-routing",
                    classifier_provider="orb",
                )

        runtime.set_topology_classifier(FakeClassifier())

        predicted = await runtime.predict_topology("build a mobile app")

        assert predicted["topology"] == "triad"
        assert predicted["reason"] == "custom:build a mobile app"
        assert predicted["classifier_model"] == "orb-lite-routing"
        assert predicted["classifier_provider"] == "orb"
        assert predicted["escalation_reason"] == "Escalate if the task grows beyond the initial decomposition."

    @pytest.mark.asyncio
    async def test_predict_topology_prompt_includes_selection_hints_and_routing_signals(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "get_config", lambda key: _all_anthropic_models_enabled() if key == "providers" else None)
        runtime = GraphRuntime()
        predictor = MockLLMClient([
            CompletionResponse(
                content=json.dumps({
                    "task_type": "broad_research",
                    "summary": "Need repo investigation before coding",
                    "complexity": 62,
                    "reason": "The task is broad enough for hierarchy",
                    "topology": "hierarchy",
                    "candidates": [{"topology": "hierarchy", "score": 0.92, "reason": "best fit"}],
                    "escalation_allowed": True,
                    "stop_early_allowed": False,
                    "escalation_reason": "Escalate to hierarchy when broad repo analysis is required.",
                    "stop_early_reason": "",
                }),
                model=ANTHROPIC_HAIKU_MODEL,
            ),
            CompletionResponse(
                content=json.dumps({"assignments": {}}),
                model=ANTHROPIC_SONNET_MODEL,
            ),
        ])
        runtime._providers = {"anthropic": predictor}  # noqa: SLF001

        predicted = await runtime.predict_topology("investigate the repository architecture and plan a migration")

        classifier_prompt = predictor.requests[0].messages[0]["content"]
        assert '"selection_hints"' in classifier_prompt
        assert '"ideal_for"' in classifier_prompt
        assert '"keywords"' in classifier_prompt
        assert 'Routing signals:' in classifier_prompt
        assert '"mentions_research": true' in classifier_prompt
        assert predicted["signals"]["mentions_research"] is True
        assert predicted["signals"]["mentions_breadth"] is True
        assert predicted["topology"] == "hierarchy"
        assert predicted["escalation_reason"] == "Escalate to hierarchy when broad repo analysis is required."
