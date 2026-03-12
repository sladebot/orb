from __future__ import annotations

import dataclasses

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentConfig
from ..graph.graph import Graph
from ..llm.client import LLMClient
from ..llm.types import ModelTier, ModelConfig
from ..messaging.bus import MessageBus
from ..messaging.channel import AgentChannel
from ..orchestrator.orchestrator import Orchestrator
from ..orchestrator.types import OrchestratorConfig
from pathlib import Path

from ..sandbox.sandbox import Sandbox
from ..tracing.logger import EventLogger
from .context import build_topology_contexts


AGENT_DEFS = {
    "coordinator": AgentConfig(
        node_id="coordinator",
        role="Coordinator",
        description=(
            "You are the routing coordinator for this team.\n"
            "When you receive the user's task, send it to the correct worker immediately with the task text unchanged. "
            "Do NOT answer the task, do NOT write code, and do NOT analyze the task yourself. "
            "You only route inputs across the graph and surface user clarifications to the correct node."
        ),
        base_complexity=20,
        suppress_context_guidelines=True,
    ),
    "coder": AgentConfig(
        node_id="coder",
        role="Coder",
        description=(
            "You write and update the implementation for programming tasks. "
            "Use the shared sandbox to inspect, write, and verify code. "
            "Coordinate with your graph neighbors for review, testing, and follow-up changes."
        ),
        base_complexity=50,
        enable_filesystem=True,
    ),
    "reviewer": AgentConfig(
        node_id="reviewer",
        role="Reviewer",
        description=(
            "You review implementation quality, correctness, and edge cases. "
            "Inspect files directly in the shared sandbox and send concrete feedback to the relevant neighbors."
        ),
        base_complexity=65,
        enable_filesystem=True,
    ),
    "tester": AgentConfig(
        node_id="tester",
        role="Tester",
        description=(
            "You validate implementations through execution, test cases, and bug discovery. "
            "Use the shared sandbox to read files, write tests, run commands, and report concrete findings."
        ),
        base_complexity=25,
        enable_filesystem=True,
    ),
}


def create_triad(
    providers: dict[str, LLMClient],
    config: OrchestratorConfig | None = None,
    model_overrides: dict[ModelTier, ModelConfig] | None = None,
    trace: bool = True,
    tier_override: ModelTier | None = None,
    agent_model_map: dict[str, "ModelConfig"] | None = None,
) -> Orchestrator:
    """Build a coordinator + 3-agent triangle and return an Orchestrator."""
    config = config or OrchestratorConfig()
    entry_agent = "coordinator" if config.entry_agent == OrchestratorConfig().entry_agent else config.entry_agent
    config = dataclasses.replace(config, entry_agent=entry_agent, synthesis_agent=None)
    topology_id = "triangle"
    topology_label = "Triad"

    # Build graph
    graph = Graph()
    for nid in AGENT_DEFS:
        graph.add_node(nid)
    graph.add_edge("coordinator", "coder")
    graph.add_edge("coder", "reviewer")
    graph.add_edge("coder", "tester")
    graph.add_edge("reviewer", "tester")

    node_roles = {nid: agent_config.role for nid, agent_config in AGENT_DEFS.items()}
    topology_contexts = build_topology_contexts(
        topology_id=topology_id,
        topology_label=topology_label,
        graph=graph,
        node_roles=node_roles,
        workflow_steps=[
            "Coordinator receives the user task and routes it into the graph.",
            "Coder implements or updates the solution in the shared sandbox.",
            "Coder hands work to Reviewer and Tester for feedback and validation.",
            "Reviewer and Tester send concrete findings back through their graph neighbors.",
            "Each node completes only after finishing its own role in the workflow.",
        ],
        completion_rules={
            "coordinator": [
                "Route the incoming task to the correct worker without solving it yourself.",
                "Do not ask the user questions unless a worker explicitly bubbles one up.",
                "Complete after routing or forwarding the necessary input.",
            ],
            "coder": [
                "Do not complete immediately after first-pass implementation.",
                "Hand off the implementation to both Reviewer and Tester before final completion.",
                "If feedback or bugs arrive, update the implementation before completing.",
            ],
            "reviewer": [
                "Review concrete files or outputs before completing.",
                "Send actionable feedback to the Coder when changes are needed.",
            ],
            "tester": [
                "Run validation against the current implementation before completing.",
                "Send failing cases or validation results to the relevant neighbors.",
            ],
        },
    )

    # Build bus
    bus = MessageBus(
        graph=graph,
        max_depth=config.max_depth,
        budget=config.budget,
        max_cooldown=config.max_cooldown,
    )

    # One sandbox shared across all agents in this run — write to cwd
    sandbox = Sandbox(root=Path.cwd())

    # Build agents
    agents: dict[str, LLMAgent] = {}
    for nid, agent_config in AGENT_DEFS.items():
        if agent_model_map and nid in agent_model_map:
            agent_config = dataclasses.replace(agent_config, pinned_model=agent_model_map[nid])
        if agent_config.enable_filesystem:
            agent_config = dataclasses.replace(agent_config, sandbox=sandbox)
        channel = AgentChannel()
        bus.register_channel(nid, channel)
        agents[nid] = LLMAgent(
            config=agent_config,
            channel=channel,
            bus=bus,
            providers=providers,
            model_overrides=model_overrides,
            tier_override=tier_override,
        )

    # Initialize agents with neighbor role info
    for nid, agent in agents.items():
        neighbor_roles = {
            n: AGENT_DEFS[n].role for n in graph.get_neighbors(nid)
        }
        agent.initialize(neighbor_roles, topology_contexts[nid])

    # Event logger
    event_logger = EventLogger(enabled=trace)

    return Orchestrator(
        agents=agents,
        bus=bus,
        config=config,
        event_logger=event_logger,
        sandbox=sandbox,
    )
