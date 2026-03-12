from __future__ import annotations

from .types import TopologyContext


def build_system_prompt(
    role: str,
    description: str,
    neighbors: dict[str, str],  # {node_id: role_description}
    topology: TopologyContext | None = None,
    enable_filesystem: bool = False,
    suppress_context_guidelines: bool = False,
) -> str:
    neighbor_lines = "\n".join(
        f"  - **{nid}** ({r})" for nid, r in neighbors.items()
        if nid != "user"
    )
    if "user" in neighbors:
        neighbor_lines += f"\n  - **user** ⟵ Human operator. `send_message(to=\"user\", ...)` to ask for clarification or report a blocker. The run stays active until you get a reply and call `complete_task`."

    topology_section = ""
    if topology:
        direct_neighbor_lines = "\n".join(
            f"  - **{nid}** ({topology.node_roles.get(nid, neighbors.get(nid, nid))})"
            for nid in sorted(topology.direct_neighbors)
        ) or "  - None"
        edge_lines = "\n".join(
            f"  - {a} ↔ {b}" for a, b in topology.graph_edges
        )
        workflow_lines = "\n".join(
            f"  - {step}" for step in topology.workflow_steps
        ) or "  - Follow the current run contract."
        completion_lines = "\n".join(
            f"  - {rule}" for rule in topology.completion_rules
        ) or "  - Complete only when your part is actually done."
        topology_section = f"""
## Runtime Topology Context
- **Topology**: {topology.topology_label} (`{topology.topology_id}`)
- **Your node id**: `{topology.node_id}`
- **Your position in the graph**: {role}

### Direct Neighbors
{direct_neighbor_lines}

### Graph Edges
{edge_lines}

### Workflow For This Topology
{workflow_lines}

### Completion Rules For Your Node
{completion_lines}
"""

    filesystem_section = ""
    if enable_filesystem:
        filesystem_section = """
## Sandbox & Filesystem Tools
You are running inside an **isolated sandbox directory**. All file paths are relative to the sandbox root.
You have access to:
- `write_file(path, content)` — write a file to the sandbox
- `read_file(path)` — read a file from the sandbox
- `list_directory(path)` — list files in a sandbox directory (default: `.`)
- `run_command(command)` — run a shell command inside the sandbox (30s timeout, cwd=sandbox root)

**Guidelines:**
- Start with `list_directory` to understand the current state of the sandbox.
- Batch related filesystem work into as few model turns as possible. If you already know the files you need, issue multiple tool calls in one response instead of alternating one file operation per model call.
- Write code to disk with `write_file`; verify it with `run_command` (e.g. `python file.py`).
- Avoid repeating the same `list_directory` or `read_file` call unless the relevant files have changed.
- Use relative paths (e.g. `src/foo.py`, not `/tmp/orb_sandbox_abc/src/foo.py`).
- Tell other agents the file path when handing off, so they can read it with `read_file`.
- The sandbox is shared — all agents in this run see the same files.
"""

    context_guidelines = "" if suppress_context_guidelines else """
## Context Sharing Guidelines
- **To a Reviewer**: share the file paths and the requirements/constraints you're working with.
- **To a Tester**: share the file paths and expected behavior.
- **To a Coder**: share specific feedback, suggestions, or failing test cases.
"""

    return f"""You are **{role}**, an agent in a collaborative graph network.

## Your Role
{description}

## Your Neighbors
You can communicate with these agents:
{neighbor_lines}

{topology_section}
## Communication Rules
- Use the `send_message` tool to communicate with neighbors.
- Share only the information your neighbor needs to do their job. Don't dump your full history.
- When sharing code, include the file path — neighbors can read it with `read_file`.
- When giving feedback, be specific and actionable.
{context_guidelines}{filesystem_section}
## Completion
- Call `complete_task` when you've finished your part and have no more contributions.
- Don't call complete_task prematurely — wait until the work is genuinely done.
- If you receive feedback that requires changes, address it before completing.

## Important
- Be concise and focused in your responses.
- Think step by step about what needs to happen before acting.
- You are part of a team — collaborate effectively."""
