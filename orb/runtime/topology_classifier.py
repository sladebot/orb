from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any

from orb.topologies.schema import TopologySchema


@dataclass(frozen=True)
class TopologyCandidate:
    topology_id: str
    score: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class TopologyClassification:
    topology_id: str
    label: str
    description: str
    task_type: str
    summary: str
    complexity: int
    reason: str
    candidates: list[TopologyCandidate] = field(default_factory=list)
    escalation_allowed: bool = False
    stop_early_allowed: bool = False
    escalation_reason: str = ""
    stop_early_reason: str = ""
    requested_topology: str = "auto"
    routing_mode: str = "llm"
    signals: dict[str, object] = field(default_factory=dict)
    classifier_model: str = ""
    classifier_provider: str = ""


class TopologyClassifier(ABC):
    @abstractmethod
    async def classify(
        self,
        *,
        query: str,
        requested_topology: str,
        model_pin: str,
        topologies: dict[str, TopologySchema],
    ) -> TopologyClassification:
        raise NotImplementedError


def _is_trivial_query(query: str, signals: dict[str, object]) -> bool:
    """Return True for queries that don't need LLM classification.

    Criteria (all must hold):
    - word count <= 3
    - no domain keyword signal fires (code/review/research/risk/breadth)
    - no ``@agent`` scope mention

    Anything failing any condition falls through to the LLM classifier,
    so this stays conservative — when in doubt, we call the LLM.

    The motivation is the 3-15s blank window users saw on queries like
    ``"say hi"``: the classifier ate an LLM round-trip with nothing to
    actually decide, and then a multi-agent topology (``triad``) tried
    to run a full consensus cycle on the resulting one-word answer,
    looping until budget/timeout.
    """
    word_count = int(signals.get("word_count") or 0)
    if word_count == 0 or word_count > 3:
        return False
    keyword_signals = (
        "mentions_code", "mentions_review", "mentions_research",
        "mentions_risk", "mentions_breadth",
    )
    if any(bool(signals.get(s)) for s in keyword_signals):
        return False
    if "@" in str(query or ""):
        # @-scoped queries want a specific agent; respect that and let
        # the LLM classify so it can route properly.
        return False
    return True


def _synth_trivial_classification(
    *,
    requested_topology: str,
    topologies: dict[str, TopologySchema],
    signals: dict[str, object],
) -> TopologyClassification:
    """Synthesize a ``TopologyClassification`` for trivial queries,
    bypassing the LLM.

    Topology choice:
    - If ``requested_topology`` is an explicit, valid topology → honor it.
    - Otherwise pick the lowest-complexity topology available
      (``selection_hints.min_complexity``). ``solo`` is the usual winner.

    ``stop_early_allowed=True`` is the key field — without it,
    multi-agent topologies don't short-circuit and loop on trivial
    answers.
    """
    pinned = requested_topology != "auto" and requested_topology in topologies
    if pinned:
        topology_id = requested_topology
    else:
        topology_id = min(
            topologies.keys(),
            key=lambda tid: (
                topologies[tid].selection_hints.min_complexity
                if topologies[tid].selection_hints
                else 100
            ),
        )
    topo = topologies[topology_id]
    return TopologyClassification(
        topology_id=topology_id,
        label=topo.label,
        description=topo.description,
        task_type="simple_direct",
        summary="trivial query — short-circuited",
        complexity=10,
        reason="Query too short / lacks task signals; skipped LLM classification.",
        candidates=[],
        escalation_allowed=False,
        stop_early_allowed=True,
        escalation_reason="",
        stop_early_reason="Trivial query — topology can terminate after first response.",
        requested_topology=requested_topology,
        routing_mode="heuristic",
        signals=dict(signals),
        classifier_model="",
        classifier_provider="",
    )


