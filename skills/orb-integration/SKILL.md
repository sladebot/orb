---
name: orb-integration
description: Operate the Orb multi-agent runtime and explain how to use Orb from an external orchestrator such as OpenClaw. Use when working on Orb daemon/dashboard operations, local-only Ollama runs, architecture understanding, model-routing behavior, or wrapper-style integrations around Orb. Not for claiming Orb is already a native OpenClaw runtime.
---

# Orb integration

Use this skill for tasks involving the Orb repo and runtime.

## Start here

1. Read `references/orb-notes.md`.
2. Identify whether the task is about:
   - architecture
   - daemon/dashboard operations
   - local-only Ollama usage
   - wrapper/integration planning
   - helper tooling
3. Prefer truthful, operator-style guidance over overstating integration maturity.

## Honest boundary

Orb is its own Python application and agent runtime.

This skill is best for:
- explaining Orb architecture
- operating Orb locally
- using Orb with local or cloud models
- documenting how an external orchestrator can call Orb
- adding helper scripts and usage patterns

This skill does not mean Orb is already a native runtime inside another system.

## Key commands

```bash
orb daemon status
orb daemon start
orb daemon restart --local-only
orb daemon restart --cloud-only
orb daemon stop
orb dashboard --connect http://127.0.0.1:8080
orb tui --connect http://127.0.0.1:8080
orb logs --no-follow
orb logs -f
```

## Local model usage

```bash
orb --local-only "task"
orb daemon restart --local-only
orb --ollama-model qwen3.5:9b "task"
orb --model qwen3.5:27b "task"
```

## Working pattern

Use this default sequence:
1. verify daemon status
2. start/restart with the right provider mode
3. inspect logs or dashboard if behavior is unclear
4. explain architecture using the reference notes
5. propose adapter code only when deeper integration is explicitly requested

## Read on demand

- `references/orb-notes.md`
- `README.md`
- `orb/cli/main.py`
- `orb/runtime/graph_runtime.py`
- `orb/llm/model_selector.py`
- `orb/llm/ollama.py`
- `CLAUDE.md`

## Helper scripts

Use scripts in `scripts/` for repetitive operations instead of retyping long command sequences.
