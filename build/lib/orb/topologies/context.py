from __future__ import annotations

from ..agent.types import TopologyContext
from ..graph.graph import Graph


def build_topology_contexts(
    *,
    topology_id: str,
    topology_label: str,
    graph: Graph,
    node_roles: dict[str, str],
    workflow_steps: list[str],
    completion_rules: dict[str, list[str]],
) -> dict[str, TopologyContext]:
    edges = sorted(
        {(min(edge.a, edge.b), max(edge.a, edge.b)) for edge in graph.edges}
    )
    contexts: dict[str, TopologyContext] = {}
    for node_id in sorted(graph.nodes):
        neighbors = {
            neighbor: node_roles.get(neighbor, neighbor)
            for neighbor in sorted(graph.get_neighbors(node_id))
        }
        contexts[node_id] = TopologyContext(
            topology_id=topology_id,
            topology_label=topology_label,
            node_id=node_id,
            role=node_roles.get(node_id, node_id),
            direct_neighbors=neighbors,
            graph_edges=edges,
            node_roles=node_roles,
            workflow_steps=workflow_steps,
            completion_rules=completion_rules.get(node_id, []),
        )
    return contexts
