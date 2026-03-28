# Sprint: Measurable, Controllable, Adaptive Orb

## Goal

Shift Orb improvement away from "make the agents smarter" in isolation and toward three system properties:

- more measurable
- more controllable
- more adaptive

If Orb is a multi-agent runtime, the largest gains should come from better coordination, better runtime control, and better evidence about what actually worked.

## Strategic Thesis

Most multi-agent failures are not raw model failures.

They are usually one of:

- the wrong topology for the task
- too little runtime control over cost, latency, or fan-out
- weak or missing verification
- shared state that is too loose to trust
- no telemetry to explain why a run succeeded or failed

This sprint should therefore prioritize infrastructure that makes Orb observable and tunable before adding more agent complexity.

## Status Snapshot

- Phase 1 is complete.
- Phase 2 is complete.
- Phase 3 is complete.
- The original heuristic-routing plan has been superseded by a pluggable classifier interface with a lightweight provider-backed classifier, so future learned routing can replace the backend without changing runtime orchestration.
- The next phase should focus on verification policy and explicit verifier behavior.

## Current Gaps

Today Orb has the foundations for agent execution, routing, and UI visibility, but it is still missing the layers needed for systematic improvement:

- topology choice is too static
- runtime telemetry is not rich enough for comparison or replay
- execution control is limited
- verification is not first-class in planning or runtime policy
- memory boundaries and access policy are not explicit enough
- workdir context selection is too blunt; Orb needs to discover relevant local files on demand instead of pulling an entire custom workspace into context
- role definitions are still too generic
- adaptation by budget, latency, and risk is under-specified
- there is no strong replay/evaluation harness for configuration comparisons
- safety constraints mostly live at the action level, not the topology layer

## Target End State

Orb should be able to do all of the following:

1. Choose an execution topology intentionally.
- single agent when the task is simple
- planner -> executor -> verifier when structure is needed
- planner -> parallel workers -> judge when breadth is needed
- escalate to stronger structures only when signals justify it

2. Enforce runtime control.
- caps on budget, latency, fan-out, and per-agent token spend
- retries and kill policies for weak workers
- early stopping when confidence is already high

3. Verify important work deliberately.
- contradiction checks
- evidence-grounding checks
- mandatory review for risky actions
- synthesis scoring before final output

4. Record enough telemetry to explain outcomes.
- quality
- cost
- latency
- retries
- disagreement
- verifier catches
- human overrides

5. Learn from prior runs.
- compare topologies on the same tasks
- detect regressions
- produce routing data for future learned policies

## Phased Plan

## Phase 1: Instrument the Runtime

Make every run measurable before changing routing behavior aggressively.

Tasks:

- [x] define a canonical `RunTrace` schema for one Orb run
- [x] define event types for topology choice, agent spawn, stage start/finish, tool call, retry, verifier decision, human override, and final outcome
- [x] add trace collection hooks in orchestrator, runtime, message bus, and agent execution paths
- [x] attach per-agent model, role, token, and latency metadata to trace events
- [x] emit stable run IDs and trace IDs across logs and UI updates
- [x] add a trace export path for offline analysis and replay input generation

Scope:

- define a structured run trace schema
- capture:
  - chosen topology
  - agent count
  - per-agent model and role
  - token usage by agent
  - latency by stage
  - tool calls
  - retries
  - verifier catches
  - worker disagreement
  - human override rate
  - final success/failure
- expose trace IDs and run summaries in daemon logs and UI surfaces
- make telemetry exportable for offline analysis

Exit criteria:

- every non-trivial run emits a machine-readable trace that explains coordination, cost, and outcome

Status:

- Complete. Trace summaries, exported traces, session-aware trace lookup, routing metadata, and per-agent model visibility are all in place.

## Phase 2: Build a Topology Library and Routing Heuristics

Make topology selection explicit instead of implicit.

Tasks:

- [x] define the first approved topology set Orb is allowed to use
- [x] encode each topology as an explicit runtime structure rather than ad hoc branching logic
- [x] define task categories that drive routing decisions
- [x] implement first-pass heuristic routing rules per task category
- [x] implement escalation rules for moving from simple to stronger topologies
- [x] implement early-stop rules for cases where extra coordination is unnecessary
- [x] record routing inputs and decisions into telemetry

