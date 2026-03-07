# Orb — Sprint Tracker

Weekly sprints. Each sprint runs Monday–Friday.

---

## Status Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Complete |
| 🔄 | In Progress |
| ⏳ | Not Started |
| ❌ | Blocked |

---

## Sprint 1 — Core Infrastructure
**Week of Feb 3, 2026** | Status: ✅ Complete

**Goal:** Graph, message types, channel-based message bus, and agent base class. No LLM calls — pure infrastructure.

### Team Graph — Graph & Types

| ID | Task | File | Status |
|----|------|------|--------|
| G1.1 | `NodeId` type alias and `Edge` dataclass with undirected equality | `orb/graph/types.py` | ✅ |
| G1.2 | `Graph` class — adjacency dict, add/remove nodes/edges, neighbor queries | `orb/graph/graph.py` | ✅ |
| G1.3 | Graph unit tests — add/remove nodes, neighbors, edge validation, triangle | `tests/test_graph.py` | ✅ |

### Team Graph — Messaging

| ID | Task | File | Status |
|----|------|------|--------|
| G1.4 | `Message` dataclass — id, chain_id, from_, to, type, depth, payload, context_slice, metadata, timestamp. `reply()` helper. `MessageType` enum | `orb/messaging/message.py` | ✅ |
| G1.5 | `AgentChannel` — asyncio.Queue wrapper with sentinel-based close | `orb/messaging/channel.py` | ✅ |
| G1.6 | Middleware: `HopCounter`, `BudgetTracker`, `CooldownTracker` | `orb/messaging/middleware.py` | ✅ |
| G1.7 | `MessageBus` — graph-aware routing with middleware and event listeners | `orb/messaging/bus.py` | ✅ |
| G1.8 | Channel tests — async put/get, FIFO, backpressure, close behavior | `tests/test_channel.py` | ✅ |
| G1.9 | Bus tests — routing, non-edge rejection, hop/budget/cooldown, listeners | `tests/test_bus.py` | ✅ |

### Team Agent — Agent Base

| ID | Task | File | Status |
|----|------|------|--------|
| A1.1 | `AgentConfig` and `AgentStatus` enum | `orb/agent/types.py` | ✅ |
| A1.2 | `AgentNode` abstract base — channel receive loop, `start()`, `stop()`, `send()` | `orb/agent/base.py` | ✅ |

### Team Agent — Memory Graph

| ID | Task | File | Status |
|----|------|------|--------|
| A1.3 | `MemoryNode` and `MemoryEdge` dataclasses | `orb/memory/memory_node.py` | ✅ |
| A1.4 | `MemoryGraph` — add/get/remove nodes+edges, BFS traversal, query by type | `orb/memory/memory_graph.py` | ✅ |
| A1.5 | `retriever.py` — `get_relevant_context()` via BFS from a memory node | `orb/memory/retriever.py` | ✅ |
| A1.6 | Memory graph tests — CRUD, edges, BFS depth, type queries, update | `tests/test_memory_graph.py` | ✅ |

### Definition of Done
- [x] `pytest tests/test_graph.py tests/test_channel.py tests/test_bus.py tests/test_memory_graph.py` — all pass
- [x] Graph supports undirected edges with triangle topology
- [x] Channels support async send/receive with backpressure and close semantics
- [x] Bus validates edges, enforces hop limit / budget / cooldown
- [x] Memory graph supports BFS traversal and typed node queries
- [x] `AgentNode` abstract class compiles and can be subclassed

---

## Sprint 2 — LLM Integration
**Week of Feb 10, 2026** | Status: ✅ Complete

**Goal:** Wire LLM providers into agent nodes. Agents can call LLMs and parse tool_use responses.

### Team LLM — Client Protocol & Types

| ID | Task | File | Status |
|----|------|------|--------|
| L2.1 | `ModelTier` enum, `ModelConfig`, `CompletionRequest`, `CompletionResponse`, `ToolCall`, `DEFAULT_MODELS` | `orb/llm/types.py` | ✅ |
| L2.2 | `LLMClient` abstract base — `async complete()`, `async close()` | `orb/llm/client.py` | ✅ |

### Team LLM — Providers

| ID | Task | File | Status |
|----|------|------|--------|
| L2.3a | `AnthropicProvider` — wraps `anthropic.AsyncAnthropic`, extracts tool_use blocks | `orb/llm/providers.py` | ✅ |
| L2.3b | `OpenAIProvider` — wraps `openai.AsyncOpenAI`, converts Anthropic-style tool schema to OpenAI format | `orb/llm/providers.py` | ✅ |
| L2.3c | `OllamaProvider` — httpx against Ollama `/api/chat`, handles tool call extraction | `orb/llm/providers.py` | ✅ |

### Team LLM — Model Selection

