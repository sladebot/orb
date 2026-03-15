# Sprint: Comprehensive Conversation Context

## Goal

Upgrade Orb's agent conversation model from:

- "append the latest incoming message and tool results"

to:

- "build and send a coherent, bounded conversation transcript that includes user turns, agent-to-agent turns, and relevant shared context"

so model requests behave more like Claude Code / Codex style session continuity.

## Current State

Today the main flow is:

1. A `Message` is delivered to an agent.
2. [`LLMAgent.process`](/Users/souranil/projects/orb/orb/agent/llm_agent.py) formats only that incoming message via `_format_incoming(...)`.
3. That formatted string is appended into the agent's private [`ConversationHistory`](/Users/souranil/projects/orb/orb/agent/conversation.py).
4. Tool results are appended as Anthropic-style `tool_result` user blocks.
5. The next model call receives only that agent's local history plus tool traces.

This means Orb is missing a true session transcript model:

- user follow-ups are not represented as first-class session turns across the graph
- agent-to-agent exchanges are not normalized into a reusable transcript object
- the "conversation" sent to the model is mostly a local working log, not a shared session history
- carryover is per-agent and lossy relative to the full collaboration

## Target End State

Each agent model call should be built from three layers:

1. Local execution history
- tool calls
- tool results
- the agent's own prior assistant outputs

2. Shared session transcript
- user requests
- direct user replies to agents
- routed agent-to-agent messages
- important completion/review/feedback turns

3. Structured runtime context
- topology position
- neighbor set
- current task/run metadata
- compacted prior-session summary when needed

The daemon/runtime should own transcript construction. Agents should consume a normalized transcript view, not invent it locally.

## Architecture Direction

### 1. Introduce a canonical conversation transcript model

Add a runtime-owned transcript abstraction, for example:

- `ConversationTurn`
  - `id`
  - `speaker`
  - `audience`
  - `kind` (`user_task`, `user_reply`, `agent_message`, `feedback`, `completion`, `system`)
  - `content`
  - `timestamp`
  - `chain_id`
  - `depth`
  - `run_turn`
  - `attachments/context_slice`

- `RunTranscript`
  - append turn
  - query recent turns
  - filter by audience/agent
  - produce summarized windows

This should live below the UI and above agent prompt assembly.

### 2. Separate shared transcript from local agent history

Keep local Anthropic/OpenAI tool-call formatting in [`ConversationHistory`](/Users/souranil/projects/orb/orb/agent/conversation.py), but stop treating it as the full session history.

Instead:

- `RunTranscript` = source of truth for multi-party conversation
- `ConversationHistory` = provider-facing local working trace

### 3. Build model requests from a composed context builder

Introduce a request builder layer, for example:

- `build_agent_request(agent, incoming_msg, transcript, local_history, topology_context, compaction_state)`

This builder should assemble:

- system prompt
- shared transcript window
- current local work state
- provider-specific tool formatting

That avoids pushing more transcript logic into `LLMAgent.process(...)`.

### 4. Make transcript selection explicit

Do not blindly dump every event forever.

The context builder should include:

- latest user task / follow-up
- recent turns involving this agent
- recent turns between this agent and its neighbors
- notable upstream/downstream summaries
- unresolved user questions or review feedback

and exclude:

- low-signal duplicate tool chatter
- irrelevant branches from unrelated agent threads unless summarized

## Phased Plan

## Phase 1: Add Runtime Transcript Infrastructure

Implement a daemon/runtime-owned transcript.

Scope:

- add transcript models under `orb/runtime/` or `orb/conversation/`
- append transcript turns whenever:
  - orchestrator injects user task
  - runtime injects user reply
  - message bus routes agent messages
  - agents complete
  - consensus/system events matter for context
- keep this independent of UI rendering

Exit criteria:

- one run produces a complete machine-readable transcript covering user and agent turns

## Phase 2: Build a Conversation Context Composer

