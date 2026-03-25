"""Tests that GraphRAGConfig is wired into GraphRuntime._run_orchestrator.

Written before implementation per CLAUDE.md rule #1.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_topo(topology_id: str = "triad", clusters: dict | None = None):
    """Return a mock TopologySchema."""
    from orb.topologies.schema import ClusterSchema
    topo = MagicMock()
    topo.id = topology_id
    topo.persist_base = None
    topo.clusters = {k: ClusterSchema(**v) for k, v in (clusters or {}).items()}
    topo.bridge = None
    return topo


def _make_runtime():
    from orb.runtime.graph_runtime import GraphRuntime
    runtime = GraphRuntime()
    runtime._providers = {}
    runtime._model_overrides = {}
    runtime._config = None
    runtime._tier_override = None
    return runtime


def _patch_run_orchestrator_deps(runtime, mock_topo, mock_orchestrator):
    """Context manager stack that patches all external deps in _run_orchestrator."""
    return [
        patch("orb.topologies.create_orchestrator", return_value=mock_orchestrator),
        patch("orb.topologies.get_loader", return_value=MagicMock(get=MagicMock(return_value=mock_topo))),
        patch("web.bridge.DashboardBridge", return_value=MagicMock(
            state=MagicMock(agents={}),
            setup_agents=MagicMock(), setup_edges=MagicMock(),
            setup_plan=MagicMock(), setup_budget=MagicMock(),
            on_message_routed=MagicMock(), on_agent_complete=AsyncMock(),
        )),
        patch.object(runtime, "_broadcast", new=AsyncMock()),
        patch.object(runtime, "_topology_meta", return_value=("Label", "Desc", {})),
        patch.object(runtime, "_topology_graph_view", return_value={}),
        patch.object(runtime, "_build_agent_model_map", return_value={}),
        patch.object(runtime, "_compactor", MagicMock(compact_messages=AsyncMock(return_value=[]))),
        patch.object(runtime, "_persist_session", MagicMock()),
        patch.object(runtime, "_sync_session_state", MagicMock()),
        patch.object(runtime, "_compact_conversation_session_if_needed", new=AsyncMock()),
        patch.object(runtime, "_pick_primary_result", return_value=("agent", "result")),
        patch("orb.cli.diff_capture.capture_diff", return_value=""),
    ]


def _mock_orchestrator(agents: dict) -> MagicMock:
    orc = MagicMock()
    orc.agents = agents
    orc.bus.graph.edges = []
    orc.bus.on_event = MagicMock()
    orc._on_agent_complete = None
    orc.config.entry_agent = list(agents.keys())[0]
    result = MagicMock()
    result.completions = {k: "done" for k in agents}
    orc.run = AsyncMock(return_value=result)
    for agent in agents.values():
        agent._conversation = MagicMock()
        agent._conversation.messages = []
    return orc


@pytest.mark.asyncio
async def test_agents_in_cluster_receive_subgraph_store(tmp_path):
    """Agents assigned to a cluster get set_subgraph_store called with a shared store."""
    from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

    coordinator = MagicMock()
    coder = MagicMock()
    agents = {"coordinator": coordinator, "coder": coder}

    mock_topo = _make_mock_topo(clusters={
        "cluster_a": {"agents": ["coordinator", "coder"]},
    })
    mock_topo.persist_base = str(tmp_path)

    runtime = _make_runtime()
    orc = _mock_orchestrator(agents)

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _patch_run_orchestrator_deps(runtime, mock_topo, orc):
            stack.enter_context(p)
        await runtime._run_orchestrator("query", "triad")

    coordinator.set_subgraph_store.assert_called_once()
    coder.set_subgraph_store.assert_called_once()

    store = coordinator.set_subgraph_store.call_args[0][0]
    assert isinstance(store, ChromaDBNetworkXStore)
    # Same cluster → same store instance
    assert coder.set_subgraph_store.call_args[0][0] is store


@pytest.mark.asyncio
async def test_agent_not_in_cluster_skips_store(tmp_path):
    """Agents not assigned to any cluster are not given a store."""
    from orb.topologies.schema import ClusterSchema

    coordinator = MagicMock()
    synth = MagicMock()
    agents = {"coordinator": coordinator, "synth": synth}

    mock_topo = _make_mock_topo(clusters={
        "cluster_a": {"agents": ["coordinator"]},
    })
    mock_topo.persist_base = str(tmp_path)

    runtime = _make_runtime()
    orc = _mock_orchestrator(agents)

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _patch_run_orchestrator_deps(runtime, mock_topo, orc):
            stack.enter_context(p)
        await runtime._run_orchestrator("query", "triad")

    coordinator.set_subgraph_store.assert_called_once()
    synth.set_subgraph_store.assert_not_called()


@pytest.mark.asyncio
async def test_no_clusters_skips_all_stores():
    """Topology with no clusters → set_subgraph_store never called."""
    agent = MagicMock()
    agents = {"coordinator": agent}

    mock_topo = _make_mock_topo(clusters={})
    runtime = _make_runtime()
    orc = _mock_orchestrator(agents)

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _patch_run_orchestrator_deps(runtime, mock_topo, orc):
            stack.enter_context(p)
        await runtime._run_orchestrator("query", "triad")

    agent.set_subgraph_store.assert_not_called()


@pytest.mark.asyncio
async def test_run_orchestrator_passes_trace_recorder():
    runtime = _make_runtime()
    agents = {"coordinator": MagicMock()}
    mock_topo = _make_mock_topo(clusters={})
    orc = _mock_orchestrator(agents)

    from contextlib import ExitStack
    patches = _patch_run_orchestrator_deps(runtime, mock_topo, orc)
    create_orchestrator_mock = None
    with ExitStack() as stack:
        for idx, p in enumerate(patches):
            current = stack.enter_context(p)
            if idx == 0:
                create_orchestrator_mock = current
        await runtime._run_orchestrator("query", "triad")

    assert create_orchestrator_mock is not None
    create_orchestrator_mock.assert_called_once()
    _, kwargs = create_orchestrator_mock.call_args
    assert kwargs["trace"] is False
    assert kwargs["trace_recorder"] is not None
