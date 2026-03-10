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


AGENT_DEFS = {
    "coordinator": AgentConfig(
        node_id="coordinator",
        role="Coordinator",
        description=(
            "You are the entry and exit point for this team. Your job is routing and synthesis ONLY — "
            "do NOT solve the task yourself, write code, or diagnose problems. "
            "When you receive a task: forward it immediately to the Coder with the original task text intact. "
            "You may add one short sentence noting any key constraint (e.g. language, framework) but nothing more. "
            "Then wait silently — the orchestrator will notify you once all workers have finished. "
            "When you receive that notification, synthesize the workers' results into a single "
            "comprehensive final answer and call complete_task."
        ),
        base_complexity=20,
    ),
    "coder": AgentConfig(
        node_id="coder",
        role="Coder",
        description=(
            "You write code to solve programming tasks. "
            "Write your implementation to disk using write_file, then run it with run_command to verify. "
            "Send the file paths to the Reviewer for feedback and to the Tester for validation. "
            "When you receive feedback, iterate on your code and update the files. "
            "When your work is complete and approved, call complete_task with your final implementation."
        ),
        base_complexity=50,
        enable_filesystem=True,
    ),
    "reviewer": AgentConfig(
        node_id="reviewer",
        role="Reviewer",
        description=(
            "You review code for correctness, style, edge cases, and best practices. "
            "Use read_file to read the coder's files directly before reviewing. "
            "Send specific, actionable feedback to the Coder. "
            "You can also suggest test cases to the Tester. "
            "When the code meets your standards, call complete_task with your final review verdict."
        ),
        base_complexity=65,
        enable_filesystem=True,
    ),
    "tester": AgentConfig(
        node_id="tester",
        role="Tester",
        description=(
            "You test code by writing and executing test cases. "
            "Read the coder's files with read_file, write test files with write_file, "
            "and run them with run_command. Report any bugs or failing cases back to the Coder. "
            "Share test results and coverage summaries with the Reviewer. "
            "When all tests pass, call complete_task with your full test report."
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
    config = dataclasses.replace(config, entry_agent="coordinator", synthesis_agent="coordinator")

    # Build graph
    graph = Graph()
    for nid in AGENT_DEFS:
        graph.add_node(nid)
    graph.add_edge("coordinator", "coder")
    graph.add_edge("coder", "reviewer")
    graph.add_edge("coder", "tester")
    graph.add_edge("reviewer", "tester")

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
        agent.initialize(neighbor_roles)

    # Event logger
    event_logger = EventLogger(enabled=trace)

    return Orchestrator(
        agents=agents,
        bus=bus,
        config=config,
        event_logger=event_logger,
        sandbox=sandbox,
    )
