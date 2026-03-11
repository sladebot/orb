"""Token-aware conversation history compaction.

When an agent's conversation history is getting long, summarize it with a
lightweight LLM call so the next run starts compact.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COMPACT_THRESHOLD = 16  # compact when message count >= this value


async def compact_history(
    messages: list[dict],
    providers: dict,
) -> list[dict]:
    """Summarize a long conversation history into a single compact context message.

    Returns a new messages list with one synthetic user message containing the
    summary, or the original list if compaction fails.
    """
    from ..llm.types import CompletionRequest, ModelTier, DEFAULT_MODELS, OPENAI_MODELS, CODEX_MODELS

    # Pick the lightest available provider
    provider = (
        providers.get("anthropic")
        or providers.get("openai")
        or providers.get("openai-codex")
        or providers.get("ollama")
    )
    if not provider:
        logger.debug("compaction: no provider available, skipping")
        return messages

    # Pick a lite/fast model config
    has_anthropic = "anthropic" in providers
    has_openai    = "openai"    in providers
    has_codex     = "openai-codex" in providers

    if has_anthropic:
        model_config = DEFAULT_MODELS.get(ModelTier.CLOUD_LITE) or DEFAULT_MODELS[ModelTier.CLOUD_FAST]
    elif has_openai:
        model_config = OPENAI_MODELS.get(ModelTier.CLOUD_LITE) or OPENAI_MODELS[ModelTier.CLOUD_FAST]
    elif has_codex:
        model_config = CODEX_MODELS.get(ModelTier.CLOUD_LITE) or CODEX_MODELS[ModelTier.CLOUD_FAST]
    else:
        model_config = DEFAULT_MODELS.get(ModelTier.LOCAL_SMALL) or DEFAULT_MODELS[ModelTier.LOCAL_MEDIUM]

    # Build a readable transcript from the messages
    transcript_parts = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, list):
            # Handle structured content blocks (Anthropic format)
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            content = " ".join(texts)
        transcript_parts.append(f"[{role}]: {str(content)[:500]}")
    transcript = "\n".join(transcript_parts)

    prompt = (
        "Summarize the following agent conversation concisely, preserving key decisions, "
        "code produced, file paths modified, and any open questions. "
        "Output ONLY the summary text.\n\n"
        f"{transcript}"
    )

    req = CompletionRequest(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system="You are a context compactor. Reply with a concise summary only.",
        model_config=model_config,
    )

    try:
        response = await provider.complete(req)
        summary = (response.content or "").strip()
        if not summary:
            logger.warning("compaction: LLM returned empty summary, keeping original history")
            return messages
        logger.info(
            "compaction: condensed %d messages → 1 summary (%d chars)",
            len(messages), len(summary),
        )
        return [
            {"role": "user",      "content": "[Session context summary]"},
            {"role": "assistant", "content": f"[Compacted context]\n{summary}"},
        ]
    except Exception as exc:
        logger.warning("compaction: LLM call failed (%s), keeping original history", exc)
        return messages
