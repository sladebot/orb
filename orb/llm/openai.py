"""OpenAI provider (api.openai.com — requires an OPENAI_API_KEY).

Uses the official `openai` Python SDK and the chat-completions endpoint.
Message history is converted from Anthropic's internal format via `_format.to_openai_messages`.
"""
from __future__ import annotations

import json
import logging
import os

from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall
from ._format import to_openai_messages

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"


class OpenAIProvider(LLMClient):
    """OpenAI chat-completions via the official `openai` SDK.

    Key resolution order:
      1. ``api_key`` constructor argument
      2. ``OPENAI_API_KEY`` env var (picked up automatically by the SDK)
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        import openai

        kwargs: dict = {}
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if key:
            kwargs["api_key"] = key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        messages = to_openai_messages(request.messages, request.system)

        kwargs: dict = {
            "model":      config.model_id  if config else DEFAULT_MODEL,
            "messages":   messages,
            "max_tokens": config.max_tokens if config else 4096,
        }

        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name":        t["name"],
                        "description": t.get("description", ""),
                        "parameters":  t.get("input_schema", {}),
                    },
                }
                for t in request.tools
            ]

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception as exc:
                    logger.warning(
                        "Failed to parse tool call arguments: %r — args: %.200r",
                        exc, tc.function.arguments,
                    )
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=args))

        return CompletionResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            model=response.model,
            stop_reason=choice.finish_reason or "",
            usage={
                "input":  response.usage.prompt_tokens      if response.usage else 0,
                "output": response.usage.completion_tokens  if response.usage else 0,
            },
        )

    async def close(self) -> None:
        await self._client.close()
