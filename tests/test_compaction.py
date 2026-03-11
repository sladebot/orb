"""Tests for conversation history compaction."""
from __future__ import annotations

import pytest

from orb.agent.compaction import compact_history, COMPACT_THRESHOLD
from orb.llm.types import CompletionResponse, ModelTier, ModelConfig
from tests.test_claude_agent import MockLLMClient


def _make_mock_providers(summary: str = "Prior work summary.") -> dict:
    client = MockLLMClient([
        CompletionResponse(content=summary, model="mock"),
    ])
    mock_cfg = ModelConfig(tier=ModelTier.CLOUD_LITE, model_id="mock", provider="anthropic")
    # Patch DEFAULT_MODELS so compact_history picks the right model config
    import orb.llm.types as lt
    lt.DEFAULT_MODELS[ModelTier.CLOUD_LITE] = mock_cfg
    # Use "anthropic" key so compact_history finds the provider
    return {"anthropic": client}


def _long_history(n: int = COMPACT_THRESHOLD) -> list[dict]:
    """Alternating user/assistant messages."""
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"message {i}"})
    return msgs


class TestCompaction:
    async def test_compacted_output_ends_with_assistant(self):
        """After compaction the history must end on an assistant turn."""
        providers = _make_mock_providers("summary text")
        msgs = _long_history(COMPACT_THRESHOLD)
        result = await compact_history(msgs, providers)
        assert result[-1]["role"] == "assistant"

    async def test_compacted_output_is_two_messages(self):
        """Compaction returns exactly [user, assistant]."""
        providers = _make_mock_providers("summary")
        msgs = _long_history(COMPACT_THRESHOLD)
        result = await compact_history(msgs, providers)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    async def test_compacted_content_contains_summary(self):
        providers = _make_mock_providers("KEY DECISION: use Python")
        msgs = _long_history(COMPACT_THRESHOLD)
        result = await compact_history(msgs, providers)
        assert "KEY DECISION: use Python" in result[1]["content"]

    async def test_no_provider_returns_original(self):
        """With no providers, compaction is a no-op."""
        msgs = _long_history(COMPACT_THRESHOLD)
        result = await compact_history(msgs, {})
        assert result == msgs

    async def test_llm_failure_returns_original(self):
        """If the LLM call raises, original history is returned."""
        from orb.llm.client import LLMClient
        from orb.llm.types import CompletionRequest

        class BrokenClient(LLMClient):
            async def complete(self, req: CompletionRequest):
                raise RuntimeError("network error")
            async def close(self): pass

        import orb.llm.types as lt
        mock_cfg = ModelConfig(tier=ModelTier.CLOUD_LITE, model_id="mock", provider="anthropic")
        lt.DEFAULT_MODELS[ModelTier.CLOUD_LITE] = mock_cfg
        providers = {"anthropic": BrokenClient()}
        msgs = _long_history(COMPACT_THRESHOLD)
        result = await compact_history(msgs, providers)
        assert result == msgs

    async def test_empty_summary_returns_original(self):
        """If LLM returns empty string, fall back to original."""
        providers = _make_mock_providers("")
        msgs = _long_history(COMPACT_THRESHOLD)
        result = await compact_history(msgs, providers)
        assert result == msgs

    async def test_structured_content_blocks_are_handled(self):
        """Anthropic-format content lists are extracted as text."""
        providers = _make_mock_providers("summary")
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": "world"},
        ] * (COMPACT_THRESHOLD // 2)
        # Should not raise
        result = await compact_history(msgs, providers)
        assert result[-1]["role"] == "assistant"
