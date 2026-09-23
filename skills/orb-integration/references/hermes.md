# Hermes adapter

Map Orb operations to the active Hermes agent/delegation interface.

- `spawn`: delegate a bounded role through Hermes's available subagent or task
  primitive. If none is exposed, run the role as a separate sequential pass.
- `send`: provide follow-up context through the task continuation primitive; if
  unavailable, start a follow-up task referencing the collected output.
- `wait`: use Hermes's task wait/status facility when available, otherwise run
  delegations synchronously.
- `collect`: retain the task result together with observable diffs, commands,
  and validation evidence.
- `cancel`: use the native cancellation primitive when exposed; otherwise
  report that cancellation is unavailable and stop scheduling follow-up work.

Detect capabilities at execution time because Hermes installations may expose
different tool sets. Preserve every required role sequentially when parallel
delegation is unavailable. Never invent tool names or claim native multi-agent
execution without an exposed capability.

For visual status, use a native Hermes task graph only when it can preserve the
Orb nodes, edges, and states. Otherwise present the rendered Mermaid graph at
topology selection and meaningful role-state transitions.

## Model allocation and peer exchange

Use per-agent model configuration only if the active delegation tool explicitly
supports it. Otherwise inherit and record `host-default`; do not create provider
clients to simulate unsupported features. Keep first proposals in separate contexts
where available, then use native peer messages or faithful host relay. Report when
sequential passes share context and cannot provide independent review.
