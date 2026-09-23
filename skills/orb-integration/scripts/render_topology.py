#!/usr/bin/env python3
"""Render a portable Orb topology and role states as Mermaid."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


STATES = {"pending", "running", "completed", "blocked", "failed"}


def render_mermaid(
    topology: dict[str, Any],
    role_states: dict[str, str] | None = None,
) -> str:
    visual = topology["visual"]
    nodes: dict[str, str] = visual["nodes"]
    states = {node_id: "pending" for node_id in nodes}
    states.update(role_states or {})

    unknown_nodes = set(states) - set(nodes)
    if unknown_nodes:
        raise ValueError(f"unknown role state: {sorted(unknown_nodes)[0]}")
    invalid_states = set(states.values()) - STATES
    if invalid_states:
        raise ValueError(f"invalid role state: {sorted(invalid_states)[0]}")

    lines = ["flowchart LR"]
    for node_id, label in nodes.items():
        lines.append(f'    {node_id}["{label} · {states[node_id]}"]')
    for source, target in visual["edges"]:
        lines.append(f"    {source} --> {target}")
    for node_id, state in states.items():
        lines.append(f"    class {node_id} {state}")

    lines.extend(
        [
            "    classDef pending fill:transparent,stroke:currentColor",
            "    classDef running fill:transparent,stroke:currentColor,stroke-width:3px",
            "    classDef completed fill:transparent,stroke:currentColor",
            "    classDef blocked fill:transparent,stroke:currentColor,stroke-dasharray:5 3",
            "    classDef failed fill:transparent,stroke:currentColor,stroke-width:3px",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("topology", type=Path)
    parser.add_argument(
        "--states",
        default="{}",
        help='JSON object such as \'{"implementer":"running"}\'',
    )
    args = parser.parse_args()
    topology = json.loads(args.topology.read_text())
    states = json.loads(args.states)
    if not isinstance(states, dict):
        parser.error("--states must be a JSON object")
    print(render_mermaid(topology, states))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
