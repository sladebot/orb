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
            "Write your implementation to disk using write_file, then verify it with run_command. "
            "Send the file paths to both Reviewer A and Reviewer B for independent review, "
            "and to the Tester for validation. "
            "When you receive feedback from either reviewer, iterate and update the files. "
            "Complete your task only after both reviewers have approved."
        ),
        base_complexity=50,
        enable_filesystem=True,
    ),
    "reviewer_a": AgentConfig(
        node_id="reviewer_a",
        role="Reviewer A",
        description=(
            "You are the first of two senior code reviewers. "
            "Use read_file to read the coder's files directly before reviewing. "
            "Review code independently for correctness, style, edge cases, and best practices. "
            "After forming your own opinion, communicate with Reviewer B to compare findings. "
            "You MUST reach consensus with Reviewer B before approving — discuss any disagreements directly with them. "
            "Only call complete_task once you and Reviewer B have explicitly agreed the code is ready. "
            "Send your final consensus decision to the Coder."
        ),
        base_complexity=65,
        enable_filesystem=True,
    ),
    "reviewer_b": AgentConfig(
        node_id="reviewer_b",
        role="Reviewer B",
        description=(
            "You are the second of two senior code reviewers. "
            "Use read_file to read the coder's files directly before reviewing. "
            "Review code independently for correctness, security, performance, and maintainability. "
            "After forming your own opinion, communicate with Reviewer A to compare findings. "
            "You MUST reach consensus with Reviewer A before approving — discuss any disagreements directly with them. "
            "Only call complete_task once you and Reviewer A have explicitly agreed the code is ready. "
            "Send your final consensus decision to the Coder."
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
            "and run them with run_command. "
            "Report any bugs or failing cases back to the Coder. "
            "Share test results and coverage summaries with both Reviewer A and Reviewer B. "
            "Complete your task when all tests pass."
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
    config = dataclasses.replace(config, entry_agent="coordinator", synthesis_agent="coordinator")

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
        agent.initialize(neighbor_roles)

    # Event logger
    event_logger = EventLogger(enabled=trace)

    return Orchestrator(
        agents=agents,
        bus=bus,
        config=config,
        sandbox=sandbox,
        event_logger=event_logger,
    )
