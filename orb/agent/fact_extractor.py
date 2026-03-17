"""FactExtractor — LLM-based fact extraction from agent turn content.

Extracts structured facts (subject-predicate-object triples) by calling an
LLM (OpenAI Codex first, Anthropic Sonnet as fallback). Fire-and-forget safe:
raises RuntimeError on failure so callers can surface it via done-callback.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from ..llm.client import LLMClient
from ..llm.types import (
    ANTHROPIC_PROVIDER,
    ANTHROPIC_SONNET_MODEL,
    OPENAI_CODEX_PROVIDER,
    CompletionRequest,
    ModelConfig,
    ModelTier,
)
from ..memory.subgraph_store import Fact, SubgraphStore

logger = logging.getLogger(__name__)

LLM_EXTRACTION_CONFIDENCE: float = 0.9

_EXTRACTION_PROMPT = (
    "Extract up to 5 key facts from the following agent response as a JSON array "
    'of {{"subject", "predicate", "object"}} triples. '
    "Return only valid JSON, no prose.\n\n{content}"
)

_CODEX_MODEL = ModelConfig(
    tier=ModelTier.CLOUD_FAST,
    model_id="gpt-5.4",
    provider=OPENAI_CODEX_PROVIDER,
    max_tokens=512,
)
_ANTHROPIC_MODEL = ModelConfig(
    tier=ModelTier.CLOUD_FAST,
    model_id=ANTHROPIC_SONNET_MODEL,
    provider=ANTHROPIC_PROVIDER,
    max_tokens=512,
)


class FactExtractor:
    """Extracts structured facts from agent turn content via LLM and writes to SubgraphStore."""

    def __init__(
        self,
        store: SubgraphStore,
        agent_id: str,
        providers: dict[str, LLMClient],
    ) -> None:
        self._store = store
        self._agent_id = agent_id
        self._providers = providers

    async def extract_and_store(self, turn_id: str, content: str) -> list[Fact]:
        """Call LLM to extract facts, parse response, upsert to store. Returns stored facts."""
        raw = await self._call_llm(content)
        facts = self._parse_facts(raw, turn_id)
        for fact in facts:
            await self._store.upsert_fact(fact)
        return facts

    async def _call_llm(self, content: str) -> str:
        """Try Codex, fall back to Anthropic Sonnet. Raises RuntimeError if both fail."""
        prompt = _EXTRACTION_PROMPT.format(content=content)
        messages = [{"role": "user", "content": prompt}]

        if OPENAI_CODEX_PROVIDER in self._providers:
            try:
                request = CompletionRequest(
                    messages=messages,
                    tools=[],
                    system="",
                    model_config=_CODEX_MODEL,
                )
                response = await self._providers[OPENAI_CODEX_PROVIDER].complete(request)
                return response.content
            except Exception as exc:
                logger.debug("Codex fact extraction failed, trying Anthropic: %s", exc)

        if ANTHROPIC_PROVIDER in self._providers:
            try:
                request = CompletionRequest(
                    messages=messages,
                    tools=[],
                    system="",
                    model_config=_ANTHROPIC_MODEL,
                )
                response = await self._providers[ANTHROPIC_PROVIDER].complete(request)
                return response.content
            except Exception as exc:
                logger.debug("Anthropic fact extraction failed: %s", exc)

        raise RuntimeError("Fact extraction failed: both Codex and Anthropic unavailable")

    def _parse_facts(self, raw: str, turn_id: str) -> list[Fact]:
        """Parse LLM JSON response into Fact objects. Raises RuntimeError on bad JSON."""
        try:
            triples = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Fact extraction returned invalid JSON: {raw[:200]}"
            ) from exc

        if not isinstance(triples, list):
            raise RuntimeError(
                f"Fact extraction returned invalid JSON: {raw[:200]}"
            )

        facts: list[Fact] = []
        for triple in triples:
            if not isinstance(triple, dict):
                logger.debug("Skipping non-dict triple: %r", triple)
                continue
            subject = triple.get("subject")
            predicate = triple.get("predicate")
            obj = triple.get("object")
            if subject is None or predicate is None or obj is None:
                logger.debug("Skipping partial triple (missing field): %r", triple)
                continue
            facts.append(Fact(
                id=uuid4().hex[:12],
                subject=str(subject),
                predicate=str(predicate),
                object=str(obj),
                agent_id=self._agent_id,
                turn_id=turn_id,
                confidence=LLM_EXTRACTION_CONFIDENCE,
                metadata={"extraction_method": "llm"},
            ))
        return facts
