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

    async def complete(
        self,
        request: CompletionRequest,
        *,
        on_chunk=None,
    ) -> CompletionResponse:
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

        if on_chunk is None:
            # Non-streaming path — unchanged from the legacy impl.
            response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            tool_calls = _parse_openai_tool_calls(choice.message.tool_calls or [])
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

        # Streaming path — tool_calls arrive incrementally and must be
        # accumulated per ``index`` (id/name land in the first chunk,
        # JSON arguments stream across many). Text deltas go straight to
        # on_chunk so the UI paints them live.
        kwargs["stream"] = True
        # ``stream_options.include_usage`` returns the usage in the final
        # chunk so we don't lose it.
        kwargs["stream_options"] = {"include_usage": True}

        content_text = ""
        pending_tool_calls: dict[int, dict] = {}
        final_model = kwargs["model"]
        finish_reason = ""
        usage: dict = {}

        stream = await self._client.chat.completions.create(**kwargs)
        async for event in stream:
            # Usage-only final chunk has no ``choices``.
            if getattr(event, "usage", None):
                u = event.usage
                usage = {
                    "input":  getattr(u, "prompt_tokens", 0) or 0,
                    "output": getattr(u, "completion_tokens", 0) or 0,
                }
            if not event.choices:
                if getattr(event, "model", None):
                    final_model = event.model
                continue
            choice = event.choices[0]
            delta = choice.delta
            if getattr(event, "model", None):
                final_model = event.model
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            text = getattr(delta, "content", None)
            if text:
                content_text += text
                await on_chunk(text)
            delta_tool_calls = getattr(delta, "tool_calls", None) or []
            for tc in delta_tool_calls:
                idx = getattr(tc, "index", 0) or 0
                slot = pending_tool_calls.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""},
                )
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

        tool_calls: list[ToolCall] = []
        for idx in sorted(pending_tool_calls):
            slot = pending_tool_calls[idx]
            try:
                args = json.loads(slot["arguments"] or "{}")
            except Exception as exc:
                logger.warning(
                    "Failed to parse streamed tool call arguments: %r — args: %.200r",
                    exc, slot["arguments"],
                )
                args = {}
            tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], input=args))

        return CompletionResponse(
            content=content_text,
            tool_calls=tool_calls,
            model=final_model,
            stop_reason=finish_reason,
            usage=usage,
        )

    async def close(self) -> None:
        await self._client.close()


def _parse_openai_tool_calls(raw_tool_calls) -> list[ToolCall]:
    """Parse the non-streaming ``choice.message.tool_calls`` into our
    ToolCall list — extracted so the streaming path and non-streaming
    path can't drift on JSON-arg fallback behaviour.
    """
    tool_calls: list[ToolCall] = []
    for tc in raw_tool_calls:
        try:
            args = json.loads(tc.function.arguments)
        except Exception as exc:
            logger.warning(
                "Failed to parse tool call arguments: %r — args: %.200r",
                exc, tc.function.arguments,
            )
            args = {}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=args))
    return tool_calls
