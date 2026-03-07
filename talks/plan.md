# Orb — Sprint Plan

## Overview

An LLM agent harness where agents are **graph nodes** (not a hierarchy) with **bidirectional communication** via Go-style async channels. The first demo is 3 agents (Coder, Reviewer, Tester) in a fully-connected triangle.

**Stack:** Python 3.12, asyncio, multi-provider LLM (Anthropic, OpenAI, Ollama)
**Environment:** conda (`orb`)

---

## Team Structure

| Team | Scope |
|------|-------|
| **Team Graph** | Graph data structures, messaging infrastructure, middleware |
| **Team LLM** | LLM client protocol, provider implementations, model selection |
| **Team Agent** | Agent base class, conversation management, prompt engineering, tools |
| **Team Platform** | Orchestrator, topologies, tracing, CLI |

---

## Sprint 1 — Core Infrastructure

**Goal:** Graph, message types, channel-based message bus, and agent base class. No LLM calls yet — pure infrastructure.

### Team Graph — Graph & Types

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| G1.1 | Implement `NodeId` type alias and `Edge` dataclass with undirected equality | `orb/graph/types.py` | — |
| G1.2 | Implement `Graph` class — adjacency dict, `add_node`, `add_edge`, `remove_node`, `remove_edge`, `get_neighbors`, `has_edge`, `nodes`/`edges` properties | `orb/graph/graph.py` | G1.1 |
| G1.3 | Write graph unit tests — add/remove nodes, neighbor queries, edge validation, triangle topology | `tests/test_graph.py` | G1.2 |

### Team Graph — Messaging

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| G1.4 | Implement `Message` dataclass — id, chain_id, from_, to, type, depth, payload, context_slice, metadata, timestamp. Include `reply()` helper. Define `MessageType` enum (SYSTEM, TASK, RESPONSE, FEEDBACK, COMPLETE) | `orb/messaging/message.py` | — |
| G1.5 | Implement `AgentChannel` — thin wrapper around `asyncio.Queue(maxsize=32)`. Methods: `send(msg)`, `receive()`, `close()`. Sentinel-based close to unblock waiters | `orb/messaging/channel.py` | G1.4 |
| G1.6 | Implement middleware: `HopCounter` (reject if depth > max), `BudgetTracker` (global message count), `CooldownTracker` (per-sender-target-chain limit) | `orb/messaging/middleware.py` | G1.4 |
| G1.7 | Implement `MessageBus` — holds Graph + dict of channels. `route(msg)` validates edge exists, runs middleware checks, delivers to destination channel. Supports event listeners for tracing | `orb/messaging/bus.py` | G1.2, G1.5, G1.6 |
| G1.8 | Write channel tests — async put/get, FIFO ordering, backpressure when full, close behavior | `tests/test_channel.py` | G1.5 |
| G1.9 | Write bus tests — routing along edges, rejection of non-edge messages, hop limit, budget exhaustion, cooldown, event listeners | `tests/test_bus.py` | G1.7 |

### Team Agent — Agent Base

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| A1.1 | Define `AgentConfig` (node_id, role, description, base_complexity, max_history) and `AgentStatus` enum (IDLE, RUNNING, COMPLETED, ERROR) | `orb/agent/types.py` | — |
| A1.2 | Implement `AgentNode` abstract base class — owns a channel, runs as `asyncio.Task` in a receive loop. Abstract `process(msg)` method. `start()`, `stop()`, `send()` helpers | `orb/agent/base.py` | A1.1, G1.5, G1.7 |

### Team Agent — Memory Graph

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| A1.3 | Define `MemoryNode` (id, content, node_type, created_at, updated_at) and `MemoryEdge` (from_id, to_id, relation) dataclasses | `orb/memory/memory_node.py` | — |
| A1.4 | Implement `MemoryGraph` — per-agent graph-structured memory store. Operations: `add_node`, `add_edge`, `get_node`, `get_connected(id, depth)` via BFS, `query_by_type`, `remove_node`, `update_node` | `orb/memory/memory_graph.py` | A1.3 |
| A1.5 | Implement `retriever.py` — `get_relevant_context()` traverses memory graph from a node, returns most recently updated nodes within depth | `orb/memory/retriever.py` | A1.4 |
| A1.6 | Write memory graph tests — add/get/remove nodes, edges, BFS traversal at various depths, query by type, update | `tests/test_memory_graph.py` | A1.4 |

