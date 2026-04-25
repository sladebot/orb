# Dashboard and TUI

## Dashboard

![Orb dashboard](orb-dashboard.png)

Three-column workbench: a topology minimap and agents list on the left, **Repository changes** (file tree + unified diff) as the hero in the middle, and a conversation drawer on the right. Top breadcrumbs show the full session workdir (tildified as `~/projects/repo`); the hero toolbar repeats it alongside branch/diff stats; agents render as compact pill chips with a role-colored status dot; a summary strip tracks status, elapsed, messages, files touched, topology, and a live throughput sparkline.

### Session config

![Session configuration modal](orb-session-modal.png)

The Session modal sets **workspace**, **topology**, and **per-node model** pins before a run. Each session is scoped to its own workdir — the sandbox is rooted there and every filesystem tool call resolves against it. No `os.chdir` in the daemon process, so concurrent sessions in different workdirs stay isolated. Agents see the absolute workdir in their system prompt (`## Working directory`).

### Repository changes

![Repository changes panel](orb-dashboard-repo.png)

Every file write the crew performs lands in the middle panel. The file tree groups by folder, shows each file's +/− line counts, and attributes the change to the agent that made it. Clicking a file renders a unified diff with per-hunk author chips.

### Live behavior

The dashboard is event-driven and shows the run as it happens:

- renders planning state as soon as topology and per-node model allocation are known
- lays out agents and edges in the topology minimap; panel height adapts to the topology graph
- surfaces `file_write` events in the Repository Changes panel as a file tree + unified diff with per-author attribution
- streams agent-to-agent messages into the Conversation drawer with routing arrows, msg-type badges, and role-colored dots
- tracks stats (status, elapsed, messages, files touched, topology, throughput) in the top summary strip
- opens a compact agent detail overlay in the left column when you click an agent row — toggles off when clicked again
- supports draggable column resize handles on desktop and a fixed-bottom composer on mobile
- locks topology + per-node model allocation on the session after the first run; subsequent messages reuse the same graph

The daemon writes more descriptive run and dashboard event logs to `~/.orb/run.log`.

### Pre-write approval (dashboard parity)

Pending writes render with a yellow approval pill; resolved writes flip to green (applied) or red (rejected). The wire contract is shared with the TUI:

- `file_write_pending {agent, request_id, path, content, old_content}` — staged, awaiting user resolution
- `file_write_rejected {agent, request_id, path, reason}` — rejected by the user or auto-rejected on run teardown
- `file_write {agent, path, content, old_content}` — fires on **approved** writes only (post-disk)

`POST /api/v1/sessions/{sid}/approvals/{request_id}` body `{action: "approve"|"reject", edited_content?, reason?}` resolves a pending write.

## TUI

![Orb TUI](orb-tui.svg)

The TUI is a single-pane chat-stream, deliberately minimal so you can keep your eyes on the conversation and the live status bar.

### Streaming

LLM responses stream token-by-token at ~20fps. Renders are coalesced via a 50ms debounce on `Turn.append`, so a 1000-chunk response feels live without the O(n²) re-render cost of rebuilding markup per chunk. Two agents responding on the same chain — e.g. coordinator and coder echoing through `triad` — keep their streams in separate lanes keyed by `(chain_id, from)`. The terminal `message` event finalizes a turn without overwriting its streamed body.

### Pre-write approval

By default, agent file writes pause for explicit user approval — the TUI prompts with `y` (accept), `a` (accept all), `e` (edit), `n` (reject). Keys work even while the composer has focus (so you can answer the prompt without first clicking out of the input field) and only intercept while a write is actually pending — typing prose like "yes, ship it" into the composer reads as text when no approval prompt is up.

`a` (accept all) latches auto-approve for the rest of the session; `e` opens `$EDITOR` on the proposed content and approves with `edited_content` on save.

Pass `--no-review` to `orb tui` to let agents write directly, or set `approval_required: false` when creating a session via the API.

### Slash commands

| Command | Effect |
|---|---|
| `/help` | List every command |
| `/clear` | Clear the stream (also clears any in-flight streaming-turn state) |
| `/stop` | Stop the current run; emits a clear whisper if no session is active |
| `/resume` | Pick a prior session from the resume modal |
| `/topology <id>` | Switch routing topology for the next run; refused if the session is locked |
| `/quit` | Exit the TUI |

The picker on launch covers topology selection (no `--topology` flag needed); the picker omits `auto` so the classifier LLM never runs on the first turn — pass `auto` directly via the API if you want classifier-driven routing.

## Trace admin

The dashboard also exposes persisted run traces and session-aware history:

- topology choice
- task type
- routing mode and reason
- classifier model
- per-agent models
- stage timing
- token usage
- retries
- verifier and override events
- final outcome

```bash
orb trace latest
orb trace latest --json
orb trace list --current-session
orb trace tail --current-session
orb trace show <run_id>
```

Trace files are stored per-session under the daemon anchor (never inside your project repo):

```text
~/.orb/daemon/sessions/{session_id}/traces/{run_id}.json
```

## Resuming a session

Every prior session that still has a snapshot on disk can be reattached:

- **Dashboard**: click **Resume** in the header; pick a session from the modal (workdir, topology, last updated).
- **TUI**: press **Ctrl+R** to open the chooser; press 1–9 to switch.
- **API**: `GET /api/v1/sessions?include=known` returns both in-memory and registry-only sessions.

Resume preserves conversation turns, agent carryover, and locked topology/models. Running LLM calls are *not* replayed — agent tool calls (bash, partial file writes) aren't idempotent, so resume lands you in an `idle` session with full context, ready for a new turn.

→ See: [Architecture](architecture.md) · [SDK](sdk.md)
