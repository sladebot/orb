from __future__ import annotations

import os

from .client import LLMClient
from .types import CompletionRequest, CompletionResponse, ToolCall


_ANTHROPIC_OAUTH_BETAS = "oauth-2025-04-20,claude-code-20250219"


def _is_anthropic_oauth_token(token: str) -> bool:
    # sk-ant-oat01-* keys are OAuth tokens issued by Claude.ai — must use Bearer auth.
    # sk-ant-api03-* and similar are console API keys — use x-api-key.
    return token.startswith("sk-ant-oat")


class AnthropicProvider(LLMClient):
    def __init__(self, api_key: str | None = None) -> None:
        import anthropic
        token = api_key or os.environ.get("ANTHROPIC_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
        if token and _is_anthropic_oauth_token(token):
            # OAuth tokens must use Authorization: Bearer, not x-api-key
            self._client = anthropic.AsyncAnthropic(
                auth_token=token,
                default_headers={"anthropic-beta": _ANTHROPIC_OAUTH_BETAS},
            )
        else:
            self._client = anthropic.AsyncAnthropic(api_key=token)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        kwargs: dict = {
            "model": config.model_id if config else "claude-sonnet-4-5-20251001",
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


def _tool_result_content_str(raw: object) -> str:
    """Coerce Anthropic tool-result content to a plain string for OpenAI/Ollama."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p)
    return str(raw) if raw is not None else ""


def _convert_to_openai_messages(messages: list[dict], system: str = "") -> list[dict]:
    """Convert Anthropic-format conversation history to OpenAI/Ollama chat format.

    Anthropic stores:
      - assistant tool calls as content list with {"type": "tool_use", ...}
      - tool results as user messages with content list of {"type": "tool_result", ...}

    OpenAI/Ollama expect:
      - assistant tool calls as message.tool_calls list
      - tool results as {"role": "tool", "tool_call_id": ..., "content": ...}
    """
    import json as _json

    converted: list[dict] = []
    if system:
        converted.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id") or f"call_{block.get('name','')}",
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": _json.dumps(block.get("input", {})),
                            },
                        })
            # Ollama rejects assistant messages with empty content + tool_calls
            text = " ".join(text_parts) or (None if tool_calls else "")
            out: dict = {"role": "assistant"}
            if text is not None:
                out["content"] = text
            if tool_calls:
                out["tool_calls"] = tool_calls
            converted.append(out)

        elif role == "user" and isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            for block in content:
                tool_call_id = block.get("tool_use_id") or ""
                result_content = _tool_result_content_str(block.get("content", ""))
                # Skip tool results with no id — Ollama rejects them
                if not tool_call_id:
                    continue
                converted.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_content,
                })

        elif isinstance(content, str):
            converted.append({"role": role, "content": content})

        else:
            converted.append(msg)

    return converted


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
        messages = _convert_to_openai_messages(request.messages, request.system)

        kwargs: dict = {
            "model": config.model_id if config else "gpt-4o",
            "messages": messages,
            "max_tokens": config.max_tokens if config else 4096,
        }

        if request.tools:
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
        # 600s — local models (qwen3.5:27b at 8192 tokens) can take several minutes
        self._client = httpx.AsyncClient(timeout=600.0)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        config = request.model_config
        messages = _convert_to_openai_messages(request.messages, request.system)

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
        if resp.status_code >= 400:
            raise Exception(f"Ollama {resp.status_code}: {resp.text[:500]}")
        data = resp.json()

        msg = data.get("message", {})
        tool_calls = []
        if msg.get("tool_calls"):
            import uuid
            import json as _json
            for tc in msg["tool_calls"]:
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
                "input": data.get("prompt_eval_count", 0),
                "output": data.get("eval_count", 0),
            },
        )

    async def close(self) -> None:
        await self._client.aclose()


class OpenAICodexProvider(LLMClient):
    """Provider for ChatGPT subscription users via the Responses API at chatgpt.com/backend-api.

    Uses the OAuth access token from `orb auth openai` and the gpt-5.4 model.
    This is free with a ChatGPT Plus/Pro subscription — no API credits needed.
    """

    _BASE_URL = "https://chatgpt.com/backend-api"
    _DEFAULT_MODEL = "gpt-5.4"

    def __init__(self, access_token: str) -> None:
        import httpx
        self._token = access_token
        self._client = httpx.AsyncClient(timeout=120.0)

    @staticmethod
    def _to_responses_input(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format conversation history to Responses API input items."""
        import json as _json
        items: list[dict] = []

        for msg in messages:
            role    = msg.get("role")
            content = msg.get("content")

            if role == "assistant" and isinstance(content, list):
                # Emit text message first, then function_call items
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
                # Tool results → function_call_output items
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

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        import json as _json
        config = request.model_config
        model  = config.model_id if config else self._DEFAULT_MODEL

        payload: dict = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": request.system or "You are a helpful assistant.",
            "input": self._to_responses_input(request.messages),
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

        # Streaming SSE response — collect all events then assemble final response
        content_text = ""
        tool_calls: list[ToolCall] = []
        final_model = model
        usage: dict = {}
        # Track in-progress function calls by index
        pending_calls: dict[int, dict] = {}

        async with self._client.stream(
            "POST",
            f"{self._BASE_URL}/codex/responses",
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
                        idx = event.get("output_index", 0)
                        call = pending_calls.pop(idx, None)
                        if call:
                            try:
                                args = _json.loads(call["arguments"] or "{}")
                            except Exception:
                                args = {}
                            tool_calls.append(ToolCall(
                                id=call["id"],
                                name=call["name"],
                                input=args,
                            ))

                elif etype == "response.completed":
                    resp_obj = event.get("response", {})
                    final_model = resp_obj.get("model", model)
                    u = resp_obj.get("usage", {})
                    usage = {
                        "input":  u.get("input_tokens", 0),
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