### Sprint 1 — Parallel Execution Map

```
Team Graph                          Team Agent
──────────                          ──────────
G1.1 ──→ G1.2 ──→ G1.3             A1.1 (parallel)
  │                                   │
G1.4 ──→ G1.5 ──→ G1.8             A1.3 ──→ A1.4 ──→ A1.5
  │         │                                    │
  │       G1.6                                 A1.6
  │         │
  └────→ G1.7 ──→ G1.9
                    │
               A1.2 (needs G1.5 + G1.7 + A1.1)
```

### Sprint 1 — Definition of Done

- [ ] `pytest tests/test_graph.py tests/test_channel.py tests/test_bus.py tests/test_memory_graph.py` — all pass
- [ ] Graph supports undirected edges with triangle topology
- [ ] Channels support async send/receive with backpressure and close semantics
- [ ] Bus validates edges, enforces hop limit / budget / cooldown
- [ ] Memory graph supports BFS traversal and typed node queries
- [ ] AgentNode abstract class compiles and can be subclassed

---

## Sprint 2 — LLM Integration

**Goal:** Wire LLM providers into agent nodes. Agents can call LLMs and parse tool_use responses.

### Team LLM — Client Protocol & Types

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| L2.1 | Define `ModelTier` enum (LOCAL_SMALL, LOCAL_MEDIUM, LOCAL_LARGE, CLOUD_FAST, CLOUD_STRONG), `ModelConfig`, `CompletionRequest`, `CompletionResponse`, `ToolCall` dataclasses. Include `DEFAULT_MODELS` mapping | `orb/llm/types.py` | — |
| L2.2 | Define `LLMClient` abstract base class — `async complete(request) -> CompletionResponse`, `async close()` | `orb/llm/client.py` | L2.1 |

### Team LLM — Providers (parallelizable)

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| L2.3a | Implement `AnthropicProvider` — wraps `anthropic.AsyncAnthropic`, converts response blocks to `CompletionResponse` with `ToolCall` extraction | `orb/llm/providers.py` | L2.2 |
| L2.3b | Implement `OpenAIProvider` — wraps `openai.AsyncOpenAI`, converts Anthropic-style tool schema to OpenAI function format, parses function call responses | `orb/llm/providers.py` | L2.2 |
| L2.3c | Implement `OllamaProvider` — uses `httpx.AsyncClient` against Ollama HTTP API (`/api/chat`), handles tool call extraction from response | `orb/llm/providers.py` | L2.2 |

### Team LLM — Model Selection

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| L2.4 | Implement `ModelSelector` — scores task complexity based on: base score (from agent role), message depth (+15 if >3), message type (+20 for feedback), payload size (+10 if >2000 chars), explicit hints, retry escalation (+10 per retry). Maps score to `ModelTier` | `orb/llm/model_selector.py` | L2.1, Sprint 1 G1.4 |
| L2.5 | Write model selector tests — verify tier assignments at each complexity bracket, escalation behavior, reset | `tests/test_model_selector.py` | L2.4 |

### Team Agent — Tools & Prompts

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| A2.1 | Implement `send_message` tool schema — dynamic `to` enum from neighbors, `content` string, optional `context` array. Implement `complete_task` tool schema — `result` string | `orb/agent/tools.py` | — |
| A2.2 | Implement `ConversationHistory` — per-agent message history with `add_user`, `add_assistant`, `add_tool_result`, `get_messages`, `_trim` (keeps first message + most recent N) | `orb/agent/conversation.py` | — |
| A2.3 | Implement `build_system_prompt()` — takes role, description, neighbor map; generates prompt with role description, neighbor list, communication rules, context sharing guidelines, completion instructions | `orb/agent/prompt_builder.py` | — |
| A2.4 | Write prompt builder tests — verify all sections present, neighbors listed | `tests/test_prompt_builder.py` | A2.3 |

