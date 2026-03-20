"""Anthropic Claude provider.

Supports both console API keys (sk-ant-api03-*) and OAuth tokens (sk-ant-oat01-*)
issued by Claude.ai.  OAuth tokens require Bearer auth and the anthropic-beta header.
"""
from __future__ import annotations

import os

from .client import LLMClient
from .types import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_MODEL,
    ANTHROPIC_SONNET_MODEL,
    CompletionRequest,
    CompletionResponse,
    ToolCall,
)

OAUTH_BETAS = ",".join([
    "oauth-2025-04-20",
    "claude-code-20250219",
    "fine-grained-tool-streaming-2025-05-14",
    "interleaved-thinking-2025-05-14",
])
DEFAULT_MODEL = ANTHROPIC_SONNET_MODEL
ANTHROPIC_MODEL_ALIASES = {
    "claude-3-5-haiku-latest": ANTHROPIC_HAIKU_MODEL,
    "claude-3-5-haiku-20241022": ANTHROPIC_HAIKU_MODEL,
    "claude-haiku-4-5": ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_HAIKU_MODEL: ANTHROPIC_HAIKU_MODEL,
    "claude-sonnet-4-0": ANTHROPIC_SONNET_MODEL,
    "claude-sonnet-4-5": ANTHROPIC_SONNET_MODEL,
    "claude-sonnet-4-20250514": ANTHROPIC_SONNET_MODEL,
    "claude-sonnet-4-5-20250929": ANTHROPIC_SONNET_MODEL,
    ANTHROPIC_SONNET_MODEL: ANTHROPIC_SONNET_MODEL,
    "claude-opus-4-0": ANTHROPIC_OPUS_MODEL,
    "claude-opus-4": ANTHROPIC_OPUS_MODEL,
    "claude-opus-4-20250514": ANTHROPIC_OPUS_MODEL,
    "claude-opus-4-5-20251101": ANTHROPIC_OPUS_MODEL,
    ANTHROPIC_OPUS_MODEL: ANTHROPIC_OPUS_MODEL,
}


def is_oauth_token(token: str) -> bool:
    """Return True if token is a Claude.ai OAuth token (requires Bearer auth)."""
    return token.startswith("sk-ant-oat")


class AnthropicProvider(LLMClient):
    """Anthropic Claude via the official `anthropic` SDK.

    Key resolution order:
      1. ``api_key`` constructor argument
      2. ``ANTHROPIC_OAUTH_TOKEN`` env var
      3. ``ANTHROPIC_API_KEY`` env var
    """

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic

        token = (
            api_key
            or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        if token and is_oauth_token(token):
            # OAuth tokens must use Authorization: Bearer + beta header
            self._client = anthropic.AsyncAnthropic(
                auth_token=token,
                default_headers={"anthropic-beta": OAUTH_BETAS},
            )
        else:
            self._client = anthropic.AsyncAnthropic(api_key=token)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        model_id = config.model_id if config else DEFAULT_MODEL
        resolved_model = ANTHROPIC_MODEL_ALIASES.get(model_id, model_id)
        kwargs: dict = {
            "model":      resolved_model,
            "max_tokens": config.max_tokens if config else 4096,
            "messages":   request.messages,
        }
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = request.tools

        response = await self._client.messages.create(**kwargs)

        content_text = ""
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        return CompletionResponse(
            content=content_text,
            tool_calls=tool_calls,
            model=response.model,
            stop_reason=response.stop_reason,
            usage={
                "input":  response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        )

    async def close(self) -> None:
        await self._client.close()
