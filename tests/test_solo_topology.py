"""Tests for the single-agent "solo" topology.

Motivation: trivial tasks (a one-liner, a rename, a short explain request)
don't need a full triad. The solo topology is one agent, no edges, no
coordinator — just a capable worker with filesystem access.
"""
from __future__ import annotations

from orb.topologies import create_orchestrator, get_loader


def test_solo_topology_is_registered():
    loader = get_loader()
    ids = loader.list_ids()
    assert "solo" in ids, f"expected 'solo' in list_ids(), got: {ids}"


def test_solo_topology_has_exactly_one_agent_and_no_edges():
    topo = get_loader().get("solo")
    assert topo is not None
    assert len(topo.agents) == 1, f"expected 1 agent, got: {list(topo.agents)}"
    assert topo.edges == [], f"expected no edges, got: {topo.edges}"
    # The entry_agent must be that sole agent.
    assert topo.entry_agent in topo.agents


def test_solo_agent_can_write_files():
    """The solo agent is the only worker — it must have filesystem access or
    it can't do any code-writing task at all.
    """
    topo = get_loader().get("solo")
    agent = next(iter(topo.agents.values()))
    assert agent.enable_filesystem is True


def test_solo_selection_hints_target_simple_tasks():
    """Classifier must know to pick solo for low-complexity tasks."""
    topo = get_loader().get("solo")
    hints = topo.selection_hints
    assert hints is not None
    # Range should start at 0 and cap well below triad's floor so the
    # classifier escalates out of solo for anything non-trivial.
    assert hints.min_complexity == 0
    assert hints.max_complexity < 40, (
        f"solo.max_complexity={hints.max_complexity} — should be a tight low band"
    )


class _StubLLMClient:
    """Minimal LLMClient for factory wiring without real HTTP traffic."""
    async def complete(self, request):  # noqa: D401
        raise NotImplementedError
    async def close(self):
        pass


def test_create_orchestrator_builds_single_agent_solo(tmp_path):
    providers = {"anthropic": _StubLLMClient()}
    orchestrator = create_orchestrator(
        "solo",
        providers=providers,
        workdir=str(tmp_path),
    )
    assert len(orchestrator.agents) == 1
    # The sandbox must be anchored to the provided workdir, not process CWD.
    sole = next(iter(orchestrator.agents.values()))
    assert sole.config.enable_filesystem is True
