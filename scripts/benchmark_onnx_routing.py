#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orb.runtime.onnx_routing import OnnxRoutingPrototype
from orb.topologies import get_loader

QUERIES = [
    "say hi",
    "implement a parser and tests",
    "review auth changes for security risk",
    "research and compare model serving options",
    "plan a migration for the runtime architecture",
    "fix the failing dashboard test",
]


def _load_topologies():
    loader = get_loader()
    return {tid: loader.get(tid) for tid in loader.list_ids()}


def _bench(fn, iterations: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(iterations):
        for query in QUERIES:
            started = time.perf_counter()
            fn(query)
            samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return {
        "count": float(len(samples)),
        "mean_ms": statistics.fmean(samples),
        "p50_ms": samples[int(len(samples) * 0.50)],
        "p95_ms": samples[int(len(samples) * 0.95) - 1],
        "max_ms": max(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export and benchmark Orb ONNX routing prototype")
    parser.add_argument("--model", default="models/routing_topology_v0.onnx")
    parser.add_argument("--report", default="docs/onnx-routing-benchmark.json")
    parser.add_argument("--iterations", type=int, default=250)
    args = parser.parse_args()

    topologies = _load_topologies()
    router = OnnxRoutingPrototype.from_default_weights(topologies=topologies)
    model_path = router.export_onnx(args.model)

    # Warm ONNX session/path once before timing.
    router.predict_onnx(
        model_path=model_path,
        query=QUERIES[0],
        requested_topology="auto",
        model_pin="auto",
        topologies=topologies,
    )

    python_stats = _bench(
        lambda query: router.predict_python(
            query=query,
            requested_topology="auto",
            model_pin="auto",
            topologies=topologies,
        ),
        args.iterations,
    )
    onnx_stats = _bench(
        lambda query: router.predict_onnx(
            model_path=model_path,
            query=query,
            requested_topology="auto",
            model_pin="auto",
            topologies=topologies,
        ),
        args.iterations,
    )
    report = {
        "model_path": str(Path(model_path)),
        "queries": QUERIES,
        "iterations_per_query": args.iterations,
        "python_surrogate": python_stats,
        "onnx_runtime": onnx_stats,
        "tradeoff": (
            "This small linear prototype is faster than an LLM classifier but can only "
            "cover cheap, feature-based routing decisions. Keep the quality gate and "
            "fall back to Orb's existing classifier for pinned, ambiguous, or low-margin routes."
        ),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
