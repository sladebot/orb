# GraphRAG — Phase Log

## Phase 1 — Storage Layer
**Status:** ✅ COMPLETE
**Started:** 2026-03-16
**Completed:** 2026-03-16

**Delivered:**
- `orb/memory/subgraph_store.py` — `Fact` dataclass + `SubgraphStore` ABC (upsert_fact, get_facts, delete_facts, query, close)
- `orb/memory/backends/__init__.py`
- `orb/memory/backends/chromadb_networkx.py` — ephemeral + persistent modes; run_in_executor for blocking calls; unique collection name per instance for test isolation
- `orb/memory/backends/zep_cloud.py` — stub; validates api_key before checking package install; all data methods raise NotImplementedError
- `orb/memory/backends/microsoft_graphrag.py` — stub; raises NotImplementedError in __init__ with redirect to chroma/zep
- `orb/memory/factory.py` — SubgraphStoreFactory.from_config("chroma"|"zep")
- `tests/test_subgraph_store.py` — 8/8 tests pass, no external services required
- `pyproject.toml` — added chromadb>=1.0.0, networkx>=3.0

**Notes:** ChromaDB EphemeralClient shares SQLite state across instances; fixed by unique collection name per store instance.

---

## Phase 2 — Fact Extraction
**Status:** ✅ COMPLETE
**Started:** 2026-03-16
**Completed:** 2026-03-16

**Delivered:**
- `orb/agent/fact_extractor.py` — FactExtractor with 4 heuristic patterns (tool use, completion, sent message, decision); confidence=0.7; fire-and-forget asyncio.create_task
- `orb/agent/llm_agent.py` — `set_subgraph_store()` method + post-tool-loop extraction hook
- `tests/test_fact_extractor.py` — 7/7 tests pass
- Full suite: 266/266 pass, no regressions

## Phase 3 — Context Assembly Swap
**Status:** ✅ COMPLETE
**Started:** 2026-03-16
**Completed:** 2026-03-16

**Delivered:**
- `orb/agent/request_context.py` — `build_graph_context_block()` (async, deduplicates query+get_facts results) + `build_request_messages_with_graph()` (4-way branch: both/graph-only/transcript-only/neither)
- `orb/agent/llm_agent.py` — 3 call sites updated; graph context awaited in process() and both nudge-loop continuations
- `tests/test_request_context.py` — 10/10 tests pass
- Full suite: 276/276 pass, no regressions

**Graph vs transcript:** When facts present, model sees structured subject-predicate-object triples above a `--- Session context ---` separator. When store empty, falls back to original flat transcript behavior.

## Phase 4 — BridgeAgent
**Status:** ✅ COMPLETE
**Started:** 2026-03-16
**Completed:** 2026-03-16

**Delivered:**
- `orb/agent/bridge_agent.py` — BridgeAgent with start/stop/merge_once; crash-safe loop; last_write_wins on (subject, predicate, object, agent_id) key
- `orb/memory/subgraph_store.py` — added abstract `get_all_facts(limit)` to ABC
- `orb/memory/backends/chromadb_networkx.py` — implemented get_all_facts (no where filter, newest-first)
- `orb/memory/backends/zep_cloud.py`, `microsoft_graphrag.py` — get_all_facts stubs
- `tests/test_bridge_agent.py` — 6/6 tests pass
- Full suite: 282/282 pass, no regressions

## Phase 5 — Topology YAML + Scale Tests
**Status:** ✅ COMPLETE
**Started:** 2026-03-16
**Completed:** 2026-03-16

**Delivered:**
- `orb/topologies/schema.py` — ClusterSchema + BridgeSchema Pydantic models; clusters/bridge fields on TopologySchema; cross-reference validation
- `orb/memory/graphrag_config.py` — GraphRAGConfig.from_topology() wires stores + BridgeAgent from parsed topology
- `orb/sample-graphrag-topology.yaml` — 5-agent, 3-cluster, 1-bridge example
- `tests/test_topology_schema_graphrag.py` — 12/12 pass
- `tests/test_scale_graphrag.py` — 4/4 pass
- Full suite: 294/294 pass (excl. scale), no regressions

**Latency observed:**
- p99 write (post-warmup): ~44ms (target: <50ms) ✅
- p99 query: ~15ms (target: <100ms) ✅

**Cleanup (post-phase):**
- `orb/memory/backends/microsoft_graphrag.py` — deleted (user request)
- `orb/memory/factory.py` — microsoft_graphrag branch removed
- `tests/test_subgraph_store.py` — MS GraphRAG test removed

---

## Phase 3 — Context Assembly Swap
**Status:** ⏳ NOT STARTED
**Target:** ~2 days
**Blocked on:** Open questions from graphrag_plan.md (fact extraction method, conflict resolution, TTL)

---

## Phase 4 — BridgeAgent
**Status:** ⏳ NOT STARTED
**Target:** ~3 days

---

## Phase 5 — Topology YAML + Scale Tests
**Status:** ⏳ NOT STARTED
**Target:** ~2 days

---

## LLM Fact Extraction Upgrade
**Status:** ✅ COMPLETE
**Date:** 2026-03-17

**Change:** Replaced heuristic regex extraction with LLM-based extraction.
- `orb/agent/fact_extractor.py` — full rewrite; `LLM_EXTRACTION_CONFIDENCE=0.9`; Codex→Sonnet fallback; raises RuntimeError on both failing; `is None` check for partial triples
- `orb/agent/llm_agent.py` — `set_subgraph_store` passes `self._providers`; fire-and-forget task gets `.add_done_callback` with `t.cancelled()` guard for `logger.warning` on failure
- `tests/test_fact_extractor.py` — 8 new LLM mock tests (7 from spec + anthropic-only provider path) replace 7 heuristic tests
