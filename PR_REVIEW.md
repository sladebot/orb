# PR #1 Review: Dynamic Topology Configuration via YAML

**Verdict: Request Changes**

Overall this is a well-structured change that replaces hardcoded Python topology factories with a declarative YAML-based system. The Pydantic validation, loader caching, and hot-reload watcher are solid. Tests are comprehensive (30 new tests all passing). A few issues to address before merge:

## Must Fix: TUI/Dashboard Parity Violation

Per `CLAUDE.md`, TUI and dashboard must stay in sync. This PR has several parity gaps:

1. **REPL gets `/topology`, `/list-topologies`, `/reload-topologies` commands — TUI gets none.** The TUI's `action_submit_input()` has no command parsing for topology switching. Users can only set topology via `--topology` at startup.

2. **Dashboard gets an interactive topology dropdown — TUI only gets display-only listing.** The idle screen in `GraphPanel.render()` now dynamically lists topologies (good), but there's no way to select one interactively.

3. **Dashboard handles `topologies_reloaded` WebSocket event — TUI ignores it.** The `_handle_server_event()` method has no case for `topologies_reloaded`, so hot-reloaded topologies won't appear in the TUI until restart.

## Should Fix

4. **Hardcoded model map in `factory.py:_resolve_model_selection()`** — Lines with `gpt-5.4`, `o3`, `qwen3.5:27b` are hardcoded provider-to-model mappings. These will go stale as models evolve. Consider deriving available models from the provider instances or making them configurable.

5. **`_topology_meta()` in `graph_runtime.py` uses fragile heuristics** — The position assignment (`if "coder" in agent_id`, `if "reviewer" in agent_id`) will misclassify custom agents with non-standard names. Consider adding a `position` or `role_hint` field to the YAML schema instead.

6. **No `graph_view` defined for either builtin topology** — Both `triangle` and `dual-review` in `defaults.py` omit `graph_view`, so they'll fall through to the auto-generated fallback (one node per row, no edges). The old hardcoded graph views in `graph_runtime.py` had nice ASCII layouts with edge lines. Either add `graph_view` to the builtins or port the old layouts into the YAML.

7. **Global singleton pattern for loader/watcher** — `_loader` and `_watcher` module-level globals make testing harder and prevent multiple instances. Consider using dependency injection or at minimum adding a `reset()` method for tests.

## Nits

8. Extra blank lines added in `tui.py` (lines 465, 2160) — minor whitespace noise.

9. `sample.py` deleted — was this intentional? The commit message doesn't mention it.

10. The `/topology` command in REPL also accepts `use topology <name>` (undocumented alias) — consider removing or documenting it.

## What's Good

- Clean Pydantic schema with cross-reference validation
- User override file at `~/.orb/topologies.yaml` with mtime-based reload
- Graceful fallback when user file is malformed (builtins still load)
- Good test coverage: schema validation, loader behavior, factory creation
- Dashboard topology dropdown is well-implemented with disabled-during-run state
- `sample-topology.yaml` is a helpful starter template
