# Python SDK

Orb ships a typed async client for programmatic control — used by external harnesses (hermes, openclaw) to drive parallel evals against one daemon.

```python
from orb.client import OrbClient

async with OrbClient("http://127.0.0.1:1337") as client:
    session = await client.create_session(
        workdir="/path/to/repo",
        topology="triad",
        agent_models={"coder": "claude-opus-4-7"},
    )
    await session.start_run("fix the failing tests")
    event = await session.wait_for_terminal()  # blocks until completed/errored
    print(event.run_state, event.final_result)
```

`OrbClient.stream_events(session_id)` yields every WebSocket event as a typed `Event` dataclass; `Event.is_terminal` is `True` for `completed` and `errored`.

## REST + WebSocket API

The SDK wraps the versioned `/api/v1/*` HTTP + WebSocket API. Every response uses the envelope:

```json
{"ok": bool, "code": "UPPER_SNAKE", "error"?: str, "data"?: dict}
```

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/sessions` | Create a session (`workdir`, `topology`, `agent_models`, `approval_required`, `streaming_enabled`) |
| `GET` | `/api/v1/sessions?include=known` | List in-memory + registry-only sessions |
| `GET` | `/api/v1/sessions/{sid}/state` | Full init payload |
| `DELETE` | `/api/v1/sessions/{sid}` | Tear down a session (async; awaits run-task drain) |
| `POST` | `/api/v1/sessions/{sid}/runs` | Start a run (`query`, `topology`) |
| `POST` | `/api/v1/sessions/{sid}/runs/inject` | Mid-run message |
| `POST` | `/api/v1/sessions/{sid}/runs/stop` | Cancel the active run |
| `POST` | `/api/v1/sessions/{sid}/approvals/{rid}` | Resolve a pending write (`approve` / `reject`) |
| `GET` | `/api/v1/ws?session_id=X` | WebSocket event stream for one session |

Every per-session WebSocket event is tagged with its originating `session_id` so multi-tenant clients can filter cleanly.

→ See: [Architecture](architecture.md) for the broadcast fan-out details · [Models](models.md) for the streaming event contract
