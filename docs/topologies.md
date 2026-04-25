# Topologies

Orb routes work through explicit topology graphs — not branching scattered across the codebase.

## Bundled topologies

| Topology | Shape | When to pick |
|---|---|---|
| `solo` | one agent, no coordinator | trivial / throwaway tasks |
| `triad` | coordinator → coder → reviewer & tester | balanced default |
| `dual-review` | coordinator → coder fans out to reviewer A, reviewer B, tester | stronger correctness/review |
| `hierarchy` | coordinator → researcher → coder → reviewer & tester | broader planning + execution |

Visual previews:

| Triad | Dual Review | Hierarchy |
|---|---|---|
| ![Triad](topology-triad.png) | ![Dual Review](topology-dual-review.png) | ![Hierarchy](topology-hierarchy.png) |

## Selecting a topology

```bash
orb tui --topology solo
orb tui --topology triad
orb tui --topology dual-review
orb tui --topology hierarchy
```

Or let Orb choose automatically:

```bash
orb tui --topology auto
```

## Task classification

Before execution, Orb performs a topology-classification step. The classifier:

- chooses a task type
- selects a topology
- records a routing reason
- returns candidate topologies
- records which model performed the classification

The classifier is behind a dedicated runtime interface — the current provider-backed lightweight classifier can be replaced by a trained in-house routing model without changing the runtime orchestration.

The dashboard surfaces:
- the classifier model used for routing
- the chosen topology
- the planned model for each agent card/node
- routing metadata in trace detail views

### Trivial-query short-circuit

The classifier's LLM call is skipped for objectively-trivial queries (≤ 3 words, no domain keywords like `code` / `review` / `risk`, no `@agent` scope). A heuristic synthesizes the classification with `stop_early_allowed=True` so multi-agent topologies like `triad` can terminate cleanly on one-word queries instead of looping.

### Explicit-topology fast path

When the user picks a concrete topology (anything other than `auto`), the classifier LLM is bypassed entirely. `_manual_prediction` inspects the query's signals and sets `stop_early_allowed` based on the same triviality heuristic. Saves the 3-15s classifier round-trip per submit.

## Topology lock

After a session's first run, Orb pins the chosen topology + per-node model map onto the session. Subsequent turns reuse the locked allocation instead of re-classifying. The TUI's `/topology <id>` slash command refuses non-matching values when a lock is active — surface the lock with the dashboard's pinned-picker UI, or start a new session to change it.

## Custom topologies

Create or edit a user-defined topology YAML:

```bash
orb topologies init
```

That copies a sample to:

```text
~/.orb/topologies.yaml
```

Orb hot-reloads topology definitions in the dashboard/runtime flow.

→ See: [Models](models.md) · [Architecture](architecture.md)