Add a composer that converts transcript + local history into provider request messages.

Scope:

- create a transcript windowing strategy per target agent
- include:
  - current user objective
  - latest relevant transcript turns
  - unresolved feedback/questions
  - topology metadata
- preserve tool-call compatibility for Anthropic/OpenAI

Exit criteria:

- model calls are built from shared transcript plus local tool state, not just `_format_incoming(...)`

## Phase 3: Refactor `LLMAgent.process(...)`

Move request assembly out of the agent loop.

Scope:

- reduce `LLMAgent.process(...)` responsibility to:
  - receive message
  - update local execution state
  - ask composer for provider request
  - execute tool loop
- stop using `_format_incoming(...)` as the primary conversation model
- keep provider-specific tool result handling in local history

Exit criteria:

- agent loop is thinner and conversation assembly is centralized

## Phase 4: Add Transcript Compaction and Relevance Filtering

A full transcript will grow quickly. Add bounded context management.

Scope:

- transcript compaction for older turns
- per-agent relevance filters
- preserve:
  - decisions
  - file/work summaries
  - open questions
  - latest user intent
- avoid token blowups from raw multi-agent chatter

Exit criteria:

- long sessions remain coherent without context overflow

## Phase 5: Expose Transcript Semantics to UI and Debugging

Once transcript is canonical, UIs should read from it.

Scope:

- dashboard/TUI can show transcript-derived conversation views
- inspector can distinguish:
  - shared transcript turn
  - local tool trace
  - user reply
  - agent handoff
- add debug visibility for the exact request context sent to each model

Exit criteria:

- UIs reflect the same conversation model the daemon uses

## Key Design Decisions

### A. Full conversation does not mean "everything verbatim forever"

Claude Code / Codex feel coherent because they preserve continuity, not because they resend unbounded raw logs.

Orb should aim for:

- authoritative transcript
- relevance-filtered request windows
- compaction for older context

not:

- every message forever in every prompt

### B. Shared transcript and local tool history should remain separate

Tool traces are needed for provider protocol correctness.

But:

- shared collaboration transcript
- local provider/tool transcript

are different layers and should not be conflated.

### C. Transcript ownership belongs in runtime, not in UIs

The daemon must remain the source of truth so:

- TUI/dashboard stay subscribers
- attach/reconnect works
- future API clients behave consistently

## Tests To Add

### Transcript correctness

- user task appears as first transcript turn
- direct user reply to an agent is recorded distinctly from the original task
- routed agent messages are recorded once with correct speaker/audience metadata
- completion and feedback events are recorded as structured turns

### Context composer behavior

- agent request includes latest user objective
- agent request includes relevant neighbor turns
- irrelevant branches are excluded
- unresolved question from reviewer/tester is preserved until answered

### Regression tests

- no consecutive invalid provider message roles
- tool results still remain Anthropic-compatible
- compaction preserves key decisions
- follow-up tasks retain prior session continuity without replaying all raw chatter

## Risks

1. Token growth
- A naive "full transcript" implementation will explode context size.

2. Provider formatting conflicts
- Anthropic/OpenAI tool-call message constraints still apply.

3. Duplicate information
- If shared transcript and local history both restate the same turn, prompts will bloat and models may behave worse.

4. Cross-agent contamination
- Every agent should not necessarily see every raw branch verbatim.

## Recommended Implementation Order

1. Runtime transcript model
2. Transcript append points in orchestrator/runtime/message bus
3. Context composer
4. `LLMAgent.process(...)` refactor
5. Compaction/relevance filtering
6. UI/debug surfaces

## Definition of Done

This feature is done when:

- each model call is built from a structured shared transcript plus local execution state
- user follow-ups and agent-to-agent discussion persist coherently across turns
- long sessions compact safely
- daemon remains the sole owner of transcript state
- TUI/dashboard reflect transcript state without owning it
