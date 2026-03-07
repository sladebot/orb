# Orb

An LLM agent harness where agents are **graph nodes** with **bidirectional communication** via Go-style async channels. Agents dynamically select models (local or cloud) based on task complexity.

## Quick Start

### Setup

```bash
# Create conda environment
conda create -n orb python=3.12 -y
conda activate orb

# Install
pip install -e ".[dev]"

# Set API keys (at least one required for cloud models)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

### Run

```bash
# Single query
python -m orb "Write a fibonacci function in Python"

# Interactive REPL
python -m orb -i

# With live web dashboard
python -m orb --dashboard "Write a fibonacci function"
```

## Architecture

Three agents collaborate in a fully-connected triangle:

```
        Coder
       /     \
  Reviewer — Tester
```

| Agent | Role | Base Complexity |
|-------|------|----------------|
| **Coder** | Writes code, iterates on feedback | 50 |
| **Reviewer** | Reviews for correctness, style, edge cases | 60 |
| **Tester** | Writes test cases, reports failures | 30 |

Agents communicate by calling `send_message` (an LLM tool) to address neighbors. Each agent selects its model dynamically based on task complexity — simple tasks use local models, complex ones escalate to cloud.

### Model Selection

| Complexity | Tier | Example Models |
|-----------|------|---------------|
| 0-30 | LOCAL_SMALL | llama3.2, qwen2.5:7b |
| 31-60 | LOCAL_MEDIUM | qwen2.5:14b |
| 61-80 | LOCAL_LARGE | qwen2.5:32b |
| 81-95 | CLOUD_FAST | claude-sonnet, gpt-4o-mini |
| 96-100 | CLOUD_STRONG | claude-opus, gpt-4o |

### Loop Prevention

| Mechanism | Default |
|-----------|---------|
| Hop count per message chain | max 10 |
| Global message budget | 200 messages |
| Per-target cooldown per chain | 5 sends |
| Timeout | 120 seconds |

## CLI Reference

```
orb [OPTIONS] [QUERY]

Arguments:
  QUERY                     Task query (omit for interactive mode)

Options:
  -i, --interactive         Interactive REPL mode
  --trace / --no-trace      Show/hide real-time message routing (default: on)
  --budget N                Global message budget (default: 200)
  --timeout N               Timeout in seconds (default: 120)
  --max-depth N             Max message hop depth (default: 10)
  --model MODEL             Override default cloud model (e.g. claude-sonnet-4-20250514)
  --local-only              Force all agents to use local models only
  --cloud-only              Force all agents to use cloud models only
  --dashboard               Launch live web dashboard
  --dashboard-port PORT     Dashboard server port (default: 8080)
```

### Examples

```bash
# Use a specific model
python -m orb --model claude-sonnet-4-20250514 "Write a sort function"

# Local models only with reduced budget
python -m orb --local-only --budget 50 "Write hello world"

# Cloud models with dashboard on custom port
python -m orb --cloud-only --dashboard --dashboard-port 3000 "Build a REST API"
```

## Web Dashboard

The dashboard provides a real-time visualization of agent collaboration.

### Starting the Dashboard

```bash
python -m orb --dashboard "Write a fibonacci function"
```

This starts a web server on port 8080 (or `--dashboard-port`) and opens your browser.

### Features

- **Graph visualization** — Canvas-rendered node-link diagram showing the agent triangle. Nodes display role, status, and model. Edges animate when messages flow between agents.
- **Message log** — Scrolling sidebar showing every message routed between agents, with timestamps, model info, and expandable content.
- **Stats bar** — Live counters for messages sent, budget remaining, elapsed time, and overall status.
- **Agent detail panel** — Click any agent node to see its current status, model, and completion result.
- **Auto-reconnect** — WebSocket reconnects automatically if the connection drops. New connections receive the full current state.

### Dashboard Architecture

```
Browser (vanilla JS)  ←──WebSocket──→  aiohttp server  ←──events──→  MessageBus
      Canvas graph                      /ws endpoint                    |
      Message log                       /api/state                   agents
      Stats bar                         / (static files)
```

The dashboard uses no frontend build tools — it's vanilla HTML, CSS, and JavaScript served directly by the aiohttp server.

## Project Structure

```
orb/
├── graph/          # Undirected graph data structure
├── messaging/      # Message types, async channels, message bus, middleware
├── agent/          # Agent base class, LLM agent, tools, prompts, conversation
├── llm/            # LLM client protocol, providers (Anthropic/OpenAI/Ollama), model selector
├── memory/         # Per-agent graph-structured memory
├── topologies/     # Topology factories (triangle)
├── orchestrator/   # Lifecycle management, task injection, result collection
├── tracing/        # Terminal-based event logging
├── cli/            # CLI entry point, REPL, display formatting
web/
├── server.py       # aiohttp WebSocket server
├── bridge.py       # Tracing-to-dashboard adapter
├── state.py        # Dashboard state snapshot
└── static/         # HTML, CSS, JS for the dashboard
tests/              # Unit and integration tests
talks/
└── plan.md         # Sprint-based implementation plan
```

## Testing

```bash
conda activate orb

# Run all unit tests
python -m pytest tests/ -v

# Run integration test (requires API key)
ANTHROPIC_API_KEY=sk-ant-... python -m pytest tests/integration/ -v
```

## LLM Providers

Orb supports three LLM providers:

| Provider | Setup | Used For |
|----------|-------|----------|
| **Anthropic** | Set `ANTHROPIC_API_KEY` | Claude models (Sonnet, Opus) |
| **OpenAI** | Set `OPENAI_API_KEY` | GPT models (GPT-4o, GPT-4o-mini) |
| **Ollama** | Run Ollama locally on port 11434 | Local models (Llama, Qwen, DeepSeek) |

At least one provider must be available. The system automatically detects which providers are configured.

## Selective Context Sharing

Agents don't forward their full conversation history. Instead, the LLM decides what context each neighbor needs:

- **Coder → Reviewer**: code + requirements
- **Coder → Tester**: code + expected behavior
- **Reviewer → Coder**: specific feedback + suggestions
- **Tester → Coder**: failing test cases + error output

This is guided by the system prompt, not hard-coded — the LLM chooses what's relevant per message.