Scope:

- define a small library of approved topologies
  - single agent
  - planner -> executor
  - planner -> executor -> verifier
  - planner -> parallel workers -> synthesizer
  - planner -> parallel workers -> judge/verifier
- add routing heuristics keyed by task shape
  - simple direct task
  - broad research
  - coding task
  - risky action task
  - ambiguous task needing decomposition
- add escalation and stop-early rules
  - when to stay single-agent
  - when to fan out
  - when to escalate to a stronger topology
  - when to stop after verifier confidence is high
- store routing decisions in telemetry for later evaluation

Exit criteria:

- Orb can explain why it selected a topology and when it escalated or stopped

Status:

- Complete.
- The runtime now classifies tasks through a dedicated `TopologyClassifier` interface and a lightweight provider-backed implementation grounded in topology metadata and selection hints.
- Routing outputs now include task type, routing mode, routing reason, candidate topologies, routing signals, classifier model, and advisory escalation/early-stop recommendations with reasons.
- Dashboard and trace views expose that routing surface end to end, which satisfies the Phase 2 requirement that Orb can explain why it selected a topology and when it would escalate or stop.
- Enforcement remains intentionally out of scope for Phase 2. Escalation and early-stop are recommendations here; actual runtime enforcement belongs to Phase 3's execution controller layer.

## Phase 3: Add Execution Controller Policies

Turn orchestration into active runtime control.

Tasks:

- add run-level budget configuration and enforcement
- add per-agent token ceilings and overage handling
- add max fan-out limits at topology execution time
- add stage and worker timeout policies
- add worker health checks and low-quality worker retry/kill rules
- add fallback behavior when a chosen topology is too expensive or unstable
- separate controller policy evaluation from topology selection logic

Scope:

- add run-level budget caps
- add per-agent token limits
- add max fan-out controls
- enforce timeouts by stage and by worker
- support early stopping on confidence and agreement signals
- kill and retry failed or low-quality workers
- fall back to simpler topologies when coordination cost exceeds value
- add policy hooks so topology routing and controller decisions stay separate

Exit criteria:

- runs respect explicit cost and latency budgets, and the runtime can cut off wasteful execution paths

Status:

- Complete.
- Orb now has a dedicated `ExecutionController` seam with a default controller implementation that stays separate from topology classification.
- Planning-time controller policy can now override a pinned topology when fan-out limits are exceeded or when routing recommends escalation to a stronger topology.
- Execution-time controller policy now treats timeout and budget exhaustion as explicit controller outcomes and can stop a run early after the first satisfactory completion when stop-early policy allows it.
- Controller decisions and interventions are persisted into runtime state, run traces, and dashboard/trace summaries, so policy behavior is inspectable after a run.
- CLI/runtime config now includes `max_fanout` as an explicit policy surface in addition to budget, timeout, depth, and cooldown controls.

## Next Phase Plan

1. Add explicit verifier and critic roles as first-class runtime policy targets.
2. Define which task classes require mandatory review before completion.
3. Add contradiction and evidence-grounding checks for multi-worker and research-heavy runs.
4. Record verifier catches and synthesis scoring in `RunTrace` for replay/eval.
5. Use those verification signals as future controller inputs rather than relying only on routing heuristics and run-shape metadata.

## Phase 4: Make Verification First-Class

Treat verification as a role and policy decision, not a bolt-on.

Tasks:

- define explicit verifier and critic roles in the runtime
- add contradiction-check workflows for multi-worker outputs
- add evidence-grounding checks for research and synthesis tasks
- define which task classes require mandatory review before completion
- add a final synthesis scoring step before returning results
- log verification cost, outcomes, and catches into run traces

Scope:

- add explicit verifier and critic role families
- support contradiction checks between workers
- support evidence-grounding checks for research-style tasks
- require stricter review for risky or destructive actions
- add final synthesis scoring before completion
- define policy for:
  - when to verify
  - what artifacts to verify
  - when verification is worth the cost

Exit criteria:

