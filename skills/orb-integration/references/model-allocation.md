# Topology and model allocation

Choose interaction structure first, then allocate models to its roles. Both are
proposals that can be revised based on observed performance. Never assume a mesh
beats a hierarchy or that a larger model is the best specialist.

## Available model catalog

Build a small catalog from host-exposed capabilities and the user's configuration:
model id, provider, supported tools, context limit, local/remote execution, observed
latency, known price if available, and task-specific evaluation evidence. Mark
unknown values unknown. A role description alone does not establish expertise.
Record requested and actual model ids separately; use `host-default` if the actual
identity is not exposed. Do not invent cross-provider access or bypass restrictions
on choosing models. No new provider credentials are required for the baseline skill.

Prefer a cheap, capable model for bounded inspection, a coding-capable model with
required tools for implementation, and stronger reasoning when a concrete unresolved
question warrants it. Diversity can come from different models, evidence sources,
or independently developed approaches; document which was actually achieved.
A reconciler can be the host and need not be the largest model or a permanent boss.

## Optional Jev / Laya routing contract

No model client ships in this skill. If the host already has an authorized callable
router, it may recommend the following **two dependent decisions**:

1. **Topology Choice:** supply the task, constraints, available role capacity, and
   descriptions of allowed topology ids. Include `defer` for ambiguous requests.
2. **Model Choices:** after the topology is selected, ask one question per role,
   choosing from eligible configured model ids plus `host-default`. Supply role
   requirements and the measured model catalog. Multiple role choices may be
   evaluated together, but each choice is advisory and not jointly budget-aware.

Use code/host checks to filter unavailable models, unsupported tools and context
limits before asking. Validate returned ids, user pins, total budget, slots, data
handling requirements, and conflicts across assignments afterward. If infeasible,
revise the plan or use the baseline host allocation. Never accept a model-generated
budget calculation as enforcement. Independent question outputs do not ensure a
coherent global plan.

A short typed output could be:

```json
{"topology":"council","assignments":{"proposer_a":"host-default","proposer_b":"host-default","critic":"host-default","reconciler":"host-default"}}
```

Keep explanations and evidence requirements with the host; decision models need
not generate them. Do not send full transcripts: select relevant task facts and
short role/candidate descriptions. Respect the configured checkpoint's tokenizer
and context limit; if essential facts do not fit, defer instead of silently
truncating. This matters particularly for short-context local models.

On timeout, errors, invalid choices, uncertain decisions, or no router, use the
host plus the optional keyword selector. Confidence thresholds must come from
routing evaluations; scores from different models are not interchangeable.
Log provider/model, inputs or safe references, candidate order, selected ids,
confidence when available, elapsed time, and fallback reason. Do not attribute a
host-selected plan to Jev/Laya.

Before enabling autonomous choices, compare recorded routes with verified outcomes,
including reordered candidates and ambiguous tasks. Measure final task quality,
cost, latency and fallback rate against a single capable agent and a fixed topology.
Start with recommendations only; no superiority claim follows from an API returning
a confident value quickly. A model roster and topology recommendation can be fast;
verifying their quality still requires evidence.
