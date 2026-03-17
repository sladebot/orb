# LLM-Based Fact Extraction — Design Spec

**Date:** 2026-03-17
**Status:** Approved

---

## Context

`FactExtractor` currently uses heuristic regex patterns to extract subject-predicate-object triples from agent turn content. The patterns are brittle and miss nuanced facts. This spec replaces the heuristic with an LLM call.

---

## Goal

Replace `_extract_facts_heuristic()` with an LLM-based extraction call. No heuristic fallback — fail loudly if the LLM is unavailable.

---

## Design

### Constructor

```python
FactExtractor(
    store: SubgraphStore,
    agent_id: str,
    providers: dict[str, LLMClient],   # required positional — not optional
)
```

`providers` is a required positional argument (not keyword-with-default) so the type system enforces it. `LLMAgent.set_subgraph_store()` holds `self._providers` and passes it through when constructing the extractor:

```python
self._fact_extractor = FactExtractor(store, self.node_id, self._providers)
```

### Extraction Flow

1. Build prompt (no system prompt, no tools):
   ```
   Extract up to 5 key facts from the following agent response as a JSON array
   of {"subject", "predicate", "object"} triples. Return only valid JSON, no prose.

   {content}
   ```
2. Construct `CompletionRequest(messages=[...], tools=[], system="", model_config=<see below>)`.
3. Try **OpenAI Codex** first — use `ModelConfig(CLOUD_FAST, "gpt-5.4", OPENAI_CODEX_PROVIDER, max_tokens=512)`. All Codex tiers resolve to the same model; `CLOUD_FAST` is used for consistency with the Anthropic fallback.
4. If `OPENAI_CODEX_PROVIDER` not in `providers` or call raises → try **Anthropic Sonnet** — use provider key constant `ANTHROPIC_PROVIDER`, `ModelConfig(CLOUD_FAST, ANTHROPIC_SONNET_MODEL, ANTHROPIC_PROVIDER, max_tokens=512)`.
5. If both fail → `raise RuntimeError("Fact extraction failed: both Codex and Anthropic unavailable")`.
6. Parse JSON response:
   - Valid JSON array → build `Fact` objects (see below)
   - Empty array `[]` → return `[]`, store nothing (success, not an error)
   - Malformed JSON → `raise RuntimeError(f"Fact extraction returned invalid JSON: {raw[:200]}")`
7. For each triple in the JSON array:
   - If any of `subject`, `predicate`, `object` keys is missing → skip that triple silently (log at DEBUG level)
   - Otherwise construct `Fact(id=uuid4().hex[:12], confidence=LLM_EXTRACTION_CONFIDENCE, metadata={"extraction_method": "llm"}, ...)`
8. Upsert all valid facts to `SubgraphStore`.

### Constants

```python
LLM_EXTRACTION_CONFIDENCE: float = 0.9
```

Defined at module level in `fact_extractor.py`. Higher than heuristic (0.7) because LLM extraction is more accurate.

### Error Surfacing (Fire-and-Forget Context)

Extraction runs as `asyncio.create_task` (fire-and-forget). Exceptions raised inside the task are silently swallowed by asyncio unless explicitly handled.

**Required pattern in `llm_agent.py`:**

```python
task = asyncio.create_task(
    self._fact_extractor.extract_and_store(msg.id, content_text),
    name=f"fact-extract-{self.node_id}-{msg.id[:8]}",
)

def _on_extraction_done(t: asyncio.Task) -> None:
    exc = t.exception()
    if exc:
        logger.warning(
            "Fact extraction failed for agent %s turn %s: %s",
            self.node_id, msg.id[:8], exc,
        )

task.add_done_callback(_on_extraction_done)
```

This routes extraction failures through the application's `logging` infrastructure immediately, not deferred to GC.

---

## What Is Removed

- `_extract_facts_heuristic()` — deleted entirely
- All regex patterns removed

---

## Files Changed

| File | Change |
|------|--------|
| `orb/agent/fact_extractor.py` | Replace heuristic with LLM call; add required `providers` positional arg; add `LLM_EXTRACTION_CONFIDENCE` constant |
| `orb/agent/llm_agent.py` | Pass `self._providers` to `FactExtractor`; add `.add_done_callback` for error logging |
| `tests/test_fact_extractor.py` | Replace heuristic tests with LLM mock tests |

---

## Tests

| Test | What it verifies |
|------|-----------------|
| `test_llm_extraction_returns_facts` | Mock Codex response with valid JSON, assert facts parsed and stored correctly |
| `test_codex_fallback_to_sonnet` | Mock Codex to raise, assert Sonnet is called |
| `test_both_providers_fail_raises` | Mock both to raise, assert `RuntimeError` |
| `test_malformed_json_raises` | Mock LLM to return non-JSON, assert `RuntimeError` |
| `test_empty_json_array_returns_no_facts` | Mock LLM to return `[]`, assert return value is `[]` and no upsert called |
| `test_partial_triple_skipped` | Mock LLM to return `[{"subject": "x"}]` (missing predicate/object), assert 0 facts stored |
| `test_set_subgraph_store_passes_providers` | Call `agent.set_subgraph_store(store)`, assert `agent._fact_extractor._providers` is `agent._providers` |

---

## Not In Scope

- Prompt tuning / few-shot examples (can be added later)
- Configurable model selection per agent (always Codex → Sonnet for now)
- Confidence varying by provider (fixed at 0.9 for all LLM extraction)