- risky or high-variance runs include structured verification rather than implicit trust in consensus

## Phase 5: Make Roles and Memory Boundaries Explicit

Reduce ambiguity in who does what and what state they can touch.

Tasks:

- define the initial role family catalog Orb will support
- map existing agent behavior onto explicit role definitions
- define memory layers for scratch, session, distilled, and human-approved state
- implement read/write policy boundaries per memory layer
- define persistence rules for what survives a run or session
- add protections against stale shared state and cross-task contamination

Scope:

- formalize role families
  - planner
  - researcher
  - coder
  - critic
  - verifier
  - synthesizer
  - router
  - budget controller
- define layered memory
  - task-local scratchpad
  - session memory
  - long-term distilled memory
  - human-approved critical memory
- define explicit memory access policy
  - who can read what
  - who can write what
  - what gets persisted
- add guardrails against stale context, contamination, and error propagation

Exit criteria:

- Orb can measure role effectiveness and memory writes are policy-driven instead of ad hoc

## Phase 6: Add Workdir-Aware Context Selection

Make local workspace context discoverable without dumping the whole tree into prompts.

Tasks:

- define how Orb should treat the current folder when launched in a custom workdir
- add a repository/workdir context policy that starts from local files without preloading the entire tree
- require nodes to discover relevant files incrementally via file reads, searches, and grep/ripgrep-style lookups
- allow different nodes to gather different context slices based on their role
- define how discovered local context is summarized, cached, or discarded between steps
- add tests that prove Orb can solve coding tasks in a custom path without naive full-workspace ingestion

Scope:

- start from the current folder contents when Orb is launched in a custom path
- do not preload the entire workspace into prompt context
- support targeted context discovery through filesystem tools and content search
- allow node-specific context acquisition
- keep discovered context bounded, inspectable, and traceable
- prevent irrelevant files from polluting every node's prompt

Exit criteria:

- Orb can work effectively in arbitrary local folders by discovering only the relevant files needed for each node's task

## Phase 7: Adapt Cost, Latency, and Risk at Runtime

Use controller signals to adapt structure to real-world constraints.

Tasks:

- define adaptation inputs: difficulty, latency target, budget, risk, tools, and model availability
- connect adaptation inputs to topology and model-tier selection
- implement first-pass cost/performance adaptation rules
- implement risk-based strengthening rules for high-stakes tasks
- record adaptation decisions and outcomes for later analysis
- keep the policy surface inspectable and overrideable by humans

Scope:

- choose topology and model tier based on:
  - task difficulty
  - latency requirement
  - budget
  - risk level
  - tool availability
  - model availability
- define clear adaptation rules such as:
  - simple task -> single cheaper agent
  - broad research -> planner + parallel researchers + judge
  - risky action -> planner + executor + strict verifier
- capture adaptation outcomes in telemetry
- keep heuristics simple and inspectable before moving to learned routing

Exit criteria:

- Orb no longer applies one coordination pattern to every task class

## Phase 8: Build Replay and Evaluation Harness

Create the system needed to compare Orb configurations systematically.

Tasks:

- define a benchmark task set representative of Orb’s target workloads
- store replayable task inputs plus run traces needed to reproduce execution
- add a harness that re-runs the same tasks across multiple configurations
- compute comparable scorecards for quality, latency, cost, and verifier performance
- add regression thresholds and automated diff reporting
- expose outputs in a format usable for routing and controller tuning

Scope:

- define benchmark task sets
- store replayable run inputs and traces
- run the same tasks across multiple topology/controller configurations
- compare:
  - quality
  - latency
  - token usage
  - total cost
  - verifier catch rate
  - human override rate
- add scorecards and regression detection
- make routing evaluation outputs usable for future learned policies

Exit criteria:

- any proposed coordination change can be compared against a baseline on the same task set

## Phase 9: Add Topology-Level Safety Constraints

Enforce safety in coordination structure, not only in individual agent actions.

Tasks:

- define the topology-level safety rules Orb must enforce
- add communication constraints for unsafe or unnecessary all-to-all patterns
- require verifier or human review before sensitive tool execution where policy demands it
- cap tool classes by role and topology position
- block unsafe delegation chains and recursive expansion patterns
- record applied safety constraints and policy violations in run traces

