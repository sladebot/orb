from __future__ import annotations

from .types import TopologyContext


def build_system_prompt(
    role: str,
    description: str,
    neighbors: dict[str, str],  # {node_id: role_description}
    topology: TopologyContext | None = None,
    enable_filesystem: bool = False,
    enable_memory: bool = False,
    memory_write_enabled: bool = False,
    eval_mode: bool = False,
    suppress_context_guidelines: bool = False,
    workdir: str = "",
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

    workdir_section = ""
    if workdir:
        workdir_section = f"""
## Working directory
- **Session workdir**: `{workdir}`
- This is the absolute path your team is operating on. When peers ask "where is the code", this is the answer. Relative paths in `read_file` / `list_directory` / `run_command` resolve against this directory.
"""

    filesystem_section = ""
    if enable_filesystem:
        workdir_line = f" Rooted at `{workdir}`." if workdir else ""
        filesystem_section = f"""
## Sandbox & Filesystem Tools
You are running inside an **isolated sandbox directory**.{workdir_line} All file paths are relative to the sandbox root.
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

    memory_section = ""
    if enable_memory:
        write_lines = ""
        mode_line = "Memory is read-only for this agent; do not try to persist new memories."
        if memory_write_enabled:
            mode_line = "Memory writes are enabled; write only durable facts, project decisions, stable architecture notes, and reusable knowledge."
            write_lines = """
- `memory_write(title, content, page_type?, tags?, sources?)` — write or update a durable memory page
- `memory_write_entity(entity, content, tags?, sources?)` — write or update an entity memory page
"""
        memory_section = f"""
## Persistent Memory Tools
You have access to Orb's persistent wiki memory vault. {mode_line}

Available memory tools:
- `memory_read(query, limit?)` — search memory pages and return compact snippets
- `memory_read_entity(entity)` — read one page by title
- `memory_read_tag(tag, limit?)` — list pages with a tag
- `memory_list_pages(limit?, page_type?)` — list page titles and metadata without full content{write_lines}

Guidelines:
- Read memory when prior project facts, decisions, architecture, or terminology would improve your answer.
- Keep memory results compact when sharing them with peers.
- Treat writes as durable: do not store transient task progress, speculation, secrets, or noisy logs.
"""

    eval_section = ""
    if eval_mode:
        eval_section = """
## Evaluation Mode
This run is being driven by an evaluation or benchmark harness. Persistent memory tools are disabled so benchmark cases do not read prior answers, leak state across runs, or write evaluation artifacts into the durable vault.
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
{context_guidelines}{filesystem_section}{memory_section}{eval_section}
## Completion
- Call `complete_task` when you've finished your part and have no more contributions.
- Don't call complete_task prematurely — wait until the work is genuinely done.
- If you receive feedback that requires changes, address it before completing.
{workdir_section}
## Important
- Be concise and focused in your responses.
- Think step by step about what needs to happen before acting.
- You are part of a team — collaborate effectively."""
