# LLM-Based Fact Extraction Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace regex-based fact extraction in `FactExtractor` with an LLM call (Codex → Anthropic Sonnet fallback), wiring errors through `logging` via `.add_done_callback`.

**Architecture:** `FactExtractor` receives the agent's `providers` dict, builds a JSON extraction prompt, tries Codex then Sonnet, parses the response into `Fact` objects, and upserts them. `LLMAgent.set_subgraph_store()` passes `self._providers` through. The fire-and-forget task gets a done-callback that routes failures to `logger.warning`.

**Tech Stack:** Python asyncio, `orb.llm.client.LLMClient`, `orb.llm.types` (CompletionRequest, ModelConfig, OPENAI_CODEX_PROVIDER, ANTHROPIC_PROVIDER), `orb.memory.subgraph_store.Fact`, `unittest.mock`

**Spec:** `docs/superpowers/specs/2026-03-17-llm-fact-extraction-design.md`

---

## Chunk 1: Rewrite `FactExtractor` with LLM extraction

**Files:**
- Modify: `orb/agent/fact_extractor.py` (full rewrite)
- Modify: `tests/test_fact_extractor.py` (replace all heuristic tests with LLM mock tests)

---

### Task 1: Replace heuristic tests with LLM mock tests

- [ ] **Step 1: Delete all existing tests in `tests/test_fact_extractor.py` and write the new LLM mock tests**

Replace the entire file with:

