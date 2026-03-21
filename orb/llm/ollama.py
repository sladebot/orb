"""Ollama provider — local models via the Ollama REST API.

Ollama exposes an OpenAI-compatible chat endpoint at ``/api/chat``.
Message history is converted from Anthropic's internal format via `_format.to_openai_messages`.
"""
from __future__ import annotations

import json as _json
import logging
import uuid

from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall
from ._format import to_openai_messages

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL    = "llama3.2:latest"
# Local models (qwen3.5:27b at 8192 tokens) can take several minutes
_TIMEOUT = 600.0

logger = logging.getLogger(__name__)


class OllamaProvider(LLMClient):
    """Ollama local-model provider.

    Talks directly to the Ollama REST API (``/api/chat``).  Requires Ollama to
    be running locally or at the URL specified by ``base_url`` / ``OLLAMA_HOST``.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, keep_alive: str | None = None) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self._keep_alive = str(keep_alive).strip() if keep_alive is not None else None
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        messages = to_openai_messages(
            request.messages,
            request.system,
            tool_arguments_as_object=True,
        )

        payload: dict = {
            "model":    config.model_id if config else DEFAULT_MODEL,
            "messages": messages,
            "stream":   False,
        }
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive

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
        model = data.get("model", payload["model"])
        prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
        output_tokens = int(data.get("eval_count", 0) or 0)
        load_duration_ns = int(data.get("load_duration", 0) or 0)
        prompt_eval_duration_ns = int(data.get("prompt_eval_duration", 0) or 0)
        eval_duration_ns = int(data.get("eval_duration", 0) or 0)
        total_duration_ns = int(data.get("total_duration", 0) or 0)
        logger.info(
            "Ollama completion model=%s base_url=%s messages=%d prompt_tokens=%d output_tokens=%d "
            "load_duration_ms=%.2f prompt_eval_duration_ms=%.2f eval_duration_ms=%.2f total_duration_ms=%.2f "
            "tools=%d keep_alive=%s",
            model,
            self._base_url,
            len(messages),
            prompt_tokens,
            output_tokens,
            load_duration_ns / 1_000_000,
            prompt_eval_duration_ns / 1_000_000,
            eval_duration_ns / 1_000_000,
            total_duration_ns / 1_000_000,
            len(request.tools or []),
            self._keep_alive or "",
        )

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
            model=model,
            stop_reason="stop",
            usage={
                "input":  prompt_tokens,
                "output": output_tokens,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
