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

