"""Token-aware conversation history compaction.

When an agent or conversation history is getting long, summarize it with a
lightweight LLM call so the next run starts compact.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

COMPACT_THRESHOLD = 16  # compact when message count >= this value


class CompactionStrategy(Protocol):
    async def compact_messages(self, messages: list[dict], providers: dict) -> list[dict]:
        ...

    async def compact_transcript(self, transcript: str, providers: dict) -> str:
        ...


class LLMConversationCompactor:
    async def compact_messages(self, messages: list[dict], providers: dict) -> list[dict]:
        """Summarize a long conversation history into a compact message list."""
        from ..llm._format import extract_text_content

        transcript_parts = []
        for message in messages:
            role = message.get("role", "unknown")
            content = message.get("content", "")
            if isinstance(content, list):
                content = extract_text_content(content)
            transcript_parts.append(f"[{role}]: {str(content)[:500]}")
        transcript = "\n".join(transcript_parts)

        summary = await _llm_summary(
            (
                "Summarize the following agent conversation concisely, preserving key decisions, "
                "code produced, file paths modified, and any open questions.\n\n"
                f"{transcript}"
            ),
            providers,
        )
        if not summary:
            return messages
        logger.info(
            "compaction: condensed %d messages → 1 summary (%d chars)",
            len(messages), len(summary),
        )
        return [
            {"role": "user", "content": "[Session context summary]"},
            {"role": "assistant", "content": f"[Compacted context]\n{summary}"},
        ]

    async def compact_transcript(self, transcript: str, providers: dict) -> str:
        """Summarize a transcript into a single compact text block."""
        prompt = (
            "Compress this conversation context for reuse in a follow-up model call. "
            "Preserve user intent, routing decisions, key implementation work, touched files, "
            "unfinished work, and blockers. Output only the compact summary.\n\n"
            f"{transcript}"
        )
        summary = await _llm_summary(prompt, providers)
        if summary:
            return summary
        return _fallback_summary(transcript)


DEFAULT_COMPACTOR = LLMConversationCompactor()


async def compact_history(
    messages: list[dict],
    providers: dict,
) -> list[dict]:
    """Backward-compatible wrapper for agent history compaction."""
    return await DEFAULT_COMPACTOR.compact_messages(messages, providers)


async def _llm_summary(prompt: str, providers: dict) -> str:
    from ..llm.types import CompletionRequest, ModelTier, DEFAULT_MODELS, ANTHROPIC_MODELS, CODEX_MODELS

    provider = (
        providers.get("anthropic")
        or providers.get("openai-codex")
        or providers.get("ollama")
        or providers.get("vmlx")
    )
    if not provider:
        logger.debug("compaction: no provider available, using fallback summary")
        return ""

    has_anthropic = "anthropic" in providers
    has_codex = "openai-codex" in providers

    if has_anthropic:
        model_config = ANTHROPIC_MODELS.get(ModelTier.CLOUD_LITE) or ANTHROPIC_MODELS[ModelTier.CLOUD_FAST]
    elif has_codex:
        model_config = CODEX_MODELS.get(ModelTier.CLOUD_LITE) or CODEX_MODELS[ModelTier.CLOUD_FAST]
    else:
        model_config = DEFAULT_MODELS.get(ModelTier.LOCAL_SMALL) or DEFAULT_MODELS[ModelTier.LOCAL_MEDIUM]

    request = CompletionRequest(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system="You are a context compactor. Reply with a concise summary only.",
        model_config=model_config,
    )

    try:
        response = await provider.complete(request)
    except Exception as exc:
        logger.warning("compaction: LLM call failed (%s), using fallback summary", exc)
        return ""

    summary = (response.content or "").strip()
    if not summary:
        logger.warning("compaction: LLM returned empty summary, using fallback summary")
        return ""
    return summary


def _fallback_summary(transcript: str, *, max_lines: int = 12) -> str:
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    if not lines:
        return ""
    excerpt = lines[-max_lines:]
    if len(excerpt) < len(lines):
        excerpt.insert(0, f"[Earlier context omitted: {len(lines) - len(excerpt)} lines]")
    return "\n".join(excerpt)
