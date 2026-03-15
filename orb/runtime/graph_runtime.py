from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from json import JSONDecodeError

from web.state import DashboardState
from orb.messaging.channel import ChannelClosed
from .transcript import RunTranscript

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[str], Awaitable[None]]


class GraphRuntime:
    """Owns orchestration and exposes a subscriber-oriented runtime interface."""

    def __init__(self, state: DashboardState | None = None) -> None:
        self.state = state or DashboardState()
        self._subscribers: set[BroadcastFn] = set()
        self._agents: dict = {}
        self._run_task: asyncio.Task | None = None
        self._providers: dict = {}
        self._config = None
        self._model_overrides = None
        self._tier_override = None
        self._session_history: list[dict] = []
        self._conv_carryover: dict[str, list] = {}
        self._turn_count: int = 0
        self._last_result = None
        self._run_transcript = RunTranscript()

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
        self._providers = providers
        self._config = config
        self._model_overrides = model_overrides
        self._tier_override = tier_override

    def current_init_event(self) -> dict:
        event = self.state.to_init_event()
        event["run_active"] = self.running
        return event

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

        agent = self._agents.get(target_id)
        if agent is None:
            return 404, {"ok": False, "error": f"Unknown agent: {target_id}"}

        msg = Message(
            from_="user",
            to=target_id,
            type=MessageType.RESPONSE,
            payload=text,
        )
        try:
            await agent.channel.send(msg)
        except ChannelClosed as exc:
            logger.exception("Failed to inject message")
            return 500, {"ok": False, "error": str(exc)}
        self._run_transcript.add_message(msg)

        await self._broadcast(json.dumps({
            "type": "message",
            "from": "user",
            "to": target_id,
            "content": text,
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

        predicted = await self.predict_topology(query, model_pin=model_pin)
        selected_topology = normalize_topology_id(topology) if topology != "auto" else predicted.get("topology", "triad")
        topology_label, topology_description, agent_positions = self._topology_meta(selected_topology)
        graph_view = self._topology_graph_view(selected_topology)
        agent_complexity = dict(predicted.get("agent_complexity") or {})
        overall_complexity = int(predicted.get("complexity", 50))
        agent_model_map = self._build_agent_model_map(
            overall_complexity,
            model_pin=model_pin,
            agent_complexity=agent_complexity,
            topology_id=selected_topology,
        )
        agent_models = {role: cfg.model_id for role, cfg in agent_model_map.items()}

        self.state.reset()
        self._last_result = None
        self._run_transcript = RunTranscript()
        self.state.run_query = query
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
                model_pin=model_pin,
                complexity=overall_complexity,
                agent_complexity=agent_complexity,
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
        models = [{"id": "auto", "label": "Auto-select", "provider": "auto", "local": False}]
        if "anthropic" in self._providers:
            models += [
                {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "anthropic", "local": False},
                {"id": "claude-sonnet-4-5-20251001", "label": "Claude Sonnet 4.5", "provider": "anthropic", "local": False},
                {"id": "claude-opus-4-20250514", "label": "Claude Opus 4", "provider": "anthropic", "local": False},
            ]
        if "openai-codex" in self._providers:
            models += [{"id": "gpt-5.4", "label": "GPT-5.4 (Codex)", "provider": "openai-codex", "local": False}]
        elif "openai" in self._providers:
            models += [
                {"id": "gpt-4o-mini", "label": "GPT-4o mini", "provider": "openai", "local": False},
                {"id": "gpt-4o", "label": "GPT-4o", "provider": "openai", "local": False},
                {"id": "o3", "label": "o3", "provider": "openai", "local": False},
            ]
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
        from orb.llm.types import ModelTier, ModelConfig

        has_ollama = "ollama" in self._providers
        has_anthropic = "anthropic" in self._providers
        has_openai = "openai" in self._providers
        has_codex = "openai-codex" in self._providers

        def ollama(model_id: str) -> ModelConfig:
            return ModelConfig(tier=ModelTier.LOCAL_LARGE, model_id=model_id, provider="ollama")

        def ant(tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider="anthropic")

        def oai(tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider="openai")

        def codex(tier: ModelTier) -> ModelConfig:
            return ModelConfig(tier=tier, model_id="gpt-5.4", provider="openai-codex")

        force_provider: str | None = None
        if model_pin and model_pin != "auto":
            if "claude" in model_pin:
                force_provider = "anthropic"
            elif model_pin == "gpt-5.4":
                force_provider = "openai-codex"
            elif "gpt" in model_pin or model_pin in ("o1", "o3", "o3-mini", "o4-mini"):
                force_provider = "openai"
            elif "qwen" in model_pin or "llama" in model_pin:
                force_provider = "ollama"

        provider_available = {
            "anthropic": has_anthropic,
            "openai": has_openai,
            "openai-codex": has_codex,
            "ollama": has_ollama,
        }
        if force_provider and not provider_available.get(force_provider):
            logger.warning("Forced provider '%s' not available; falling back to auto", force_provider)
            force_provider = None

        q9 = ollama("qwen3.5:9b") if has_ollama and force_provider in (None, "ollama") else None
        q27 = ollama("qwen3.5:27b") if has_ollama and force_provider in (None, "ollama") else None
        use_ant = has_anthropic and force_provider in (None, "anthropic")
        use_oai = has_openai and force_provider in (None, "openai")
        use_codex = has_codex and force_provider in (None, "openai-codex")

        haiku = (ant(ModelTier.CLOUD_LITE, "claude-haiku-4-5-20251001") if use_ant else
                 codex(ModelTier.CLOUD_LITE) if use_codex else
                 oai(ModelTier.CLOUD_LITE, "gpt-4o-mini") if use_oai else None)
        sonnet = (ant(ModelTier.CLOUD_FAST, "claude-sonnet-4-5-20251001") if use_ant else
                  codex(ModelTier.CLOUD_FAST) if use_codex else
                  oai(ModelTier.CLOUD_FAST, "gpt-4o") if use_oai else None)
        opus = (ant(ModelTier.CLOUD_STRONG, "claude-opus-4-20250514") if use_ant else
                codex(ModelTier.CLOUD_STRONG) if use_codex else
                oai(ModelTier.CLOUD_STRONG, "o3") if use_oai else None)

        def best(*choices):
            return next((c for c in choices if c is not None), None)

        def pick(score: int):
            if score <= 25:
                return best(q9, q27, haiku, sonnet, opus)
            if score <= 45:
                return best(q27, haiku, q9, sonnet, opus)
            if score <= 60:
                return best(haiku, q27, sonnet, opus)
            if score <= 75:
                return best(sonnet, haiku, opus)
            return best(opus, sonnet)

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
            reviewer_cfg = pick(max(scores[rid] for rid in reviewer_ids))
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
            cfg = pick(score)
            if cfg is not None:
                result[agent_id] = cfg

        return result

    async def _llm_predict_topology(self, query: str, model_pin: str = "auto") -> dict:
        from orb.llm.types import CompletionRequest, ModelTier, DEFAULT_MODELS, OPENAI_MODELS, CODEX_MODELS

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
            or self._providers.get("openai")
            or self._providers.get("openai-codex")
            or self._providers.get("ollama")
        )
        if not predict_provider:
            return _default_result()

        using_openai = "anthropic" not in self._providers and "openai" in self._providers
        using_codex = "anthropic" not in self._providers and "openai" not in self._providers and "openai-codex" in self._providers
        using_ollama = "anthropic" not in self._providers and "openai" not in self._providers and "openai-codex" not in self._providers

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

        if using_openai:
            model_config = OPENAI_MODELS.get(ModelTier.CLOUD_LITE) or OPENAI_MODELS[ModelTier.CLOUD_FAST]
        elif using_codex:
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
                or DEFAULT_MODELS.get(ModelTier.CLOUD_LITE)
                or DEFAULT_MODELS[ModelTier.CLOUD_FAST]
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
        agent_model_map = self._build_agent_model_map(
            overall_complexity,
            model_pin,
            agent_complexity,
            topology_id=topology,
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
        model_pin: str = "auto",
        complexity: int = 50,
        agent_complexity: dict | None = None,
    ) -> None:
        from orb.agent.compaction import COMPACT_THRESHOLD, compact_history
        from web.bridge import DashboardBridge

        self._turn_count += 1
        bridge = DashboardBridge(self.state, self._broadcast)
        effective_overrides = dict(self._model_overrides or {})
        agent_model_map = self._build_agent_model_map(complexity, model_pin, agent_complexity, topology_id=topology)
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

        if self._session_history:
            lines = ["=== Prior session context ==="]
            for i, h in enumerate(self._session_history[-5:], 1):
                lines.append(f"[{i}] User: {h['query']}")
                if h["result"]:
                    lines.append(f"     Result: {h['result'][:200]}")
            lines.append("=== End of prior context ===\n")
            query = "\n".join(lines) + query

        if self._conv_carryover:
            for aid, agent in orchestrator.agents.items():
                if aid not in self._conv_carryover or not self._conv_carryover[aid]:
                    continue
                msgs = list(self._conv_carryover[aid])
                while msgs:
                    last = msgs[-1]
                    role = last.get("role")
                    content = last.get("content", "")
                    if role == "user":
                        msgs.pop()
                        continue
                    if role == "assistant" and isinstance(content, list) and any(
                        b.get("type") == "tool_use" for b in content
                    ):
                        msgs.pop()
                        continue
                    break
                if msgs:
                    agent._conversation.messages = msgs

        self._agents = orchestrator.agents

        try:
            result = await orchestrator.run(query)
        except Exception:
            logger.exception("Orchestrator run failed")
            result = None
        else:
            self.state.completed = True

        new_carryover: dict[str, list] = {}
        for aid, agent in orchestrator.agents.items():
            msgs = list(agent._conversation.messages)
            if len(msgs) >= COMPACT_THRESHOLD:
                msgs = await compact_history(msgs, self._providers)
            new_carryover[aid] = msgs
        self._conv_carryover = new_carryover

        synthesis_id = orchestrator.config.synthesis_agent
        if result:
            _, summary = self._pick_primary_result(result.completions)
            if not summary:
                summary = next(iter(result.completions.values()), "")
        else:
            summary = ""
        self._session_history.append({"query": query.split("=== End of prior context ===\n")[-1], "result": summary[:300]})

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
            if final_result:
                await self._broadcast(json.dumps({
                    "type": "run_complete",
                    "result": final_result,
                    "agent": final_agent_id,
                    "diff": diff,
                    "elapsed": round(elapsed, 2),
                    "session_turn": len(self._session_history),
                    "routed": self.state.message_count,
                }))

        self._last_result = result
