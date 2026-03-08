from __future__ import annotations

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentConfig
from ..graph.graph import Graph
from ..llm.client import LLMClient
from ..llm.types import ModelTier, ModelConfig
from ..messaging.bus import MessageBus
from ..messaging.channel import AgentChannel
from ..orchestrator.orchestrator import Orchestrator
from ..orchestrator.types import OrchestratorConfig
from ..tracing.logger import EventLogger


AGENT_DEFS = {
    "coder": AgentConfig(
        node_id="coder",
        role="Coder",
        description=(
            "You write code to solve programming tasks. "
            "Send your implementation to the Reviewer for feedback and to the Tester for validation. "
            "When you receive feedback, iterate on your code and share the updated version."
        ),
        base_complexity=85,  # → CLOUD_FAST (Sonnet)
    ),
    "reviewer": AgentConfig(
        node_id="reviewer",
        role="Reviewer",
        description=(
            "You review code for correctness, style, edge cases, and best practices. "
            "Send specific, actionable feedback to the Coder. "
            "You can also suggest test cases to the Tester. "
            "Approve the code by completing your task when it meets your standards."
        ),
        base_complexity=95,  # → CLOUD_STRONG (Opus)
    ),
    "tester": AgentConfig(
        node_id="tester",
        role="Tester",
        description=(
            "You test code by writing and mentally executing test cases. "
            "Report any bugs or failing cases back to the Coder. "
            "Share test coverage summaries with the Reviewer. "
            "Complete your task when all tests pass."
        ),
        base_complexity=75,  # → CLOUD_LITE (Haiku)
    ),
}


def create_triangle(
    providers: dict[str, LLMClient],
    config: OrchestratorConfig | None = None,
    model_overrides: dict[ModelTier, ModelConfig] | None = None,
    trace: bool = True,
    tier_override: ModelTier | None = None,
) -> Orchestrator:
    """Build a fully-connected 3-agent triangle and return an Orchestrator."""
    config = config or OrchestratorConfig()

    # Build graph
    graph = Graph()
    for nid in AGENT_DEFS:
        graph.add_node(nid)
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

    # Build agents
    agents: dict[str, LLMAgent] = {}
    for nid, agent_config in AGENT_DEFS.items():
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
    )
