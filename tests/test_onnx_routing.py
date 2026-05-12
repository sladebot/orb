from __future__ import annotations

import pytest

from orb.runtime.onnx_routing import (
    ONNX_ROUTING_FEATURES,
    OnnxRoutingFallbackReason,
    OnnxRoutingPrototype,
    extract_routing_features,
)
from orb.topologies import get_loader


@pytest.fixture
def topologies():
    loader = get_loader()
    return {tid: loader.get(tid) for tid in loader.list_ids()}


def test_feature_extractor_reuses_classifier_signals(topologies):
    features = extract_routing_features(
        query="fix auth regression",
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )

    assert list(features.names) == ONNX_ROUTING_FEATURES
    assert features.values.shape == (1, len(ONNX_ROUTING_FEATURES))
    assert features.signals["mentions_code"] is True
    assert features.signals["mentions_review"] is True
    assert features.signals["mentions_risk"] is True


def test_python_surrogate_routes_obvious_tasks(topologies):
    router = OnnxRoutingPrototype.from_default_weights(topologies=topologies)

    simple = router.predict_python(
        query="say hi",
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )
    coding = router.predict_python(
        query="implement a parser and tests",
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )
    research = router.predict_python(
        query="research and compare model serving options",
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )

    assert simple.topology_id == "solo"
    assert coding.topology_id in {"triad", "hierarchy"}
    assert research.topology_id == "hierarchy"
    assert coding.confidence > 0.45


def test_pinned_topology_bypasses_onnx_for_quality(topologies):
    router = OnnxRoutingPrototype.from_default_weights(topologies=topologies)

    decision = router.route_with_quality_gate(
        query="say hi",
        requested_topology="triad",
        model_pin="auto",
        topologies=topologies,
    )

    assert decision.used_fallback is True
    assert decision.fallback_reason == OnnxRoutingFallbackReason.PINNED_TOPOLOGY
    assert decision.topology_id == "triad"


def test_uncertain_prediction_uses_fallback(topologies):
    router = OnnxRoutingPrototype.from_default_weights(
        topologies=topologies,
        quality_floor=0.99,
    )

    decision = router.route_with_quality_gate(
        query="implement tests",
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )

    assert decision.used_fallback is True
    assert decision.fallback_reason == OnnxRoutingFallbackReason.LOW_CONFIDENCE
    assert decision.routing_mode == "heuristic-fallback"


def test_exports_onnx_file_when_runtime_dependencies_are_available(tmp_path, topologies):
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")

    router = OnnxRoutingPrototype.from_default_weights(topologies=topologies)
    model_path = tmp_path / "routing.onnx"
    router.export_onnx(model_path)

    assert model_path.exists()
    prediction = router.predict_onnx(
        model_path=model_path,
        query="research serving architecture",
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )
    assert prediction.topology_id == "hierarchy"
    assert prediction.latency_ms >= 0
