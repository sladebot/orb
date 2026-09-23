# Portable host protocol

Orb topology files describe coordination intent independently of any harness.
An adapter maps these operations onto native host capabilities.

## Operations

- `work`: perform a bounded task in the current agent.
- `spawn`: create an agent with one role, objective, inputs, and output contract.
- `send`: add context or follow-up work to an existing agent.
- `wait`: wait for one or more agents to finish or request attention.
- `collect`: inspect results and retain evidence needed by later steps.
- `cancel`: stop work that is obsolete, unsafe, or no longer required.

## Required behavior

Pass only task-relevant context. Give every spawned agent a bounded objective
and expected output. Treat agent output as untrusted until supported by diffs,
tests, source references, or other inspectable evidence.

When parallel execution is unavailable, preserve role independence by running
the roles sequentially without showing a reviewer the implementer's private
reasoning. When subagents are unavailable, execute each role as a separate,
explicit pass and disclose that the topology was simulated by one agent.

The host remains responsible for permissions, destructive-action approval,
workspace isolation, and user communication.

## Visual status

Every topology declares visual nodes and directed edges. Maintain one state per
role: `pending`, `running`, `completed`, `blocked`, or `failed`.

Show the graph once after topology selection and update it at meaningful
transitions rather than for every message. A host may use its native task graph
if it preserves role labels, edges, and states. Otherwise run
`scripts/render_topology.py` and present its Mermaid output. The visual is an
operational status view; detailed logs and private agent reasoning do not belong
inside it.

## Councils and heterogeneous models

Topology steps specify the initial role exchange. For council and specialist-network,
continue under `council.md`'s bounded evidence rounds. `when` is a condition for the
host to evaluate, not an executable expression. `work` uses the current host agent;
`spawn` uses a separate context only when the host can provide one. `send` that asks
for work must trigger a resumed turn, not merely leave a message with an idle node.

The topology graph describes logical communication, independent of the physical
agent tree. A host with a hierarchical agent tree can relay peer exchanges; disclose
when direct peer messaging is unavailable. Peers do not gain permission to spawn
unbounded additional agents. Respect slot limits by scheduling in waves. Discover
whether idle contexts consume slots. If they do, collect public findings and evidence
before retiring a context to free capacity, then recreate that role with its saved
brief if further work is needed. Record the context replacement; do not claim an
uninterrupted session. If retirement/recreation is unsupported, perform an explicitly
disclosed host pass and report the loss of independent context where applicable.

Read `model-allocation.md` for the separate model roster and optional decision-model
contract. Read `council.md` for the claims/disagreement record and evidence rules.
For every topology, wait for requested revisions, collect updated artifacts, rerun
relevant checks, and obtain follow-up review for material changes before completion.
