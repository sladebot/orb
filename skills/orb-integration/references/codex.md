# Codex adapter

Map Orb operations to Codex's native collaboration tools.

- `spawn`: call `spawn_agent` with a bounded task name and minimal relevant
  context. Use shared-workspace agents for read-only research and review;
  coordinate write ownership explicitly.
- `send`: call `send_message` for context updates or `followup_task` to trigger
  another bounded turn.
- `wait`: call `wait_agent`; inspect agent messages and final status.
- `collect`: use the agent's final result plus workspace diffs and test output.
- `cancel`: call `interrupt_agent`.

Codex supports parallel execution when collaboration slots are available.
Follow repository instructions and the user's authorization in every delegated
task. Use the primary agent to synthesize results and resolve conflicts.

For visual status, render Mermaid in a commentary update at topology selection
and meaningful role-state transitions. Use `list_agents` to reconcile displayed
state with the actual Codex agent tree.

## Model allocation and peer exchange

Inspect the current tool schemas and governing instructions before choosing models.
Use a model override only when both the tool and active instructions permit it;
otherwise inherit the host model. Do not assume another provider is available.
If overrides require a particular context-fork mode, obey that requirement and pass
an explicit task brief. For independent first proposals, prefer fresh contexts with
identical source facts and no peer proposals when the exposed API supports that.

Treat the agent tree as execution ownership; it need not determine deliberation
edges. Use `send_message` for updates to a running agent and `followup_task` for work
on an idle agent. Relay peer questions through the host if direct access is restricted.
Keep the reconciler in the primary agent when another slot would add no useful work.
