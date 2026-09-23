# Claude Code adapter

Map Orb operations to Claude Code's native task/subagent facilities.

- `spawn`: create a Task/subagent with a bounded role, relevant paths, and an
  explicit output contract.
- `send`: resume or message the task when the host exposes follow-up input;
  otherwise create a new bounded follow-up task with the collected result.
- `wait`: await Task completion and surface clarification requests to the user.
- `collect`: inspect the returned result, repository diff, and test evidence.
- `cancel`: stop the Task when supported; otherwise stop consuming its result
  and report the limitation.

Use parallel Tasks only when the active Claude Code environment exposes them.
Keep one writer per overlapping file set. Use Claude Code's native permissions
and hooks; Orb must not bypass them.

For visual status, use Claude Code's native task display when it exposes the
same roles and states. Otherwise present the rendered Mermaid graph at topology
selection and meaningful role-state transitions.

## Model allocation and peer exchange

Discover the current subagent tool schema; names and model options vary by version.
Select only exposed model choices and respect host/user pins. If individual model
selection is unavailable, inherit the host default and record that limitation.
A fresh subagent receives source facts and its task brief without peer proposals
for its first pass. Use available team messaging for peer exchanges; otherwise relay
attributed findings through the host. Never invent cross-provider support.