### Team Agent — LLM Agent

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| A2.5 | Implement `LLMAgent(AgentNode)` — full message processing pipeline: format incoming message → store in memory graph → select model tier → call provider → parse tool calls → handle `send_message` (route via bus) and `complete_task` (signal orchestrator) → manage conversation history | `orb/agent/llm_agent.py` | A1.2, A2.1, A2.2, A2.3, L2.2, L2.4, A1.4 |
| A2.6 | Write LLM agent tests with `MockLLMClient` — text response processing, send_message tool routing, complete_task signaling, non-neighbor rejection, context passthrough | `tests/test_claude_agent.py` | A2.5 |

### Sprint 2 — Parallel Execution Map

```
Team LLM                           Team Agent
─────────                          ──────────
L2.1 ──→ L2.2 ──→ L2.3a            A2.1 (parallel)
              │──→ L2.3b            A2.2 (parallel)
              │──→ L2.3c            A2.3 ──→ A2.4
              │
         L2.4 ──→ L2.5
                                    A2.5 (needs L2.2 + L2.4 + A2.1-A2.3 + Sprint 1)
                                      │
                                    A2.6
```

### Sprint 2 — Definition of Done

- [ ] `pytest tests/test_model_selector.py tests/test_prompt_builder.py tests/test_claude_agent.py` — all pass
- [ ] All three LLM providers compile and conform to `LLMClient` protocol
- [ ] Model selector routes correctly across all 5 tiers
- [ ] LLM agent processes messages end-to-end with mock client
- [ ] Tool calls (`send_message`, `complete_task`) correctly parsed and handled
- [ ] Context sharing works — `send_message` context flows to recipient

---

## Sprint 3 — Triangle Demo & Orchestration

**Goal:** 3-agent triangle topology running end-to-end with orchestration, tracing, and lifecycle management.

### Team Platform — Tracing

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P3.1 | Implement `EventLogger` — subscribes to bus events, prints real-time trace with rich formatting: `[elapsed] Agent (model) -> Agent: "preview..."`. Color-coded per agent role. Stores event history for inspection | `orb/tracing/logger.py` | Sprint 1 G1.4 |

### Team Platform — Orchestrator

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P3.2 | Define `OrchestratorConfig` (timeout, budget, max_depth, max_cooldown, entry_agent) and `RunResult` (success, completions dict, message_count, timed_out, error) | `orb/orchestrator/types.py` | — |
| P3.3 | Implement `Orchestrator` — manages agent lifecycle: wires completion callbacks, starts all agents as tasks, injects initial query to entry agent, waits for all agents to complete OR timeout, stops all agents, collects results | `orb/orchestrator/orchestrator.py` | P3.2, Sprint 2 A2.5, P3.1 |
| P3.4 | Write orchestrator tests — basic run with all agents completing, timeout scenario, missing entry agent | `tests/test_orchestrator.py` | P3.3 |

### Team Platform — Triangle Topology

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P3.5 | Define agent configs for Coder (base_complexity=50), Reviewer (base_complexity=60), Tester (base_complexity=30) with role-specific descriptions and context sharing guidance | `orb/topologies/triangle.py` | Sprint 2 A2.5 |
| P3.6 | Implement `create_triangle()` factory — builds graph with 3 nodes + 3 edges, creates bus with middleware, instantiates LLMAgent per node, initializes with neighbor roles, returns configured Orchestrator | `orb/topologies/triangle.py` | P3.3, P3.5 |
| P3.7 | Write triangle topology tests — verify 3 agents, fully connected graph, agents have tools and system prompts | `tests/test_triangle.py` | P3.6 |

### Team Platform — Integration Test

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P3.8 | Write E2E integration test — gated behind `ANTHROPIC_API_KEY` env var, runs fibonacci task through full triangle, verifies completions and message count | `tests/integration/test_e2e.py` | P3.6 |

### Sprint 3 — Parallel Execution Map

```
Team Platform
─────────────
P3.1 (parallel)      P3.2 (parallel)
                       │
                     P3.3 ←── P3.1
                       │
P3.5 (parallel)      P3.4
  │
P3.6 ←── P3.3
  │
P3.7
  │
P3.8
```

### Agent Roles Reference

