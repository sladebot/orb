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

First run after installation:

```bash
orb onboard
```

This walks through initial auth and common runtime settings.

---

## Authentication

Credentials are stored in `~/.orb/credentials.json` (mode 600).

```bash
# Guided onboarding for auth + common settings
orb onboard

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

`orb onboard` is the easiest entry point for first-time setup. It walks through:
- Anthropic auth
- OpenAI auth
- local model enable/disable
- current auth/config status

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

```
/list-topologies          # show all available topologies
/topology dual-review     # switch topology for the next run
/reload-topologies        # hot-reload ~/.orb/topologies.yaml
```

To bootstrap a user topology file from Orb's bundled sample:

```bash
orb topologies init
```

### Terminal TUI

```bash
# Start an isolated local backend and open the TUI
orb --tui

# Preferred daemon client flow
orb tui

# Attach the TUI to a specific Orb daemon
orb tui --connect http://127.0.0.1:8080
```

Launches a full-screen Textual TUI. Type tasks directly in the input bar. You can submit multiple tasks in sequence without restarting.

### Daemon mode

```bash
# Foreground daemon
orb daemon --host 127.0.0.1 --port 8080

# Equivalent explicit foreground form
orb daemon run --host 127.0.0.1 --port 8080

# Background daemon lifecycle
orb daemon start --host 0.0.0.0 --port 8080
orb daemon status
orb daemon restart --host 0.0.0.0 --port 8080
orb daemon stop
```

`orb daemon start` creates a managed background process. By default each daemon start gets a fresh temp workspace under `/tmp/orb-daemon-*`; pass `--workdir` to keep a fixed workspace. The daemon owns the backend runtime, API, WebSocket event stream, dashboard, topology selection, and graph execution. Attach UIs to it separately:

```bash
# Attach TUI
orb tui

# Open the browser dashboard
orb dashboard

# Start a run remotely, then inspect in the browser dashboard
orb dashboard "build a REST API"
```

### Web dashboard

```bash
# Preferred daemon client flow
orb dashboard

# Start a run on the daemon, then open the dashboard
orb dashboard "build a REST API"

# Start dashboard and wait for a task from the browser (embedded backend)
orb --dashboard

# Run a query and keep the dashboard open to inspect afterward (embedded backend)
orb --dashboard "build a REST API"

# Custom port
orb --dashboard --dashboard-port 3000

# Preferred production flow: run daemon, then open its URL
orb daemon --host 127.0.0.1 --port 8080
orb dashboard
```

Opens a WebSocket-backed web UI at `http://localhost:8080`. The canvas graph shows agent nodes and animates edges as messages flow.

### TUI and dashboard together

```bash
orb --tui --dashboard
orb --tui --dashboard --dashboard-port 3000
```

Runs the TUI in the foreground and serves the web dashboard as a sidecar. Both views update from the same event stream.

For persistent or multi-device use, prefer `orb daemon` plus `orb tui` / `orb dashboard` instead of the embedded sidecar mode.

---

## CLI Flags

```
orb [OPTIONS] [QUERY]
```

| Flag | Default | Description |
|------|---------|-------------|
| `query` | — | Task to run (omit for interactive mode) |
| `-i`, `--interactive` | off | Interactive REPL mode |
| `--topology` | `triad` | Agent topology id — any builtin or custom id (e.g. `triad`, `dual-review`). Run `orb --list-topologies` to see all available. |
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
┌───────────────────────────────────────────────────────────────┐
│ ORB  Triad  Running  msgs 12  budget 188  t/d/o  ctrl+p  ?   │
├──────────────────────────────────────┬────────────────────────┤
│ Live graph + run health              │ Agent inspector        │
│ Active nodes, waiting nodes,         │ Selected node status,  │
│ heartbeat, and last activity         │ neighbors, activity,   │
│                                      │ transcript, result     │
├──────────────────────────────────────┼────────────────────────┤
│ Timeline / Changes / Output          │                        │
│ Timeline: high-signal activity       │                        │
│ Changes: changed files + diffs       │                        │
│ Output: primary + supporting result  │                        │
├───────────────────────────────────────────────────────────────┤
│ task > Describe a task…                                     │
└───────────────────────────────────────────────────────────────┘
```

The graph and inspector stay visible while the center workspace switches between:
- `Timeline` for routed activity and user questions
- `Changes` for changed files and diffs
- `Output` for primary and supporting completions

### Agent status icons

| Icon | Status |
|------|--------|
| `○` | idle |
| `◔` | waiting |
| `●` | running (with spinner animation) |
| `✓` | completed |
| `✗` | error |

### Agent detail pane

Select any agent to open the detail pane on the right, which shows:
- Live status and heartbeat age
- Topology position and neighbors
- Current activity text
- Message count and recent transcript
- Files touched by that node
- Completion result (once finished)

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `t` | Show Timeline |
| `d` | Show Changes |
| `o` | Show Output |
| `Ctrl+P` | Open command launcher |
| `1-6` or `Ctrl+1-6` | Select / inspect an agent |
| `Tab` | Cycle to next agent |
| `Escape` | Deselect / close detail pane |
| `?` | Open help overlay |
| `r` | Open result screen (files changed + diff + agent results) |
| `s` | Save results to file (from result screen) |
| `y` | Copy result to clipboard (selected agent, or primary result) |
| `Ctrl+K` | Cancel the current run |
| `Ctrl+G` | Clear the current reply draft |
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

Press `s` to save the output to a timestamped markdown file (`orb_result_YYYYMMDD_HHMMSS.md`). Press `y` to copy the selected or primary result to the clipboard.

### Conversational follow-ups

After a run completes, typing a new task continues the session with full context. Orb now preserves a shared run transcript plus each agent's local execution history, so follow-up tasks carry forward:
- user requests and replies
- agent-to-agent messages
- direct worker completions
- summarized prior session context

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

Topologies are defined in YAML and loaded at startup. Orb ships two builtins; you can add your own at `~/.orb/topologies.yaml`.

Initialize a starter file with:

```bash
orb topologies init
```

### Triangle (default)

```
Coordinator
     │
   Coder ─── Reviewer
     │            │
   Tester ────────╯
