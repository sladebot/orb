# Orb

An LLM agent collaboration network. Agents are **graph nodes** that communicate via Go-style async channels over a **MessageBus**. Each agent selects its model tier dynamically based on task complexity — local models for simple work, cloud models for demanding tasks.

---

## Table of Contents

- [Installation](#installation)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Basic Usage](#basic-usage)
- [CLI Flags](#cli-flags)
- [TUI Guide](#tui-guide)
- [Topologies](#topologies)
- [Model Tiers and Providers](#model-tiers-and-providers)
- [Log Streaming](#log-streaming)
- [Web Dashboard](#web-dashboard)
- [Architecture](#architecture)
- [Loop Prevention](#loop-prevention)
- [Project Structure](#project-structure)
- [Testing](#testing)

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
# Anthropic subscription flow:
# 1. run `claude setup-token` in another terminal
# 2. copy the generated token
# 3. paste it into Orb
orb auth anthropic

# Or store an Anthropic API key directly
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

The auth system also reads `ANTHROPIC_API_KEY`, `ANTHROPIC_OAUTH_TOKEN`, and `OPENAI_API_KEY` environment variables as fallbacks. `orb auth status` shows which source is active for each provider and whether OAuth tokens are still valid.

For Anthropic subscription auth, Orb does not run the Claude browser flow itself. It guides you through the supported Claude CLI route: run `claude setup-token`, then paste the resulting setup-token into `orb auth anthropic`.

For remote or SSH sessions, `orb auth openai` prints the authorization URL and prompts you to paste the redirect URL from your browser instead of starting a local callback server.

Running `orb auth` with no subcommand is equivalent to `orb auth status`.

---

## Configuration

Persistent settings are stored in `~/.orb/config.json`.

```bash
# Show all current settings and their sources
orb config show

# Get a single setting
orb config get local-models

# Enable or disable Ollama local model discovery
orb config set local-models true
orb config set local-models false
```

| Setting | Default | Description |
|---------|---------|-------------|
| `local-models` | `true` | Whether to detect and use Ollama local models |

---

## Basic Usage

### Single query

```bash
orb "write a snake game in Python"
```

Runs the agent topology once, prints a live trace to the terminal, then outputs the final synthesized result.

### Interactive REPL

```bash
orb -i
```

Opens a prompt loop. Submit tasks one at a time; agents are rebuilt fresh each run.

### Terminal TUI

```bash
orb --tui
```

Launches a full-screen Textual TUI. Type tasks directly in the input bar. You can submit multiple tasks in sequence without restarting.

### Web dashboard

```bash
# Start dashboard and wait for a task from the browser
orb --dashboard

# Run a query and keep the dashboard open to inspect afterward
orb --dashboard "build a REST API"

# Custom port
orb --dashboard --dashboard-port 3000
```

Opens a WebSocket-backed web UI at `http://localhost:8080`. The canvas graph shows agent nodes and animates edges as messages flow.

### TUI and dashboard together

```bash
orb --tui --dashboard
orb --tui --dashboard --dashboard-port 3000
```

Runs the TUI in the foreground and serves the web dashboard as a sidecar. Both views update from the same event stream.

---

## CLI Flags

```
orb [OPTIONS] [QUERY]
```

| Flag | Default | Description |
|------|---------|-------------|
| `query` | — | Task to run (omit for interactive mode) |
| `-i`, `--interactive` | off | Interactive REPL mode |
| `--topology` | `triangle` | Agent topology: `triangle` or `dual-review` |
| `--budget N` | 200 | Global message budget (hard ceiling) |
| `--timeout N` | 600.0 | Timeout in seconds |
| `--max-depth N` | 10 | Max message hop depth per chain |
| `--model MODEL` | — | Override cloud model for all tiers (e.g. `claude-sonnet-4-6`) |
| `--local-only` | off | Force all agents to `LOCAL_MEDIUM` tier |
| `--cloud-only` | off | Force all agents to `CLOUD_FAST` tier |
| `--ollama-model MODEL` | `$OLLAMA_MODEL` | Ollama model to use for all local tiers (e.g. `qwen3.5:9b`) |
| `--dashboard` | off | Launch live web dashboard |
| `--dashboard-port PORT` | 8080 | Dashboard server port |
| `--tui` | off | Launch interactive terminal TUI |
| `--logs` | off | Show live log panel in TUI (requires `--tui`); also streams to `~/.orb/run.log` |
| `--trace` / `--no-trace` | on | Show or hide real-time message routing in terminal |
| `-v`, `--verbose` | on | Enable debug logging |
| `-q`, `--quiet` | off | Suppress verbose logging |
| `--dev` | off | Dev mode: auto-restart on changes to `orb/` or `web/` |

### Examples

```bash
# Specific cloud model
orb --model claude-sonnet-4-6 "write a sort function"

# Local models only with a tighter budget
orb --local-only --budget 50 "hello world"

# Dual-review topology with dashboard on a custom port
orb --topology dual-review --dashboard --dashboard-port 3000 "build a REST API"

# TUI with a specific Ollama model
orb --tui --ollama-model qwen3.5:9b

# Cloud only, no trace output
orb --cloud-only --no-trace "explain merge sort"

# TUI with log panel visible
orb --tui --logs "write a fibonacci function"
```

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

The live panel (top section) is fixed — it always shows the topology graph and each agent's current status with their latest message activity. The message feed below it scrolls independently.

### Agent status icons

| Icon | Status |
|------|--------|
| `○` | idle |
| `◔` | waiting |
| `●` | running (with spinner animation) |
| `✓` | completed |
| `✗` | error |

### Code panel

When an agent writes a file, a **code panel** automatically appears on the right side of the screen. It shows the full file content with line numbers and syntax coloring. The panel header displays the agent name, file path, language, and line count.

### Agent detail pane

Select any agent to open the detail pane on the right, which shows:
- Live activity text (what the agent is currently doing)
- Model name
- Message count
- Time in current state
- Full message history
- Completion result (once finished)

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `1` | Select / inspect Coordinator |
| `2` | Select / inspect Coder |
| `3` | Select / inspect Reviewer (or Reviewer A in dual-review) |
| `4` | Select / inspect Reviewer A (dual-review) |
| `5` | Select / inspect Reviewer B (dual-review) |
| `6` | Select / inspect Tester |
| `Tab` | Cycle to next agent |
| `Escape` | Deselect / close detail pane |
| `r` | Open result screen (files changed + diff + agent results) |
| `s` | Save results to file (from result screen) |
| `y` | Copy result to clipboard (selected agent, or synthesis result) |
| `Ctrl+K` | Cancel the current run |
| `Ctrl+L` | Clear the message feed |
| `/` | Focus the input bar |
| `Ctrl+C` | Quit |

To select and copy arbitrary text from the terminal, hold **Shift** while clicking and dragging. This bypasses the TUI's mouse capture and uses native terminal selection.

### Input bar

The input bar supports multi-line content:

- **Enter** — submit the current text
- **Paste** — newlines in pasted text are preserved; press Enter when ready to send

### Result screen

Press `r` after a run completes to open the full-screen result screen, which shows:
- Files changed (git diff summary)
- Colored diff of all file changes
- Each agent's final result

Press `s` to save the output to a timestamped markdown file (`orb_result_YYYYMMDD_HHMMSS.md`). Press `y` to copy the synthesis result to the clipboard.

### Conversational follow-ups

After a run completes, typing a new task continues the session with full context. Each agent's conversation history is preserved across runs, and a session summary is prepended to the new task so agents remember what was built before.

```
orb --tui
> write a snake game in Python       # first run
> add a high score leaderboard       # follow-up — agents remember the code
> now add sound effects              # and again
```

### Asking for clarification

If an agent needs more information, it can send a message to `user` instead of calling `complete_task`. The run stays active, the input bar turns amber and shows which agent is waiting, and the next submission is routed directly to that agent as a reply.

### @mention

Type `@agentname` in the input bar to focus an agent. The agent bar at the bottom highlights all active agents when `@` is detected.

```
@coder               # select Coder and open its detail pane
@reviewer look again # select Reviewer, then forward "look again" as a query
```

### Mid-run injection

If a run is active (status = Running), new input is forwarded directly to the coordinator's channel rather than starting a fresh run. This lets you steer the team mid-flight.

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

Four agents: Coordinator routes and synthesizes; Coder writes and iterates; Reviewer checks for correctness, style, and edge cases; Tester writes and runs test cases. All three worker agents can communicate with each other directly.

```bash
orb --topology triangle "write a binary search tree"
```

| Agent | Base complexity | Filesystem access |
|-------|----------------|-------------------|
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

Five agents. Two reviewers — Reviewer A and Reviewer B — are assigned to **different providers** when possible, so they evaluate code from independent perspectives. They must reach explicit consensus before approving. Tester reports to both reviewers.

```bash
orb --topology dual-review "write a concurrent queue"
```

Reviewer provider priority: Anthropic → OpenAI Codex → OpenAI → Ollama. Reviewer B is assigned the next available provider after Reviewer A.

---

## Model Tiers and Providers

### Providers

| Provider | Setup | Notes |
|----------|-------|-------|
| **Anthropic** | `orb auth anthropic` or `ANTHROPIC_API_KEY` | Claude Haiku, Sonnet, Opus |
| **OpenAI** | `orb auth openai` or `OPENAI_API_KEY` | GPT-4o-mini, GPT-4o, o3 |
| **OpenAI Codex** | `orb auth openai` (OAuth) | gpt-5.4 via ChatGPT Plus/Pro subscription |
| **Ollama** | Run Ollama locally on port 11434 | Llama, Qwen, DeepSeek, etc. |

At least one provider must be available. The system detects configured providers automatically on startup. Ollama can also be reached via `OLLAMA_HOST` or a non-openai.com `OPENAI_BASE_URL`.

### Model tiers

Agents select a model tier based on their `base_complexity` score. If the preferred tier is unavailable, the system walks up to the next available tier.

| Tier | Description | Default model |
|------|-------------|---------------|
| `LOCAL_SMALL` | ~9B params | `qwen3.5:9b` (Ollama) |
| `LOCAL_MEDIUM` | ~14–27B params | `qwen3.5:27b` (Ollama) |
| `LOCAL_LARGE` | ~27–30B params | `qwen3.5:27b` (Ollama) |
| `CLOUD_LITE` | Fast and cheap | `claude-haiku-4-5-20251001` / `gpt-4o-mini` |
| `CLOUD_FAST` | Balanced | `claude-sonnet-4-6` / `gpt-4o` |
| `CLOUD_STRONG` | Most capable | `claude-opus-4-6` / `o3` |

### Model selection flags

```bash
# Use only local Ollama models
orb --local-only "hello world"

# Use only cloud models
orb --cloud-only "refactor this codebase"

# Override the cloud model for all agents
orb --model claude-sonnet-4-6 "write unit tests"

# Override the Ollama model for all local tiers
orb --ollama-model qwen3.5:9b "explain quicksort"
```

---

## Log Streaming

All runs write logs to `~/.orb/run.log` (rotating, 5 MB max with 2 backups).

```bash
# Stream logs from a running orb process (follow mode on by default)
orb logs

# Follow mode explicitly
orb logs -f

# Print existing logs and exit (no follow)
orb logs --no-follow

# Show last N lines (default: 50)
orb logs -n 100

# Filter by minimum log level
orb logs --level INFO
orb logs --level WARNING

# Clear the log file
orb logs --clear
```

The `--logs` flag when used with `--tui` adds a live log panel at the bottom of the TUI screen.

---

## Web Dashboard

```bash
# Serve dashboard and wait for a task from the browser
orb --dashboard

# Run a query with dashboard visible
orb --dashboard "build a REST API"

# Custom port
orb --dashboard --dashboard-port 3000
```

Opens a WebSocket-backed web UI at `http://localhost:8080` (or the specified port). The dashboard provides:

- **Live graph canvas** — agent nodes with animated edges as messages route between them
- **Scrollable message log** — real-time feed of all inter-agent messages
- **Stats bar** — message count, budget usage, run status
- **Agent detail panel** — click any node to see that agent's messages and results
- **Files changed section** — after completion, shows a collapsible syntax-highlighted diff of all files written during the run

No frontend build step is required — the UI is plain HTML, CSS, and JS served directly by the aiohttp server. New browser connections receive a full state snapshot on connect.

---

## Demo Video

Use the recording helper to generate `.mov` demos for TUI and dashboard on macOS:

```bash
# Record both demos into demos/
./scripts/record_demo_video.sh both

# Record only TUI
./scripts/record_demo_video.sh tui

# Record only dashboard
./scripts/record_demo_video.sh dashboard
```

Optional environment variables:

- `DEMO_QUERY` custom prompt shown in the demo
- `DEMO_DURATION` seconds per clip (default: `25`)
- `DEMO_DASHBOARD_PORT` dashboard port (default: `8080`)
- `DEMO_DISPLAY_ID` optional display index for macOS `screencapture` (example: `1`)
- `ORB_CMD` command used to launch orb (default: `python -m orb.cli.main`)

---

## Architecture

### MessageBus

All inter-agent communication flows through a central `MessageBus`. The bus holds a directed `Graph` of allowed routes, enforces a global message budget, per-chain hop limits, and per-target cooldowns to prevent loops.

Bus events (`injected`, `routed`) are emitted to registered listeners — the terminal live display, the web dashboard bridge, and the TUI all subscribe to these events.

### Orchestrator

The `Orchestrator` wires agents to channels, injects the initial task into the entry agent (`coordinator`), and monitors completion. When all worker agents have called `complete_task`, the orchestrator notifies the synthesis agent (also `coordinator`) to produce the final answer, then shuts down remaining agents.

### Agent

Each `LLMAgent` holds an `AgentChannel` (async queue), a system prompt built from its role description and neighbor roster, and a rolling conversation history. On each turn, the agent calls the LLM with a tool set:

| Tool | Description |
|------|-------------|
| `send_message` | Route a message to a neighboring agent |
| `complete_task` | Mark the agent's work as done |
| `write_file` | Write a file to the shared sandbox |
| `read_file` | Read a file from the shared sandbox |
| `list_directory` | List files in a directory |
| `run_command` | Execute a shell command in the sandbox |

If the LLM returns a text-only response (no tool call), the agent nudges it up to 3 times before giving up. If the preferred model fails, the agent walks through a prioritized fallback list of available providers and tiers.

### Sandbox

Agents with `enable_filesystem=True` share a `Sandbox` scoped to the current working directory. All file writes and command executions are routed through the sandbox.

### Web dashboard

```
Browser (vanilla JS) ←─ WebSocket ─→ aiohttp server ←─ events ─→ MessageBus
      canvas graph                    /ws endpoint                    │
      message log                     /api/state                   agents
      stats bar                       / (static files)
```

The `DashboardBridge` adapts raw bus events into JSON state updates broadcast to all connected clients.

---

## Loop Prevention

| Mechanism | Default |
|-----------|---------|
| Global message budget | 200 messages |
| Max hop depth per chain | 10 |
| Per-target cooldown per chain | configurable (`max_cooldown`) |
| Run timeout | 600 seconds |

---

## Project Structure

```
orb/
├── agent/          # LLMAgent, AgentConfig, tool definitions, prompt builder, conversation
├── cli/            # CLI entry point (main.py), REPL, TUI (tui.py), auth (auth.py), config (config.py), display
├── graph/          # Directed graph data structure
├── llm/            # LLMClient protocol, Anthropic/OpenAI/Ollama providers, model registry
├── memory/         # Per-agent memory graph
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

## Testing

```bash
conda activate orb
pytest tests/ -v

# Integration tests (require a live API key)
ANTHROPIC_API_KEY=sk-ant-... pytest tests/integration/ -v
```

---

## License

This project is licensed under the GNU General Public License v3.0. See [`LICENSE`](LICENSE).

Copyright (C) 2026 Souranil Sen.
