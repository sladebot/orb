"""VMLX provider — local models via an OpenAI-compatible HTTP API.

Expected endpoints:
  - ``POST /v1/chat/completions``
  - ``GET  /v1/models``
"""
from __future__ import annotations

import json
import logging

from ._format import to_openai_messages
from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_MODEL = "qwen"
_TIMEOUT = 600.0

logger = logging.getLogger(__name__)


class VmlxProvider(LLMClient):
    """VMLX local-model provider using an OpenAI-compatible API surface."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: str | None = None) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self._api_key = str(api_key).strip() if api_key else None
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        messages = to_openai_messages(request.messages, request.system)

        payload: dict = {
            "model": config.model_id if config else DEFAULT_MODEL,
            "messages": messages,
            "max_tokens": config.max_tokens if config else 4096,
            "temperature": config.temperature if config else 0.7,
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

        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers or None,
        )
        if resp.status_code >= 400:
            raise Exception(f"VMLX {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return CompletionResponse(content="", model=str(data.get("model") or payload["model"]))
        choice = choices[0] or {}
        message = choice.get("message") or {}

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            function = tc.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            tool_calls.append(ToolCall(
                id=str(tc.get("id") or ""),
                name=str(function.get("name") or ""),
                input=parsed_args,
            ))

        usage = data.get("usage") or {}
        model = str(data.get("model") or payload["model"])
        logger.info(
            "VMLX completion model=%s base_url=%s messages=%d prompt_tokens=%d output_tokens=%d tools=%d",
            model,
            self._base_url,
            len(messages),
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
            len(request.tools or []),
        )

        return CompletionResponse(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            model=model,
            stop_reason=str(choice.get("finish_reason") or ""),
            usage={
                "input": int(usage.get("prompt_tokens", 0) or 0),
                "output": int(usage.get("completion_tokens", 0) or 0),
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
