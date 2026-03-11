from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from aiohttp import web

from .state import DashboardState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# Cache-buster: changes every server restart so browsers always fetch fresh JS/CSS
_BUILD_TS = str(int(time.time()))


class DashboardServer:
    """aiohttp-based WebSocket server for the live dashboard."""

    def __init__(self, state: DashboardState, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._app = web.Application()
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._agents: dict = {}  # live agent objects keyed by agent_id

        # Fields for UI-initiated runs
        self._run_task: asyncio.Task | None = None
        self._providers: dict = {}
        self._config = None
        self._model_overrides = None
        self._tier_override = None

        # Conversational continuity across runs
        self._session_history: list[dict] = []       # [{query, result}] per completed run
        self._conv_carryover: dict[str, list] = {}   # agent_id → conversation messages
        self._turn_count: int = 0                     # increments each run

        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        self._app.router.add_post("/api/inject", self._inject_handler)
        self._app.router.add_post("/api/start", self._start_handler)
        self._app.router.add_post("/api/stop", self._stop_run_handler)
        self._app.router.add_get("/api/run-status", self._run_status_handler)
        self._app.router.add_get("/api/predict-topology", self._predict_topology_handler)
        self._app.router.add_get("/api/models", self._models_handler)
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR)

    def set_agents(self, agents: dict) -> None:
        """Store a reference to live agent objects for direct message injection."""
        self._agents = agents

    def set_providers(self, providers: dict, config, model_overrides, tier_override) -> None:
        """Store provider/config info so the UI can start runs via /api/start."""
        self._providers = providers
        self._config = config
        self._model_overrides = model_overrides
        self._tier_override = tier_override

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Dashboard server running at http://localhost:{self.port}")

    async def stop(self) -> None:
        # Cancel any running orchestrator task
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
        # Close all WebSocket connections
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, data: str) -> None:
        """Send data to all connected WebSocket clients."""
        closed = []
        for ws in self._clients:
            try:
                await ws.send_str(data)
            except (ConnectionResetError, Exception):
                closed.append(ws)
        for ws in closed:
            self._clients.discard(ws)

    async def _index_handler(self, request: web.Request) -> web.Response:
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace("/static/style.css", f"/static/style.css?v={_BUILD_TS}")
        html = html.replace("/static/graph.js",  f"/static/graph.js?v={_BUILD_TS}")
        html = html.replace("/static/app.js",    f"/static/app.js?v={_BUILD_TS}")
        return web.Response(text=html, content_type="text/html")

    async def _state_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.to_init_event())

    async def _inject_handler(self, request: web.Request) -> web.Response:
        """POST /api/inject — send a message directly to an agent's channel."""
        from orb.messaging.message import Message, MessageType

        if request.content_length and request.content_length > 1_048_576:  # 1 MB limit
            return web.json_response({"ok": False, "error": "Request too large"}, status=413)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        target_id = body.get("to", "").strip()
        text = body.get("message", "").strip()

        if not target_id:
            return web.json_response({"ok": False, "error": "Missing 'to' field"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "Missing 'message' field"}, status=400)

        if self._run_task is None or self._run_task.done():
            return web.json_response({"ok": False, "error": "No run in progress"}, status=400)

        agent = self._agents.get(target_id)
        if agent is None:
            return web.json_response(
                {"ok": False, "error": f"Unknown agent: {target_id}"}, status=404
            )

        msg = Message(
            from_="user",
            to=target_id,
            type=MessageType.RESPONSE,  # Arrives as a reply, not a new task
            payload=text,
        )

        try:
            await agent.channel.send(msg)
        except Exception as exc:
            logger.exception("Failed to inject message")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

        # Show the injected message in the dashboard feed immediately
        await self.broadcast(json.dumps({
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

        return web.json_response({"ok": True})

    async def _start_handler(self, request: web.Request) -> web.Response:
        """POST /api/start — start an orchestrator run from the browser UI."""
        if request.content_length and request.content_length > 1_048_576:  # 1 MB limit
            return web.json_response({"ok": False, "error": "Request too large"}, status=413)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        query = (body.get("query") or "").strip()
        topology = (body.get("topology") or "triangle").strip()
        model_pin = (body.get("model") or "auto").strip()
        complexity = int(body.get("complexity", 50))
        agent_complexity = body.get("agent_complexity") or {}

        if not query:
            return web.json_response({"ok": False, "error": "Query must not be empty"}, status=400)

        if topology not in ("triangle", "dual-review"):
            return web.json_response(
                {"ok": False, "error": "topology must be 'triangle' or 'dual-review'"}, status=400
            )

        if not self._providers:
            return web.json_response(
                {"ok": False, "error": "Server has no providers configured"}, status=500
            )

        # Reject if a run is already in progress
        if self._run_task is not None and not self._run_task.done():
            return web.json_response({"ok": False, "error": "Run already in progress"})

        # Reset state for a fresh run
        self.state.reset()

        # Start orchestrator as a background task
        self._run_task = asyncio.create_task(
            self._run_orchestrator(query, topology, model_pin=model_pin, complexity=complexity,
                                   agent_complexity=agent_complexity)
        )
        self._run_task.add_done_callback(
            lambda t: logger.error("Run task failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )

        return web.json_response({"ok": True})

    async def _stop_run_handler(self, request: web.Request) -> web.Response:
        """POST /api/stop — cancel the currently running orchestrator task."""
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            await self.broadcast(json.dumps({"type": "stopped"}))
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "No run in progress"})

    async def _predict_topology_handler(self, request: web.Request) -> web.Response:
        """GET /api/predict-topology?q=...&model=... — predict topology using LLM if available."""
        q     = request.rel_url.query.get("q",     "").strip()
        model = request.rel_url.query.get("model", "auto").strip()
        if not q:
            return web.json_response({"topology": "triangle", "label": "Triad",
                                      "description": "Coder → Reviewer → Tester"})

        result = await self._llm_predict_topology(q, model_pin=model)
        return web.json_response(result)

    def _build_agent_model_map(
        self,
        complexity: int,
        model_pin: str = "auto",
        agent_complexity: dict | None = None,
    ) -> dict:
        """Map per-agent complexity scores to ModelConfig. Returns {agent_id: ModelConfig}.

        When agent_complexity is provided each agent is independently mapped to a tier
        based on its own score. Falls back to overall complexity for missing keys.
        """
        from orb.llm.types import ModelTier, ModelConfig

        has_ollama    = "ollama"        in self._providers
        has_anthropic = "anthropic"     in self._providers
        has_openai    = "openai"        in self._providers
        has_codex     = "openai-codex"  in self._providers

        def ollama(model_id: str) -> ModelConfig:
            return ModelConfig(tier=ModelTier.LOCAL_LARGE, model_id=model_id, provider="ollama")

        def ant(tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider="anthropic")

        def oai(tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider="openai")

        def codex(tier: ModelTier) -> ModelConfig:
            return ModelConfig(tier=tier, model_id="gpt-5.4", provider="openai-codex")

        # Detect which provider the user wants from the model_pin
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

        # If the forced provider isn't actually available, fall back to auto mode
        provider_available = {
            "anthropic": has_anthropic, "openai": has_openai,
            "openai-codex": has_codex, "ollama": has_ollama,
        }
        if force_provider and not provider_available.get(force_provider):
            logger.warning(f"Forced provider '{force_provider}' not available; falling back to auto")
            force_provider = None

        # Build available models per tier.
        q9  = ollama("qwen3.5:9b")  if has_ollama and force_provider in (None, "ollama") else None
        q27 = ollama("qwen3.5:27b") if has_ollama and force_provider in (None, "ollama") else None

        use_ant   = has_anthropic and force_provider in (None, "anthropic")
        use_oai   = has_openai    and force_provider in (None, "openai")
        use_codex = has_codex     and force_provider in (None, "openai-codex")

        # Priority: anthropic > openai-codex > openai (api key) for cloud tiers
        haiku  = (ant(ModelTier.CLOUD_LITE,   "claude-haiku-4-5-20251001")  if use_ant   else
                  codex(ModelTier.CLOUD_LITE)                                if use_codex else
                  oai(ModelTier.CLOUD_LITE,   "gpt-4o-mini")                if use_oai   else None)
        sonnet = (ant(ModelTier.CLOUD_FAST,   "claude-sonnet-4-5-20251001") if use_ant   else
                  codex(ModelTier.CLOUD_FAST)                                if use_codex else
                  oai(ModelTier.CLOUD_FAST,   "gpt-4o")                     if use_oai   else None)
        opus   = (ant(ModelTier.CLOUD_STRONG, "claude-opus-4-20250514")     if use_ant   else
                  codex(ModelTier.CLOUD_STRONG)                              if use_codex else
                  oai(ModelTier.CLOUD_STRONG, "o3")                         if use_oai   else None)

        def best(*choices):
            return next((c for c in choices if c is not None), None)

        def pick(c: int) -> ModelConfig | None:
            """Map a 0-100 per-agent complexity score to the best available ModelConfig.

            Score bands (intentionally graduated so reviewers with high scores
            get cloud-strong while testers/coordinators with low scores stay local):
              ≤ 25  tiny routing tasks           → q9  → q27 → haiku
              ≤ 45  straightforward work         → q27 → haiku → sonnet
              ≤ 60  moderate complexity          → haiku → q27 → sonnet
              ≤ 75  complex implementation       → sonnet → haiku → opus
              > 75  deep expertise / critique    → opus → sonnet
            """
            if c <= 25:
                return best(q9, q27, haiku, sonnet, opus)
            elif c <= 45:
                return best(q27, haiku, q9, sonnet, opus)
            elif c <= 60:
                return best(haiku, q27, sonnet, opus)
            elif c <= 75:
                return best(sonnet, haiku, opus)
            else:
                return best(opus, sonnet)

        ac = agent_complexity or {}
        coordinator_score = ac.get("coordinator", 20)
        coder_score       = ac.get("coder",       complexity)
        tester_score      = ac.get("tester",      30)
        # Reviewer must be at least as capable as the coder — it needs to catch
        # every mistake the coder makes, so a weaker model would be a blind spot.
        reviewer_score    = max(ac.get("reviewer", complexity), coder_score)

        coordinator_cfg = pick(coordinator_score)
        coder_cfg       = pick(coder_score)
        reviewer_cfg    = pick(reviewer_score)
        tester_cfg      = pick(tester_score)

        if not tester_cfg or not coder_cfg or not reviewer_cfg:
            logger.warning("Failed to build agent-model map: missing config, proceeding without model hints")
            return {}

        # For dual-review: assign reviewer_a and reviewer_b to different providers when auto mode.
        if force_provider is None:
            alt_candidates = [c for c in [opus, sonnet, haiku, q27, q9] if c is not None]
            reviewer_a_cfg = reviewer_cfg
            reviewer_b_cfg = next(
                (c for c in alt_candidates if c.provider != reviewer_a_cfg.provider),
                reviewer_cfg,
            )
        else:
            reviewer_a_cfg = reviewer_cfg
            reviewer_b_cfg = reviewer_cfg

        return {
            "coordinator": coordinator_cfg,
            "coder":       coder_cfg,
            "reviewer":    reviewer_cfg,
            "reviewer_a":  reviewer_a_cfg,
            "reviewer_b":  reviewer_b_cfg,
            "tester":      tester_cfg,
        }

    async def _llm_predict_topology(self, query: str, model_pin: str = "auto") -> dict:
        """Use a fast cloud model to classify the query. Returns full prediction dict."""
        from orb.llm.types import CompletionRequest, ModelTier, DEFAULT_MODELS, ModelConfig, OPENAI_MODELS, CODEX_MODELS
        import json as _json

        def _default_result(complexity: int = 50, reason: str = "No cloud LLM provider available") -> dict:
            topology = "triangle" if complexity < 65 else "dual-review"
            labels = {
                "triangle":    ("Triad",       "Coder → Reviewer → Tester"),
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
                "topology": topology, "label": chosen_label, "description": chosen_desc,
                "complexity": complexity, "reason": reason,
                "agent_models": agent_models,
                "options": [
                    {"topology": topology, "label": chosen_label, "description": chosen_desc, "chosen": True},
                    {"topology": other,    "label": other_label,  "description": other_desc,  "chosen": False},
                ],
            }

        # Pick provider: prefer Anthropic, then OpenAI API key, then Codex, then Ollama
        predict_provider = (
            self._providers.get("anthropic")
            or self._providers.get("openai")
            or self._providers.get("openai-codex")
            or self._providers.get("ollama")
        )
        if not predict_provider:
            return _default_result()

        using_openai  = "anthropic" not in self._providers and "openai" in self._providers
        using_codex   = "anthropic" not in self._providers and "openai" not in self._providers and "openai-codex" in self._providers
        using_ollama  = "anthropic" not in self._providers and "openai" not in self._providers and "openai-codex" not in self._providers

        prompt = (
            f"Analyze this software task and respond with JSON only.\n\n"
            f"Task: {query}\n\n"
            "Respond with this exact JSON structure:\n"
            '{"complexity": <0-100 integer>, "reason": "<one sentence why>", '
            '"topology": "<triangle or dual-review>", '
            '"agent_complexity": {"coordinator": <0-100>, "coder": <0-100>, '
            '"reviewer": <0-100>, "tester": <0-100>}}\n\n'
            "complexity: overall task difficulty (0=trivial, 100=extremely complex/critical)\n"
            "agent_complexity: per-role difficulty scores — how hard is each agent's specific job:\n"
            "  coordinator: routing/synthesis overhead (usually 10-30)\n"
            "  coder: implementation difficulty (matches overall complexity)\n"
            "  reviewer: depth of review needed — set higher than coder for tricky edge cases/security\n"
            "  tester: test coverage complexity — set lower for simple unit tests, higher for integration\n"
            "topology:\n"
            "  triangle: simple features, small bug fixes, straightforward scripts (complexity < 65)\n"
            "  dual-review: complex algorithms, ML/AI models, system design, architecture, security, "
            "performance-critical code, anything requiring deep expertise or consensus (complexity >= 65)"
        )

        # Pick a fast model from whichever cloud provider is active
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
            logger.warning(f"Topology prediction LLM call failed: {exc}")
            return _default_result()
        raw = (response.content or "").strip()
        logger.debug(f"Topology prediction raw response: {raw!r}")
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        if len(raw) > 500_000:  # 500KB limit for topology prediction responses
            logger.warning(f"LLM response too large to parse ({len(raw)} bytes), using default")
            return _default_result()
        try:
            parsed = _json.loads(raw.strip())
        except Exception:
            logger.warning(f"Failed to parse topology prediction response: {raw!r}")
            return _default_result()
        topology = parsed.get("topology", "triangle")
        if "dual" in topology or "review" in topology:
            topology = "dual-review"
        else:
            topology = "triangle"

        labels = {
            "triangle":    ("Triad",    "Coder → Reviewer → Tester"),
            "dual-review": ("Dual Review", "2× Opus reviewers reach consensus"),
        }
        chosen_label, chosen_desc = labels[topology]
        other = "dual-review" if topology == "triangle" else "triangle"
        other_label, other_desc = labels[other]

        overall_complexity = int(parsed.get("complexity", 50))
        agent_complexity = parsed.get("agent_complexity") or {}
        # Validate and clamp agent complexity scores
        agent_complexity = {
            k: max(0, min(100, int(v)))
            for k, v in agent_complexity.items()
            if k in ("coordinator", "coder", "reviewer", "tester")
        }

        # Build human-readable model labels for the prediction card
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
                {"topology": other,    "label": other_label,  "description": other_desc,  "chosen": False},
            ],
        }

    async def _models_handler(self, request: web.Request) -> web.Response:
        """GET /api/models — return available model options."""
        models = [{"id": "auto", "label": "Auto", "provider": "auto"}]
        if "anthropic" in self._providers:
            models += [
                {"id": "claude-haiku-4-5-20251001", "label": "Haiku",  "provider": "anthropic"},
                {"id": "claude-sonnet-4-5-20251001",  "label": "Sonnet", "provider": "anthropic"},
                {"id": "claude-opus-4-20250514",    "label": "Opus",   "provider": "anthropic"},
            ]
        if "openai-codex" in self._providers:
            models += [
                {"id": "gpt-5.4", "label": "GPT-5.4 (Codex)", "provider": "openai-codex"},
            ]
        if "openai" in self._providers:
            models += [
                {"id": "gpt-4o-mini", "label": "GPT-4o mini", "provider": "openai"},
                {"id": "gpt-4o",      "label": "GPT-4o",      "provider": "openai"},
                {"id": "o3",          "label": "o3",           "provider": "openai"},
            ]
        if "ollama" in self._providers:
            models += [
                {"id": "qwen3.5:9b",  "label": "Qwen 9b",  "provider": "ollama", "local": True},
                {"id": "qwen3.5:27b", "label": "Qwen 27b", "provider": "ollama", "local": True},
            ]
        return web.json_response({"models": models})

    async def _run_status_handler(self, request: web.Request) -> web.Response:
        """GET /api/run-status — return whether a run is currently active."""
        running = self._run_task is not None and not self._run_task.done()
        return web.json_response({
            "running": running,
            "message_count": self.state.message_count,
        })

    async def _run_orchestrator(self, query: str, topology: str, model_pin: str = "auto",
                                complexity: int = 50, agent_complexity: dict | None = None) -> None:
        """Build topology, wire dashboard bridge, and run the orchestrator."""
        from web.bridge import DashboardBridge
        from orb.llm.types import ModelTier, ModelConfig
        self._turn_count += 1

        bridge = DashboardBridge(self.state, self.broadcast)

        # Build effective model overrides as a fallback (kept for compatibility)
        effective_overrides = dict(self._model_overrides or {})

        # Build per-agent model assignments based on per-agent complexity (takes precedence over overrides)
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

        # Set up bridge with topology info
        agent_roles = {aid: a.config.role for aid, a in orchestrator.agents.items()}
        bridge.setup_agents(agent_roles)
        bridge.setup_edges([(e.a, e.b) for e in orchestrator.bus.graph.edges])

        # Pre-populate planned model for each agent so the init event shows
        # the correct model immediately (before any messages are exchanged).
        if agent_model_map:
            for aid, cfg in agent_model_map.items():
                if aid in bridge.state.agents:
                    bridge.state.agents[aid].model = cfg.model_id
        if self._config:
            bridge.setup_budget(self._config.budget)

        # Broadcast the init event so the UI switches out of launch-panel mode
        init_event = self.state.to_init_event()
        init_event["run_active"] = True
        await self.broadcast(json.dumps(init_event))

        # Wire bridge into bus events
        orchestrator.bus.on_event(bridge.on_message_routed)

        # Wire completion callbacks
        original_on_complete = orchestrator._on_agent_complete

        async def wrapped_on_complete(agent_id, result):
            agent_obj = orchestrator.agents.get(agent_id)
            model = getattr(agent_obj, "_last_model", "") if agent_obj else ""
            model = model or ""
            if model:
                await bridge.on_agent_status(agent_id, "completed", model)
            await bridge.on_agent_complete(agent_id, result)
            await original_on_complete(agent_id, result)

        orchestrator._on_agent_complete = wrapped_on_complete

        # Wire activity callbacks into every agent
        async def on_agent_activity(agent_id: str, activity: str) -> None:
            await self.broadcast(json.dumps({
                "type": "agent_activity",
                "agent": agent_id,
                "activity": activity,
            }))

        for agent in orchestrator.agents.values():
            agent._on_activity = on_agent_activity

        # Wire file-write callbacks so the TUI (and any other WS client) sees diffs
        def _make_file_write_cb(aid: str):
            def cb(_, path: str, content: str, old_content: str = "") -> None:
                asyncio.ensure_future(self.broadcast(json.dumps({
                    "type": "file_write",
                    "agent": aid,
                    "path": path,
                    "content": content,
                    "old_content": old_content,
                })))
            return cb

        for aid, agent in orchestrator.agents.items():
            agent._on_file_write = _make_file_write_cb(aid)

        # ── Conversational continuity ──────────────────────────────────────────
        # Build context preamble from prior session runs
        if self._session_history:
            lines = ["=== Prior session context ==="]
            for i, h in enumerate(self._session_history[-5:], 1):
                lines.append(f"[{i}] User: {h['query']}")
                if h["result"]:
                    lines.append(f"     Result: {h['result'][:200]}")
            lines.append("=== End of prior context ===\n")
            query = "\n".join(lines) + query

        # Restore agent conversation histories from the previous run.
        # Strip backward from the end until the conversation is in a "clean" state:
        # an assistant message with no tool_use blocks (pure text).  This prevents
        # two classes of 400 errors from the Anthropic API:
        #   1. Consecutive user messages — if we restore ending on a tool_result and
        #      immediately add_user() for the new task.
        #   2. Orphaned tool_use — if the last assistant message has tool_use blocks
        #      whose corresponding tool_result was stripped, the API rejects the next
        #      call with "unexpected tool_use_id in tool_result blocks".
        if self._conv_carryover:
            for aid, agent in orchestrator.agents.items():
                if aid not in self._conv_carryover or not self._conv_carryover[aid]:
                    continue
                msgs = list(self._conv_carryover[aid])
                # Strip until we end on a clean assistant message (text, no tool_use)
                while msgs:
                    last = msgs[-1]
                    role = last.get("role")
                    content = last.get("content", "")
                    if role == "user":
                        # Trailing user messages (tool_results or plain text)
                        msgs.pop()
                        continue
                    if role == "assistant" and isinstance(content, list) and any(
                        b.get("type") == "tool_use" for b in content
                    ):
                        # Assistant has tool_use blocks whose tool_results were stripped
                        msgs.pop()
                        continue
                    break  # Clean: assistant with text-only content
                if msgs:
                    agent._conversation.messages = msgs

        # Store agent refs for message injection
        self.set_agents(orchestrator.agents)

        try:
            result = await orchestrator.run(query)
        except Exception:
            logger.exception("Orchestrator run failed")
            result = None
        else:
            self.state.completed = True

        # Save conversation histories and session summary for next run
        # Compact any agent whose history has grown large
        from orb.agent.compaction import compact_history, COMPACT_THRESHOLD
        new_carryover: dict[str, list] = {}
        for aid, agent in orchestrator.agents.items():
            msgs = list(agent._conversation.messages)
            if len(msgs) >= COMPACT_THRESHOLD:
                msgs = await compact_history(msgs, self._providers)
            new_carryover[aid] = msgs
        self._conv_carryover = new_carryover
        synthesis_id = orchestrator.config.synthesis_agent
        if result:
            summary = (
                result.completions.get(synthesis_id, "")
                or next(iter(result.completions.values()), "")
            )
        else:
            summary = ""
        self._session_history.append({"query": query.split("=== End of prior context ===\n")[-1], "result": summary[:300]})

        # Broadcast final stats
        elapsed = time.time() - self.state.start_time
        await self.broadcast(json.dumps({
            "type": "stats",
            "message_count": self.state.message_count,
            "budget_remaining": self.state.budget_remaining,
            "elapsed": round(elapsed, 2),
        }))

        # Explicitly complete any agents that were shut down without calling complete_task
        if result:
            for agent_id in orchestrator.agents:
                if agent_id not in result.completions:
                    await bridge.on_agent_complete(agent_id, "[shutdown]")

        # Broadcast the final synthesized result to show in the chat log
        synthesis_id = orchestrator.config.synthesis_agent
        if result and synthesis_id and synthesis_id in result.completions:
            final_result = result.completions[synthesis_id]
            try:
                from orb.cli.diff_capture import capture_diff
                diff = capture_diff()
            except Exception:
                diff = ""
            await self.broadcast(json.dumps({
                "type": "run_complete",
                "result": final_result,
                "diff": diff,
                "elapsed": round(elapsed, 2),
                "session_turn": len(self._session_history),
                "routed": self.state.message_count,
            }))

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            logger.info(f"Dashboard client connected ({len(self._clients)} total)")

            # Send current state on connect
            try:
                init_event = self.state.to_init_event()
                init_event["run_active"] = self._run_task is not None and not self._run_task.done()
                await ws.send_str(json.dumps(init_event))
            except Exception:
                pass

            async for msg in ws:
                pass  # We don't expect client messages, just keep connection alive
        finally:
            self._clients.discard(ws)
            logger.info(f"Dashboard client disconnected ({len(self._clients)} total)")

        return ws
