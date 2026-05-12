from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import numpy as np

from orb.runtime.topology_classifier import (
    ProviderBackedTopologyClassifier,
    TopologyClassification,
)
from orb.topologies.schema import TopologySchema

ONNX_ROUTING_FEATURES: list[str] = [
    "word_count_norm",
    "requested_topology_is_valid",
    "model_pin_active",
    "mentions_code",
    "mentions_review",
    "mentions_research",
    "mentions_risk",
    "mentions_breadth",
    "is_trivial_query",
]

DEFAULT_TOPOLOGY_ORDER: list[str] = ["solo", "triad", "dual-review", "hierarchy"]

# A small linear surrogate for Orb's current routing/orchestration heuristics.
# Rows are ONNX_ROUTING_FEATURES, columns are DEFAULT_TOPOLOGY_ORDER.
DEFAULT_ROUTING_WEIGHTS = np.array(
    [
        [-2.0, 0.5, 1.0, 1.0],  # word_count_norm
        [0.0, 0.0, 0.0, 0.0],  # requested_topology_is_valid
        [0.0, 0.0, 0.0, 0.0],  # model_pin_active
        [-1.0, 2.0, 0.5, 0.5],  # mentions_code
        [-1.0, 0.2, 2.0, 0.3],  # mentions_review
        [-1.0, 0.0, 0.2, 2.0],  # mentions_research
        [-1.0, 0.2, 2.0, 0.5],  # mentions_risk
        [-1.0, 0.1, 1.2, 1.5],  # mentions_breadth
        [4.0, -1.0, -1.0, -1.0],  # is_trivial_query
    ],
    dtype=np.float32,
)
DEFAULT_ROUTING_BIAS = np.array([0.5, 0.7, 0.2, 0.1], dtype=np.float32)


class OnnxRoutingFallbackReason(str, Enum):
    PINNED_TOPOLOGY = "pinned_topology"
    LOW_CONFIDENCE = "low_confidence"
    LOW_MARGIN = "low_margin"
    INVALID_TOPOLOGY = "invalid_topology"
    ONNX_UNAVAILABLE = "onnx_unavailable"


@dataclass(frozen=True)
class RoutingFeatures:
    names: tuple[str, ...]
    values: np.ndarray
    signals: dict[str, object]


@dataclass(frozen=True)
class RoutingPrediction:
    topology_id: str
    scores: dict[str, float]
    confidence: float
    margin: float
    latency_ms: float = 0.0
    routing_mode: str = "onnx-surrogate"


@dataclass(frozen=True)
class RoutingDecision:
    topology_id: str
    confidence: float
    margin: float
    scores: dict[str, float]
    used_fallback: bool
    fallback_reason: OnnxRoutingFallbackReason | None = None
    routing_mode: str = "onnx-surrogate"


def extract_routing_features(
    *,
    query: str,
    requested_topology: str,
    model_pin: str,
    topologies: dict[str, TopologySchema],
) -> RoutingFeatures:
    """Convert Orb routing signals into the stable numeric ONNX input.

    The feature source intentionally reuses ProviderBackedTopologyClassifier's
    existing _query_signals helper so ONNX export stays aligned with Orb's
    production classifier prompt and heuristic short-circuit behavior.
    """

    signals = ProviderBackedTopologyClassifier._query_signals(
        query=query,
        requested_topology=requested_topology,
        model_pin=model_pin,
        topologies=topologies,
    )
    word_count = float(signals.get("word_count") or 0.0)
    is_trivial = (
        word_count > 0
        and word_count <= 3
        and not any(
            bool(signals.get(name))
            for name in (
                "mentions_code",
                "mentions_review",
                "mentions_research",
                "mentions_risk",
                "mentions_breadth",
            )
        )
        and "@" not in str(query or "")
    )
    values = np.array(
        [
            min(word_count, 80.0) / 80.0,
            1.0 if bool(signals.get("requested_topology_is_valid")) else 0.0,
            1.0 if bool(signals.get("model_pin_active")) else 0.0,
            1.0 if bool(signals.get("mentions_code")) else 0.0,
            1.0 if bool(signals.get("mentions_review")) else 0.0,
            1.0 if bool(signals.get("mentions_research")) else 0.0,
            1.0 if bool(signals.get("mentions_risk")) else 0.0,
            1.0 if bool(signals.get("mentions_breadth")) else 0.0,
            1.0 if is_trivial else 0.0,
        ],
        dtype=np.float32,
    ).reshape(1, -1)
    return RoutingFeatures(
        names=tuple(ONNX_ROUTING_FEATURES),
        values=values,
        signals=signals,
    )