| Agent | Role | Sends To | Shares |
|-------|------|----------|--------|
| **Coder** | Writes code | Reviewer: code + requirements | Tester: code + expected behavior |
| **Reviewer** | Reviews code | Coder: specific feedback + suggestions | Tester: review concerns to test for |
| **Tester** | Tests code | Coder: failing test cases + errors | Reviewer: test coverage summary |

### Sprint 3 — Definition of Done

- [ ] `pytest tests/test_triangle.py tests/test_orchestrator.py` — all pass
- [ ] Triangle topology creates fully-connected 3-agent graph
- [ ] Orchestrator manages lifecycle: inject task → agents collaborate → collect results
- [ ] Tracing prints real-time message flow with model info and timing
- [ ] E2E test passes with live Anthropic API (manual verification)
- [ ] Completion: all agents call `complete_task` OR budget exhausted OR timeout

---

## Sprint 4 — CLI Interface

**Goal:** User-facing CLI with single-query and interactive REPL modes.

### Team Platform — Display

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P4.1 | Implement terminal display helpers using `rich` — `print_header()` (banner), `print_result()` (per-agent result panels, color-coded), `print_error()` | `orb/cli/display.py` | — |

### Team Platform — REPL

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P4.2 | Implement interactive REPL — loop with prompt, creates fresh triangle per query, prints results, supports quit/exit | `orb/cli/repl.py` | P4.1, Sprint 3 P3.6 |

### Team Platform — CLI Entry Point

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| P4.3 | Implement `build_providers()` — constructs available providers based on env vars and `--local-only`/`--cloud-only` flags | `orb/cli/main.py` | Sprint 2 L2.3a-c |
| P4.4 | Implement argument parser — positional query, `-i` interactive, `--trace`/`--no-trace`, `--budget`, `--timeout`, `--max-depth`, `--model`, `--local-only`, `--cloud-only` | `orb/cli/main.py` | — |
| P4.5 | Implement `async_main()` — wire args to config, route to single-query or REPL mode, handle tier overrides and model overrides | `orb/cli/main.py` | P4.2, P4.3, P4.4 |
| P4.6 | Create `__main__.py` entry point and wire `project.scripts` in pyproject.toml | `orb/__main__.py`, `pyproject.toml` | P4.5 |

### Sprint 4 — Parallel Execution Map

```
Team Platform
─────────────
P4.1 (parallel)    P4.4 (parallel)    P4.3 (parallel)
  │                  │                   │
P4.2               P4.5 ←── P4.2 + P4.3
                     │
                   P4.6
```

### CLI Usage Reference

```bash
# Single query
orb "Write a fibonacci function"

# Interactive REPL
orb -i

# With tracing
orb --trace "Write a fibonacci function"

# Budget and timeout
orb --budget 50 --timeout 60 "Write hello world"

# Model override
orb --model claude-sonnet-4-20250514 "Write a sort function"

# Local models only
orb --local-only "Write hello world"

# Cloud models only
orb --cloud-only "Write a complex parser"
```

### Sprint 4 — Definition of Done

- [ ] `python -m orb "Write a fibonacci function"` — runs successfully
- [ ] `python -m orb -i` — opens interactive REPL
- [ ] `--trace` shows real-time message routing with timing and model info
- [ ] `--local-only` and `--cloud-only` correctly constrain model selection
- [ ] `--budget` and `--timeout` are respected
- [ ] `--model` overrides the default cloud model
- [ ] Error messages shown when no providers are available

---

## Sprint 5 — Live Web Dashboard

**Goal:** A real-time web dashboard that visualizes the agent graph, message flow, agent status, and conversation history as agents collaborate.

### Team Platform — WebSocket Server

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| W5.1 | Implement `DashboardServer` — aiohttp-based WebSocket server. Subscribes to MessageBus events and broadcasts JSON updates to connected clients. Endpoints: `GET /` (serve static files), `WS /ws` (real-time events), `GET /api/state` (current snapshot) | `web/server.py` | Sprint 3 P3.1 |
| W5.2 | Implement `DashboardBridge` — adapter between the tracing system and dashboard. Converts internal events to dashboard-friendly JSON: agent status changes, message routing, completions, budget/timer updates | `web/bridge.py` | W5.1, Sprint 1 G1.4 |
| W5.3 | Implement `DashboardState` — maintains a snapshot of the full system state (graph topology, agent statuses, message log, budget remaining, elapsed time) for new client connections and `/api/state` | `web/state.py` | W5.2 |

