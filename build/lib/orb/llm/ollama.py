"""Ollama provider — local models via the Ollama REST API.

Ollama exposes an OpenAI-compatible chat endpoint at ``/api/chat``.
Message history is converted from Anthropic's internal format via `_format.to_openai_messages`.
"""
from __future__ import annotations

import json as _json
import uuid

from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall
from ._format import to_openai_messages

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL    = "llama3.2:latest"
# Local models (qwen3.5:27b at 8192 tokens) can take several minutes
_TIMEOUT = 600.0


class OllamaProvider(LLMClient):
    """Ollama local-model provider.

    Talks directly to the Ollama REST API (``/api/chat``).  Requires Ollama to
    be running locally or at the URL specified by ``base_url`` / ``OLLAMA_HOST``.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        messages = to_openai_messages(request.messages, request.system)

        payload: dict = {
            "model":    config.model_id if config else DEFAULT_MODEL,
            "messages": messages,
            "stream":   False,
        }

        if request.tools:
            payload["tools"] = [
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

        resp = await self._client.post(f"{self._base_url}/api/chat", json=payload)
        if resp.status_code >= 400:
            raise Exception(f"Ollama {resp.status_code}: {resp.text[:500]}")
        data = resp.json()

        msg = data.get("message", {})
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = _json.loads(raw_args)
                except Exception:
                    raw_args = {}
            tool_calls.append(ToolCall(
                id=f"toolu_{uuid.uuid4().hex[:16]}",
                name=fn.get("name", ""),
                input=raw_args,
            ))

        return CompletionResponse(
            content=msg.get("content", ""),
            tool_calls=tool_calls,
            model=data.get("model", ""),
            stop_reason="stop",
            usage={
                "input":  data.get("prompt_eval_count", 0),
                "output": data.get("eval_count", 0),
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
