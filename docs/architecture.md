# Architecture

## Repo layout

```text
orb/
├── agent/          # agent runtime, tools, compaction, prompting
├── cli/            # CLI entrypoints, daemon management, auth, config, TUI
│   └── paths.py    # single source of truth for ~/.orb/ layout
├── llm/            # provider integrations (vmlx, omlx, ollama, openai-codex, anthropic)
├── memory/         # GraphRAG and Chroma-backed memory backends
├── messaging/      # message bus, channels, message types
├── orchestrator/   # orchestrator runtime
├── runtime/        # graph runtime, classifier, session and trace plumbing
├── topologies/     # bundled topology definitions, schema, loader, factory
├── tracing/        # run trace schema and persistence helpers
web/
├── bridge.py       # runtime events → dashboard state
├── server.py       # API, websocket, dashboard, trace admin endpoints
├── api_v1.py       # envelope-based /api/v1/* routes
└── static/         # browser UI
```

## Filesystem layout

Orb anchors all of its own state under `~/.orb/` — your project workdir is never polluted with bookkeeping files. The session's `workdir` is strictly the sandbox root for agent file operations; Orb writes zero bytes there.

```text
~/.orb/
├── config.json                  # provider + model config
├── run.log                      # shared daemon log
└── daemon/                      # fixed anchor (never /tmp)
    ├── daemon.json              # pid / host / port
    ├── registry.json            # session index (survives restarts)
    └── sessions/
        └── {session_id}/
            ├── snapshot.json    # ConversationSession (turns, carryover)
            ├── dashboard.json   # dashboard state
            └── traces/
                ├── {run_id}.json
                └── by-session/{session_id}.json
```

The daemon always anchors at `~/.orb/daemon/` regardless of the shell's CWD — so registries, snapshots, and traces survive restarts and reboot-wiped `/tmp`.

## Multi-tenant daemon

A single daemon registers N `GraphRuntime` instances keyed by `session_id`. Broadcasts are multiplexed onto one WebSocket (`/api/v1/ws?session_id=X`); every payload is tagged with its originating session so the dashboard can filter per-client.

The registry at `~/.orb/daemon/registry.json` maps each session to its workdir + snapshot path. On daemon restart, the dashboard's stale `?session=...` URL transparently resurrects the session from disk; on genuinely missing sessions the WS handler emits `SESSION_NOT_FOUND` and the frontend drops the stale URL before reconnecting.

If a daemon dies mid-run, the resurrected session's dashboard state is **sanitized** on restore — any `run_state: running` or stuck agent statuses from the crashed daemon are rewritten to `errored` / `idle` so the UI doesn't show a phantom in-flight run.

```bash
orb sessions list
orb sessions show <prefix>
orb sessions rm <prefix>
orb sessions prune --older-than 7d
```

## Run lifecycle

Explicit state machine: `idle → planning → running → completed | errored | stopping`. Every transition broadcasts a `run_state_changed` event so clients see the full lifecycle.

- `start_run` fires `start_run_begin` (IDLE → PLANNING)
- After topology + models are decided, `orchestrator_task_created` fires (PLANNING → RUNNING)
- Successful completion fires `orchestrator_succeeded` (RUNNING → COMPLETED)
- `stop_requested` fires (PLANNING/RUNNING → STOPPING), then `stop_finished` lands in IDLE
- Any unhandled exception fires `orchestrator_errored` (PLANNING/RUNNING/STOPPING → ERRORED)

`adelete_session` awaits the run-task drain with a bounded timeout (uses `asyncio.wait` rather than `asyncio.wait_for` so a task that swallows `CancelledError` doesn't hang the HTTP worker forever).

## Broadcast event types

Every per-session event is tagged with its originating `session_id` and fanned out to every WebSocket subscribed to that session.

| Event type | When |
|---|---|
| `init` | Client connects; run starts (re-broadcast in planning) |
| `run_state_changed` | FSM transition |
| `message` | Message routed between agents |
| `message_delta` | Streamed token chunk (one stream per `(chain_id, from)`) |
| `agent_status` | Agent enters running / completed / errored |
| `agent_activity` | Free-form activity text from an agent |
| `agent_stats` | Per-agent message count / status / model |
| `agent_heartbeat` | Periodic keep-alive |
| `complete` | Agent emits a final result |
| `plan_step` | Planning milestone (classifier, routing, allocation) |
| `file_write` | Approved file write hit disk |
| `file_write_pending` | Staged file write awaiting user approval |
| `file_write_rejected` | User (or teardown) rejected a staged write |
| `run_complete` | Run terminated successfully |
| `topologies_reloaded` | Topology YAML hot-reloaded (broadcast to all clients) |
| `error` | Session-not-found WS error |

## Pre-write approval pipeline

When `approval_required` is set on a session:

1. Agent's `_handle_write_file` awaits `_on_write_request(agent, path, content, old_content)`.
2. `GraphRuntime.request_write_approval` mints a `request_id`, stores a `PendingApproval` dataclass with an `asyncio.Future`, broadcasts `file_write_pending`, awaits the future.
3. `POST /approvals/{request_id}` resolves the future with `(approved, effective_content)`. On approve the agent writes (using `edited_content` if provided); on reject the agent skips the write and records a `tool_result` saying it was rejected so the next LLM turn doesn't 400 on a dangling tool_use.
4. Auto-reject fires on run stop, session delete, and orchestrator `CancelledError`/`Exception` paths so no agent ever strands on a future.

The hook is wired only when `approval_required is True` — zero overhead on the default direct-write path.

## Topology classifier

The classifier is behind a dedicated runtime interface. The current implementation is provider-backed (an LLM call) but skips the round-trip when:

- The session has a locked topology (re-uses it)
- The caller passes an explicit topology (`_manual_prediction` synthesizes a classification with a query-aware `stop_early_allowed`)
- The query is objectively trivial (≤ 3 words, no domain keywords, no `@agent` scope) — heuristic synth with `stop_early_allowed=True` so multi-agent topologies short-circuit cleanly

→ See: [Models](models.md) · [Topologies](topologies.md) · [SDK](sdk.md)