### Team Frontend — Graph Visualization

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| W5.4 | Build HTML shell — single-page app with layout panels: graph view (center), message log (right sidebar), agent detail (bottom), stats bar (top) | `web/static/index.html` | — |
| W5.5 | Build CSS — dark theme, agent color coding (cyan=Coder, yellow=Reviewer, green=Tester), animated edges for active messages, status indicators | `web/static/style.css` | — |
| W5.6 | Implement graph renderer — Canvas/SVG-based node-link diagram. Nodes show agent name, role, status (colored ring). Edges animate when messages flow. Node size pulses on activity | `web/static/graph.js` | W5.4, W5.5 |
| W5.7 | Implement WebSocket client — connects to `/ws`, parses JSON events, dispatches to graph renderer, message log, and stats bar. Handles reconnection | `web/static/app.js` | W5.6 |
| W5.8 | Implement message log panel — scrolling list of messages with timestamp, from/to, model used, payload preview. Click to expand full content. Color-coded by agent | `web/static/app.js` | W5.7 |
| W5.9 | Implement stats bar — live counters for messages sent, budget remaining, elapsed time, agent completion status | `web/static/app.js` | W5.7 |

### Team Platform — CLI Integration

| Task | Description | Files | Dependencies |
|------|-------------|-------|--------------|
| W5.10 | Add `--dashboard` flag to CLI — starts web server on port 8080, opens browser, runs query with dashboard bridge attached | `orb/cli/main.py` | W5.1, W5.2 |
| W5.11 | Add `--dashboard-port` flag for custom port | `orb/cli/main.py` | W5.10 |

### Sprint 5 — Parallel Execution Map

```
Team Platform (Backend)              Team Frontend
───────────────────────              ─────────────
W5.1 ──→ W5.2 ──→ W5.3              W5.4 + W5.5 (parallel)
                    │                     │
               W5.10 ──→ W5.11      W5.6 ──→ W5.7 ──→ W5.8 + W5.9 (parallel)
```

### Dashboard Event Protocol (WebSocket JSON)

```json
// Agent status change
{"type": "agent_status", "agent": "coder", "status": "running", "model": "qwen2.5:14b"}

// Message routed
{"type": "message", "from": "coder", "to": "reviewer", "content": "Here's my code...",
 "model": "qwen2.5:14b", "depth": 1, "elapsed": 1.5, "chain_id": "abc123"}

// Agent completed
{"type": "complete", "agent": "coder", "result": "Final implementation: ..."}

// Stats update
{"type": "stats", "message_count": 5, "budget_remaining": 195, "elapsed": 3.2}

// Initial state (sent on connect)
{"type": "init", "agents": [...], "edges": [...], "messages": [...], "stats": {...}}
```

### Sprint 5 — Definition of Done

- [ ] `orb --dashboard "Write a fibonacci function"` opens browser with live dashboard
- [ ] Graph visualization shows 3 agents in triangle layout with role labels
- [ ] Edges animate when messages flow between agents
- [ ] Agent nodes change color based on status (idle/running/completed)
- [ ] Message log shows real-time message flow with model info
- [ ] Stats bar shows live budget, timer, and completion status
- [ ] Dashboard works with page refresh (gets full state on reconnect)
- [ ] No external frontend build tools required — vanilla HTML/CSS/JS

---

## Cross-Sprint: Loop Prevention Mechanisms

These are implemented incrementally across sprints but form a unified safety system.

| Mechanism | Sprint | Owner | Default | Implementation |
|-----------|--------|-------|---------|----------------|
| Hop count per chain | 1 | Team Graph | max 10 | `Message.depth` incremented on `reply()`, checked in `HopCounter` middleware |
| Global message budget | 1 | Team Graph | 200 | `BudgetTracker` middleware in `MessageBus.route()` |
| Agent cooldown | 1 | Team Graph | 5 per target per chain | `CooldownTracker` middleware, keyed on `(from, to, chain_id)` |
| Timeout | 3 | Team Platform | 120s | `asyncio.wait_for()` in `Orchestrator.run()` |

