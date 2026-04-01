from __future__ import annotations

import dataclasses
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentConfig
from ..graph.graph import Graph
from ..messaging.bus import MessageBus
from ..messaging.channel import InProcessChannel
from ..orchestrator.orchestrator import Orchestrator
from ..orchestrator.types import OrchestratorConfig
from ..sandbox.sandbox import Sandbox
from ..tracing.logger import EventLogger
from ..tracing.run_trace import RunTrace
from .context import build_topology_contexts
from .loader import get_loader, normalize_topology_id
from .schema import ModelSelectionSchema, TopologySchema

if TYPE_CHECKING:
    from ..llm.client import LLMClient
    from ..llm.types import ModelConfig, ModelTier

logger = logging.getLogger(__name__)


def create_orchestrator(
    topology_id: str,
    providers: dict[str, "LLMClient"],
    config: OrchestratorConfig | None = None,
    model_overrides: dict["ModelTier", "ModelConfig"] | None = None,
    trace: bool = True,
    trace_recorder: RunTrace | None = None,
    tier_override: "ModelTier | None" = None,
    agent_model_map: dict[str, "ModelConfig"] | None = None,
    mode: str = "single",
) -> Orchestrator:
    """Build an Orchestrator from a YAML-defined topology."""
    loader = get_loader()
    topology_id = normalize_topology_id(topology_id)
    topo_def = loader.get(topology_id)
    if topo_def is None:
        available = ", ".join(loader.list_ids())
        raise ValueError(
            f"Unknown topology '{topology_id}'. Available: {available}"
        )

    return _build_from_schema(
        topo_def=topo_def,
        providers=providers,
        config=config,
        model_overrides=model_overrides,
        trace=trace,
        trace_recorder=trace_recorder,
        tier_override=tier_override,
        agent_model_map=agent_model_map,
        mode=mode,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _build_common(
    topo_def: TopologySchema,
    providers: dict[str, "LLMClient"],
    config: OrchestratorConfig | None,
    agent_model_map: dict[str, "ModelConfig"] | None,
    trace: bool,
    trace_recorder: RunTrace | None,
):
    """Build the shared pieces: config, agent_defs, graph, topology_contexts, bus, sandbox, trace."""
    config = config or OrchestratorConfig()
    entry = (
        topo_def.entry_agent
        if config.entry_agent == OrchestratorConfig().entry_agent
        else config.entry_agent
    )
    config = dataclasses.replace(
        config,
        entry_agent=entry,
        synthesis_agent=topo_def.synthesis_agent,
    )

    agent_defs: dict[str, AgentConfig] = {}
    for node_id, agent_schema in topo_def.agents.items():
        pinned_model = None
        if agent_model_map and node_id in agent_model_map:
            pinned_model = agent_model_map[node_id]
        elif agent_schema.model_selection:
            pinned_model = _resolve_model_selection(
                agent_schema.model_selection,
                providers,
                agent_defs,
            )

        agent_defs[node_id] = AgentConfig(
            node_id=node_id,
            role=agent_schema.role,
            description=agent_schema.description.strip(),
            base_complexity=agent_schema.base_complexity,
            max_history=agent_schema.max_history,
            pinned_model=pinned_model,
            enable_filesystem=agent_schema.enable_filesystem,
            suppress_context_guidelines=agent_schema.suppress_context_guidelines,
        )

    graph = Graph()
    for nid in agent_defs:
        graph.add_node(nid)
    for a, b in topo_def.edges:
        graph.add_edge(a, b)

    node_roles = {nid: ac.role for nid, ac in agent_defs.items()}
    topology_contexts = build_topology_contexts(
        topology_id=topo_def.id,
        topology_label=topo_def.label,
        graph=graph,
        node_roles=node_roles,
        workflow_steps=topo_def.workflow_steps,
        completion_rules=topo_def.completion_rules,
    )

    bus = MessageBus(
        graph=graph,
        max_depth=config.max_depth,
        budget=config.budget,
        max_cooldown=config.max_cooldown,
    )

    sandbox = Sandbox(root=Path.cwd())
    trace_recorder = trace_recorder or (RunTrace() if trace else None)

    return config, agent_defs, graph, topology_contexts, bus, sandbox, trace_recorder


def _build_from_schema(
    topo_def: TopologySchema,
    providers: dict[str, "LLMClient"],
    config: OrchestratorConfig | None = None,
    model_overrides: dict["ModelTier", "ModelConfig"] | None = None,
    trace: bool = True,
    trace_recorder: RunTrace | None = None,
    tier_override: "ModelTier | None" = None,
    agent_model_map: dict[str, "ModelConfig"] | None = None,
    mode: str = "single",
) -> Orchestrator:
    config, agent_defs, graph, topology_contexts, bus, sandbox, trace_recorder = _build_common(
        topo_def, providers, config, agent_model_map, trace, trace_recorder,
    )

    if mode == "amux":
        return _build_process_mode(
            topo_def, agent_defs, graph, topology_contexts, bus, sandbox,
            trace_recorder, config, trace,
        )

    # ── Local mode (default) ──
    agents: dict[str, LLMAgent] = {}
    for nid, agent_config in agent_defs.items():
        if agent_config.enable_filesystem:
            agent_config = dataclasses.replace(agent_config, sandbox=sandbox)
        channel = InProcessChannel()
        bus.register_channel(nid, channel)
        agents[nid] = LLMAgent(
            config=agent_config,
            channel=channel,
            bus=bus,
            providers=providers,
            model_overrides=model_overrides,
            tier_override=tier_override,
            trace=trace_recorder,
        )

    for nid, agent in agents.items():
        neighbor_roles = {
            n: agent_defs[n].role for n in graph.get_neighbors(nid)
        }
        agent.initialize(neighbor_roles, topology_contexts[nid])

    event_logger = EventLogger(enabled=trace, trace=trace_recorder)

    return Orchestrator(
        agents=agents,
        bus=bus,
        config=config,
        event_logger=event_logger,
        trace=trace_recorder,
        topology_id=topo_def.id,
        sandbox=sandbox,
    )


def _build_process_mode(
    topo_def: TopologySchema,
    agent_defs: dict[str, AgentConfig],
    graph: Graph,
    topology_contexts: dict,
    bus: MessageBus,
    sandbox: Sandbox,
    trace_recorder: RunTrace | None,
    config: OrchestratorConfig,
    trace: bool,
) -> Orchestrator:
    """Build an Orchestrator that runs agents in separate processes over UDS."""
    from ..runtime.uds_server import UDSServer

    socket_dir = tempfile.mkdtemp(prefix="orb-")
    agent_ids = list(agent_defs.keys())
    uds_server = UDSServer(socket_dir=socket_dir, agent_ids=agent_ids)

    # Register InProcessChannel placeholders on the bus for each agent.
    # Once all subprocesses connect, the orchestrator replaces these with live
    # UDSChannels so that bus.route() actually delivers to the subprocesses.
    agents: dict[str, LLMAgent] = {}
    agent_configs_for_process: dict[str, tuple[AgentConfig, dict]] = {}

    for nid, agent_config in agent_defs.items():
        if agent_config.enable_filesystem:
            agent_config = dataclasses.replace(agent_config, sandbox=sandbox)

        channel = InProcessChannel()
        bus.register_channel(nid, channel)

        # Store config + topology context for subprocess bootstrap
        agent_configs_for_process[nid] = (agent_config, topology_contexts[nid].to_dict())

    event_logger = EventLogger(enabled=trace, trace=trace_recorder)

    orchestrator = Orchestrator(
        agents=agents,
        bus=bus,
        config=config,
        event_logger=event_logger,
        trace=trace_recorder,
        topology_id=topo_def.id,
        sandbox=sandbox,
    )

    # Attach process mode metadata for the orchestrator to use
    orchestrator._uds_server = uds_server
    orchestrator._socket_dir = socket_dir
    orchestrator._agent_configs_for_process = agent_configs_for_process
    orchestrator._process_mode = True

    logger.info(f"Process mode orchestrator created, socket dir: {socket_dir}")
    return orchestrator


def _resolve_model_selection(
    selection: ModelSelectionSchema,
    providers: dict[str, object],
    existing_agents: dict[str, AgentConfig],
) -> "ModelConfig | None":
    """Resolve model_selection hints to a concrete ModelConfig."""
    from ..llm.types import ANTHROPIC_MODELS, CODEX_MODELS, ModelConfig, ModelTier

    provider_models: dict[str, ModelConfig] = {
        "anthropic": ANTHROPIC_MODELS[ModelTier.CLOUD_STRONG],
        "openai-codex": CODEX_MODELS[ModelTier.CLOUD_STRONG],
        "ollama": ModelConfig(ModelTier.LOCAL_LARGE, "qwen3.5:27b", "ollama"),
        "vmlx": ModelConfig(ModelTier.LOCAL_LARGE, "qwen", "vmlx"),
    }

    available = [p for p in provider_models if p in providers]

    if selection.prefer_different_provider_than:
        ref_agent = existing_agents.get(selection.prefer_different_provider_than)
        if ref_agent and ref_agent.pinned_model:
            exclude = ref_agent.pinned_model.provider
            candidates = [p for p in available if p != exclude]
            if candidates:
                return provider_models[candidates[0]]

    if selection.prefer_provider and selection.prefer_provider in available:
        return provider_models[selection.prefer_provider]

    for provider in selection.fallback_providers:
        if provider in available:
            return provider_models[provider]

    if available:
        return provider_models[available[0]]

    return None