| ID | Task | File | Status |
|----|------|------|--------|
| L2.4 | `ModelSelector` — complexity scoring (base + depth + type + payload + hints + retries) → `ModelTier` | `orb/llm/model_selector.py` | ✅ |
| L2.5 | Model selector tests — tier assignments per bracket, escalation, reset | `tests/test_model_selector.py` | ✅ |

### Team Agent — Tools & Prompts

| ID | Task | File | Status |
|----|------|------|--------|
| A2.1 | `send_message` and `complete_task` tool schemas | `orb/agent/tools.py` | ✅ |
| A2.2 | `ConversationHistory` — add_user/assistant/tool_result, get_messages, trim | `orb/agent/conversation.py` | ✅ |
| A2.3 | `build_system_prompt()` — role, description, neighbors, communication rules | `orb/agent/prompt_builder.py` | ✅ |
| A2.4 | Prompt builder tests — all sections present, neighbors listed | `tests/test_prompt_builder.py` | ✅ |

### Team Agent — LLM Agent

| ID | Task | File | Status |
|----|------|------|--------|
| A2.5 | `LLMAgent` — full pipeline: format → memory → model select → LLM call → tool handling → history | `orb/agent/llm_agent.py` | ✅ |
| A2.6 | LLM agent tests with `MockLLMClient` — text response, send_message routing, complete_task, non-neighbor rejection | `tests/test_claude_agent.py` | ✅ |

### Definition of Done
- [x] `pytest tests/test_model_selector.py tests/test_prompt_builder.py tests/test_claude_agent.py` — all pass
- [x] All three LLM providers conform to `LLMClient` protocol
- [x] Model selector routes correctly across all 5 tiers
- [x] LLM agent processes messages end-to-end with mock client
- [x] Tool calls (`send_message`, `complete_task`) correctly parsed and handled
- [x] Context sharing works — `send_message` context flows to recipient

---

## Sprint 3 — Triangle Demo & Orchestration
**Week of Feb 17, 2026** | Status: ✅ Complete

**Goal:** 3-agent triangle topology running end-to-end with orchestration, tracing, and lifecycle management.

### Team Platform — Tracing

| ID | Task | File | Status |
|----|------|------|--------|
| P3.1 | `EventLogger` — bus event subscriber, rich-formatted real-time trace with timing and model info | `orb/tracing/logger.py` | ✅ |

### Team Platform — Orchestrator

| ID | Task | File | Status |
|----|------|------|--------|
| P3.2 | `OrchestratorConfig` and `RunResult` types | `orb/orchestrator/types.py` | ✅ |
| P3.3 | `Orchestrator` — lifecycle: wire callbacks, start agents, inject task, wait for completion/timeout, collect results | `orb/orchestrator/orchestrator.py` | ✅ |
| P3.4 | Orchestrator tests — normal run, timeout, missing entry agent | `tests/test_orchestrator.py` | ✅ |

### Team Platform — Triangle Topology

| ID | Task | File | Status |
|----|------|------|--------|
| P3.5 | Agent configs for Coder (50), Reviewer (60), Tester (30) with role descriptions | `orb/topologies/triangle.py` | ✅ |
| P3.6 | `create_triangle()` factory — builds graph + bus + agents + orchestrator | `orb/topologies/triangle.py` | ✅ |
| P3.7 | Triangle topology tests — 3 agents, fully connected, tools and prompts initialized | `tests/test_triangle.py` | ✅ |

### Team Platform — Integration Test

| ID | Task | File | Status |
|----|------|------|--------|
| P3.8 | E2E integration test — gated on `ANTHROPIC_API_KEY`, fibonacci task through full triangle | `tests/integration/test_e2e.py` | ✅ |

### Definition of Done
- [x] `pytest tests/test_triangle.py tests/test_orchestrator.py` — all pass
- [x] Triangle topology creates fully-connected 3-agent graph
- [x] Orchestrator manages lifecycle: inject task → agents collaborate → collect results
- [x] Tracing prints real-time message flow with model info and timing
- [x] E2E test passes with live Anthropic API
- [x] Completion: all agents call `complete_task` OR budget exhausted OR timeout

---

## Sprint 4 — CLI Interface
**Week of Feb 24, 2026** | Status: ✅ Complete

**Goal:** User-facing CLI with single-query and interactive REPL modes.

### Team Platform — Display & REPL

| ID | Task | File | Status |
|----|------|------|--------|
| P4.1 | Terminal display helpers using `rich` — `print_header()`, `print_result()`, `print_error()` | `orb/cli/display.py` | ✅ |
| P4.2 | Interactive REPL — prompt loop, fresh triangle per query, quit/exit support | `orb/cli/repl.py` | ✅ |

### Team Platform — CLI Entry Point