Scope:

- limit unsafe all-to-all communication
- require verifier review before sensitive tool execution where appropriate
- require human approval for destructive actions
- cap tool classes available to each role
- prevent unsafe delegation chains or unbounded recursive delegation
- make safety policy visible in run traces

Exit criteria:

- Orb can explain which topology-level constraints were applied to a run and why

## Phase 10: Move from Heuristics to Learned Routing

Only after telemetry and replay are solid, start learning from outcomes.

Tasks:

- define the training/evaluation dataset shape from replay and telemetry outputs
- implement a lightweight learned routing policy candidate
- compare learned routing against heuristic baselines on held-out tasks
- keep heuristic fallback available for every learned-routing decision
- define promotion thresholds for quality, cost, and latency before rollout
- expose learned-routing decisions in an auditable way

Scope:

- use replay and telemetry data to evaluate routing quality
- train or tune a lightweight topology-selection policy
- keep learned routing auditable with fallback heuristics
- gate learned routing behind evaluation thresholds
- compare learned vs heuristic routing on held-out tasks

Exit criteria:

- learned routing beats or matches heuristic routing on benchmark quality/cost/latency without losing debuggability

## Key Design Decisions

### A. Measure before optimizing

Without run traces, Orb cannot distinguish:

- bad topology selection
- weak verification
- runtime budget failures
- model-quality failures

Telemetry is not a nice-to-have. It is the prerequisite for improvement.

### B. Keep routing, control, and verification separate

These are related but distinct layers:

- router chooses structure
- controller enforces runtime policy
- verifier checks result quality and risk

Separating them makes failures easier to debug and policies easier to evolve.

### C. Prefer explicit heuristics before learned policies

Orb should not jump straight to opaque learned routing.

Start with:

- approved topologies
- clear routing heuristics
- explicit controller rules
- measurable evaluation

Then learn from that data.

### D. Shared memory must be policy-driven

More shared context is not automatically better.

Orb should optimize for:

- relevant state
- bounded persistence
- role-appropriate access
- human approval for critical facts

not:

- universal read/write access
- unrestricted carryover across tasks

## Tests To Add

### Telemetry and trace correctness

- topology choice is recorded for every routed run
- per-agent token and latency accounting is stable
- retries and verifier catches are logged correctly
- human approval and override events are persisted distinctly

### Routing and controller behavior

- simple tasks stay on simple topologies
- risky tasks trigger stricter topologies
- max fan-out and timeout policies are enforced
- fallback to simpler topology works when workers fail or budgets are exceeded

### Verification behavior

- contradiction checks catch divergent worker outputs
- evidence-grounding checks fail unsupported claims
- risky tasks require verifier completion before finalization

### Replay and regression coverage

- the same task set can be replayed across multiple configurations
- scorecards are reproducible
- regressions in quality/cost/latency are detected automatically

## Risks

1. Over-instrumentation
- Telemetry that is too expensive or noisy can slow the runtime and obscure useful signals.

2. Policy sprawl
- If routing, verification, safety, and controller rules are added ad hoc, behavior will become hard to reason about.

3. False confidence from weak evaluation
- A replay harness with poor benchmarks will optimize Orb for the wrong things.

4. Memory contamination
- Shared state without strict write/read policy will amplify bad intermediate results.

5. Coordination overhead
- Stronger topologies can cost more than they help on ordinary tasks.

## Recommended Implementation Order

1. Runtime telemetry and trace schema
2. Approved topology library
3. Routing heuristics
4. Execution controller policies
5. Verification roles and checks
6. Explicit roles and memory policy
7. Replay/evaluation harness
8. Topology-level safety constraints
9. Learned routing experiments

## Definition of Done

This sprint direction is successful when:

- Orb can explain how a run was structured, controlled, and verified
- topology choice is explicit and measurable
- cost, latency, and fan-out are enforced by policy
- risky tasks receive stronger verification and safety constraints
- memory access is layered and intentional
- configuration changes can be replayed and compared against baselines
- learned routing is deferred until heuristic routing and evaluation are strong enough to trust
