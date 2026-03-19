from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from json import JSONDecodeError
from dataclasses import dataclass
from pathlib import Path

from web.state import DashboardState
from orb.cli.config import get as get_config
from orb.agent.compaction import COMPACT_THRESHOLD, DEFAULT_COMPACTOR, CompactionStrategy
from orb.messaging.channel import ChannelClosed
from .transcript import ConversationSession, RunTranscript

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[str], Awaitable[None]]
SESSION_TURN_COMPACT_THRESHOLD = 60


@dataclass
class AgentModelAssignment:
    config: object
    reason: str = ""


class GraphRuntime:
    """Owns orchestration and exposes a subscriber-oriented runtime interface."""

    def __init__(
        self,
        state: DashboardState | None = None,
        *,
        session_path: Path | None = None,
        compactor: CompactionStrategy | None = None,
    ) -> None:
        self.state = state or DashboardState()
        self._subscribers: set[BroadcastFn] = set()
        self._agents: dict = {}
        self._run_task: asyncio.Task | None = None
        self._providers: dict = {}
        self._all_providers: dict = {}
        self._enabled_providers: list[str] = []
        self._config = None
        self._model_overrides = None
        self._tier_override = None
        self._session_path = session_path
        self._compactor = compactor or DEFAULT_COMPACTOR
        self._conversation_session = self._load_session()
        self._turn_count: int = 0
        self._last_result = None
        self._run_transcript = RunTranscript(session=self._conversation_session)

    @staticmethod
    def _available_topologies() -> dict:
        from orb.topologies import get_loader

        return get_loader().list_all()

    @staticmethod
    def _topology_meta(topology_id: str) -> tuple[str, str, dict[str, str]]:
        from orb.topologies import normalize_topology_id

        topo = GraphRuntime._available_topologies().get(normalize_topology_id(topology_id))
        if topo is None:
            return ("Unknown", "Unknown topology", {})

        positions: dict[str, str] = {}
        default_positions = {
            "entry": "entry router",
            "implementation": "implementation hub",
            "review": "quality edge",
            "validation": "validation edge",
            "discovery": "discovery layer",
            "worker": "worker node",
        }
        for agent_id, agent in topo.agents.items():
            positions[agent_id] = (
                agent.position_label
                or default_positions.get(agent.category, "worker node")
            )
        return (topo.label, topo.description, positions)

    @staticmethod
    def _topology_graph_view(topology_id: str) -> dict:
        from orb.topologies import normalize_topology_id

        topo = GraphRuntime._available_topologies().get(normalize_topology_id(topology_id))
        if topo is None:
            return {"rows": [], "order": []}

        if topo.graph_view is not None:
            return {
                "rows": topo.graph_view.rows,
                "order": topo.graph_view.order,
            }

        # Auto-generate a basic fallback when no graph_view is defined
        order = list(topo.agents.keys())
        rows: list[list[dict]] = []
        for agent_id in order:
            rows.append([{"node": agent_id}])
        return {"rows": rows, "order": order}

    @staticmethod
    def _topology_options(selected_id: str) -> list[dict]:
        options = []
        for topology_id, topo in GraphRuntime._available_topologies().items():
            options.append({
                "topology": topology_id,
                "label": topo.label,
                "description": topo.description,
                "chosen": topology_id == selected_id,
            })
        return options

    @staticmethod
    def _heuristic_topology_ranking(query: str, complexity: int) -> list[tuple[str, float]]:
        query_l = query.lower()
        scores: list[tuple[str, float]] = []
        for topology_id, topo in GraphRuntime._available_topologies().items():
            score = 0.0
            hints = topo.selection_hints
            if hints is not None:
                if hints.min_complexity <= complexity <= hints.max_complexity:
                    score += 2.0
                else:
                    score -= 0.5
                for keyword in hints.keywords:
                    if keyword.lower() in query_l:
                        score += 1.0
                for phrase in hints.ideal_for:
                    if any(token in query_l for token in phrase.lower().split()):
                        score += 0.2
            categories = {agent.category for agent in topo.agents.values()}
            if "discovery" in categories and any(token in query_l for token in ("research", "explore", "understand", "investigate", "plan")):
                score += 1.0
            if sum(1 for agent in topo.agents.values() if agent.category == "review") >= 2 and complexity >= 65:
                score += 1.0
            if sum(1 for agent in topo.agents.values() if agent.category == "review") == 1 and complexity < 65:
                score += 0.5
            scores.append((topology_id, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    @property
    def last_result(self):
        return self._last_result

    def subscribe(self, callback: BroadcastFn) -> None:
        self._subscribers.add(callback)

    def unsubscribe(self, callback: BroadcastFn) -> None:
        self._subscribers.discard(callback)

    async def _broadcast(self, data: str) -> None:
        stale: list[BroadcastFn] = []
        for callback in self._subscribers:
            try:
                await callback(data)
            except Exception:
                stale.append(callback)
        for callback in stale:
            self._subscribers.discard(callback)

    def configure(self, providers: dict, config, model_overrides, tier_override) -> None:
        self._all_providers = dict(providers)
        self._config = config
        self._model_overrides = model_overrides
        self._tier_override = tier_override
        provider_cfg = get_config("providers") or {}
        enabled = [
            name for name, value in provider_cfg.items()
            if isinstance(value, dict) and bool(value.get("enabled")) and name in self._all_providers
        ]
        self._enabled_providers = enabled
        if enabled:
            self._providers = {
                name: client
                for name, client in self._all_providers.items()
                if name in set(enabled)
            }
            if not self._providers:
                self._providers = dict(self._all_providers)
        else:
            self._providers = dict(self._all_providers)

    def settings_payload(self) -> dict:
        model_payload = self.models_payload()
        available = sorted(self._all_providers.keys())
        provider_config = get_config("providers") or {}
        return {
            "available_providers": available,
            "providers": {
                name: {
                    "enabled": bool((provider_config.get(name) or {}).get("enabled", True)),
                    "active": name in self._providers,
                }
                for name in available
            },
            "models": model_payload.get("models", []),
        }

    def _resolved_session_path(self) -> Path:
        return self._session_path or (Path.cwd() / ".orb" / "session.json")

    def _load_session(self) -> ConversationSession:
        path = self._resolved_session_path()
        if not path.exists():
            return ConversationSession()
        try:
            return ConversationSession.load(path)
        except (OSError, JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load conversation session %s: %s", path, exc)
            return ConversationSession()

    def _persist_session(self) -> None:
        try:
            self._conversation_session.save(self._resolved_session_path())
        except OSError as exc:
            logger.warning("Failed to persist conversation session: %s", exc)

    def _sync_session_state(self) -> None:
        self.state.workdir = str(Path.cwd())
        self.state.session_turn = self._conversation_session.user_turn_count()
        self.state.session_id = self._conversation_session.session_id
        self.state.session_generation = self._conversation_session.generation

    @staticmethod
    def _sanitize_carryover(messages: list[dict]) -> list[dict]:
        msgs = list(messages)
        while msgs:
            last = msgs[-1]
            role = last.get("role")
            content = last.get("content", "")
            if role == "user":
                msgs.pop()
                continue
            if role == "assistant" and isinstance(content, list) and any(
                block.get("type") == "tool_use" for block in content
            ):
                msgs.pop()
                continue
            break
        return msgs

    def _resolve_conversation_target(
        self,
        text: str,
        *,
        default_target: str,
        known_targets: set[str] | None = None,
    ) -> tuple[str, str]:
        match = re.match(r"^@(\w+)\s*", text.strip())
        if not match:
            return default_target, text.strip()

        target_id = match.group(1).lower()
        remainder = text[match.end():].strip()
        if not remainder:
            return default_target, text.strip()
        if known_targets and target_id not in known_targets:
            return default_target, text.strip()
        return target_id, remainder

    async def _compact_conversation_session_if_needed(self) -> None:
        if self._conversation_session.turn_count() < SESSION_TURN_COMPACT_THRESHOLD:
            return
        transcript = self._conversation_session.render_prior_context(
            recent_turns=self._conversation_session.turn_count()
        )
        summary = await self._compactor.compact_transcript(transcript, self._providers)
        if not summary:
            return
        self._conversation_session.apply_compaction(summary, preserve_recent_turns=8)

    def current_init_event(self) -> dict:
        self._sync_session_state()
        event = self.state.to_init_event()
        event["run_active"] = self.running
        return event

    async def _record_plan_step(self, stage: str, title: str, detail: str) -> None:
        from web.state import PlanStepRecord

        elapsed = round(time.time() - self.state.start_time, 2)
        self.state.plan_steps.append(PlanStepRecord(stage=stage, title=title, detail=detail, elapsed=elapsed))
        if len(self.state.plan_steps) > 20:
            self.state.plan_steps = self.state.plan_steps[-20:]
        await self._broadcast(json.dumps({
            "type": "plan_step",
            "stage": stage,
            "title": title,
            "detail": detail,
            "elapsed": elapsed,
        }))

    async def stop(self) -> None:
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def wait_for_run(self) -> None:
        if self._run_task:
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def inject_message(self, target_id: str, text: str) -> tuple[int, dict]:
        from orb.messaging.message import Message, MessageType

        if not self.running:
            return 400, {"ok": False, "error": "No run in progress"}

        known_targets = {aid.lower() for aid in self._agents}
        resolved_target, resolved_text = self._resolve_conversation_target(
            text,
            default_target=target_id,
            known_targets=known_targets,
        )

        agent = self._agents.get(resolved_target)
        if agent is None:
            return 404, {"ok": False, "error": f"Unknown agent: {resolved_target}"}

        msg = Message(
            from_="user",
            to=resolved_target,
            type=MessageType.RESPONSE,
            payload=resolved_text,
        )
        try:
            await agent.channel.send(msg)
        except ChannelClosed as exc:
            logger.exception("Failed to inject message")
            return 500, {"ok": False, "error": str(exc)}
        self._run_transcript.add_message(msg)
        self._persist_session()
        self._sync_session_state()

        await self._broadcast(json.dumps({
            "type": "message",
            "from": "user",
            "to": resolved_target,
            "content": resolved_text,
            "model": "",
            "depth": 0,
            "elapsed": 0,
            "chain_id": msg.chain_id,
            "msg_type": "response",
            "context_slice": [],
        }))
        return 200, {"ok": True}

    async def start_run(
        self,
        query: str,
        topology: str,
        model_pin: str = "auto",
    ) -> tuple[int, dict]:
        from orb.topologies import normalize_topology_id

        if not self._providers:
            return 500, {"ok": False, "error": "Server has no providers configured"}
        if self.running:
            return 200, {"ok": False, "error": "Run already in progress"}

        self.state.reset()
        self._last_result = None
        self._run_transcript = RunTranscript(session=self._conversation_session)
        requested_target, query = self._resolve_conversation_target(
            query,
            default_target="coordinator",
        )
        self.state.run_query = query
        self._sync_session_state()
        await self._record_plan_step("planning", "Starting run planning", "Analyzing task, topology, and per-agent model allocation.")
        predicted = await self.predict_topology(query, model_pin=model_pin)
        selected_topology = normalize_topology_id(topology) if topology != "auto" else predicted.get("topology", "triad")
        await self._record_plan_step(
            "topology",
            "Selected topology",
            f"{predicted.get('label') or selected_topology}: {predicted.get('reason') or predicted.get('description') or ''}".strip(),
        )
        topology_label, topology_description, agent_positions = self._topology_meta(selected_topology)
        graph_view = self._topology_graph_view(selected_topology)
        agent_complexity = dict(predicted.get("agent_complexity") or {})
        overall_complexity = int(predicted.get("complexity", 50))
        heuristic_model_map = self._build_agent_model_map(
            overall_complexity,
            model_pin=model_pin,
            agent_complexity=agent_complexity,
            topology_id=selected_topology,
        )
        await self._record_plan_step(
            "allocator",
            "Built baseline model map",
            ", ".join(f"{aid}={cfg.model_id}" for aid, cfg in heuristic_model_map.items()),
        )
        agent_model_map, _agent_model_reasons = await self._llm_assign_agent_models(
            query,
            selected_topology,
            overall_complexity,
            agent_complexity,
            heuristic_model_map,
        )
        await self._record_plan_step(
            "allocator",
            "Pinned per-node models",
            ", ".join(f"{aid}={cfg.model_id}" for aid, cfg in agent_model_map.items()),
        )
        agent_models = {role: cfg.model_id for role, cfg in agent_model_map.items()}
        self.state.topology_id = selected_topology
        self.state.topology_label = topology_label
        self.state.topology_description = topology_description
        self.state.agent_complexity = agent_complexity
        self.state.agent_models = agent_models
        self.state.agent_positions = agent_positions
        self.state.graph_view = graph_view
        self._run_task = asyncio.create_task(
            self._run_orchestrator(
                query,
                selected_topology,
                initial_target=requested_target,
                model_pin=model_pin,
                complexity=overall_complexity,
                agent_complexity=agent_complexity,
                agent_model_map=agent_model_map,
            )
        )
        self._run_task.add_done_callback(
            lambda t: logger.error("Run task failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )
        return 200, {"ok": True}

    async def stop_run(self) -> dict:
        if self.running:
            self._run_task.cancel()
            await self._broadcast(json.dumps({"type": "stopped"}))
            return {"ok": True}
        return {"ok": False, "error": "No run in progress"}

    def models_payload(self) -> dict:
        from orb.llm.types import ANTHROPIC_MODEL_LABELS, ANTHROPIC_PROVIDER, ANTHROPIC_MODELS

        models = [{"id": "auto", "label": "Auto-select", "provider": "auto", "local": False}]
        if "anthropic" in self._providers:
            for config in ANTHROPIC_MODELS.values():
                models.append({
                    "id": config.model_id,
                    "label": ANTHROPIC_MODEL_LABELS[config.model_id],
                    "provider": ANTHROPIC_PROVIDER,
                    "local": False,
                })
        if "openai-codex" in self._providers:
            models += [{"id": "gpt-5.4", "label": "GPT-5.4 (Codex)", "provider": "openai-codex", "local": False}]
        if "ollama" in self._providers:
            models += [
                {"id": "qwen3.5:9b", "label": "Qwen 9b", "provider": "ollama", "local": True},
                {"id": "qwen3.5:27b", "label": "Qwen 27b", "provider": "ollama", "local": True},
            ]
        return {"models": models}

    def _pick_primary_result(self, completions: dict[str, str]) -> tuple[str | None, str]:
        preferred = ["coder", "reviewer", "reviewer_a", "reviewer_b", "tester", "coordinator"]
        for agent_id in preferred:
            result = completions.get(agent_id, "")
            if result and not result.startswith("Consensus:") and result != "[shutdown]":
                return agent_id, result
        for agent_id, result in completions.items():
            if result and not result.startswith("Consensus:") and result != "[shutdown]":
                return agent_id, result
        return None, ""

    async def predict_topology(self, query: str, model_pin: str = "auto") -> dict:
        if not query:
            first_id, topo = next(iter(self._available_topologies().items()))
            return {
                "topology": first_id,
                "label": topo.label,
                "description": topo.description,
                "options": self._topology_options(first_id),
            }
        return await self._llm_predict_topology(query, model_pin=model_pin)

    def _build_agent_model_map(
        self,
        complexity: int,
        model_pin: str = "auto",
        agent_complexity: dict | None = None,
        topology_id: str = "triad",
    ) -> dict:
        from orb.llm.types import (
            ANTHROPIC_HAIKU_MODEL,
            ANTHROPIC_OPUS_MODEL,
            ANTHROPIC_PROVIDER,
            ANTHROPIC_SONNET_MODEL,
            ModelConfig,
            ModelTier,
            OPENAI_CODEX_PROVIDER,
            OLLAMA_PROVIDER,
        )

        has_ollama = "ollama" in self._providers
        has_anthropic = "anthropic" in self._providers
        has_codex = "openai-codex" in self._providers

        def ollama(model_id: str) -> ModelConfig:
            return ModelConfig(tier=ModelTier.LOCAL_LARGE, model_id=model_id, provider=OLLAMA_PROVIDER)

        def ant(tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider=ANTHROPIC_PROVIDER)

        def codex(tier: ModelTier) -> ModelConfig:
            return ModelConfig(tier=tier, model_id="gpt-5.4", provider=OPENAI_CODEX_PROVIDER)

        force_provider: str | None = None
        if model_pin and model_pin != "auto":
            if "claude" in model_pin:
                force_provider = "anthropic"
            elif model_pin == "gpt-5.4":
                force_provider = "openai-codex"
            elif "qwen" in model_pin or "llama" in model_pin:
                force_provider = "ollama"

        provider_available = {
            "anthropic": has_anthropic,
            "openai-codex": has_codex,
            "ollama": has_ollama,
        }
        if force_provider and not provider_available.get(force_provider):
            logger.warning("Forced provider '%s' not available; falling back to auto", force_provider)
            force_provider = None

        q9 = ollama("qwen3.5:9b") if has_ollama and force_provider in (None, "ollama") else None
        q27 = ollama("qwen3.5:27b") if has_ollama and force_provider in (None, "ollama") else None
        use_ant = has_anthropic and force_provider in (None, "anthropic")
        use_codex = has_codex and force_provider in (None, "openai-codex")

        haiku = (ant(ModelTier.CLOUD_LITE, ANTHROPIC_HAIKU_MODEL) if use_ant else
                 codex(ModelTier.CLOUD_LITE) if use_codex else None)
        sonnet = (ant(ModelTier.CLOUD_FAST, ANTHROPIC_SONNET_MODEL) if use_ant else
                  codex(ModelTier.CLOUD_FAST) if use_codex else None)
        opus = (ant(ModelTier.CLOUD_STRONG, ANTHROPIC_OPUS_MODEL) if use_ant else
                codex(ModelTier.CLOUD_STRONG) if use_codex else None)

        def best(*choices):
            return next((c for c in choices if c is not None), None)

        def pick(score: int):
            if score <= 25:
                return best(q9, q27, haiku, sonnet, opus)
            if score <= 45:
                return best(q27, sonnet, haiku, q9, opus)
            if score <= 60:
                return best(sonnet, haiku, q27, opus)
            if score <= 75:
                return best(sonnet, haiku, opus)
            return best(opus, sonnet)

        def pick_for_role(category: str, score: int):
            if category == "entry":
                return best(q9, q27, haiku, sonnet, opus)
            if category == "implementation":
                if score <= 55:
                    return best(sonnet, haiku, q27, opus)
                if score <= 80:
                    return best(sonnet, opus, haiku)
                return best(opus, sonnet)
            if category == "review":
                if score <= 50:
                    return best(sonnet, haiku, q27, opus)
                if score <= 80:
                    return best(sonnet, opus, haiku)
                return best(opus, sonnet)
            if category == "validation":
                if score <= 35:
                    return best(haiku, q27, q9, sonnet, opus)
                return best(sonnet, haiku, q27, opus)
            if category == "discovery":
                if score <= 45:
                    return best(sonnet, haiku, q27, opus)
                if score <= 80:
                    return best(sonnet, opus, haiku)
                return best(opus, sonnet)
            return pick(score)

        topo = self._available_topologies().get(topology_id)
        if topo is None:
            logger.warning("Failed to build agent-model map: unknown topology '%s'", topology_id)
            return {}

        ac = agent_complexity or {}
        implementation_scores = [
            ac.get(agent_id, complexity)
            for agent_id, agent in topo.agents.items()
            if agent.category == "implementation"
        ]
        implementation_baseline = max(implementation_scores) if implementation_scores else complexity
        scores: dict[str, int] = {}
        for agent_id, agent in topo.agents.items():
            if agent.category == "entry" or agent_id == topo.entry_agent:
                scores[agent_id] = ac.get(agent_id, 20)
            elif agent.category == "discovery":
                scores[agent_id] = ac.get(agent_id, max(40, complexity - 10))
            elif agent.category == "implementation":
                scores[agent_id] = ac.get(agent_id, complexity)
            elif agent.category == "review":
                scores[agent_id] = max(ac.get(agent_id, complexity), implementation_baseline)
            elif agent.category == "validation":
                scores[agent_id] = ac.get(agent_id, 30)
            else:
                scores[agent_id] = ac.get(agent_id, complexity)

        result: dict[str, ModelConfig] = {}
        reviewer_ids = [aid for aid, agent in topo.agents.items() if agent.category == "review"]
        if reviewer_ids:
            reviewer_cfg = pick_for_role("review", max(scores[rid] for rid in reviewer_ids))
            if reviewer_cfg is None:
                logger.warning("Failed to build reviewer model config, proceeding without model hints")
                return {}
            if len(reviewer_ids) >= 2 and force_provider is None:
                alt_candidates = [c for c in [opus, sonnet, haiku, q27, q9] if c is not None]
                result[reviewer_ids[0]] = reviewer_cfg
                result[reviewer_ids[1]] = next((c for c in alt_candidates if c.provider != reviewer_cfg.provider), reviewer_cfg)
                for reviewer_id in reviewer_ids[2:]:
                    result[reviewer_id] = reviewer_cfg
            else:
                for reviewer_id in reviewer_ids:
                    result[reviewer_id] = reviewer_cfg

        for agent_id, score in scores.items():
            if agent_id in result:
                continue
            category = topo.agents[agent_id].category
            cfg = pick_for_role(category, score)
            if cfg is not None:
                result[agent_id] = cfg

        return result

    def _allocator_model_config(self):
        from orb.llm.types import ModelTier, ModelConfig, ANTHROPIC_MODELS, CODEX_MODELS

        if "openai-codex" in self._providers:
            return CODEX_MODELS.get(ModelTier.CLOUD_FAST) or CODEX_MODELS.get(ModelTier.CLOUD_STRONG)
        if "anthropic" in self._providers:
            return ANTHROPIC_MODELS.get(ModelTier.CLOUD_FAST) or ANTHROPIC_MODELS.get(ModelTier.CLOUD_STRONG)
        return None

    def _available_model_choices(self) -> list[dict]:
        from orb.llm.types import ANTHROPIC_MODEL_DESCRIPTIONS, ANTHROPIC_MODELS

        choices: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for provider_name, configs in (
            ("anthropic", [
                (config.model_id, ANTHROPIC_MODEL_DESCRIPTIONS[config.model_id])
                for config in ANTHROPIC_MODELS.values()
            ]),
            ("openai-codex", [
                ("gpt-5.4", "strong coding and reasoning"),
            ]),
            ("ollama", [
                ("qwen3.5:9b", "local small"),
                ("qwen3.5:27b", "local larger"),
            ]),
        ):
            if provider_name not in self._providers:
                continue
            for model_id, description in configs:
                key = (provider_name, model_id)
                if key in seen:
                    continue
                seen.add(key)
                choices.append({
                    "provider": provider_name,
                    "model": model_id,
                    "description": description,
                })
        return choices

    def _validate_agent_model_assignments(
        self,
        topology_id: str,
        raw_assignments: dict | None,
        heuristic_map: dict,
    ) -> tuple[dict, dict[str, str]]:
        topo = self._available_topologies().get(topology_id)
        if topo is None:
            return heuristic_map, {}

        validated: dict = {}
        reasons: dict[str, str] = {}
        choice_lookup = {(c["provider"], c["model"]) for c in self._available_model_choices()}
        for agent_id, agent in topo.agents.items():
            item = (raw_assignments or {}).get(agent_id)
            provider = item.get("provider") if isinstance(item, dict) else None
            model_id = item.get("model") if isinstance(item, dict) else None
            reason = item.get("reason", "") if isinstance(item, dict) else ""
            if provider and model_id and (provider, model_id) in choice_lookup:
                from orb.llm.types import ModelConfig, ModelTier
                tier = heuristic_map.get(agent_id).tier if agent_id in heuristic_map else (
                    ModelTier.CLOUD_FAST if provider in {"anthropic", "openai-codex"} else ModelTier.LOCAL_LARGE
                )
                validated[agent_id] = ModelConfig(tier=tier, model_id=model_id, provider=provider)
                if reason:
                    reasons[agent_id] = str(reason)
            elif agent_id in heuristic_map:
                validated[agent_id] = heuristic_map[agent_id]
        return validated, reasons

    async def _llm_assign_agent_models(
        self,
        query: str,
        topology_id: str,
        complexity: int,
        agent_complexity: dict | None,
        heuristic_map: dict,
    ) -> tuple[dict, dict[str, str]]:
        from orb.llm.types import CompletionRequest

        allocator_model = self._allocator_model_config()
        if allocator_model is None:
            return heuristic_map, {}

        provider = self._providers.get(allocator_model.provider)
        if provider is None:
            return heuristic_map, {}

        topo = self._available_topologies().get(topology_id)
        if topo is None:
            return heuristic_map, {}

        available_choices = self._available_model_choices()
        heuristic_preview = {
            agent_id: {
                "provider": cfg.provider,
                "model": cfg.model_id,
            }
            for agent_id, cfg in heuristic_map.items()
        }
        prompt = (
            "Assign the best provider/model for each agent in this run.\n"
            "Prefer strong models for implementation/review when justified, but do not overspend.\n"
            "Use only the listed available choices.\n"
            "Return JSON only with this shape:\n"
            '{"assignments":{"agent_id":{"provider":"...","model":"...","reason":"..."}}}\n\n'
            f"Task: {query}\n"
            f"Topology: {topo.label} ({topology_id})\n"
            f"Overall complexity: {complexity}\n"
            f"Agents: {json.dumps({aid: {'role': a.role, 'category': a.category, 'description': a.description} for aid, a in topo.agents.items()})}\n"
            f"Agent complexity: {json.dumps(agent_complexity or {})}\n"
            f"Available model choices: {json.dumps(available_choices)}\n"
            f"Heuristic baseline: {json.dumps(heuristic_preview)}\n"
        )
        req = CompletionRequest(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system=(
                "You are a runtime model allocator. "
                "Assign one provider/model per agent for the full run. "
                "Be cost-aware but prefer stronger models for coder/reviewer/research roles when needed. "
                "Return valid JSON only."
            ),
            model_config=allocator_model,
        )
        try:
            response = await provider.complete(req)
        except Exception as exc:
            logger.warning("Agent model allocation LLM call failed: %s", exc)
            return heuristic_map, {}

        raw = (response.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            parsed = json.loads(raw.strip())
        except JSONDecodeError:
            logger.warning("Failed to parse agent model assignment response: %r", raw)
            return heuristic_map, {}

        validated, reasons = self._validate_agent_model_assignments(
            topology_id,
            parsed.get("assignments") if isinstance(parsed, dict) else None,
            heuristic_map,
        )
        return (validated or heuristic_map), reasons

    async def _llm_predict_topology(self, query: str, model_pin: str = "auto") -> dict:
        from orb.llm.types import CompletionRequest, ModelTier, DEFAULT_MODELS, ANTHROPIC_MODELS, CODEX_MODELS

        def _default_result(complexity: int = 50, reason: str = "No cloud LLM provider available") -> dict:
            available_topologies = self._available_topologies()
            ranked = self._heuristic_topology_ranking(query, complexity)
            topology = ranked[0][0] if ranked else next(iter(available_topologies.keys()))
            topo = available_topologies[topology]
            agent_model_map = self._build_agent_model_map(complexity, model_pin, topology_id=topology)
            agent_models = {
                role: cfg.model_id for role, cfg in agent_model_map.items()
            }
            return {
                "topology": topology,
                "label": topo.label,
                "description": topo.description,
                "complexity": complexity,
                "reason": reason,
                "agent_models": agent_models,
                "options": self._topology_options(topology),
            }

        predict_provider = (
            self._providers.get("anthropic")
            or self._providers.get("openai-codex")
            or self._providers.get("ollama")
        )
        if not predict_provider:
            return _default_result()

        using_codex = "anthropic" not in self._providers and "openai-codex" in self._providers
        using_ollama = "anthropic" not in self._providers and "openai-codex" not in self._providers

        available_topologies = self._available_topologies()
        prompt = (
            f"Analyze this software task and respond with JSON only.\n\n"
            f"Task: {query}\n\n"
            "Available topologies:\n"
            + "\n".join(
                f'- {topology_id}: {topo.label} — {topo.description}'
                for topology_id, topo in available_topologies.items()
            )
            + "\n\nRespond with this exact JSON structure:\n"
            '{"complexity": <0-100 integer>, "reason": "<one sentence why>", '
            '"topology": "<one topology id from the list above>", '
            '"agent_complexity": {"<agent_id>": <0-100>, "...": <0-100>}}\n\n'
            "complexity: overall task difficulty (0=trivial, 100=extremely complex/critical)\n"
            "agent_complexity: per-agent difficulty scores for the selected topology\n"
        )

        if using_codex:
            model_config = CODEX_MODELS.get(ModelTier.CLOUD_LITE) or CODEX_MODELS[ModelTier.CLOUD_FAST]
        elif using_ollama:
            model_config = DEFAULT_MODELS.get(ModelTier.LOCAL_SMALL) or DEFAULT_MODELS[ModelTier.LOCAL_MEDIUM]
        else:
            cloud_overrides = {
                t: cfg for t, cfg in (self._model_overrides or {}).items()
                if getattr(cfg, "provider", None) == "anthropic"
            }
            model_config = (
                cloud_overrides.get(ModelTier.CLOUD_FAST)
                or cloud_overrides.get(ModelTier.CLOUD_LITE)
                or ANTHROPIC_MODELS.get(ModelTier.CLOUD_LITE)
                or ANTHROPIC_MODELS[ModelTier.CLOUD_FAST]
            )

        req = CompletionRequest(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system="You are a task complexity analyzer. Reply with valid JSON only, no other text.",
            model_config=model_config,
        )
        try:
            response = await predict_provider.complete(req)
        except Exception as exc:
            logger.warning("Topology prediction LLM call failed: %s", exc)
            return _default_result()

        raw = (response.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        if len(raw) > 500_000:
            logger.warning("LLM response too large to parse (%d bytes), using default", len(raw))
            return _default_result()
        try:
            parsed = json.loads(raw.strip())
        except JSONDecodeError:
            logger.warning("Failed to parse topology prediction response: %r", raw)
            return _default_result()

        topology = parsed.get("topology")
        if topology not in available_topologies:
            ranked = self._heuristic_topology_ranking(query, int(parsed.get("complexity", 50)))
            topology = ranked[0][0] if ranked else next(iter(available_topologies.keys()))
        topo = available_topologies[topology]
        overall_complexity = int(parsed.get("complexity", 50))
        agent_complexity = {
            k: max(0, min(100, int(v)))
            for k, v in (parsed.get("agent_complexity") or {}).items()
            if k in topo.agents
        }
        heuristic_model_map = self._build_agent_model_map(
            overall_complexity,
            model_pin,
            agent_complexity,
            topology_id=topology,
        )
        agent_model_map, _agent_model_reasons = await self._llm_assign_agent_models(
            query,
            topology,
            overall_complexity,
            agent_complexity,
            heuristic_model_map,
        )
        agent_models = {
            role: cfg.model_id for role, cfg in agent_model_map.items()
        }
        return {
            "topology": topology,
            "label": topo.label,
            "description": topo.description,
            "complexity": overall_complexity,
            "reason": parsed.get("reason", ""),
            "agent_complexity": agent_complexity,
            "agent_models": agent_models,
            "options": self._topology_options(topology),
        }

    async def _run_orchestrator(
        self,
        query: str,
        topology: str,
        initial_target: str = "coordinator",
        model_pin: str = "auto",
        complexity: int = 50,
        agent_complexity: dict | None = None,
        agent_model_map: dict | None = None,
    ) -> None:
        from web.bridge import DashboardBridge
        from web.state import ActivityRecord

        self._turn_count += 1
        bridge = DashboardBridge(self.state, self._broadcast)
        effective_overrides = dict(self._model_overrides or {})
        agent_model_map = agent_model_map or self._build_agent_model_map(
            complexity, model_pin, agent_complexity, topology_id=topology
        )
        topology_label, topology_description, agent_positions = self._topology_meta(topology)
        graph_view = self._topology_graph_view(topology)

        from orb.topologies import create_orchestrator
        orchestrator = create_orchestrator(
            topology,
            providers=self._providers,
            config=self._config,
            model_overrides=effective_overrides or None,
            trace=False,
            tier_override=self._tier_override,
            agent_model_map=agent_model_map or None,
        )

        agent_roles = {aid: a.config.role for aid, a in orchestrator.agents.items()}
        bridge.setup_agents(agent_roles)
        bridge.setup_edges([(e.a, e.b) for e in orchestrator.bus.graph.edges])
        bridge.setup_plan(
            query=query,
            topology_id=topology,
            topology_label=topology_label,
            topology_description=topology_description,
            agent_complexity=agent_complexity,
            agent_models={aid: cfg.model_id for aid, cfg in agent_model_map.items()},
            agent_positions=agent_positions,
            graph_view=graph_view,
        )
        if agent_model_map:
            for aid, cfg in agent_model_map.items():
                if aid in bridge.state.agents:
                    bridge.state.agents[aid].model = cfg.model_id
        for aid, score in (agent_complexity or {}).items():
            if aid in bridge.state.agents:
                bridge.state.agents[aid].complexity = int(score)
        if self._config:
            bridge.setup_budget(self._config.budget)

        await self._broadcast(json.dumps(self.current_init_event() | {"run_active": True}))
        orchestrator.bus.on_event(bridge.on_message_routed)
        orchestrator.bus.on_event(lambda _event, msg: self._run_transcript.add_message(msg))
        orchestrator._transcript = self._run_transcript

        original_on_complete = orchestrator._on_agent_complete

        async def wrapped_on_complete(agent_id, result):
            agent_obj = orchestrator.agents.get(agent_id)
            model = getattr(agent_obj, "_last_model", "") if agent_obj else ""
            if model:
                await bridge.on_agent_status(agent_id, "completed", model)
            await bridge.on_agent_complete(agent_id, result)
            await original_on_complete(agent_id, result)

        orchestrator._on_agent_complete = wrapped_on_complete

        async def on_agent_activity(agent_id: str, activity: str) -> None:
            self.state.activity_events.append(ActivityRecord(
                agent=agent_id,
                activity=activity,
                elapsed=round(time.time() - self.state.start_time, 2),
            ))
            if len(self.state.activity_events) > 100:
                self.state.activity_events = self.state.activity_events[-100:]
            await self._broadcast(json.dumps({"type": "agent_activity", "agent": agent_id, "activity": activity}))

        async def on_agent_heartbeat(agent_id: str, payload: dict) -> None:
            await bridge.on_agent_heartbeat(agent_id, payload)

        for agent in orchestrator.agents.values():
            agent._on_activity = on_agent_activity
            agent._on_heartbeat = on_agent_heartbeat
            agent._shared_transcript = self._run_transcript

        def _make_file_write_cb(aid: str):
            def cb(_, path: str, content: str, old_content: str = "") -> None:
                asyncio.ensure_future(self._broadcast(json.dumps({
                    "type": "file_write",
                    "agent": aid,
                    "path": path,
                    "content": content,
                    "old_content": old_content,
                })))
            return cb

        for aid, agent in orchestrator.agents.items():
            agent._on_file_write = _make_file_write_cb(aid)

        # Wire GraphRAG subgraph stores if the topology defines clusters
        from orb.topologies import get_loader
        from orb.memory.graphrag_config import GraphRAGConfig
        topo_schema = get_loader().get(topology)
        if topo_schema and topo_schema.clusters:
            graphrag_cfg = GraphRAGConfig.from_topology(topo_schema)
            for aid, agent in orchestrator.agents.items():
                cluster_name = graphrag_cfg.agent_cluster_map.get(aid)
                if cluster_name:
                    agent.set_subgraph_store(graphrag_cfg.cluster_stores[cluster_name])

        if self._conversation_session.agent_carryover:
            for aid, agent in orchestrator.agents.items():
                if aid not in self._conversation_session.agent_carryover:
                    continue
                msgs = self._sanitize_carryover(self._conversation_session.agent_carryover[aid])
                if msgs:
                    agent._conversation.messages = msgs

        self._agents = orchestrator.agents

        try:
            run_target = initial_target if initial_target in orchestrator.agents else orchestrator.config.entry_agent
            result = await orchestrator.run(query, entry_agent=run_target)
        except Exception:
            logger.exception("Orchestrator run failed")
            result = None
        else:
            self.state.completed = True

        new_carryover: dict[str, list] = {}
        for aid, agent in orchestrator.agents.items():
            msgs = list(agent._conversation.messages)
            if len(msgs) >= COMPACT_THRESHOLD:
                msgs = await self._compactor.compact_messages(msgs, self._providers)
            new_carryover[aid] = msgs
        self._conversation_session.agent_carryover = new_carryover

        if result:
            _, summary = self._pick_primary_result(result.completions)
            if not summary:
                summary = next(iter(result.completions.values()), "")
        else:
            summary = ""
        await self._compact_conversation_session_if_needed()
        self._persist_session()
        self._sync_session_state()

        elapsed = time.time() - self.state.start_time
        await self._broadcast(json.dumps({
            "type": "stats",
            "message_count": self.state.message_count,
            "budget_remaining": self.state.budget_remaining,
            "elapsed": round(elapsed, 2),
        }))

        if result:
            for agent_id in orchestrator.agents:
                if agent_id not in result.completions:
                    await bridge.on_agent_complete(agent_id, "[shutdown]")

        if result:
            final_agent_id, final_result = self._pick_primary_result(result.completions)
            from orb.cli.diff_capture import capture_diff
            diff = capture_diff()
            self.state.final_agent = final_agent_id or ""
            self.state.final_result = final_result or ""
            self.state.final_diff = diff or ""
            self.state.session_turn = self._conversation_session.user_turn_count()
            if final_result:
                await self._broadcast(json.dumps({
                    "type": "run_complete",
                    "result": final_result,
                    "agent": final_agent_id,
                    "diff": diff,
                    "elapsed": round(elapsed, 2),
                    "session_turn": self._conversation_session.user_turn_count(),
                    "session_id": self._conversation_session.session_id,
                    "session_generation": self._conversation_session.generation,
                    "routed": self.state.message_count,
                }))

        self._last_result = result
