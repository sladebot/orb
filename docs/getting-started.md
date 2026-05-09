# Getting started

## Daemon + clients

Start the daemon:

```bash
orb daemon start
```

The daemon binds to `0.0.0.0:1337` by default — accessible from other hosts on the LAN. Pass `--host 127.0.0.1` for localhost-only.

Attach the TUI:

```bash
orb tui
```

Open the dashboard:

```bash
orb dashboard
```

Stop the daemon:

```bash
orb daemon stop
```

For a different port:

```bash
orb daemon start --port 5000
orb tui --port 5000
orb dashboard --connect http://127.0.0.1:5000
```

## Start work directly from the client

You can pass a query as the first positional argument:

```bash
orb tui --port 5000 "fix the failing tests"
orb dashboard --connect http://127.0.0.1:5000 "review the current diff"
```

## Scope a session to a workdir

Sessions are anchored to their own workdir without the daemon ever calling `os.chdir`, so concurrent sessions in other folders stay isolated:

```bash
orb dashboard --workdir ~/projects/url-shortener
```

## Pick a topology and pin per-node models

```bash
orb dashboard \
  --agent-model coder=claude-opus-4-7 \
  --agent-model reviewer=claude-sonnet-4-6 \
  "add a rate limit to /shorten"
```

The dashboard exposes workspace, topology, and per-node model controls from the **⊘ Session** button in the chrome. The CLI supports `--workdir` and repeatable `--agent-model role=model_id` pins; choose topology in the dashboard session modal.

## Dashboard workflow

1. Open the dashboard (`orb dashboard`). The **Session** modal auto-opens on first load so you can pick a workdir before anything else happens.
2. Browse to a folder with the built-in file picker or paste an absolute path. Orb auto-detects whether it's a git repo and offers to run `git init` inline if it isn't.
3. Pick a topology (with inline SVG preview) and optionally pin a model per node — or leave everything on Auto.
4. Type a task in the composer and press **Send** (⌘↵).
5. The topology panel lights up as nodes start working; file writes stream into the Repository changes panel with per-author attribution; the Conversation drawer streams agent-to-agent messages live.
6. When planning completes, Orb pins the topology + per-node model map onto the session — follow-up turns reuse that allocation instead of re-classifying.
7. To return to a prior session after a daemon restart (or just to continue an older run), click **Resume** in the chrome — the modal lists every known session with workdir, topology, and last-touched timestamp; clicking one reattaches the dashboard.

## TUI workflow

The TUI is single-pane by design and modeled on chat clients.

- The picker on launch covers topology selection (no `--topology` flag needed)
- `Enter` submits the composer; `Ctrl+Enter` does the same for muscle memory
- Slash commands: `/help`, `/clear`, `/stop`, `/resume`, `/topology <id>`, `/quit`
- Approval keys (when enabled): `y` accept, `a` accept all, `e` edit, `n` reject — work even with the composer focused

See [Dashboard and TUI](dashboard-and-tui.md) for the full UX details.

→ Next: [CLI reference](cli.md) · [Topologies](topologies.md)
