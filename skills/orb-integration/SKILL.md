---
name: orb-integration
description: Coordinate an adaptive council of agents to compare proposals, challenge assumptions, reconcile disagreements through evidence, and implement decisions. Use for explicit Orb requests, competing approaches, or work benefiting from specialist collaboration and independent review.
---

# Orb council

Orb defines how agents deliberate. The host harness owns models, tools,
permissions, workspace access, sessions, and agent lifecycle. Keep two decisions
separate: **how participants interact** (topology) and **which available models
fill their roles** (allocation). Model size does not confer decision authority.

## Run Orb

1. Read [the portable protocol](references/protocol.md). Detect the host and read
   one adapter: [Codex](references/codex.md), [Claude Code](references/claude-code.md),
   or [Hermes](references/hermes.md). For another host, map only exposed capabilities.
2. Select the smallest sufficient topology from the table below. Honor an explicit
   selection. The optional helper is an advisory keyword baseline, not a learned router:
   `python <skill-dir>/scripts/select_topology.py "<task>" [--topology council]`.
3. Read only that topology's JSON. Read [model allocation](references/model-allocation.md)
   when choosing participants; it also defines the optional Jev/Laya decision interface.
   Record actual model availability and context isolation, including unsupported capabilities.
4. For `council` and `specialist-network`, read [the council protocol](references/council.md).
   Set the objective, evidence requirements, role ownership, and a bounded round budget.
   Use three evidence rounds by default unless the task or user specifies another limit.
5. Render initial visual status with
   `python <skill-dir>/scripts/render_topology.py <topology.json>`.
   Show Mermaid or a native equivalent before delegation; update at meaningful role-state
   transitions. Role states are `pending`, `running`, `completed`, `blocked`, `failed`.
6. Execute the topology through the host adapter. Preserve independent first proposals,
   route specific challenges to relevant peers, and inspect the resulting evidence.
7. Reconcile the result against completion requirements. For councils, run
   `python <skill-dir>/scripts/validate_council.py <decision-record.json>` before
   reporting completion. Inspect evidence yourself: this checker validates bookkeeping,
   not truth, model calibration, or the actual execution of reviews.

Resolve `<skill-dir>` to the directory containing this file; commands must work from
any project working directory. Resolve topology paths inside `references/topologies/`.

## Choose interaction structure

| Topology | Use when | Reference |
|---|---|---|
| solo | A narrow task needs no council | [solo](references/topologies/solo.json) |
| implement-review | A concrete change needs implementation and independent criticism | [implement-review](references/topologies/implement-review.json) |
| research-build | Missing knowledge must be gathered before implementation | [research-build](references/topologies/research-build.json) |
| parallel-review | Separate correctness and risk reviews are useful | [parallel-review](references/topologies/parallel-review.json) |
| council | Competing ideas or tradeoffs require independent proposals and reconciliation | [council](references/topologies/council.json) |
| specialist-network | Separable subproblems need peer exchange and integration checks | [specialist-network](references/topologies/specialist-network.json) |

These are starting structures, not rankings. Change topology only for a named gap
(e.g. unresolved architecture dispute or missing expertise). Preserve evidence and
remaining budget across changes, record the reason, and update the graph. For an
explicitly pinned topology, adapt within it or explain the limitation instead of
silently replacing it. Never expand simply to manufacture agreement.

## Boundaries

- Do not launch or depend on the legacy Orb daemon, dashboard, provider clients,
  message bus, or agent loop. A skill uses the host's execution facilities.
- Use supported model selection only. If unavailable, use the host default and
  disclose that model diversity was not achieved. Do not pretend role prompts
  create different models or established expertise.
- Preserve required roles sequentially when slots are limited. Without isolated
  contexts, disclose that review is a separate pass by one agent, not independent.
- Keep write ownership disjoint or use host-provided isolation. Delegation does not
  broaden authorization. Advisory routing cannot bypass permissions or required checks.
- Jev and Laya are optional integrations, not bundled dependencies. Do not install
  weights, call paid services, or expose project content merely to load this skill.
