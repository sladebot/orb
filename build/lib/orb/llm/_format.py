"""Shared message-format helpers for OpenAI-compatible providers (OpenAI, Ollama).

The app uses Anthropic's internal message format as the canonical representation.
These helpers convert that format to the OpenAI chat-completions wire format.
"""
from __future__ import annotations

import json as _json


def extract_text_content(content: list[dict]) -> str:
    """Extract plain text from an Anthropic-format content block list."""
    return " ".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def tool_result_to_str(raw: object) -> str:
    """Coerce an Anthropic tool-result content value to a plain string."""
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


def to_openai_messages(messages: list[dict], system: str = "") -> list[dict]:
    """Convert Anthropic-format conversation history to OpenAI/Ollama chat format.

    Anthropic stores:
      - assistant tool calls as content list with {"type": "tool_use", ...}
      - tool results as user messages with content list of {"type": "tool_result", ...}

    OpenAI/Ollama expect:
      - assistant tool calls as message.tool_calls list
      - tool results as {"role": "tool", "tool_call_id": ..., "content": ...}
    """
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
                            "id": block.get("id") or f"call_{block.get('name', '')}",
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
                result_content = tool_result_to_str(block.get("content", ""))
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
