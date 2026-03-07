from __future__ import annotations

import os

from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall


class AnthropicProvider(LLMClient):
    def __init__(self, api_key: str | None = None) -> None:
        import anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        kwargs: dict = {
            "model": config.model_id if config else "claude-sonnet-4-20250514",
            "max_tokens": config.max_tokens if config else 4096,
            "messages": request.messages,
        }
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = request.tools

        response = await self._client.messages.create(**kwargs)

        content_text = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))

        return CompletionResponse(
            content=content_text,
            tool_calls=tool_calls,
            model=response.model,
            stop_reason=response.stop_reason,
            usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        )

    async def close(self) -> None:
        await self._client.close()


class OpenAIProvider(LLMClient):
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        import openai
        kwargs: dict = {}
        if api_key or os.environ.get("OPENAI_API_KEY"):
            kwargs["api_key"] = api_key or os.environ.get("OPENAI_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        import json
        config = request.model_config
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(request.messages)

        kwargs: dict = {
            "model": config.model_id if config else "gpt-4o",
            "messages": messages,
            "max_tokens": config.max_tokens if config else 4096,
        }

        if request.tools:
            # Convert Anthropic-style tool schema to OpenAI format
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in request.tools
            ]

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        return CompletionResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            model=response.model,
            stop_reason=choice.finish_reason or "",
            usage={
                "input": response.usage.prompt_tokens if response.usage else 0,
                "output": response.usage.completion_tokens if response.usage else 0,
            },
        )

    async def close(self) -> None:
        await self._client.close()


class OllamaProvider(LLMClient):
    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        import httpx
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(request.messages)

        payload = {
            "model": config.model_id if config else "llama3.2:latest",
            "messages": messages,
            "stream": False,
        }

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in request.tools
            ]

        resp = await self._client.post(f"{self._base_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        msg = data.get("message", {})
        tool_calls = []
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tool_calls.append(ToolCall(
                    id=fn.get("name", ""),
                    name=fn.get("name", ""),
                    input=fn.get("arguments", {}),
                ))

        return CompletionResponse(
            content=msg.get("content", ""),
            tool_calls=tool_calls,
            model=data.get("model", ""),
            stop_reason="stop",
            usage={
                "input": data.get("prompt_eval_count", 0),
                "output": data.get("eval_count", 0),
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
