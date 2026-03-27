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
            "Choose one task type from: simple_direct, coding, review, broad_research, risky_action, ambiguous_decomposition.\n"
            "Choose the best topology from the listed topologies. If the requested topology is not 'auto', still classify the task but honor the pin.\n"
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
            '"stop_early_allowed": <true|false>}\n\n'
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
            classifier_model=str(response.model or planner_model.model_id or ""),
            classifier_provider=str(planner_model.provider or ""),
        )

    @staticmethod
    def _topology_payload(topologies: dict[str, TopologySchema]) -> dict[str, Any]:
        return {
            topology_id: {
                "label": topo.label,
                "description": topo.description,
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
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if not isinstance(item, dict):
                    continue
                candidate_topology = str(item.get("topology") or "").strip()
                if candidate_topology not in topologies:
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

        reason = str(parsed.get("reason") or "")
        if pinned:
            reason = f"User pinned topology {topo.label}; classifier context: {reason}".strip()

        complexity = max(0, min(100, int(parsed.get("complexity", 50))))
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
            requested_topology=requested_topology,
            routing_mode="pinned" if pinned else "llm",
            signals={},
            classifier_model=classifier_model,
            classifier_provider=classifier_provider,
        )
