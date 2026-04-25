# CLI reference

```bash
orb --help
```

## Top-level commands

| Command | Purpose |
|---|---|
| `orb onboard` | Initial auth + provider setup |
| `orb auth <provider>` | Configure provider credentials |
| `orb daemon` | Daemon lifecycle (`start` / `stop` / `restart` / `run` / `status`) |
| `orb tui` | Attach the terminal UI to the daemon |
| `orb dashboard` | Open the browser dashboard |
| `orb sessions` | List / show / rm / prune active and on-disk sessions |
| `orb trace` | Inspect persisted run traces (`latest`, `list`, `tail`, `show`) |
| `orb topologies` | Manage user topology definitions |
| `orb models` | Inspect and refresh provider model catalogs |
| `orb config` | Read / write provider settings |
| `orb logs` | Stream `~/.orb/run.log` |

## Useful global flags

| Flag | Effect |
|---|---|
| `--model MODEL` | Pin a model |
| `--local-only` | Restrict to local providers |
| `--cloud-only` | Restrict to cloud providers |
| `--budget N` | Set a global message budget |
| `--timeout N` | Set timeout in seconds |
| `--connect URL` | Attach TUI / dashboard to an existing daemon |

## TUI-specific flags

- `--review` / `--no-review` — pre-write approval (default: on). With approval on, agent file writes pause for explicit `y/a/e/n` user confirmation.
- `--no-prompt` — skip the startup topology prompt; defaults to `triad` for non-interactive runs.
- `--workdir PATH` — scope the session to a folder (defaults to current directory).
- `--logs` — show a live log panel.

## Daemon-specific flags

- `--host` — bind address (default `0.0.0.0`)
- `--port` — port (default `1337`)
- `--workdir` — daemon workspace directory
- `--local-only` / `--cloud-only` — provider gating

## Sessions

```bash
orb sessions list                    # active + on-disk
orb sessions show <prefix>
orb sessions rm <prefix>
orb sessions prune --older-than 7d
```

## Traces

```bash
orb trace latest
orb trace latest --json
orb trace list --current-session
orb trace tail --current-session
orb trace show <run_id>
```

Trace files live per-session under the daemon anchor (never inside your project repo):

```text
~/.orb/daemon/sessions/{session_id}/traces/{run_id}.json
```

→ See: [Architecture](architecture.md) for daemon internals · [Getting started](getting-started.md)