def _softmax(scores: np.ndarray) -> np.ndarray:
    scores = scores.astype(np.float64)
    shifted = scores - np.max(scores)
    exp = np.exp(shifted)
    return exp / np.sum(exp)


class OnnxRoutingPrototype:
    """Prototype ONNX-exportable routing surrogate with a quality fallback.

    This is deliberately a surrogate, not a replacement for Orb's LLM-backed
    classifier. The model covers cheap/obvious topology choices and falls back
    when the user pins topology or the ONNX confidence/margin is weak.
    """

    def __init__(
        self,
        *,
        topology_order: Sequence[str],
        weights: np.ndarray,
        bias: np.ndarray,
        quality_floor: float = 0.45,
        margin_floor: float = 0.08,
    ) -> None:
        self.topology_order = list(topology_order)
        self.weights = np.asarray(weights, dtype=np.float32)
        self.bias = np.asarray(bias, dtype=np.float32)
        self.quality_floor = quality_floor
        self.margin_floor = margin_floor

    @classmethod
    def from_default_weights(
        cls,
        *,
        topologies: dict[str, TopologySchema],
        quality_floor: float = 0.45,
        margin_floor: float = 0.08,
    ) -> "OnnxRoutingPrototype":
        topology_order = [tid for tid in DEFAULT_TOPOLOGY_ORDER if tid in topologies]
        column_indices = [DEFAULT_TOPOLOGY_ORDER.index(tid) for tid in topology_order]
        return cls(
            topology_order=topology_order,
            weights=DEFAULT_ROUTING_WEIGHTS[:, column_indices],
            bias=DEFAULT_ROUTING_BIAS[column_indices],
            quality_floor=quality_floor,
            margin_floor=margin_floor,
        )

    def predict_python(
        self,
        *,
        query: str,
        requested_topology: str,
        model_pin: str,
        topologies: dict[str, TopologySchema],
    ) -> RoutingPrediction:
        started = time.perf_counter()
        features = extract_routing_features(
            query=query,
            requested_topology=requested_topology,
            model_pin=model_pin,
            topologies=topologies,
        )
        raw_scores = (features.values @ self.weights + self.bias).reshape(-1)
        return self._prediction_from_scores(
            raw_scores,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            routing_mode="python-surrogate",
        )

    def predict_onnx(
        self,
        *,
        model_path: str | Path,
        query: str,
        requested_topology: str,
        model_pin: str,
        topologies: dict[str, TopologySchema],
    ) -> RoutingPrediction:
        import onnxruntime as ort

        features = extract_routing_features(
            query=query,
            requested_topology=requested_topology,
            model_pin=model_pin,
            topologies=topologies,
        )
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        started = time.perf_counter()
        raw_scores = session.run(None, {"features": features.values})[0].reshape(-1)
        return self._prediction_from_scores(
            raw_scores,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            routing_mode="onnx-surrogate",
        )

    def route_with_quality_gate(
        self,
        *,
        query: str,
        requested_topology: str,
        model_pin: str,
        topologies: dict[str, TopologySchema],
        model_path: str | Path | None = None,
    ) -> RoutingDecision:
        if requested_topology != "auto" and requested_topology in topologies:
            prediction = self.predict_python(
                query=query,
                requested_topology=requested_topology,
                model_pin=model_pin,
                topologies=topologies,
            )
            return self._fallback_decision(
                topology_id=requested_topology,
                prediction=prediction,
                reason=OnnxRoutingFallbackReason.PINNED_TOPOLOGY,
            )

        if requested_topology != "auto" and requested_topology not in topologies:
            prediction = self.predict_python(
                query=query,
                requested_topology=requested_topology,
                model_pin=model_pin,
                topologies=topologies,
            )
            return self._fallback_decision(
                topology_id=prediction.topology_id,
                prediction=prediction,
                reason=OnnxRoutingFallbackReason.INVALID_TOPOLOGY,
            )

        try:
            prediction = (
                self.predict_onnx(
                    model_path=model_path,
                    query=query,
                    requested_topology=requested_topology,
                    model_pin=model_pin,
                    topologies=topologies,
                )
                if model_path is not None
                else self.predict_python(
                    query=query,
                    requested_topology=requested_topology,
                    model_pin=model_pin,
                    topologies=topologies,
                )
            )
        except Exception:
            prediction = self.predict_python(
                query=query,
                requested_topology=requested_topology,
                model_pin=model_pin,
                topologies=topologies,
            )
            return self._fallback_decision(
                topology_id=prediction.topology_id,
                prediction=prediction,
                reason=OnnxRoutingFallbackReason.ONNX_UNAVAILABLE,
            )

        if prediction.confidence < self.quality_floor:
            return self._fallback_decision(
                topology_id=prediction.topology_id,
                prediction=prediction,
                reason=OnnxRoutingFallbackReason.LOW_CONFIDENCE,
            )
        if prediction.margin < self.margin_floor:
            return self._fallback_decision(
                topology_id=prediction.topology_id,
                prediction=prediction,
                reason=OnnxRoutingFallbackReason.LOW_MARGIN,
            )
        return RoutingDecision(
            topology_id=prediction.topology_id,
            confidence=prediction.confidence,
            margin=prediction.margin,
            scores=prediction.scores,
            used_fallback=False,
            routing_mode=prediction.routing_mode,
        )

    def export_onnx(self, path: str | Path) -> Path:
        """Export the linear surrogate to ONNX.

        Requires the optional `onnx` package at export time. Runtime inference
        only needs `onnxruntime` plus the generated `.onnx` file.
        """

        import onnx
        from onnx import TensorProto, helper, numpy_helper

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        features = helper.make_tensor_value_info(
            "features", TensorProto.FLOAT, [None, len(ONNX_ROUTING_FEATURES)]
        )
        scores = helper.make_tensor_value_info(
            "scores", TensorProto.FLOAT, [None, len(self.topology_order)]
        )
        weight_init = numpy_helper.from_array(self.weights, name="routing_weights")
        bias_init = numpy_helper.from_array(self.bias, name="routing_bias")
        matmul = helper.make_node("MatMul", ["features", "routing_weights"], ["linear"])
        add = helper.make_node("Add", ["linear", "routing_bias"], ["scores"])
        graph = helper.make_graph(
            [matmul, add],
            "orb_routing_surrogate_v0",
            [features],
            [scores],
            [weight_init, bias_init],
        )
        model = helper.make_model(
            graph,
            producer_name="orb.onnx_routing",
            opset_imports=[helper.make_operatorsetid("", 13)],
        )
        model.ir_version = 7
        metadata = {
            "topology_order": ",".join(self.topology_order),
            "feature_order": ",".join(ONNX_ROUTING_FEATURES),
            "fallback_policy": (
                f"fallback if pinned, confidence < {self.quality_floor}, "
                f"or margin < {self.margin_floor}"
            ),
        }
        for key, value in metadata.items():
            prop = model.metadata_props.add()
            prop.key = key
            prop.value = value
        onnx.checker.check_model(model)
        onnx.save(model, path)
        return path

    def _prediction_from_scores(
        self,
        raw_scores: np.ndarray,
        *,
        latency_ms: float,
        routing_mode: str,
    ) -> RoutingPrediction:
        probs = _softmax(raw_scores)
        order = np.argsort(probs)[::-1]
        best_idx = int(order[0])
        runner_up = float(probs[int(order[1])]) if len(order) > 1 else 0.0
        confidence = float(probs[best_idx])
        scores = {
            topology_id: float(raw_scores[idx])
            for idx, topology_id in enumerate(self.topology_order)
        }
        return RoutingPrediction(
            topology_id=self.topology_order[best_idx],
            scores=scores,
            confidence=confidence,
            margin=confidence - runner_up,
            latency_ms=latency_ms,
            routing_mode=routing_mode,
        )

    @staticmethod
    def _fallback_decision(
        *,
        topology_id: str,
        prediction: RoutingPrediction,
        reason: OnnxRoutingFallbackReason,
    ) -> RoutingDecision:
        return RoutingDecision(
            topology_id=topology_id,
            confidence=prediction.confidence,
            margin=prediction.margin,
            scores=prediction.scores,
            used_fallback=True,
            fallback_reason=reason,
            routing_mode="heuristic-fallback",
        )


__all__ = [
    "DEFAULT_TOPOLOGY_ORDER",
    "ONNX_ROUTING_FEATURES",
    "OnnxRoutingFallbackReason",
    "OnnxRoutingPrototype",
    "RoutingDecision",
    "RoutingFeatures",
    "RoutingPrediction",
    "TopologyClassification",
    "extract_routing_features",
]
