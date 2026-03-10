# Orb

An LLM agent collaboration network. Agents are **graph nodes** that communicate via Go-style async channels over a **MessageBus**. Each agent selects its model tier dynamically based on task complexity — local models for simple work, cloud models for demanding tasks.

---

## Installation

```bash
# Create a Python 3.11+ environment (conda or venv)
conda create -n orb python=3.12 -y
conda activate orb

# Install in editable mode
pip install -e .

# Install with dev dependencies (pytest, pytest-asyncio)
pip install -e ".[dev]"
```

Requires Python 3.11+. At least one LLM provider must be reachable at runtime.

---

## Authentication

Credentials are stored in `~/.orb/credentials.json` (mode 600).

```bash
# Store an Anthropic API key
orb auth anthropic --api-key sk-ant-...

# Store an OpenAI API key directly
orb auth openai --api-key sk-...

# OpenAI OAuth browser flow (opens browser, exchanges PKCE code)
orb auth openai

# Show current auth status for all providers
orb auth status

# Revoke all stored credentials
orb auth logout
```

The auth system also reads `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` environment variables as fallbacks. `orb auth status` shows which source is active for each provider and whether OAuth tokens are still valid.

For remote/SSH sessions, `orb auth openai` prints the authorization URL and prompts you to paste the redirect URL from your browser instead of starting a local callback server.

---

## LLM Providers

| Provider | Setup | Models |
|----------|-------|--------|
| **Anthropic** | `orb auth anthropic` or `ANTHROPIC_API_KEY` | Claude Sonnet, Opus |
| **OpenAI** | `orb auth openai` or `OPENAI_API_KEY` | GPT-4o, GPT-4o-mini, o3 |
| **Ollama** | Run Ollama locally on port 11434 | Llama, Qwen, DeepSeek, etc. |

At least one provider must be available. The system detects configured providers automatically on startup.

### Model tiers

Agents select a model tier based on the task complexity score of their role:

| Tier | Complexity | Example models |
|------|-----------|----------------|
| LOCAL_SMALL | 0–30 | llama3.2, qwen2.5:7b |
| LOCAL_MEDIUM | 31–60 | qwen2.5:14b |
| LOCAL_LARGE | 61–80 | qwen2.5:32b |
| CLOUD_FAST | 81–95 | claude-sonnet, gpt-4o-mini |
| CLOUD_STRONG | 96–100 | claude-opus, gpt-4o |

---

## Usage modes

### 1. Single query

```bash
orb "write a snake game in Python"
```

Runs the agent topology once, prints a live trace to the terminal, then outputs the final synthesized result.

### 2. Interactive REPL

```bash
orb -i
```

Opens a prompt loop. Submit tasks one at a time; agents are rebuilt fresh each run.

### 3. Web dashboard

```bash
# Start dashboard and wait for a task from the browser
orb --dashboard

# Run a query and keep the dashboard open to inspect afterward
orb --dashboard "build a REST API"

# Custom port
orb --dashboard --dashboard-port 3000
```

Opens a WebSocket-backed web UI at `http://localhost:8080`. The canvas graph shows agent nodes and animates edges as messages flow. A scrollable message log, stats bar, and agent detail panel update in real time.

### 4. Terminal TUI

```bash
orb --tui
```

Launches a full-screen Textual TUI. Type tasks directly in the input bar. You can submit multiple tasks in sequence without restarting.

### 5. TUI + Dashboard together

```bash
orb --tui --dashboard
orb --tui --dashboard --dashboard-port 3000
```

Runs the TUI in the foreground and serves the web dashboard as a sidecar. Both views update from the same event stream.

---

## TUI Guide

