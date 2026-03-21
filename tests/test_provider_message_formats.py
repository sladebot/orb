from __future__ import annotations

from types import SimpleNamespace

import pytest

from orb.llm.anthropic import AnthropicProvider
from orb.llm.openai import OpenAIProvider
from orb.llm.types import CompletionRequest, ModelConfig, ModelTier


@pytest.mark.asyncio
async def test_openai_provider_stringifies_replayed_tool_arguments(monkeypatch):
    captured: dict = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                model="gpt-5.4",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=[]),
                        finish_reason="stop",
                    ),
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    provider = OpenAIProvider(api_key="sk-test")
    req = CompletionRequest(
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call_1", "name": "demo", "input": {"x": 1}},
                ],
            },
        ],
        model_config=ModelConfig(ModelTier.CLOUD_FAST, "gpt-5.4", "openai-codex"),
    )

    await provider.complete(req)
    await provider.close()

    assert captured["messages"] == [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "demo",
                        "arguments": '{"x": 1}',
                    },
                },
            ],
        },
    ]


@pytest.mark.asyncio
async def test_anthropic_provider_preserves_native_message_schema(monkeypatch):
    captured: dict = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                model="claude-haiku-4-5-20251001",
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    class FakeAsyncAnthropic:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

        async def close(self):
            return None

    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeAsyncAnthropic)

    provider = AnthropicProvider(api_key="sk-ant-api03-test")
    req_messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling tool"},
                {"type": "tool_use", "id": "call_1", "name": "demo", "input": {"x": 1}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
            ],
        },
    ]
    req = CompletionRequest(
        messages=req_messages,
        model_config=ModelConfig(ModelTier.CLOUD_LITE, "claude-haiku-4-5-20251001", "anthropic"),
    )

    await provider.complete(req)
    await provider.close()

    assert captured["messages"] == req_messages
