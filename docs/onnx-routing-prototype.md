# ONNX routing prototype

This prototype exports a small linear ONNX surrogate for Orb's routing/orchestration classifier. It is designed as a fast path for obvious task classification and topology selection, not as a full replacement for the existing LLM-backed classifier.

## Artifacts

- `orb/runtime/onnx_routing.py` — feature extractor, Python surrogate, ONNX export, ONNX inference, and quality-gated fallback decision.
- `models/routing_topology_v0.onnx` — exported ONNX graph with a single `features -> MatMul -> Add -> scores` path.
- `scripts/benchmark_onnx_routing.py` — reproducible export and latency benchmark script.
- `docs/onnx-routing-benchmark.json` — benchmark output from this prototype run.

## Feature contract

The ONNX input is `features: float32[batch, 9]` in this order:

1. `word_count_norm`
2. `requested_topology_is_valid`
3. `model_pin_active`
4. `mentions_code`
5. `mentions_review`
6. `mentions_research`
7. `mentions_risk`
8. `mentions_breadth`
9. `is_trivial_query`

The feature extractor deliberately reuses `ProviderBackedTopologyClassifier._query_signals` so this prototype tracks Orb's current classifier prompt signals.

## Fallback policy

The ONNX fast path is bypassed or downgraded to fallback when:

- the user pins a topology (`requested_topology != "auto"`), so Orb preserves explicit routing intent;
- the requested topology is invalid;
- ONNX Runtime is unavailable or inference errors;
- softmax confidence is below `quality_floor` (default `0.45`);
- the margin between the best and runner-up topology is below `margin_floor` (default `0.08`).

In production, the fallback target should be Orb's existing `ProviderBackedTopologyClassifier.classify(...)`. In this prototype's synchronous helper, fallback is represented by `routing_mode="heuristic-fallback"` and the selected topology from the safe Python surrogate or pinned topology.

## Tradeoffs

ONNX improves latency and predictability for cheap routing because it removes the LLM round-trip. The tradeoff is flexibility: a static linear model cannot interpret new topology semantics, nuanced requirements, or policy changes unless its feature extraction and weights are updated and re-exported. The quality gate is therefore part of the design, not an afterthought.

Use ONNX for:

- trivial or obvious routing decisions;
- latency-sensitive pre-routing in the TUI/dashboard;
- deterministic offline experiments.

Keep the LLM classifier for:

- ambiguous decomposition;
- user-pinned or custom topology requests;
- new topology definitions with semantics not captured by current features;
- high-risk routing where wrong topology choice is more expensive than latency.

## Reproduce

Run from the repository root after installing optional export dependencies (`onnx`, `onnxruntime`):

```bash
python scripts/benchmark_onnx_routing.py --iterations 250
```
