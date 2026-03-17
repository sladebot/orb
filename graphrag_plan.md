# GraphRAG — Implementation Plan

## What GraphRAG Is Here

GraphRAG is a retrieval pattern, not a single tool. It replaces the flat
transcript window that each agent currently receives with structured fact
graphs. Instead of "here are the last N messages", agents get "here are the
relevant facts about the task, the agents involved, and prior work, retrieved
by walking a knowledge graph."

## Backend Strategy

Three backends, one interface (`SubgraphStore`). Switch with a single config
key. Default for local/private: ChromaDB + NetworkX. Default for cloud teams
with a shared graph: Zep Cloud.

| Backend              | Fit                                        | Notes                                   |
|----------------------|--------------------------------------------|-----------------------------------------|
| ChromaDB + NetworkX  | Local, offline, full control               | No external service needed              |
| Zep Cloud            | Managed, proven for live agent systems     | Python SDK, free tier works for dev     |
| Microsoft GraphRAG   | Static document corpora                    | Expensive, wrong fit for live systems   |

## Open Questions (decide before Phase 3)

1. **Fact extraction method**: LLM-based (accurate, costs tokens) vs rule-based
   heuristics (fast, cheap, less precise)?
2. **Merge conflict resolution**: Last-write-wins, or LLM arbitration for
   contradicting facts from different cluster agents?
3. **Graph TTL**: How long should facts live before expiry?
4. **Topology visibility**: Should BridgeAgent facts be visible to all agents
   or only to agents that share a bridge?
5. **Fallback**: If the graph store is unavailable, fall back to flat
   transcript window or hard fail?

---

## Phase 1 — Storage Layer (~3 days)

**Goal:** Define `SubgraphStore` interface and implement all three backends.
No changes to agents or context assembly yet.

**Scope:**
- `orb/memory/subgraph_store.py` — abstract base class
- `orb/memory/backends/chromadb_networkx.py` — local backend
- `orb/memory/backends/zep_cloud.py` — Zep Cloud backend (optional at runtime)
- `orb/memory/backends/microsoft_graphrag.py` — stub, disabled by default
- `orb/memory/factory.py` — instantiate backend from config key
- Tests for all backends (ChromaDB+NetworkX path must be runnable without
  external services)

**Exit criteria:**
- `SubgraphStore` interface is defined with `upsert_fact`, `get_facts`,
  `delete_facts`, `query` methods
- ChromaDB+NetworkX backend passes all tests locally
- Zep Cloud backend can connect and round-trip facts (skipped in CI without key)
- `SubgraphStoreFactory.from_config("chroma")` returns the right backend

---

## Phase 2 — Fact Extraction (~2 days)

**Goal:** After each agent turn, extract structured facts and write them to
the SubgraphStore. Fire-and-forget — never blocks the agent loop.

**Scope:**
- `orb/agent/fact_extractor.py` — lightweight post-turn extraction
- Hook into `LLMAgent.process()` after tool calls complete
- Facts schema: `{subject, predicate, object, agent_id, turn_id, confidence}`
- Extraction strategy: rule-based by default; optional LLM pass if
  `GRAPHRAG_EXTRACTION_LLM=1`

**Exit criteria:**
- After each completed agent turn, facts are written to SubgraphStore
- No blocking on the hot path — extraction runs as `asyncio.create_task`
- Facts from a full two-agent triad run are inspectable via CLI or test

---

## Phase 3 — Context Assembly Swap (~2 days)

**Goal:** Replace the flat shared transcript window with graph-retrieved
context when building model requests.

**Scope:**
- Extend `build_request_messages()` in `orb/agent/request_context.py` to
  accept an optional `SubgraphStore` and `agent_id`
- When store is set: query graph for relevant facts → format as context block
- When store is absent: fall back to existing flat transcript window
- `LLMAgent` receives the store via constructor injection

**Exit criteria:**
- Graph-retrieved context replaces transcript for agents with a store
- Flat transcript fallback works when store is `None`
- Model requests are verifiably different when graph has facts vs when empty

---

## Phase 4 — BridgeAgent (~3 days)

**Goal:** Background async agent that merges subgraphs from different
clusters on a timed interval.

**Scope:**
- `orb/agent/bridge_agent.py` — async background task
- Reads facts from two or more SubgraphStores and writes merged facts to a
  shared "bridge graph"
- Conflict resolution: last-write-wins (configurable)
- Runs every N seconds (default 30s), configurable via topology YAML
- Topology YAML gets a new `bridge` block:
  ```yaml
  bridge:
    interval_s: 30
    stores: [cluster_a, cluster_b]
    target_store: bridge_graph
  ```

**Exit criteria:**
- BridgeAgent merges two cluster graphs into a bridge graph on interval
- Merged facts are queryable by any agent with access to bridge graph
- No agent turn is blocked by bridge merge

---

## Phase 5 — Topology YAML + Scale Tests (~2 days)

**Goal:** Clusters and bridges declared in YAML; 20-agent load test passes.

**Scope:**
- Extend topology YAML schema with `clusters` and `bridge` blocks
- Topology loader creates SubgraphStores per cluster
- Load test: 20-agent graph, 3 clusters, 1 BridgeAgent, sustained 5-minute run
- Memory and latency targets: fact write < 50ms p99, query < 100ms p99

**Exit criteria:**
- Full YAML-driven topology with clusters and bridge compiles and runs
- 20-agent load test completes without OOM or deadlock
- p99 latency targets met for fact write and query

---

## Implementation Order

1. Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5

Each phase is a gate for the next. Do not start Phase 3 before deciding on
the open questions above.

## Definition of Done

- All five phases complete
- ChromaDB+NetworkX backend usable in local dev without any external service
- Zep Cloud backend usable with env var `ZEP_API_KEY`
- Each agent's model request visibly draws from graph-retrieved facts, not
  just a flat transcript window
- 20-agent topology runs without regression on existing tests