```

Four agents: Coordinator routes inputs through the graph; Coder writes and iterates; Reviewer checks correctness and edge cases; Tester validates behavior. The coordinator does not synthesize task content itself.

```bash
orb --topology triad "write a binary search tree"
```

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

Five agents. Two reviewers are assigned to **different providers** when possible so they evaluate code from independent perspectives. They must reach explicit consensus before approving.

```bash
orb --topology dual-review "write a concurrent queue"
```

### Custom topologies

Define your own topology in YAML and place it at `~/.orb/topologies.yaml`. `orb topologies init` copies the bundled `sample-topology.yaml` into place as a starting point. The sample is a fully commented 6-agent example (Coordinator → Researcher → Coder → Security Reviewer + Code Reviewer → Tester).

```yaml
# ~/.orb/topologies.yaml
version: "1.0"

topologies:
  my-topology:
    id: "my-topology"
    label: "My Topology"
    description: "..."
    entry_agent: "coordinator"

    agents:
      coordinator:
        role: "Coordinator"
        description: "..."
        base_complexity: 20

      coder:
        role: "Coder"
        description: "..."
        base_complexity: 55
        enable_filesystem: true

    edges:
      - [coordinator, coder]

    workflow_steps:
      - "Coordinator routes the task to the Coder."

    completion_rules:
      coordinator:
        - "Route the task without solving it yourself."
      coder:
        - "Complete after implementing the solution."
```

```bash
orb --topology my-topology "build a URL shortener"
```

The file is watched while the web dashboard is running — edits are hot-reloaded and the topology dropdown updates automatically without a restart. In the REPL, run `/reload-topologies` to pick up changes manually.

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
- **Topology dropdown** — anchored top-right of the graph; switch topologies between runs; updates automatically on hot-reload
- **Scrollable message log** — real-time feed of all inter-agent messages
- **Stats bar** — message count, budget usage, elapsed time, active topology, run status
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

The `Orchestrator` wires agents to channels, injects the initial task into the entry agent (`coordinator`), and monitors completion. The coordinator is a router, not a synthesis agent. Runtime state, topology choice, and completion tracking live in the backend daemon so the TUI and dashboard remain subscriber-only clients.

### Agent

Each `LLMAgent` holds an `AgentChannel` (async queue), a system prompt built from its role description and neighbor roster, local execution history, and access to the shared run transcript. On each turn, the agent calls the LLM with:
- a filtered shared transcript window
- its local execution/tool history
- topology context and neighbor roster
- the tool set below

| Tool | Description |
|------|-------------|
| `send_message` | Route a message to a neighboring agent |
| `complete_task` | Mark the agent's work as done |
| `write_file` | Write a file to the shared sandbox |
| `read_file` | Read a file from the shared sandbox |
| `list_directory` | List files in a directory |
| `run_command` | Execute a shell command in the sandbox |

If the LLM returns a text-only response (no tool call), the agent nudges it up to 3 times before giving up. If the preferred model fails, the agent walks through a prioritized fallback list of available providers and tiers. Heartbeats are emitted while agents are live so subscriber UIs can show liveness without owning runtime logic.

### Sandbox

Agents with `enable_filesystem=True` share a `Sandbox` scoped to the daemon workspace. For foreground CLI runs that is the current working directory; for `orb daemon` it is the daemon workdir (by default a fresh `/tmp/orb-daemon-*` directory). All file writes and command executions are routed through the sandbox.

### Web dashboard

```
Browser (vanilla JS) ←─ WebSocket ─→ aiohttp server ←─ events ─→ MessageBus
      canvas graph                    /ws endpoint                    │
      message log                     /api/state                   agents
      stats bar                       / (static files)
```

The `DashboardBridge` adapts raw bus events into JSON state updates broadcast to all connected clients. The daemon is authoritative for agent lifecycle state, topology metadata, and graph rendering data; the TUI and dashboard render that state rather than recreating orchestration locally.

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
├── topologies/     # YAML loader, Pydantic schema, factory, hot-reload watcher, bundled defaults
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
pytest tests/ -v

# Integration tests (require a live API key)
ANTHROPIC_API_KEY=sk-ant-... pytest tests/integration/ -v
```

---

## License

This project is licensed under the GNU General Public License v3.0. See [`LICENSE`](LICENSE).

Copyright (C) 2026 Souranil Sen.