```python
"""Tests for LLM-based FactExtractor — written BEFORE implementation per CLAUDE.md rule #1."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orb.agent.fact_extractor import FactExtractor, LLM_EXTRACTION_CONFIDENCE
from orb.llm.client import LLMClient
from orb.llm.types import CompletionResponse
from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(json_payload: str) -> CompletionResponse:
    return CompletionResponse(content=json_payload, tool_calls=[], model="test-model")


def _make_providers(codex_response=None, codex_raises=None,
                    anthropic_response=None, anthropic_raises=None):
    """Build a fake providers dict with mock LLMClients."""
    from orb.llm.types import OPENAI_CODEX_PROVIDER, ANTHROPIC_PROVIDER

    providers = {}

    if codex_response is not None or codex_raises is not None:
        codex = AsyncMock(spec=LLMClient)
        if codex_raises:
            codex.complete.side_effect = codex_raises
        else:
            codex.complete.return_value = _make_response(codex_response)
        providers[OPENAI_CODEX_PROVIDER] = codex

    if anthropic_response is not None or anthropic_raises is not None:
        ant = AsyncMock(spec=LLMClient)
        if anthropic_raises:
            ant.complete.side_effect = anthropic_raises
        else:
            ant.complete.return_value = _make_response(anthropic_response)
        providers[ANTHROPIC_PROVIDER] = ant

    return providers


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_extraction_returns_facts():
    """Mock Codex returns valid JSON; facts are parsed and stored."""
    store = ChromaDBNetworkXStore()
    payload = json.dumps([
        {"subject": "Alice", "predicate": "wrote", "object": "tests"},
        {"subject": "Bob", "predicate": "reviewed", "object": "PR #42"},
    ])
    providers = _make_providers(codex_response=payload)
    extractor = FactExtractor(store, "agent-a", providers)

    facts = await extractor.extract_and_store("turn-1", "Alice wrote tests. Bob reviewed PR #42.")

    assert len(facts) == 2
    predicates = {f.predicate for f in facts}
    assert predicates == {"wrote", "reviewed"}
    for f in facts:
        assert f.confidence == pytest.approx(LLM_EXTRACTION_CONFIDENCE)
        assert f.metadata["extraction_method"] == "llm"
        assert f.agent_id == "agent-a"
        assert f.turn_id == "turn-1"

    stored = await store.get_facts("agent-a")
    stored_ids = {f.id for f in stored}
    for fact in facts:
        assert fact.id in stored_ids

    await store.close()


@pytest.mark.asyncio
async def test_codex_fallback_to_sonnet():
    """Codex raises; Anthropic Sonnet is called as fallback."""
    store = ChromaDBNetworkXStore()
    payload = json.dumps([{"subject": "Alice", "predicate": "wrote", "object": "code"}])
    providers = _make_providers(
        codex_raises=RuntimeError("codex down"),
        anthropic_response=payload,
    )
    extractor = FactExtractor(store, "agent-b", providers)

    facts = await extractor.extract_and_store("turn-2", "Alice wrote code.")

    assert len(facts) == 1
    assert facts[0].predicate == "wrote"
    # Codex was called once and failed; Anthropic was called
    from orb.llm.types import OPENAI_CODEX_PROVIDER, ANTHROPIC_PROVIDER
    providers[OPENAI_CODEX_PROVIDER].complete.assert_called_once()
    providers[ANTHROPIC_PROVIDER].complete.assert_called_once()

    await store.close()


@pytest.mark.asyncio
async def test_both_providers_fail_raises():
    """Both Codex and Anthropic raise; RuntimeError is propagated."""
    store = ChromaDBNetworkXStore()
    providers = _make_providers(
        codex_raises=RuntimeError("codex down"),
        anthropic_raises=RuntimeError("anthropic down"),
    )
    extractor = FactExtractor(store, "agent-c", providers)

    with pytest.raises(RuntimeError, match="Fact extraction failed"):
        await extractor.extract_and_store("turn-3", "some content")

    await store.close()


@pytest.mark.asyncio
async def test_malformed_json_raises():
    """LLM returns non-JSON; RuntimeError is raised."""
    store = ChromaDBNetworkXStore()
    providers = _make_providers(codex_response="not valid json at all")
    extractor = FactExtractor(store, "agent-d", providers)

    with pytest.raises(RuntimeError, match="invalid JSON"):
        await extractor.extract_and_store("turn-4", "some content")

    await store.close()


@pytest.mark.asyncio
async def test_empty_json_array_returns_no_facts():
    """LLM returns []; no facts stored, no error raised."""
    store = ChromaDBNetworkXStore()
    providers = _make_providers(codex_response="[]")
    extractor = FactExtractor(store, "agent-e", providers)

    facts = await extractor.extract_and_store("turn-5", "some content")

    assert facts == []
    stored = await store.get_facts("agent-e")
    assert stored == []

    await store.close()


@pytest.mark.asyncio
async def test_partial_triple_skipped():
    """Triple missing 'predicate' and 'object' is skipped; 0 facts stored."""
    store = ChromaDBNetworkXStore()
    payload = json.dumps([{"subject": "x"}])  # missing predicate and object
    providers = _make_providers(codex_response=payload)
    extractor = FactExtractor(store, "agent-f", providers)

    facts = await extractor.extract_and_store("turn-6", "some content")

    assert facts == []
    stored = await store.get_facts("agent-f")
    assert stored == []

    await store.close()


@pytest.mark.asyncio
async def test_set_subgraph_store_passes_providers():
    """LLMAgent.set_subgraph_store passes self._providers to FactExtractor."""
    from orb.agent.llm_agent import LLMAgent
    from orb.agent.types import AgentConfig
    from orb.graph.graph import Graph
    from orb.messaging.bus import MessageBus
    from orb.messaging.channel import AgentChannel
    from orb.llm.types import ModelConfig, ModelTier

    graph = Graph()
    graph.add_node("agent-test")
    bus = MessageBus(graph)
    channel = AgentChannel()
    bus.register_channel("agent-test", channel)

    config = AgentConfig(node_id="agent-test", role="Tester", description="Test agent")
    mock_model = ModelConfig(tier=ModelTier.LOCAL_SMALL, model_id="mock", provider="mock")

    class _StubClient(LLMClient):
        async def complete(self, req): raise NotImplementedError
        async def close(self): pass

    providers = {"mock": _StubClient()}
    agent = LLMAgent(
        config, channel, bus, providers,
        model_overrides={t: mock_model for t in ModelTier},
    )

    assert agent._fact_extractor is None

    store = ChromaDBNetworkXStore()
    agent.set_subgraph_store(store)

    assert agent._fact_extractor is not None
    assert agent._fact_extractor._providers is providers
    assert agent._subgraph_store is store

    await store.close()
```

- [ ] **Step 2: Run tests to confirm they all fail (implementation not yet written)**

```bash
python -m pytest tests/test_fact_extractor.py -v 2>&1 | head -40
```

Expected: all 7 tests FAIL (ImportError or AttributeError — `FactExtractor` still has old signature)

---

### Task 2: Rewrite `orb/agent/fact_extractor.py`

- [ ] **Step 3: Replace `fact_extractor.py` with the LLM-based implementation**

