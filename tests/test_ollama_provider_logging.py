import logging

import pytest

from orb.llm.ollama import OllamaProvider
from orb.llm.types import CompletionRequest, CompletionResponse, ModelConfig, ModelTier


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "model": "qwen3.5:9b",
            "message": {"content": "ok", "tool_calls": []},
            "prompt_eval_count": 321,
            "eval_count": 44,
            "load_duration": 250_000_000,
            "prompt_eval_duration": 1_500_000_000,
            "eval_duration": 800_000_000,
            "total_duration": 2_650_000_000,
        }


class _FakeClient:
    def __init__(self) -> None:
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return _FakeResponse()

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ollama_provider_logs_timing_breakdown(caplog):
    client = _FakeClient()
    provider = OllamaProvider(base_url="http://ollama.example:11434", keep_alive="-1")
    provider._client = client  # noqa: SLF001

    request = CompletionRequest(
        system="You are concise.",
        messages=[{"role": "user", "content": "hello"}],
        model_config=ModelConfig(provider="ollama", model_id="qwen3.5:9b", tier=ModelTier.LOCAL_SMALL),
    )

    with caplog.at_level(logging.INFO, logger="orb.llm.ollama"):
        response = await provider.complete(request)

    assert response.content == "ok"
    assert response.usage == {"input": 321, "output": 44}
    assert client.calls[0][1]["keep_alive"] == "-1"

    message = caplog.text
    assert "Ollama completion model=qwen3.5:9b" in message
    assert "prompt_tokens=321" in message
    assert "output_tokens=44" in message
    assert "load_duration_ms=250.00" in message
    assert "prompt_eval_duration_ms=1500.00" in message
    assert "eval_duration_ms=800.00" in message
    assert "total_duration_ms=2650.00" in message
    assert "keep_alive=-1" in message