| ID | Task | File | Status |
|----|------|------|--------|
| P4.3 | `build_providers()` — constructs providers from env vars and `--local-only`/`--cloud-only` flags | `orb/cli/main.py` | ✅ |
| P4.4 | Argument parser — query, `-i`, `--trace`, `--budget`, `--timeout`, `--max-depth`, `--model`, `--local-only`, `--cloud-only` | `orb/cli/main.py` | ✅ |
| P4.5 | `async_main()` — wire args to config, single-query or REPL, tier/model overrides | `orb/cli/main.py` | ✅ |
| P4.6 | `__main__.py` entry point and `project.scripts` wiring | `orb/__main__.py`, `pyproject.toml` | ✅ |

### Definition of Done
- [x] `python -m orb "Write a fibonacci function"` runs successfully
- [x] `python -m orb -i` opens interactive REPL
- [x] `--trace` shows real-time message routing with timing and model info
- [x] `--local-only` and `--cloud-only` constrain model selection correctly
- [x] `--budget` and `--timeout` are respected
- [x] `--model` overrides the default cloud model
- [x] Error messages shown when no providers are available

---

## Sprint 5 — Live Web Dashboard
**Week of Mar 3, 2026** | Status: ✅ Complete

**Goal:** Real-time web dashboard visualizing the agent graph, message flow, and agent status as agents collaborate.

### Team Platform — WebSocket Server

| ID | Task | File | Status |
|----|------|------|--------|
| W5.1 | `DashboardServer` — aiohttp WebSocket server, endpoints: `GET /`, `WS /ws`, `GET /api/state` | `web/server.py` | ✅ |
| W5.2 | `DashboardBridge` — tracing-to-dashboard adapter, converts events to dashboard JSON | `web/bridge.py` | ✅ |
| W5.3 | `DashboardState` — full system state snapshot (graph, agent statuses, message log, budget, timer) | `web/state.py` | ✅ |

### Team Frontend — Graph Visualization

| ID | Task | File | Status |
|----|------|------|--------|
| W5.4 | HTML shell — graph view, message log sidebar, agent detail panel, stats bar | `web/static/index.html` | ✅ |
| W5.5 | CSS — dark theme, agent color coding, animated edges, status indicators | `web/static/style.css` | ✅ |
| W5.6 | Canvas graph renderer — node-link diagram, status rings, edge animation on message flow | `web/static/graph.js` | ✅ |
| W5.7 | WebSocket client — connects to `/ws`, dispatches events, handles reconnection | `web/static/app.js` | ✅ |
| W5.8 | Message log panel — scrolling list, timestamp, from/to, model, payload preview, expand | `web/static/app.js` | ✅ |
| W5.9 | Stats bar — live messages sent, budget remaining, elapsed time, completion status | `web/static/app.js` | ✅ |

### Team Platform — CLI Integration

| ID | Task | File | Status |
|----|------|------|--------|
| W5.10 | `--dashboard` flag — starts web server, opens browser, attaches dashboard bridge | `orb/cli/main.py` | ✅ |
| W5.11 | `--dashboard-port` flag for custom port | `orb/cli/main.py` | ✅ |

### Definition of Done
- [x] `orb --dashboard "Write a fibonacci function"` opens browser with live dashboard
- [x] Graph shows 3 agents in triangle layout with role labels
- [x] Edges animate when messages flow between agents
- [x] Agent nodes change color based on status (idle/running/completed)
- [x] Message log shows real-time message flow with model info
- [x] Stats bar shows live budget, timer, and completion status
- [x] Dashboard works with page refresh (full state on reconnect)
- [x] No external frontend build tools — vanilla HTML/CSS/JS

---

## Sprint 6 — TBD
**Week of Mar 10, 2026** | Status: ⏳ Not Started

> Backlog items to consider for the next sprint. Reprioritize before the sprint starts.

### Potential Items

| ID | Item | Area | Priority |
|----|------|------|----------|
| S6.1 | Dynamic topology — support >3 agents and non-triangle graphs | Core | High |
| S6.2 | Streaming LLM responses — token-level streaming to dashboard | LLM | Medium |
| S6.3 | Persistent memory — serialize/deserialize `MemoryGraph` to disk | Memory | Medium |
| S6.4 | Cross-agent memory queries — agents can query each other's memory | Memory | Low |
| S6.5 | Memory compaction/summarization — prune old memory nodes via LLM | Memory | Low |
| S6.6 | Agent detail panel — click node to view full conversation history | Dashboard | Medium |
| S6.7 | Replay mode — record a run and replay in dashboard | Dashboard | Low |
| S6.8 | More topologies — star, chain, hub-and-spoke factories | Topology | Medium |
| S6.9 | Typed memory nodes with embeddings for semantic retrieval | Memory | Low |
| S6.10 | Cost tracking — track token usage and estimated cost per run | LLM | Medium |