```python
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
        request = CompletionRequest(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system="",
        )

        # Try Codex first
        if OPENAI_CODEX_PROVIDER in self._providers:
            try:
                codex_request = CompletionRequest(
                    messages=request.messages,
                    tools=[],
                    system="",
                    model_config=_CODEX_MODEL,
                )
                response = await self._providers[OPENAI_CODEX_PROVIDER].complete(codex_request)
                return response.content
            except Exception as exc:
                logger.debug("Codex fact extraction failed, trying Anthropic: %s", exc)

        # Fall back to Anthropic Sonnet
        if ANTHROPIC_PROVIDER in self._providers:
            try:
                anthropic_request = CompletionRequest(
                    messages=request.messages,
                    tools=[],
                    system="",
                    model_config=_ANTHROPIC_MODEL,
                )
                response = await self._providers[ANTHROPIC_PROVIDER].complete(anthropic_request)
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
            if not subject or not predicate or not obj:
                logger.debug(
                    "Skipping partial triple (missing field): %r", triple
                )
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
```

- [ ] **Step 4: Run the new tests**

```bash
python -m pytest tests/test_fact_extractor.py -v
```

Expected: **7/7 PASS**

---

### Task 3: Update `LLMAgent.set_subgraph_store` and fire-and-forget callback

- [ ] **Step 5: Update `set_subgraph_store` in `orb/agent/llm_agent.py`**

Find (lines 127–131):
```python
    def set_subgraph_store(self, store) -> None:
        """Attach a SubgraphStore and create a FactExtractor for this agent."""
        self._subgraph_store = store
        from .fact_extractor import FactExtractor
        self._fact_extractor = FactExtractor(store, self.node_id)
```

Replace with:
```python
    def set_subgraph_store(self, store) -> None:
        """Attach a SubgraphStore and create a FactExtractor for this agent."""
        self._subgraph_store = store
        from .fact_extractor import FactExtractor
        self._fact_extractor = FactExtractor(store, self.node_id, self._providers)
```

- [ ] **Step 6: Update the fire-and-forget block in `LLMAgent.process()` (lines 400–409)**

Find:
```python
        if self._fact_extractor and assistant_content:
            content_text = " ".join(
                block.get("text", "") for block in assistant_content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if content_text.strip():
                asyncio.create_task(
                    self._fact_extractor.extract_and_store(msg.id, content_text),
                    name=f"fact-extract-{self.node_id}-{msg.id[:8]}",
                )
```

Replace with:
```python
        if self._fact_extractor and assistant_content:
            content_text = " ".join(
                block.get("text", "") for block in assistant_content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if content_text.strip():
                _task = asyncio.create_task(
                    self._fact_extractor.extract_and_store(msg.id, content_text),
                    name=f"fact-extract-{self.node_id}-{msg.id[:8]}",
                )

                def _on_extraction_done(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.warning(
                            "Fact extraction failed for agent %s turn %s: %s",
                            self.node_id, msg.id[:8], exc,
                        )

                _task.add_done_callback(_on_extraction_done)
```

- [ ] **Step 7: Run the full test suite (excluding integration)**

```bash
python -m pytest tests/ --ignore=tests/integration -x -q
```

Expected: all tests pass, count ≥ 294

- [ ] **Step 8: Commit**

```bash
git add orb/agent/fact_extractor.py orb/agent/llm_agent.py tests/test_fact_extractor.py
git commit -m "feat: replace heuristic fact extraction with LLM-based extraction (Codex → Sonnet fallback)"
```

---

## Chunk 2: Cleanup and docs

### Task 4: Update phase log

- [ ] **Step 9: Update `phase_log.md` to record the LLM extraction upgrade**

Append to the bottom of `phase_log.md`:

```markdown
## LLM Fact Extraction Upgrade
**Status:** ✅ COMPLETE
**Date:** 2026-03-17

**Change:** Replaced heuristic regex extraction with LLM-based extraction.
- `orb/agent/fact_extractor.py` — full rewrite; `LLM_EXTRACTION_CONFIDENCE=0.9`; Codex→Sonnet fallback; raises RuntimeError on both failing
- `orb/agent/llm_agent.py` — `set_subgraph_store` passes `self._providers`; fire-and-forget task gets `.add_done_callback` for `logger.warning` on failure
- `tests/test_fact_extractor.py` — 7 new LLM mock tests replace 7 heuristic tests
```

- [ ] **Step 10: Final full suite run to confirm no regressions**

```bash
python -m pytest tests/ --ignore=tests/integration -q
```

Expected: all tests pass

- [ ] **Step 11: Commit**

```bash
git add phase_log.md
git commit -m "docs: record LLM fact extraction upgrade in phase log"
```
