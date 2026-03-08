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
            "Send your implementation to both Reviewer A and Reviewer B for independent review. "
            "Also send to the Tester for validation. "
            "When you receive feedback from either reviewer, iterate and share the updated version with both. "
            "Complete your task only after both reviewers have approved."
        ),
        base_complexity=85,
    ),
    "reviewer_a": AgentConfig(
        node_id="reviewer_a",
        role="Reviewer A",
        description=(
            "You are the first of two senior code reviewers. "
            "Review code independently for correctness, style, edge cases, and best practices. "
            "After forming your own opinion, communicate with Reviewer B to compare findings. "
            "You MUST reach consensus with Reviewer B before approving — discuss any disagreements directly with them. "
            "Only call complete_task once you and Reviewer B have explicitly agreed the code is ready. "
            "Send your final consensus decision to the Coder."
        ),
        base_complexity=96,
    ),
    "reviewer_b": AgentConfig(
        node_id="reviewer_b",
        role="Reviewer B",
        description=(
            "You are the second of two senior code reviewers. "
            "Review code independently for correctness, security, performance, and maintainability. "
            "After forming your own opinion, communicate with Reviewer A to compare findings. "
            "You MUST reach consensus with Reviewer A before approving — discuss any disagreements directly with them. "
            "Only call complete_task once you and Reviewer A have explicitly agreed the code is ready. "
            "Send your final consensus decision to the Coder."
        ),
        base_complexity=96,
    ),
    "tester": AgentConfig(
        node_id="tester",
        role="Tester",
        description=(
            "You test code by writing and mentally executing test cases. "
            "Report any bugs or failing cases back to the Coder. "
            "Share test coverage summaries with both Reviewer A and Reviewer B. "
            "Complete your task when all tests pass."
        ),
        base_complexity=75,
    ),
}


def create_dual_review(
    providers: dict[str, LLMClient],
    config: OrchestratorConfig | None = None,
    model_overrides: dict[ModelTier, ModelConfig] | None = None,
    trace: bool = True,
    tier_override: ModelTier | None = None,
) -> Orchestrator:
    """Build a 4-agent dual-review graph and return an Orchestrator.

    reviewer_a is pinned to Anthropic Opus.
    reviewer_b is pinned to Ollama if available, otherwise falls back to Anthropic Opus.
    This lets two different providers debate and reach consensus.
    """
    config = config or OrchestratorConfig()

    # Both reviewers use Anthropic Opus for high-quality independent review
    reviewer_a_model = ModelConfig(
        tier=ModelTier.CLOUD_STRONG,
        model_id="claude-opus-4-20250514",
        provider="anthropic",
    )
    reviewer_b_model = ModelConfig(
        tier=ModelTier.CLOUD_STRONG,
        model_id="claude-opus-4-20250514",
        provider="anthropic",
    )

    # Apply pinned models to reviewer configs
    agent_defs = dict(AGENT_DEFS)
    import dataclasses
    agent_defs["reviewer_a"] = dataclasses.replace(
        AGENT_DEFS["reviewer_a"], pinned_model=reviewer_a_model
    )
    agent_defs["reviewer_b"] = dataclasses.replace(
        AGENT_DEFS["reviewer_b"], pinned_model=reviewer_b_model
    )

    # Build graph
    graph = Graph()
    for nid in agent_defs:
        graph.add_node(nid)
    graph.add_edge("coder", "reviewer_a")
    graph.add_edge("reviewer_a", "coder")
    graph.add_edge("coder", "reviewer_b")
    graph.add_edge("reviewer_b", "coder")
    graph.add_edge("reviewer_a", "reviewer_b")
    graph.add_edge("reviewer_b", "reviewer_a")
    graph.add_edge("coder", "tester")
    graph.add_edge("tester", "coder")
    graph.add_edge("tester", "reviewer_a")
    graph.add_edge("reviewer_a", "tester")
    graph.add_edge("tester", "reviewer_b")
    graph.add_edge("reviewer_b", "tester")

    # Build bus
    bus = MessageBus(
        graph=graph,
        max_depth=config.max_depth,
        budget=config.budget,
        max_cooldown=config.max_cooldown,
    )

    # Build agents
    agents: dict[str, LLMAgent] = {}
    for nid, agent_config in agent_defs.items():
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
            n: agent_defs[n].role for n in graph.get_neighbors(nid)
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
