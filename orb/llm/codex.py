"""OpenAI Codex provider — ChatGPT subscription via chatgpt.com/backend-api.

Uses the OAuth access token from ``orb auth openai`` and the gpt-5.4 model.
This is free with a ChatGPT Plus/Pro subscription — no API credits needed.

The Responses API streams SSE events; this provider collects them and assembles
a single CompletionResponse before returning.
"""
from __future__ import annotations

import json as _json

from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall

_BASE_URL      = "https://chatgpt.com/backend-api"
_DEFAULT_MODEL = "gpt-5.4"


class OpenAICodexProvider(LLMClient):
    """ChatGPT subscription (Plus/Pro) via the Responses API at chatgpt.com.

    Requires an OAuth access token obtained via ``orb auth openai``.
    """

    def __init__(self, access_token: str) -> None:
        import httpx

        self._token  = access_token
        self._client = httpx.AsyncClient(timeout=120.0)

    # ── message-format conversion ─────────────────────────────────────────────

    @staticmethod
    def _to_responses_input(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format conversation history to Responses API input items."""
        items: list[dict] = []

        for msg in messages:
            role    = msg.get("role")
            content = msg.get("content")

            if role == "assistant" and isinstance(content, list):
                text_parts: list[str] = []
                func_calls: list[dict] = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        func_calls.append({
                            "type":      "function_call",
                            "call_id":   block.get("id", ""),
                            "name":      block.get("name", ""),
                            "arguments": _json.dumps(block.get("input", {})),
                        })
                if text_parts:
                    items.append({"type": "message", "role": "assistant",
                                  "content": " ".join(text_parts)})
                items.extend(func_calls)

            elif (role == "user" and isinstance(content, list)
                  and all(b.get("type") == "tool_result" for b in content)):
                for block in content:
                    items.append({
                        "type":    "function_call_output",
                        "call_id": block.get("tool_use_id", ""),
                        "output":  block.get("content", ""),
                    })

            elif role == "assistant" and isinstance(content, str):
                items.append({"type": "message", "role": "assistant", "content": content})

            elif role == "user" and isinstance(content, str):
                items.append({"type": "message", "role": "user", "content": content})

        return items

    # ── core completion ───────────────────────────────────────────────────────

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        model  = config.model_id if config else _DEFAULT_MODEL

        payload: dict = {
            "model":        model,
            "store":        False,
            "stream":       True,
            "instructions": request.system or "You are a helpful assistant.",
            "input":        self._to_responses_input(request.messages),
        }

        if request.tools:
            payload["tools"] = [
                {
                    "type":        "function",
                    "name":        t["name"],
                    "description": t.get("description", ""),
                    "parameters":  t.get("input_schema", {}),
                }
                for t in request.tools
            ]

        content_text = ""
        tool_calls: list[ToolCall] = []
        final_model  = model
        usage: dict  = {}
        pending_calls: dict[int, dict] = {}

        async with self._client.stream(
            "POST",
            f"{_BASE_URL}/codex/responses",
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
        ) as stream:
            if stream.status_code >= 400:
                body = await stream.aread()
                raise Exception(f"ChatGPT Codex API {stream.status_code}: {body.decode()[:500]}")

            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw in ("", "[DONE]"):
                    continue
                try:
                    event = _json.loads(raw)
                except Exception:
                    continue

                etype = event.get("type", "")

                if etype == "response.output_text.delta":
                    content_text += event.get("delta", "")

                elif etype == "response.output_item.added":
                    item = event.get("item", {})
                    if item.get("type") == "function_call":
                        idx = event.get("output_index", 0)
                        pending_calls[idx] = {
                            "id":        item.get("call_id") or item.get("id", ""),
                            "name":      item.get("name", ""),
                            "arguments": item.get("arguments", ""),
                        }

                elif etype == "response.function_call_arguments.delta":
                    idx = event.get("output_index", 0)
                    if idx in pending_calls:
                        pending_calls[idx]["arguments"] += event.get("delta", "")

                elif etype == "response.output_item.done":
                    item = event.get("item", {})
                    if item.get("type") == "function_call":
                        idx  = event.get("output_index", 0)
                        call = pending_calls.pop(idx, None)
                        if call:
                            try:
                                args = _json.loads(call["arguments"] or "{}")
                            except Exception:
                                args = {}
                            tool_calls.append(ToolCall(
                                id=call["id"], name=call["name"], input=args,
                            ))

                elif etype == "response.completed":
                    resp_obj    = event.get("response", {})
                    final_model = resp_obj.get("model", model)
                    u = resp_obj.get("usage", {})
                    usage = {
                        "input":  u.get("input_tokens",  0),
                        "output": u.get("output_tokens", 0),
                    }

        return CompletionResponse(
            content=content_text,
            tool_calls=tool_calls,
            model=final_model,
            stop_reason="completed",
            usage=usage,
        )

    async def close(self) -> None:
        await self._client.aclose()