The TUI is built with [Textual](https://github.com/Textualize/textual).

### Layout

```
┌─────────────────────────────────────────────────────┐
│  ORB  │  Triad  │  msgs 12  │  budget 188  │  Running │  ← stats bar
├──────────────────────────────────┬──────────────────┤
│  Topology graph (fixed)          │                  │
│  ─────────────────────────────── │  Agent detail    │
│  Agent nodes + latest activity   │  pane (opens     │
│  (always visible, no scroll)     │  on selection)   │
├──────────────────────────────────│                  │
│  Message feed (scrollable)       │                  │
│                                  │                  │
├──────────────────────────────────┴──────────────────┤
│  @ Coordinator [1]  Coder [2]  Reviewer [3] ...     │  ← agent bar
├─────────────────────────────────────────────────────┤
│  >  Describe a task…                                │  ← input
└─────────────────────────────────────────────────────┘
```

The **live panel** (top section) is fixed — it always shows the topology graph and each agent's current status with their latest message activity. The **message feed** below it scrolls independently.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `1` | Select / inspect Coordinator |
| `2` | Select / inspect Coder |
| `3` | Select / inspect Reviewer (or Reviewer A in dual-review) |
| `4` | Select / inspect Reviewer B |
| `5` | Select / inspect Reviewer B (dual-review) |
| `6` | Select / inspect Tester |
| `Esc` | Deselect / close detail pane |
| `Ctrl+C` | Quit |

Selecting an agent opens the **detail pane** on the right showing the agent's full message thread and completion result.

### @mention

Type `@agentname` in the input bar to focus an agent. The agent bar at the bottom highlights all active agents when `@` is detected.

```
@coder               # select Coder and open its detail pane
@reviewer look again # select Reviewer, then forward "look again" as a query
```

### Mid-run injection

If a run is active (status = **Running**), new input is forwarded directly to the coordinator's channel rather than starting a fresh run. This lets you steer the team mid-flight.

---

## CLI flags

```
orb [OPTIONS] [QUERY]
```

| Flag | Default | Description |
|------|---------|-------------|
| `query` | — | Task to run (omit for REPL) |
| `-i`, `--interactive` | off | Interactive REPL mode |
| `--topology` | `triangle` | Agent topology: `triangle` or `dual-review` |
| `--budget N` | 200 | Global message budget (hard ceiling) |
| `--timeout N` | 600.0 | Timeout in seconds |
| `--max-depth N` | 10 | Max message hop depth per chain |
| `--model MODEL` | — | Override cloud model for all tiers (e.g. `claude-sonnet-4-20250514`) |
| `--local-only` | off | Force all agents to LOCAL_MEDIUM tier |
| `--cloud-only` | off | Force all agents to CLOUD_FAST tier |
| `--ollama-model MODEL` | `$OLLAMA_MODEL` | Ollama model to use for all local tiers (e.g. `qwen3.5:9b`) |
| `--dashboard` | off | Launch live web dashboard |
| `--dashboard-port PORT` | 8080 | Dashboard server port |
| `--tui` | off | Launch interactive terminal TUI |
| `--trace` / `--no-trace` | on | Show/hide real-time message routing in terminal |
| `-v`, `--verbose` | on | Enable debug logging |
| `-q`, `--quiet` | off | Suppress verbose logging |
| `--dev` | off | Dev mode: auto-restart on changes to `orb/` or `web/` |

### Examples

```bash
# Specific model
orb --model claude-sonnet-4-20250514 "write a sort function"

# Local models only with a tighter budget
orb --local-only --budget 50 "hello world"

# Dual-review topology with dashboard on a custom port
orb --topology dual-review --dashboard --dashboard-port 3000 "build a REST API"

# TUI with a local Ollama model
orb --tui --ollama-model qwen3.5:9b

# Cloud only, no trace output
orb --cloud-only --no-trace "explain merge sort"
```

---

## Topologies

### Triangle (default)

```
Coordinator
     │
   Coder ─── Reviewer
     │            │
   Tester ────────╯
```

Four agents: Coordinator routes and synthesizes; Coder writes and iterates; Reviewer checks for correctness, style, and edge cases; Tester writes and runs test cases. All three worker agents communicate with each other directly.

```bash
orb --topology triangle "write a binary search tree"
```

| Agent | Base complexity | Filesystem |
|-------|----------------|------------|
| Coordinator | 20 | no |
| Coder | 50 | yes |
| Reviewer | 65 | yes |
| Tester | 25 | yes |

### Dual Review

```
Coordinator
     │
   Coder
  ╱     ╲
Rev A   Rev B
  ╲     ╱
   Tester
```

Five agents. Two reviewers — Reviewer A and Reviewer B — are assigned to **different providers** when possible so they evaluate code from independent perspectives. They must reach explicit consensus before approving. Tester reports to both reviewers.

```bash
orb --topology dual-review "write a concurrent queue"
```

Reviewer provider priority: Anthropic → OpenAI → Ollama. Reviewer B is assigned the next available provider after Reviewer A.

---

## Architecture

### MessageBus

All inter-agent communication flows through a central `MessageBus`. The bus holds a directed `Graph` of allowed routes, enforces a global message budget, per-chain hop limits, and per-target cooldowns to prevent loops.

Bus events (`injected`, `routed`) are emitted to registered listeners — the terminal live display, the web dashboard bridge, and the TUI all subscribe to these events.

### Orchestrator

The `Orchestrator` wires agents to channels, injects the initial task into the entry agent (`coordinator`), and monitors completion. When all agents have called `complete_task`, the orchestrator signals the synthesis agent to produce the final answer.

### Agent

Each `LLMAgent` holds an `AgentChannel` (async queue), a system prompt built from its role description and neighbor roster, and a rolling conversation history. On each turn, the agent calls the LLM with a tool set that includes `send_message`, `complete_task`, `write_file`, `read_file`, and `run_command`. The model tier is selected dynamically from the agent's `base_complexity` score unless overridden.

### Sandbox

Agents with `enable_filesystem=True` share a `Sandbox` scoped to the current working directory. File writes and command execution are routed through the sandbox.

### Web dashboard

```
Browser (vanilla JS) ←─ WebSocket ─→ aiohttp server ←─ events ─→ MessageBus
      canvas graph                    /ws endpoint                    │
      message log                     /api/state                   agents
      stats bar                       / (static files)
```

No frontend build step — plain HTML, CSS, and JS served directly by the aiohttp server. The `DashboardBridge` adapts raw bus events into JSON state updates broadcast to all connected clients. New connections receive a full state snapshot on connect.

---

## Project structure

```
orb/
├── agent/          # LLMAgent, AgentConfig, tool definitions, prompt builder, conversation
├── cli/            # CLI entry point (main.py), REPL, TUI (tui.py), auth (auth.py), display
├── graph/          # Directed graph data structure
├── llm/            # LLMClient protocol, Anthropic/OpenAI/Ollama providers, model registry
├── messaging/      # Message types, async AgentChannel, MessageBus, middleware
├── orchestrator/   # Orchestrator lifecycle, OrchestratorConfig, result types
├── sandbox/        # Sandboxed filesystem and command execution
├── topologies/     # Topology factories: triad.py (triangle), dual_review.py
└── tracing/        # EventLogger for terminal tracing
web/
├── server.py       # aiohttp WebSocket + HTTP server
├── bridge.py       # MessageBus → dashboard state adapter
├── state.py        # DashboardState snapshot
└── static/         # index.html, app.js, graph.js, style.css
tests/              # Unit and integration tests (pytest-asyncio)
```

---

## Loop prevention

| Mechanism | Default |
|-----------|---------|
| Global message budget | 200 messages |
| Max hop depth per chain | 10 |
| Per-target cooldown per chain | configurable (`max_cooldown`) |
| Run timeout | 600 seconds |

---

## Testing

```bash
conda activate orb
pytest tests/ -v

# Integration tests (require a live API key)
ANTHROPIC_API_KEY=sk-ant-... pytest tests/integration/ -v
```
