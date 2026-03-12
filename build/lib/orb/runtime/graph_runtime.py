from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from web.state import DashboardState

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
            except (asyncio.CancelledError, Exception):
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
        except Exception as exc:
            logger.exception("Failed to inject message")
            return 500, {"ok": False, "error": str(exc)}

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
        complexity: int = 50,
        agent_complexity: dict | None = None,
    ) -> tuple[int, dict]:
        if not self._providers:
            return 500, {"ok": False, "error": "Server has no providers configured"}
        if self.running:
            return 200, {"ok": False, "error": "Run already in progress"}

        if topology == "auto":
            predicted = await self.predict_topology(query, model_pin=model_pin)
            topology = predicted.get("topology", "triangle")

        self.state.reset()
        self._last_result = None
        self._run_task = asyncio.create_task(
            self._run_orchestrator(
                query,
                topology,
                model_pin=model_pin,
                complexity=complexity,
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
            return {"topology": "triangle", "label": "Triad", "description": "Coder → Reviewer → Tester"}
        return await self._llm_predict_topology(query, model_pin=model_pin)

    def _build_agent_model_map(
        self,
        complexity: int,
        model_pin: str = "auto",
        agent_complexity: dict | None = None,
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

        ac = agent_complexity or {}
        coordinator_score = ac.get("coordinator", 20)
        coder_score = ac.get("coder", complexity)
        tester_score = ac.get("tester", 30)
        reviewer_score = max(ac.get("reviewer", complexity), coder_score)

        coordinator_cfg = pick(coordinator_score)
        coder_cfg = pick(coder_score)
        reviewer_cfg = pick(reviewer_score)
        tester_cfg = pick(tester_score)

        if not tester_cfg or not coder_cfg or not reviewer_cfg:
            logger.warning("Failed to build agent-model map: missing config, proceeding without model hints")
            return {}

        if force_provider is None:
            alt_candidates = [c for c in [opus, sonnet, haiku, q27, q9] if c is not None]
            reviewer_a_cfg = reviewer_cfg
            reviewer_b_cfg = next((c for c in alt_candidates if c.provider != reviewer_a_cfg.provider), reviewer_cfg)
        else:
            reviewer_a_cfg = reviewer_cfg
            reviewer_b_cfg = reviewer_cfg

        return {
            "coordinator": coordinator_cfg,
            "coder": coder_cfg,
            "reviewer": reviewer_cfg,
            "reviewer_a": reviewer_a_cfg,
            "reviewer_b": reviewer_b_cfg,
            "tester": tester_cfg,
        }

    async def _llm_predict_topology(self, query: str, model_pin: str = "auto") -> dict:
        from orb.llm.types import CompletionRequest, ModelTier, DEFAULT_MODELS, OPENAI_MODELS, CODEX_MODELS

        def _default_result(complexity: int = 50, reason: str = "No cloud LLM provider available") -> dict:
            topology = "triangle" if complexity < 65 else "dual-review"
            labels = {
                "triangle": ("Triad", "Coder → Reviewer → Tester"),
                "dual-review": ("Dual Review", "2× Opus reviewers reach consensus"),
            }
            chosen_label, chosen_desc = labels[topology]
            other = "dual-review" if topology == "triangle" else "triangle"
            other_label, other_desc = labels[other]
            agent_model_map = self._build_agent_model_map(complexity, model_pin)
            agent_models = {
                role: cfg.model_id for role, cfg in agent_model_map.items()
                if role in ("coordinator", "coder", "reviewer", "tester")
            }
            return {
                "topology": topology,
                "label": chosen_label,
                "description": chosen_desc,
                "complexity": complexity,
                "reason": reason,
                "agent_models": agent_models,
                "options": [
                    {"topology": topology, "label": chosen_label, "description": chosen_desc, "chosen": True},
                    {"topology": other, "label": other_label, "description": other_desc, "chosen": False},
                ],
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

        prompt = (
            f"Analyze this software task and respond with JSON only.\n\n"
            f"Task: {query}\n\n"
            "Respond with this exact JSON structure:\n"
            '{"complexity": <0-100 integer>, "reason": "<one sentence why>", '
            '"topology": "<triangle or dual-review>", '
            '"agent_complexity": {"coordinator": <0-100>, "coder": <0-100>, '
            '"reviewer": <0-100>, "tester": <0-100>}}\n\n'
            "complexity: overall task difficulty (0=trivial, 100=extremely complex/critical)\n"
            "agent_complexity: per-role difficulty scores\n"
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
        except Exception:
            logger.warning("Failed to parse topology prediction response: %r", raw)
            return _default_result()

        topology = parsed.get("topology", "triangle")
        topology = "dual-review" if "dual" in topology or "review" in topology else "triangle"
        labels = {
            "triangle": ("Triad", "Coder → Reviewer → Tester"),
            "dual-review": ("Dual Review", "2× Opus reviewers reach consensus"),
        }
        chosen_label, chosen_desc = labels[topology]
        other = "dual-review" if topology == "triangle" else "triangle"
        other_label, other_desc = labels[other]
        overall_complexity = int(parsed.get("complexity", 50))
        agent_complexity = {
            k: max(0, min(100, int(v)))
            for k, v in (parsed.get("agent_complexity") or {}).items()
            if k in ("coordinator", "coder", "reviewer", "tester")
        }
        agent_model_map = self._build_agent_model_map(overall_complexity, model_pin, agent_complexity)
        agent_models = {
            role: cfg.model_id for role, cfg in agent_model_map.items()
            if role in ("coordinator", "coder", "reviewer", "tester")
        }
        return {
            "topology": topology,
            "label": chosen_label,
            "description": chosen_desc,
            "complexity": overall_complexity,
            "reason": parsed.get("reason", ""),
            "agent_complexity": agent_complexity,
            "agent_models": agent_models,
            "options": [
                {"topology": topology, "label": chosen_label, "description": chosen_desc, "chosen": True},
                {"topology": other, "label": other_label, "description": other_desc, "chosen": False},
            ],
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
        agent_model_map = self._build_agent_model_map(complexity, model_pin, agent_complexity)

        if topology == "dual-review":
            from orb.topologies.dual_review import create_dual_review
            orchestrator = create_dual_review(
                providers=self._providers,
                config=self._config,
                model_overrides=effective_overrides or None,
                trace=False,
                tier_override=self._tier_override,
                agent_model_map=agent_model_map or None,
            )
        else:
            from orb.topologies.triad import create_triad
            orchestrator = create_triad(
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
        if agent_model_map:
            for aid, cfg in agent_model_map.items():
                if aid in bridge.state.agents:
                    bridge.state.agents[aid].model = cfg.model_id
        if self._config:
            bridge.setup_budget(self._config.budget)

        await self._broadcast(json.dumps(self.current_init_event() | {"run_active": True}))
        orchestrator.bus.on_event(bridge.on_message_routed)

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
            try:
                from orb.cli.diff_capture import capture_diff
                diff = capture_diff()
            except Exception:
                diff = ""
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
