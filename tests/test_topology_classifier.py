"""Tests for ProviderBackedTopologyClassifier fallback behavior."""
from __future__ import annotations

import json
import pytest

from orb.runtime.topology_classifier import ProviderBackedTopologyClassifier
from orb.topologies.schema import TopologySchema


# ---------------------------------------------------------------------------
# Minimal stub topology
# ---------------------------------------------------------------------------

def _make_topo(tid="simple"):
    """Return a minimal TopologySchema stub."""
    from orb.topologies.schema import AgentSchema
    return TopologySchema(
        id=tid,
        label=tid.capitalize(),
        description="A test topology",
        entry_agent="agent_a",
        agents={"agent_a": AgentSchema(role="worker", description="does stuff")},
        edges=[],
        workflow_steps=[],
        completion_rules={},
    )


_TOPOLOGIES = {"simple": _make_topo("simple")}

_VALID_RESPONSE = json.dumps({
    "task_type": "simple_direct",
    "summary": "A simple test task",
    "complexity": 10,
    "reason": "It is simple",
    "topology": "simple",
    "candidates": [{"topology": "simple", "score": 1.0, "reason": "only option"}],
    "escalation_allowed": False,
    "stop_early_allowed": False,
    "escalation_reason": "",
    "stop_early_reason": "",
})


# ---------------------------------------------------------------------------
# Fake provider helpers
# ---------------------------------------------------------------------------

class _FailingProvider:
    """Simulates a provider whose server is not running."""
    def __init__(self, name="vmlx"):
        self.name = name
        self.call_count = 0

    async def complete(self, req):
        self.call_count += 1
        import httpx
        raise httpx.ConnectError("All connection attempts failed")


class _WorkingProvider:
    """Simulates a provider that succeeds."""
    def __init__(self, name="anthropic"):
        self.name = name
        self.call_count = 0

    async def complete(self, req):
        self.call_count += 1
        from types import SimpleNamespace
        return SimpleNamespace(content=_VALID_RESPONSE, model="claude-haiku")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_on_connect_error():
    """When the primary provider fails with ConnectError, the classifier should
    fall back to the next available provider and succeed."""
    from orb.llm.types import ModelConfig, ModelTier

    failing = _FailingProvider("vmlx")
    working = _WorkingProvider("anthropic")

    providers = {"vmlx": failing, "anthropic": working}

    def model_configs_fn():
        return [
            ModelConfig(ModelTier.LOCAL_SMALL, "qwen", "vmlx"),
            ModelConfig(ModelTier.CLOUD_LITE, "claude-haiku-4-5", "anthropic"),
        ]

    classifier = ProviderBackedTopologyClassifier(
        model_configs_fn,
        lambda name: providers.get(name),
    )

    result = await classifier.classify(
        query="Fix the bug",
        requested_topology="auto",
        model_pin="auto",
        topologies=_TOPOLOGIES,
    )

    assert result.topology_id == "simple"
    assert result.classifier_provider == "anthropic"
    # vmlx was tried first, then anthropic succeeded
    assert failing.call_count == 1
    assert working.call_count == 1


@pytest.mark.asyncio
async def test_no_fallback_when_primary_succeeds():
    """When the primary provider succeeds, no fallback should be attempted."""
    from orb.llm.types import ModelConfig, ModelTier

    working1 = _WorkingProvider("anthropic")
    working2 = _WorkingProvider("openai")
    providers = {"anthropic": working1, "openai": working2}

    def model_configs_fn():
        return [
            ModelConfig(ModelTier.CLOUD_LITE, "claude-haiku-4-5", "anthropic"),
            ModelConfig(ModelTier.CLOUD_LITE, "gpt-4o", "openai"),
        ]

    classifier = ProviderBackedTopologyClassifier(
        model_configs_fn,
        lambda name: providers.get(name),
    )

    result = await classifier.classify(
        query="Fix the bug",
        requested_topology="auto",
        model_pin="auto",
        topologies=_TOPOLOGIES,
    )

    assert result.topology_id == "simple"
    assert working1.call_count == 1
    assert working2.call_count == 0  # never tried


@pytest.mark.asyncio
async def test_all_providers_fail_raises():
    """When every provider fails with ConnectError, a RuntimeError is raised."""
    from orb.llm.types import ModelConfig, ModelTier

    fail1 = _FailingProvider("vmlx")
    fail2 = _FailingProvider("ollama")
    providers = {"vmlx": fail1, "ollama": fail2}

    def model_configs_fn():
        return [
            ModelConfig(ModelTier.LOCAL_SMALL, "qwen", "vmlx"),
            ModelConfig(ModelTier.LOCAL_MEDIUM, "llama3:8b", "ollama"),
        ]

    classifier = ProviderBackedTopologyClassifier(
        model_configs_fn,
        lambda name: providers.get(name),
    )

    with pytest.raises(RuntimeError, match="Topology classification call failed"):
        await classifier.classify(
            query="Fix the bug",
            requested_topology="auto",
            model_pin="auto",
            topologies=_TOPOLOGIES,
        )

    assert fail1.call_count == 1
    assert fail2.call_count == 1


@pytest.mark.asyncio
async def test_no_providers_raises():
    """When model_configs_fn returns empty list, raises RuntimeError."""
    classifier = ProviderBackedTopologyClassifier(
        lambda: [],
        lambda name: None,
    )

    with pytest.raises(RuntimeError, match="No providers available"):
        await classifier.classify(
            query="Fix the bug",
            requested_topology="auto",
            model_pin="auto",
            topologies=_TOPOLOGIES,
        )