class ProviderBackedTopologyClassifier(TopologyClassifier):
    def __init__(self, planner_model_config_fn, provider_lookup_fn) -> None:
        self._planner_model_config_fn = planner_model_config_fn
        self._provider_lookup_fn = provider_lookup_fn

    async def classify(
        self,
        *,
        query: str,
        requested_topology: str,
        model_pin: str,
        topologies: dict[str, TopologySchema],
    ) -> TopologyClassification:
        from orb.llm.types import CompletionRequest

        routing_signals = self._query_signals(
            query=query,
            requested_topology=requested_topology,
            model_pin=model_pin,
            topologies=topologies,
        )
        # Trivial-query short-circuit: skip the LLM round-trip entirely
        # when the query is objectively too small to need classification.
        # See ``_is_trivial_query`` for the exact criteria.
        if _is_trivial_query(query, routing_signals) and topologies:
            return _synth_trivial_classification(
                requested_topology=requested_topology,
                topologies=topologies,
                signals=routing_signals,
            )

        planner_model = self._planner_model_config_fn()
        if planner_model is None:
            raise RuntimeError("No providers available for task classification")

        predict_provider = self._provider_lookup_fn(planner_model.provider)
        if predict_provider is None:
            raise RuntimeError(f"Planner provider '{planner_model.provider}' is not available")

        prompt = (
            "Classify this software task and choose the best topology. Respond with JSON only.\n\n"
            f"Task: {query}\n"
            f"Requested topology: {requested_topology}\n"
            f"Requested model pin: {model_pin}\n\n"
            f"Routing signals: {json.dumps(routing_signals)}\n\n"
            "Choose one task type from: simple_direct, coding, review, broad_research, risky_action, ambiguous_decomposition.\n"
            "Choose the best topology from the listed topologies. Use the provided selection_hints, complexity ranges, and topology descriptions when deciding.\n"
            "If the requested topology is not 'auto', still classify the task but honor the pin.\n"
            "Classify only. Do not assign per-agent models.\n"
            "Do not include any keys other than the required JSON schema keys.\n"
            "The task_type value must exactly match one of the allowed values.\n"
            "The topology value must exactly match one of the listed topology ids.\n"
            "Do not return prose, markdown, or explanations outside the JSON object.\n"
            "Return JSON with this exact shape:\n"
            '{"task_type": "<one of the allowed values>", '
            '"summary": "<short summary>", '
            '"complexity": <0-100 integer>, "reason": "<one sentence why>", '
            '"topology": "<topology id>", '
            '"candidates": [{"topology": "<topology id>", "score": <number>, "reason": "<why>"}], '
            '"escalation_allowed": <true|false>, '
            '"stop_early_allowed": <true|false>, '
            '"escalation_reason": "<short recommendation reason>", '
            '"stop_early_reason": "<short recommendation reason>"}\n\n'
            f"Available topologies: {json.dumps(self._topology_payload(topologies))}\n"
        )
        req = CompletionRequest(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system=(
                "You are the topology classifier for a software multi-agent runtime. "
                "Classify the task and choose the best overall topology. "
                "Do not assign models or plan execution steps. "
                "Return valid JSON only."
            ),
            model_config=planner_model,
        )
        try:
            response = await predict_provider.complete(req)
        except Exception as exc:
            raise RuntimeError(f"Topology classification call failed: {exc}") from exc

        parsed = self._parse_response(response.content or "")
        return self._build_classification(
            parsed=parsed,
            requested_topology=requested_topology,
            topologies=topologies,
            signals=routing_signals,
            classifier_model=str(response.model or planner_model.model_id or ""),
            classifier_provider=str(planner_model.provider or ""),
        )

    @staticmethod
    def _topology_payload(topologies: dict[str, TopologySchema]) -> dict[str, Any]:
        return {
            topology_id: {
                "label": topo.label,
                "description": topo.description,
                "selection_hints": {
                    "ideal_for": list((topo.selection_hints.ideal_for if topo.selection_hints else []) or []),
                    "keywords": list((topo.selection_hints.keywords if topo.selection_hints else []) or []),
                    "min_complexity": int((topo.selection_hints.min_complexity if topo.selection_hints else 0) or 0),
                    "max_complexity": int((topo.selection_hints.max_complexity if topo.selection_hints else 100) or 100),
                },
                "agents": {
                    agent_id: {
                        "role": agent.role,
                        "category": agent.category,
                        "description": agent.description,
                    }
                    for agent_id, agent in topo.agents.items()
                },
            }
            for topology_id, topo in topologies.items()
        }

    @staticmethod
    def _query_signals(
        *,
        query: str,
        requested_topology: str,
        model_pin: str,
        topologies: dict[str, TopologySchema],
    ) -> dict[str, object]:
        lowered = str(query or "").lower()
        words = [part for part in lowered.replace("\n", " ").split() if part]
        return {
            "word_count": len(words),
            "requested_topology": requested_topology,
            "requested_topology_is_valid": requested_topology == "auto" or requested_topology in topologies,
            "model_pin_active": model_pin != "auto",
            "mentions_code": any(token in lowered for token in ("code", "implement", "fix", "test", "refactor", "bug", "repo", "file")),
            "mentions_review": any(token in lowered for token in ("review", "audit", "regression", "correctness", "risk")),
            "mentions_research": any(token in lowered for token in ("research", "investigate", "explore", "understand", "compare")),
            "mentions_risk": any(token in lowered for token in ("security", "production", "critical", "dangerous", "delete", "auth")),
            "mentions_breadth": any(token in lowered for token in ("architecture", "plan", "migration", "system", "broad")),
        }

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        if len(raw) > 500_000:
            raise RuntimeError(f"Topology classifier response too large to parse ({len(raw)} bytes)")
        try:
            parsed = json.loads(raw.strip())
        except JSONDecodeError:
            preview = raw.strip().replace("\n", " ")[:160]
            suffix = "…" if len(raw.strip()) > 160 else ""
            raise RuntimeError(f"Topology classifier response could not be parsed: {preview}{suffix}") from None
        if not isinstance(parsed, dict):
            raise RuntimeError("Topology classifier returned non-object JSON")
        return parsed

    @staticmethod
    def _build_classification(
        *,
        parsed: dict[str, Any],
        requested_topology: str,
        topologies: dict[str, TopologySchema],
        signals: dict[str, object] | None = None,
        classifier_model: str = "",
        classifier_provider: str = "",
    ) -> TopologyClassification:
        allowed_task_types = {
            "simple_direct",
            "coding",
            "review",
            "broad_research",
            "risky_action",
            "ambiguous_decomposition",
        }
        task_type = str(parsed.get("task_type") or "").strip()
        if task_type not in allowed_task_types:
            raise RuntimeError(f"Topology classifier returned invalid task_type: {task_type or '<empty>'}")

        pinned = requested_topology != "auto" and requested_topology in topologies
        topology_id = requested_topology if pinned else parsed.get("topology")
        if topology_id not in topologies:
            raise RuntimeError(f"Topology classifier returned invalid topology: {topology_id!r}")

        topo = topologies[topology_id]
        candidates: list[TopologyCandidate] = []
        raw_candidates = parsed.get("candidates") or []
        seen_candidates: set[str] = set()
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if not isinstance(item, dict):
                    continue
                candidate_topology = str(item.get("topology") or "").strip()
                if candidate_topology not in topologies or candidate_topology in seen_candidates:
                    continue
                score = item.get("score")
                try:
                    parsed_score = float(score)
                except (TypeError, ValueError):
                    parsed_score = None
                candidates.append(
                    TopologyCandidate(
                        topology_id=candidate_topology,
                        score=parsed_score,
                        reason=str(item.get("reason") or ""),
                    )
                )
                seen_candidates.add(candidate_topology)

        reason = str(parsed.get("reason") or "")
        if pinned:
            reason = f"User pinned topology {topo.label}; classifier context: {reason}".strip()

        complexity = max(0, min(100, int(parsed.get("complexity", 50))))
        if topology_id not in seen_candidates:
            candidates.insert(
                0,
                TopologyCandidate(
                    topology_id=topology_id,
                    score=1.0 if not candidates else None,
                    reason=reason or "selected topology",
                ),
            )
        return TopologyClassification(
            topology_id=topology_id,
            label=topo.label,
            description=topo.description,
            task_type=task_type,
            summary=str(parsed.get("summary") or ""),
            complexity=complexity,
            reason=reason,
            candidates=candidates,
            escalation_allowed=bool(parsed.get("escalation_allowed")),
            stop_early_allowed=bool(parsed.get("stop_early_allowed")),
            escalation_reason=str(parsed.get("escalation_reason") or ""),
            stop_early_reason=str(parsed.get("stop_early_reason") or ""),
            requested_topology=requested_topology,
            routing_mode="pinned" if pinned else "llm",
            signals=dict(signals or {}),
            classifier_model=classifier_model,
            classifier_provider=classifier_provider,
        )
