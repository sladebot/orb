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
            "You only route work and surface direct user clarifications to the correct node."
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
    "reviewer_a": AgentConfig(
        node_id="reviewer_a",
        role="Reviewer A",
        description=(
            "You are one of two independent reviewers. "
            "Inspect files in the shared sandbox, review them from your perspective, and communicate with the other reviewer and relevant neighbors."
        ),
        base_complexity=65,
        enable_filesystem=True,
    ),
    "reviewer_b": AgentConfig(
        node_id="reviewer_b",
        role="Reviewer B",
        description=(
            "You are one of two independent reviewers. "
            "Inspect files in the shared sandbox, review them from your perspective, and communicate with the other reviewer and relevant neighbors."
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


def _pick_reviewer_models(providers: dict) -> tuple["ModelConfig", "ModelConfig"]:
    """Return (reviewer_a_model, reviewer_b_model) from different providers when possible.

    Priority per reviewer: anthropic > openai-codex > openai > ollama.
    reviewer_b falls back to a different provider than reviewer_a, then same if no other choice.
    """
    # Ordered candidate configs per provider
    candidates: list[ModelConfig] = []
    if "anthropic" in providers:
        candidates.append(ModelConfig(ModelTier.CLOUD_STRONG, "claude-opus-4-20250514", "anthropic"))
    if "openai-codex" in providers:
        candidates.append(ModelConfig(ModelTier.CLOUD_STRONG, "gpt-5.4", "openai-codex"))
    if "openai" in providers:
        candidates.append(ModelConfig(ModelTier.CLOUD_STRONG, "o3", "openai"))
    if "ollama" in providers:
        candidates.append(ModelConfig(ModelTier.LOCAL_LARGE, "qwen3.5:27b", "ollama"))

    if not candidates:
        # Absolute fallback — should never happen if providers is non-empty
        default = ModelConfig(ModelTier.CLOUD_STRONG, "claude-opus-4-20250514", "anthropic")
        return default, default

    reviewer_a = candidates[0]
    # Pick first candidate with a different provider for reviewer_b
    reviewer_b = next(
        (c for c in candidates[1:] if c.provider != reviewer_a.provider),
        candidates[1] if len(candidates) > 1 else reviewer_a,
    )
    return reviewer_a, reviewer_b


def create_dual_review(
    providers: dict[str, LLMClient],
    config: OrchestratorConfig | None = None,
    model_overrides: dict[ModelTier, ModelConfig] | None = None,
    trace: bool = True,
    tier_override: ModelTier | None = None,
    agent_model_map: dict[str, "ModelConfig"] | None = None,
) -> Orchestrator:
    """Build a 4-agent dual-review graph and return an Orchestrator.

    Reviewers are assigned to different providers when possible so they debate
    from independent perspectives. Provider priority: anthropic > openai-codex > openai > ollama.
    If agent_model_map is provided, it overrides the default reviewer pinned models.
    """
    config = config or OrchestratorConfig()
    entry_agent = "coordinator" if config.entry_agent == OrchestratorConfig().entry_agent else config.entry_agent
    config = dataclasses.replace(config, entry_agent=entry_agent, synthesis_agent=None)
    topology_id = "dual-review"
    topology_label = "Dual Review"

    # Pick reviewer models from different providers when possible
    reviewer_a_model, reviewer_b_model = _pick_reviewer_models(providers)

    # Apply pinned models to reviewer configs (agent_model_map takes priority)
    agent_defs = dict(AGENT_DEFS)
    agent_defs["reviewer_a"] = dataclasses.replace(
        AGENT_DEFS["reviewer_a"],
        pinned_model=(agent_model_map.get("reviewer_a") if agent_model_map else None) or reviewer_a_model,
    )
    agent_defs["reviewer_b"] = dataclasses.replace(
        AGENT_DEFS["reviewer_b"],
        pinned_model=(agent_model_map.get("reviewer_b") if agent_model_map else None) or reviewer_b_model,
    )
    # Apply agent_model_map to coder and tester if provided
    if agent_model_map:
        for nid in ("coder", "tester"):
            if nid in agent_model_map:
                agent_defs[nid] = dataclasses.replace(
                    AGENT_DEFS[nid], pinned_model=agent_model_map[nid]
                )

    # Build graph
    graph = Graph()
    for nid in agent_defs:
        graph.add_node(nid)
    graph.add_edge("coordinator", "coder")
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

    node_roles = {nid: agent_config.role for nid, agent_config in agent_defs.items()}
    topology_contexts = build_topology_contexts(
        topology_id=topology_id,
        topology_label=topology_label,
        graph=graph,
        node_roles=node_roles,
        workflow_steps=[
            "Coordinator routes the incoming task to the Coder.",
            "Coder implements or updates the solution in the shared sandbox.",
            "Coder hands the work to Reviewer A, Reviewer B, and Tester.",
            "The two reviewers compare findings with each other before approval.",
            "Tester validates the implementation and sends concrete failures or pass signals.",
            "Each node completes only after finishing its own role in the workflow.",
        ],
        completion_rules={
            "coordinator": [
                "Route the incoming task to the correct worker without solving it yourself.",
                "Forward user clarifications to the right node when needed.",
                "Complete after routing or forwarding the necessary input.",
            ],
            "coder": [
                "Do not complete immediately after first-pass implementation.",
                "Hand off the implementation to both reviewers and the tester before final completion.",
                "Incorporate reviewer and tester feedback before completing.",
            ],
            "reviewer_a": [
                "Review the implementation independently before completing.",
                "Discuss disagreements directly with Reviewer B before signaling approval.",
            ],
            "reviewer_b": [
                "Review the implementation independently before completing.",
                "Discuss disagreements directly with Reviewer A before signaling approval.",
            ],
            "tester": [
                "Run validation against the current implementation before completing.",
                "Send failing cases or validation results to the coder and reviewers.",
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
    for nid, agent_config in agent_defs.items():
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
            n: agent_defs[n].role for n in graph.get_neighbors(nid)
        }
        agent.initialize(neighbor_roles, topology_contexts[nid])

    # Event logger
    event_logger = EventLogger(enabled=trace)

    return Orchestrator(
        agents=agents,
        bus=bus,
        config=config,
        sandbox=sandbox,
        event_logger=event_logger,
    )