---

## Cross-Sprint: Model Selection Strategy

Implemented in Sprint 2 (Team LLM), consumed by all agents from Sprint 2 onward.

```
Complexity Score    Model Tier         Example Models
─────────────────────────────────────────────────────
Low (0-30)         LOCAL_SMALL (9B)   llama3.2:latest, qwen2.5:7b
Medium (31-60)     LOCAL_MEDIUM (14B) qwen2.5:14b, deepseek-r1:14b
High (61-80)       LOCAL_LARGE (30B)  qwen2.5:32b, deepseek-r1:32b
Very High (81-95)  CLOUD_FAST         claude-sonnet, gpt-4o
Critical (96-100)  CLOUD_STRONG       claude-opus, gpt-4o
```

### Scoring Heuristics

| Signal | Points | Condition |
|--------|--------|-----------|
| Base score | varies | From `AgentConfig.base_complexity` (Tester=30, Coder=50, Reviewer=60) |
| Deep chain | +15 | `msg.depth > 3` |
| Feedback type | +20 | `msg.type == FEEDBACK` |
| Large payload | +10 | `len(msg.payload) > 2000` |
| Explicit hint high | +25 | `msg.metadata["complexity"] == "high"` |
| Explicit hint low | -15 | `msg.metadata["complexity"] == "low"` |
| Self-escalation | +10 each | Per retry after low-quality response |

---

## Cross-Sprint: Memory Graph

Implemented in Sprint 1 (Team Agent), integrated into LLMAgent in Sprint 2.

### Data Model

```
MemoryNode(id, content: str, node_type: str, created_at, updated_at)
MemoryEdge(from_id, to_id, relation: str)
```

### Relations

- `derived_from` — output derived from input
- `related_to` — contextually related
- `supersedes` — newer version replaces older
- `followed_by` — sequential ordering

### Future Evolution (not in scope)

- Typed nodes with embeddings
- Weighted edges for relevance scoring
- Cross-agent memory queries
- Memory compaction / summarization

---

## Dependencies

```
anthropic          # Claude SDK
openai             # OpenAI SDK (also Ollama OpenAI-compat mode)
httpx              # HTTP client for Ollama native API
pydantic           # Message validation
pytest             # Testing
pytest-asyncio     # Async test support
rich               # Terminal formatting
aiohttp            # WebSocket server for dashboard
```

---

## Full File Manifest

```
orb/
├── __init__.py
├── __main__.py
├── graph/
│   ├── __init__.py
│   ├── graph.py
│   └── types.py
├── messaging/
│   ├── __init__.py
│   ├── message.py
│   ├── channel.py
│   ├── bus.py
│   └── middleware.py
├── agent/
│   ├── __init__.py
│   ├── base.py
│   ├── types.py
│   ├── llm_agent.py
│   ├── tools.py
│   ├── conversation.py
│   └── prompt_builder.py
├── llm/
│   ├── __init__.py
│   ├── client.py
│   ├── providers.py
│   ├── model_selector.py
│   └── types.py
├── memory/
│   ├── __init__.py
│   ├── memory_graph.py
│   ├── memory_node.py
│   └── retriever.py
├── topologies/
│   ├── __init__.py
│   └── triangle.py
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator.py
│   └── types.py
├── tracing/
│   ├── __init__.py
│   └── logger.py
├── cli/
│   ├── __init__.py
│   ├── main.py
│   ├── repl.py
│   └── display.py
web/
├── server.py
├── bridge.py
├── state.py
└── static/
    ├── index.html
    ├── style.css
    ├── graph.js
    └── app.js
tests/
├── __init__.py
├── test_graph.py
├── test_channel.py
├── test_bus.py
├── test_memory_graph.py
├── test_model_selector.py
├── test_prompt_builder.py
├── test_claude_agent.py
├── test_triangle.py
├── test_orchestrator.py
└── integration/
    ├── __init__.py
    └── test_e2e.py
```
