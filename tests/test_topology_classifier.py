"""Tests for ``orb.runtime.topology_classifier``.

Focus: the trivial-query short-circuit that must skip the LLM on short
no-signal queries so the TUI can return in <1s for queries like
``"say hi"`` instead of eating a 3–15s classifier round-trip.
"""

from __future__ import annotations

import pytest

from orb.runtime.topology_classifier import (
    ProviderBackedTopologyClassifier,
    TopologyClassification,
)
from orb.topologies import get_loader


@pytest.fixture
def topologies():
    loader = get_loader()
    return {tid: loader.get(tid) for tid in loader.list_ids()}


@pytest.fixture
def classifier_with_fake_llm():
    """Classifier that tracks whether the LLM was called.

    If ``classify`` reaches the LLM path, ``RuntimeError`` fires — so a
    test asserting "classifier must short-circuit" can just look at the
    return value; if the short-circuit didn't fire, the test blows up
    with the sentinel.
    """

    llm_calls: list[str] = []

    class _ExplodingProvider:
        async def complete(self, req):  # noqa: ANN001
            llm_calls.append(str(req.messages[0].get("content", ""))[:80])
            raise RuntimeError(
                "LLM was invoked — trivial-query short-circuit failed to fire"
            )

    class _FakeModelConfig:
        provider = "fake"
        model_id = "fake-model"

    def _planner_model_config_fn():
        return _FakeModelConfig()

    def _provider_lookup_fn(name):  # noqa: ARG001
        return _ExplodingProvider()

    classifier = ProviderBackedTopologyClassifier(
        planner_model_config_fn=_planner_model_config_fn,
        provider_lookup_fn=_provider_lookup_fn,
    )
    return classifier, llm_calls


class TestTrivialQueryShortCircuit:
    """The classifier must NOT call the LLM on trivial queries — it
    should synthesize a ``TopologyClassification`` directly.

    Trivial criteria (see ``_is_trivial_query``):
    - ``word_count <= 3``
    - no domain keyword signals (no code/review/research/risk/breadth)
    - no ``@agent`` scope mention in the query

    The synthesized classification must carry ``stop_early_allowed=True``
    so multi-agent topologies can terminate after a single response
    rather than running a full consensus loop (which is what caused
    ``triad`` to hang forever on ``"say hi"`` pre-fix).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "say hi",
            "hello",
            "ping",
            "hi there",
        ],
    )
    async def test_trivial_query_skips_llm(
        self, query: str, topologies, classifier_with_fake_llm
    ):
        classifier, llm_calls = classifier_with_fake_llm
        result = await classifier.classify(
            query=query,
            requested_topology="auto",
            model_pin="auto",
            topologies=topologies,
        )
        assert isinstance(result, TopologyClassification)
        assert llm_calls == [], (
            f"LLM was called for trivial query {query!r}: {llm_calls}"
        )
        assert result.stop_early_allowed is True, (
            "Trivial queries must set stop_early_allowed=True — otherwise "
            "multi-agent topologies like triad loop forever waiting for "
            "consensus on a one-word answer."
        )
        assert result.task_type == "simple_direct"
        assert result.complexity <= 20
        assert result.routing_mode == "heuristic"

    @pytest.mark.asyncio
    async def test_trivial_query_with_explicit_topology_honors_pin(
        self, topologies, classifier_with_fake_llm
    ):
        """When the user pins a topology AND the query is trivial, honor
        the pin — don't second-guess them by picking ``solo``.
        """
        classifier, llm_calls = classifier_with_fake_llm
        result = await classifier.classify(
            query="hi",
            requested_topology="triad",
            model_pin="auto",
            topologies=topologies,
        )
        assert llm_calls == []
        assert result.topology_id == "triad"
        assert result.stop_early_allowed is True

    @pytest.mark.asyncio
    async def test_trivial_auto_topology_picks_simplest_available(
        self, topologies, classifier_with_fake_llm
    ):
        """With ``auto`` topology + trivial query, pick the lowest-complexity
        topology available (``solo`` when present)."""
        classifier, _ = classifier_with_fake_llm
        result = await classifier.classify(
            query="ping",
            requested_topology="auto",
            model_pin="auto",
            topologies=topologies,
        )
        assert result.topology_id == "solo"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            # Short but has code keyword.
            "fix the test",
            # Short but has review keyword.
            "review this",
            # Short but has risk keyword.
            "delete auth",
            # More than 3 words — falls through to LLM regardless.
            "can you write a short script",
        ],
    )
    async def test_non_trivial_query_still_calls_llm(
        self, query: str, topologies, classifier_with_fake_llm
    ):
        """Queries with domain signals or long enough to plausibly need
        real classification must fall through to the LLM path. The
        exploding provider proves the short-circuit did NOT fire.
        """
        classifier, _ = classifier_with_fake_llm
        with pytest.raises(RuntimeError, match="LLM was invoked"):
            await classifier.classify(
                query=query,
                requested_topology="auto",
                model_pin="auto",
                topologies=topologies,
            )

    @pytest.mark.asyncio
    async def test_agent_mention_forces_llm(self, topologies, classifier_with_fake_llm):
        """@-scoped queries always need classification — the user wants
        a specific agent to handle the message, not a short-circuited
        solo response.
        """
        classifier, _ = classifier_with_fake_llm
        with pytest.raises(RuntimeError, match="LLM was invoked"):
            await classifier.classify(
                query="@coder hi",
                requested_topology="auto",
                model_pin="auto",
                topologies=topologies,
            )


