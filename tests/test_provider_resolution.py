import types

import pytest

from orb.llm import registry
from orb.llm.anthropic import AnthropicProvider
from orb.runtime.graph_runtime import GraphRuntime


def test_registry_uses_anthropic_setup_token_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_SETUP_TOKEN", "sk-ant-oat01-from-env")

    assert registry._anthropic_api_key() == "sk-ant-oat01-from-env"


def test_registry_uses_ollama_keep_alive_from_config(monkeypatch):
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.setattr(
        "orb.cli.config.get",
        lambda key: {"ollama": {"keep_alive": "-1"}} if key == "providers" else None,
    )

    assert registry._ollama_keep_alive() == "-1"


def test_build_providers_local_only_keeps_ollama_without_startup_liveness(monkeypatch):
    monkeypatch.setattr("orb.cli.config.local_models_enabled", lambda: True)
    monkeypatch.setattr(
        "orb.cli.config.get",
        lambda key: {"ollama": {"enabled": True}, "vmlx": {"enabled": False}} if key == "providers" else None,
    )
    monkeypatch.setattr(registry, "_ollama_base_url", lambda: "http://localhost:11434")

    class FakeOllamaProvider:
        def __init__(self, base_url: str, keep_alive=None):
            self.base_url = base_url
            self.keep_alive = keep_alive

    monkeypatch.setattr("orb.llm.registry.OllamaProvider", FakeOllamaProvider, raising=False)
    monkeypatch.setattr("orb.llm.ollama.OllamaProvider", FakeOllamaProvider)

    providers = registry.build_providers(local_only=True, cloud_only=False)

    assert "ollama" in providers


def test_build_providers_local_only_keeps_vmlx_when_enabled(monkeypatch):
    monkeypatch.setattr("orb.cli.config.local_models_enabled", lambda: True)
    monkeypatch.setattr(
        "orb.cli.config.get",
        lambda key: {"ollama": {"enabled": False}, "vmlx": {"enabled": True, "base_url": "http://localhost:1234/v1"}} if key == "providers" else None,
    )
    monkeypatch.setattr(registry, "_vmlx_base_url", lambda: "http://localhost:1234/v1")

    class FakeVmlxProvider:
        def __init__(self, base_url: str, api_key=None):
            self.base_url = base_url
            self.api_key = api_key

    monkeypatch.setattr("orb.llm.registry.VmlxProvider", FakeVmlxProvider, raising=False)
    monkeypatch.setattr("orb.llm.vmlx.VmlxProvider", FakeVmlxProvider)

    providers = registry.build_providers(local_only=True, cloud_only=False)

    assert "vmlx" in providers


def test_anthropic_provider_reads_setup_token_env(monkeypatch):
    captured = {}

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_SETUP_TOKEN", "sk-ant-oat01-from-env")
    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeAsyncAnthropic)

    AnthropicProvider()

    assert captured["auth_token"] == "sk-ant-oat01-from-env"
    assert "oauth-2025-04-20" in captured["default_headers"]["anthropic-beta"]


@pytest.mark.asyncio
async def test_fetch_ollama_catalog_uses_registry_base_url(monkeypatch):
    requested = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": "qwen3.5:27b"}]}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, timeout):
            requested["url"] = url
            requested["timeout"] = timeout
            return FakeResponse()

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://remote-ollama.example:11434/v1")
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    runtime = GraphRuntime()
    catalog, defaults = await runtime._fetch_ollama_catalog()

    assert requested["url"] == "http://remote-ollama.example:11434/api/tags"
    assert catalog == [{"id": "qwen3.5:27b", "label": "qwen3.5:27b", "local": True}]
    assert defaults["local_medium"] == "qwen3.5:27b"


@pytest.mark.asyncio
async def test_fetch_vmlx_catalog_uses_registry_base_url(monkeypatch):
    requested = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "Qwen/Qwen2.5-Coder-7B-Instruct"}]}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, timeout, headers=None):
            requested["url"] = url
            requested["timeout"] = timeout
            requested["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(registry, "_vmlx_base_url", lambda: "http://remote-vmlx.example:1234/v1")
    monkeypatch.setattr(
        "orb.runtime.graph_runtime.get_config",
        lambda key: {"vmlx": {}} if key == "providers" else None,
    )

    runtime = GraphRuntime()
    catalog, defaults = await runtime._fetch_vmlx_catalog()

    assert requested["url"] == "http://remote-vmlx.example:1234/v1/models"
    assert catalog == [{"id": "Qwen/Qwen2.5-Coder-7B-Instruct", "label": "Qwen/Qwen2.5-Coder-7B-Instruct", "local": True}]
    assert defaults["local_small"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
