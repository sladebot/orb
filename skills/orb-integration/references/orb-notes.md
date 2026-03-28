# Orb notes

## Summary

Orb is a Python multi-agent runtime with:
- multiple cooperating agent roles
- dynamic topology selection
- per-agent model allocation
- local Ollama and cloud providers
- a TUI and web dashboard
- persistent session and GraphRAG-style memory

## Important files

- `README.md` — user-facing overview and commands
- `pyproject.toml` — dependencies and CLI entrypoint
- `orb/cli/main.py` — daemon control, logs, dashboard, TUI, run flags
- `orb/runtime/graph_runtime.py` — topology prediction, model assignment, runtime orchestration
- `orb/llm/model_selector.py` — complexity-to-tier scoring
- `orb/llm/ollama.py` — Ollama provider implementation
- `orb/topologies/` — topology definitions and loading
- `orb/agent/` — agent loop, prompt builder, tool handling, compaction
- `orb/messaging/` — bus, channels, messages
- `orb/memory/` — retrieval, graph store, GraphRAG wiring
- `web/server.py`, `web/bridge.py` — dashboard backend and event bridge
- `CLAUDE.md` — keep TUI and dashboard behavior aligned

## Runtime facts

- Default dashboard URL: `http://127.0.0.1:8080`
- Default log file: `~/.orb/run.log`
- Daemon state: `~/.orb/daemon.json`
- User topologies: `~/.orb/topologies.yaml`

## Commands

### Setup

```bash
conda create -n orb python=3.12 -y && conda activate orb
pip install -e .
orb onboard
```

### Daemon and UI

```bash
orb daemon start
orb daemon status
orb daemon restart --local-only
orb daemon restart --cloud-only
orb daemon stop
orb tui --connect http://127.0.0.1:8080
orb dashboard --connect http://127.0.0.1:8080
```

### Logging

```bash
orb logs --no-follow
orb logs -f
```

## Model routing

Routing happens in layers:
- complexity scoring in `orb/llm/model_selector.py`
- topology and per-agent assignment in `orb/runtime/graph_runtime.py`
- runtime constraints via `--local-only`, `--cloud-only`, `--model`, `--ollama-model`

For local-first usage:

```bash
orb --local-only "task"
orb daemon restart --local-only
orb --ollama-model qwen3.5:9b "task"
```

## External orchestrator integration

### What works now

An external orchestrator can operate Orb as a local engine by:
- checking daemon status
- starting/stopping/restarting Orb
- selecting local-only or cloud-only modes
- inspecting logs and dashboard
- choosing pinned Ollama models

### Minimum viable integration

The simplest useful integration is wrapper-style:
1. ensure Orb is installed
2. run Orb with the desired flags
3. inspect output/logs/dashboard
4. summarize results back to the user

### What would need more code

Deeper integration would require adapters for:
- task submission and result harvesting
- state/tool translation between systems
- tighter automation around Orb sessions

## Cautions

- Dependencies are heavier than a tiny utility CLI.
- 27b local models may be slow and need enough memory.
- Dashboard exposure should be deliberate.
- Be honest about current integration maturity.
