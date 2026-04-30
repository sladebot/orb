# Orb

[![PyPI](https://img.shields.io/pypi/v/orb-agents.svg)](https://pypi.org/project/orb-agents/)
[![Python](https://img.shields.io/pypi/pyversions/orb-agents.svg)](https://pypi.org/project/orb-agents/)
[![License: GPL v3](https://img.shields.io/badge/license-GPLv3-blue.svg)](LICENSE)

> Multi-tenant multi-agent coding runtime — daemon, terminal UI, browser dashboard, persisted run traces, topology selection, per-node model allocation, and a Python SDK for harness integration.

![Orb dashboard](docs/orb-dashboard.png)

Orb treats coordination as a runtime problem, not just a prompt problem. It picks a topology, classifies the task, assigns a model per node, runs the graph, and records enough telemetry to inspect what happened afterward. A single daemon hosts N concurrent sessions — each with its own workdir, FSM, and dashboard state — so external harnesses can run parallel evals against one Orb instance.

## Highlights

- **Four bundled topologies** (`solo`, `triad`, `dual-review`, `hierarchy`) with a classifier that picks the smallest one the task actually needs.
- **Per-node model allocation** — each node gets its own model based on role, complexity, and provider availability, instead of one model across the whole graph.
- **Multi-tenant daemon** hosting N concurrent sessions on `0.0.0.0:1337` with a versioned `/api/v1/*` HTTP + WebSocket API.
- **Live token streaming** in TUI and dashboard with per-`(chain_id, from)` lane separation; multi-agent runs render each agent's stream independently.
- **Pre-write approval** gate for agent file writes — `y / a / e / n` keys (or auto-approve via `--no-review`).
- **Auto-restoring sessions** on daemon restart; explicit run state machine (`idle → planning → running → completed | errored | stopping`); persisted session-aware traces.
- **Five providers** — `vmlx`, `omlx`, `openai-codex`, `ollama`, `anthropic` — with bounded timeouts and user-visible retry reasons.
- **Python SDK** (`orb.client.OrbClient`) for programmatic control.

## Quick start

```bash
pip install orb-agents
orb onboard
orb daemon start
orb tui
```

Or install from source:

```bash
git clone https://github.com/sladebot/orb.git && cd orb
pip install -e .
```

Full setup including provider auth: [docs/install.md](docs/install.md).

## TUI

![Orb TUI](docs/orb-tui.svg)

Streaming tokens, per-write approval prompts, slash commands. Press `Enter` to submit, `?` for help. Full UX in [docs/dashboard-and-tui.md](docs/dashboard-and-tui.md).

## Topologies

| Solo | Triad | Dual Review | Hierarchy |
|---|---|---|---|
| (single agent) | ![Triad](docs/topology-triad.png) | ![Dual Review](docs/topology-dual-review.png) | ![Hierarchy](docs/topology-hierarchy.png) |
| One agent end-to-end | Coordinator → Coder → Reviewer + Tester | Coordinator → Coder fans to Reviewer A, Reviewer B, Tester | Coordinator → Researcher → Coder → Reviewer + Tester |

More on classification, locking, and custom topologies in [docs/topologies.md](docs/topologies.md).

## Documentation

| Topic | Doc |
|---|---|
| Install + provider auth | [docs/install.md](docs/install.md) |
| First run, daemon, sessions | [docs/getting-started.md](docs/getting-started.md) |
| CLI reference | [docs/cli.md](docs/cli.md) |
| Topologies + classifier + custom | [docs/topologies.md](docs/topologies.md) |
| Models, providers, streaming, GraphRAG | [docs/models.md](docs/models.md) |
| Dashboard, TUI, slash commands, traces | [docs/dashboard-and-tui.md](docs/dashboard-and-tui.md) |
| Python SDK + REST/WS API | [docs/sdk.md](docs/sdk.md) |
| Architecture, repo layout, daemon internals | [docs/architecture.md](docs/architecture.md) |
| Tests + parity rules | [docs/development.md](docs/development.md) |

## Status

Currently shipping:

- multi-tenant daemon (N concurrent sessions, per-session workdir isolation)
- daemon-backed TUI and dashboard (WS multiplexed by `session_id`)
- session resume + auto-sanitization of stale in-flight markers
- Python SDK + envelope-based `/api/v1/*` HTTP API
- explicit runtime state machine, persisted session-aware traces
- four bundled topologies with hot-reload + inline SVG preview
- provider-backed topology classification (with trivial-query short-circuit + explicit-topology fast path)
- per-node model allocation
- five providers with bounded timeouts and user-visible retry reasons
- live token streaming with per-`(chain_id, from)` lane separation
- pre-write file approval pipeline (TUI `y/a/e/n` + dashboard pill UI)

Next major layer: execution control — budget enforcement, timeout/fan-out policy, and controller-driven early stop/escalation.

## License

GNU GPL v3.0. Copyright (C) 2026 Souranil Sen.
