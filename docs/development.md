# Development

## Run tests

```bash
pytest -q
```

Useful targeted suites:

```bash
pytest -q tests/test_cli_main.py
pytest -q tests/test_server_api.py
pytest -q tests/test_run_trace.py
pytest -q tests/test_server_events.py
pytest -q tests/test_streaming.py
pytest -q tests/test_streaming_flow_e2e.py
pytest -q tests/test_approval_flow_e2e.py
pytest -q tests/test_tui_dashboard_parity.py
```

The integration suite at `tests/integration/` is gated by env credentials; expect it to skip on a fresh checkout.

## TUI ↔ dashboard parity

`tests/test_tui_dashboard_parity.py` cross-checks that both `orb/cli/tui_repl.py` and `web/static/app.js` handle every broadcast event type from `web/state.py` / `web/bridge.py`. New event types must land in both clients in the same PR — the parity harness will fail otherwise.

## Strict warnings

The streaming pipeline coalesces renders via Textual's `set_timer`, which constructs a coroutine even when there's no event loop. The TUI guards this with an `is_mounted` check; the suite passes under strict warnings:

```bash
pytest tests/ --ignore=tests/integration -W error::RuntimeWarning
```

## Coding conventions

- Follow `CLAUDE.md` for TUI/dashboard parity rules and pre-write test discipline.
- Daemon defaults to `0.0.0.0:1337` so dashboards on other machines work without an extra flag.
- All `~/.orb/` paths route through `orb/cli/paths.py`.
- Provider/model selection comes from config + provider catalog — no hardcoded model IDs in runtime paths.

## Logs

```bash
orb logs                  # tails ~/.orb/run.log
tail -f ~/.orb/run.log    # equivalent
```

→ See: [Architecture](architecture.md)
