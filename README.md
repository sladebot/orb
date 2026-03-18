# Orb

A network of LLM agents that collaborate to solve tasks. Agents communicate via async channels over a shared message bus, pick models dynamically based on task complexity, and build up a persistent knowledge graph across runs.

---

## Quickstart

```bash
# Install
conda create -n orb python=3.12 -y && conda activate orb
pip install -e .

# First-time setup (auth + settings)
orb onboard

# Run a task
orb "write a snake game in Python"
```

---

## Authentication

```bash
orb auth anthropic          # Anthropic API key or Claude subscription token
orb auth openai             # OpenAI API key or OAuth (opens browser)
orb auth status             # Show what's configured
```

Credentials are stored in `~/.orb/credentials.json`. You can also set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` environment variables directly.

---

## Usage

### Single query

```bash
orb "refactor this module to be async"
```

### Interactive mode

```bash
orb -i
```

### Terminal UI

```bash
orb --tui
```

Full-screen Textual TUI. Type tasks in the input bar, watch agents collaborate live, follow up without restarting.

### Web dashboard

```bash
orb --dashboard
orb --dashboard "build a REST API"
```

Opens `http://localhost:8080` — live graph canvas, message feed, agent details, and file diffs.

### Daemon mode

```bash
orb daemon start            # background daemon
orb tui                     # attach TUI
orb dashboard               # open browser dashboard
orb daemon stop
```

---

## Topologies

Topologies define the agent graph. Switch with `--topology`:

```bash
orb --topology triad "write a binary search tree"
orb --topology dual-review "write a concurrent queue"
orb --topology hierarchy "plan and implement a refactor"
```

| Topology | Agents | Best for |
|----------|--------|----------|
| `triad` | Coordinator → Coder ↔ Reviewer ↔ Tester | General coding tasks |
| `dual-review` | + two independent reviewers | High-correctness tasks |
| `hierarchy` | + dedicated researcher | Complex planning + implementation |

### Custom topologies

```bash
orb topologies init         # copy sample to ~/.orb/topologies.yaml
```

Edit `~/.orb/topologies.yaml` to define your own agent graphs. The dashboard hot-reloads on save.

---

## GraphRAG Memory

Agents extract and persist structured facts across runs. Each topology gets its own knowledge store at `~/.orb/chroma/<topology_id>/<cluster>` — agents get smarter about a domain the more you use a topology.

To define clusters in a topology YAML:

```yaml
persist_base: "~/.orb/chroma"   # auto-scoped per topology id

clusters:
  implementation:
    agents: [coordinator, coder]
  review:
    agents: [reviewer, tester]
```

To browse stored facts:

```bash
chroma run --path ~/.orb/chroma --port 8001
npx chromadb-admin              # then open http://localhost:3000
```

---

## Model Tiers

Agents pick a model tier based on task complexity:

| Tier | Models |
|------|--------|
| Local (small / medium / large) | Ollama — Qwen, Llama, DeepSeek |
| Cloud lite | Claude Haiku, GPT-4o-mini |
| Cloud fast | Claude Sonnet, GPT-4o |
| Cloud strong | Claude Opus, o3 |

```bash
orb --local-only "hello world"          # force Ollama
orb --cloud-only "complex refactor"     # force cloud
orb --model claude-opus-4-6 "..."       # pin a specific model
```

---

## Common Flags

| Flag | Description |
|------|-------------|
| `--topology` | Agent topology (`auto`, `triad`, `dual-review`, `hierarchy`, or custom id) |
| `--budget N` | Max message count (default: 200) |
| `--tui` | Terminal UI |
| `--dashboard` | Web dashboard at localhost:8080 |
| `--local-only` | Force all agents to use Ollama |
| `--cloud-only` | Force all agents to use cloud models |
| `--model MODEL` | Override model for all agents |
| `--ollama-model MODEL` | Override Ollama model |

---

## Testing

```bash
pytest tests/ -v

# Integration tests (requires live API key)
ANTHROPIC_API_KEY=sk-ant-... pytest tests/integration/ -v
```

---

## Project Structure

```
orb/
├── agent/          # LLMAgent, tools, fact extraction, prompt building
├── cli/            # CLI, REPL, TUI, auth, config
├── llm/            # LLM clients (Anthropic, OpenAI, Ollama)
├── memory/         # GraphRAG — SubgraphStore, ChromaDB backend, BridgeAgent
├── messaging/      # MessageBus, AgentChannel, message types
├── orchestrator/   # Run lifecycle, completion tracking
├── topologies/     # YAML loader, schema, built-in topologies
└── runtime/        # GraphRuntime — wires everything together
web/
├── server.py       # aiohttp server + WebSocket
├── bridge.py       # Bus events → dashboard state
└── static/         # Dashboard UI (HTML/CSS/JS)
```

---

## License

GNU GPL v3.0 — Copyright (C) 2026 Souranil Sen.
