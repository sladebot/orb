from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from json import JSONDecodeError
from pathlib import Path

import httpx
from orb.agent.compaction import COMPACT_THRESHOLD, DEFAULT_COMPACTOR, CompactionStrategy
from orb.cli.config import get as get_config, load_config, save_config
from orb.messaging.channel import ChannelClosed
from orb.tracing.run_trace import RunTrace
from web.state import DashboardState
from .topology_classifier import ProviderBackedTopologyClassifier, TopologyClassifier
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
        self._session_path_explicit = session_path is not None
        self._compactor = compactor or DEFAULT_COMPACTOR
        self._conversation_session = self._load_session()
        if not self._session_path_explicit:
            self._session_path = self._default_session_path(self._conversation_session.session_id)
        self._turn_count: int = 0
        self._last_result = None
        self._last_trace: RunTrace | None = None
        self._run_transcript = RunTranscript(session=self._conversation_session)
        self._topology_classifier: TopologyClassifier = ProviderBackedTopologyClassifier(
            self._planner_model_config,
            lambda provider_name: self._providers.get(provider_name),
        )

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

        # Auto-generate a layered fallback when no graph_view is defined.
        # This keeps simple topologies like triad branched instead of stacked
        # one node per row in the dashboard.
        order = list(topo.agents.keys())
        if not order:
            return {"rows": [], "order": []}

        root = topo.entry_agent if topo.entry_agent in topo.agents else order[0]
        outgoing: dict[str, list[str]] = {agent_id: [] for agent_id in order}
        incoming: dict[str, list[str]] = {agent_id: [] for agent_id in order}
        for edge in topo.edges:
            if isinstance(edge, tuple) and len(edge) == 2:
                source, target = edge
            else:
                source = getattr(edge, "a", None)
                target = getattr(edge, "b", None)
            if source in outgoing and target in outgoing:
                outgoing[source].append(target)
                incoming[target].append(source)

        layers: dict[str, int] = {root: 0}
        queue: list[str] = [root]
        while queue:
            current = queue.pop(0)
            depth = layers[current]
            for nxt in outgoing.get(current, []):
                if nxt not in layers:
                    layers[nxt] = depth + 1
                    queue.append(nxt)

        def infer_layer(agent_id: str) -> int:
            agent = topo.agents[agent_id]
            category = (agent.category or "").lower()
            role = (agent.role or agent_id).lower()
            key = f"{category} {role}"
            if "entry" in key or "coordinator" in key:
                return 0
            if "discovery" in key or "research" in key:
                return 1
            if "implement" in key or "coder" in key or "worker" in key:
                return 2
            if "review" in key or "validation" in key or "test" in key:
                return 3
            return 2

        for agent_id in order:
            if agent_id not in layers:
                parent_layers = [layers[parent] for parent in incoming.get(agent_id, []) if parent in layers]
                layers[agent_id] = (min(parent_layers) + 1) if parent_layers else infer_layer(agent_id)

        unique_layers = sorted(set(layers.values()))
        positions: dict[str, int] = {root: 0}
        rows: list[list[dict]] = []
        for layer in unique_layers:
            row_ids = [agent_id for agent_id in order if layers.get(agent_id) == layer]
            row_ids.sort(
                key=lambda agent_id: (
                    sum(positions[parent] for parent in incoming.get(agent_id, []) if parent in positions)
                    / max(1, len([parent for parent in incoming.get(agent_id, []) if parent in positions])),
                    order.index(agent_id),
                )
            )
            for index, agent_id in enumerate(row_ids):
                positions[agent_id] = index
            rows.append([{"node": agent_id} for agent_id in row_ids])
        return {"rows": rows, "order": order}

    @staticmethod
    def _topology_options(selected_id: str, candidates: list[dict] | None = None) -> list[dict]:
        candidate_map = {
            str(item.get("topology")): item
            for item in (candidates or [])
            if isinstance(item, dict) and item.get("topology")
        }
        options = []
        for topology_id, topo in GraphRuntime._available_topologies().items():
            candidate = candidate_map.get(topology_id, {})
            options.append({
                "topology": topology_id,
                "label": topo.label,
                "description": topo.description,
                "chosen": topology_id == selected_id,
                "score": candidate.get("score"),
                "reason": candidate.get("reason", ""),
            })
        return options

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
        enabled = [
            name for name in self._all_providers
            if self._provider_enabled(name) and self._provider_has_enabled_models(name)
        ]
        self._enabled_providers = enabled
        self._providers = {
            name: client
            for name, client in self._all_providers.items()
            if name in set(enabled)
        }

    def set_topology_classifier(self, classifier: TopologyClassifier) -> None:
        self._topology_classifier = classifier

    @staticmethod
    def _provider_config_entry(provider: str) -> dict:
        provider_cfg = get_config("providers") or {}
        entry = provider_cfg.get(provider) or {}
        return entry if isinstance(entry, dict) else {}

    @classmethod
    def _provider_enabled(cls, provider: str) -> bool:
        return bool(cls._provider_config_entry(provider).get("enabled", True))

    @classmethod
    def _canonical_model_id(cls, provider: str, model_id: str) -> str:
        if provider == "anthropic":
            from orb.llm.anthropic import ANTHROPIC_MODEL_ALIASES

            return ANTHROPIC_MODEL_ALIASES.get(model_id, model_id)
        return model_id

    @classmethod
    def _provider_model_enabled(cls, provider: str, model_id: str) -> bool:
        canonical_model_id = cls._canonical_model_id(provider, model_id)
        models = cls._provider_config_entry(provider).get("models") or {}
        if not isinstance(models, dict) or not models:
            return True
        entry = models.get(model_id)
        if entry is None and canonical_model_id != model_id:
            entry = models.get(canonical_model_id)
        if entry is None:
            return True
        if isinstance(entry, bool):
            return entry
        if isinstance(entry, dict):
            return bool(entry.get("enabled", True))
        return True

    @classmethod
    def _provider_known_models(cls, provider: str) -> list[str]:
        from orb.llm.types import (
            ANTHROPIC_HAIKU_MODEL,
            ANTHROPIC_OPUS_MODEL,
            ANTHROPIC_SONNET_MODEL,
        )

        defaults = {
            "anthropic": [
                ANTHROPIC_HAIKU_MODEL,
                ANTHROPIC_SONNET_MODEL,
                ANTHROPIC_OPUS_MODEL,
            ],
            "openai-codex": ["gpt-5.4-mini", "gpt-5.4"],
            "ollama": ["qwen3.5:9b", "qwen3.5:27b"],
            "vmlx": [],
            "omlx": [],
        }
        model_ids = list(defaults.get(provider, []))
        for item in cls._provider_config_entry(provider).get("catalog") or []:
            if isinstance(item, dict) and item.get("id"):
                model_id = cls._canonical_model_id(provider, str(item["id"]))
                if model_id not in model_ids:
                    model_ids.append(model_id)
        return model_ids

    @classmethod
    def _provider_has_enabled_models(cls, provider: str) -> bool:
        if not cls._provider_enabled(provider):
            return False
        known = cls._provider_known_models(provider)
        if not known:
            return True
        return any(cls._provider_model_enabled(provider, model_id) for model_id in known)

    @classmethod
    def _provider_catalog(cls, provider: str) -> list[dict]:
        catalog = cls._provider_config_entry(provider).get("catalog") or []
        filtered: list[dict] = []
        seen: set[str] = set()
        for item in catalog:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            model_id = cls._canonical_model_id(provider, str(item["id"]))
            if not cls._provider_model_enabled(provider, model_id) or model_id in seen:
                continue
            normalized = dict(item)
            normalized["id"] = model_id
            filtered.append(normalized)
            seen.add(model_id)
        return filtered

    @classmethod
    def _provider_default_model(cls, provider: str, key: str) -> str:
        defaults = cls._provider_config_entry(provider).get("default_models") or {}
        model_id = defaults.get(key) if isinstance(defaults, dict) else None
        candidate = cls._canonical_model_id(provider, str(model_id) if model_id else "")
        if candidate and cls._provider_model_enabled(provider, candidate):
            return candidate
        catalog = cls._provider_catalog(provider)
        first_enabled = next((str(item["id"]) for item in catalog), None)
        if first_enabled:
            return first_enabled
        known_models = cls._provider_known_models(provider)
        first_known_enabled = next(
            (model_id for model_id in known_models if cls._provider_model_enabled(provider, model_id)),
            None,
        )
        return first_known_enabled or ""

    @classmethod
    def _provider_configured_model_entries(cls, provider: str, *, local: bool) -> list[dict]:
        catalog = cls._provider_catalog(provider)
        if catalog:
            return [
                {
                    "id": str(item["id"]),
                    "label": str(item.get("label") or item["id"]),
                    "local": local,
                }
                for item in catalog
            ]

        entry = cls._provider_config_entry(provider)
        defaults = entry.get("default_models") or {}
        configured: list[dict] = []
        seen: set[str] = set()
        if isinstance(defaults, dict):
            for model_id in defaults.values():
                canonical = cls._canonical_model_id(provider, str(model_id or ""))
                if not canonical or canonical in seen or not cls._provider_model_enabled(provider, canonical):
                    continue
                configured.append({"id": canonical, "label": canonical, "local": local})
                seen.add(canonical)
        if configured:
            return configured

        return [
            {"id": model_id, "label": model_id, "local": local}
            for model_id in cls._provider_known_models(provider)
            if model_id and cls._provider_model_enabled(provider, model_id)
        ]

    @staticmethod
    def _anthropic_label_for_model(model_id: str) -> str:
        parts = model_id.replace("claude-", "").split("-")
        family = parts[0].capitalize() if parts else "Claude"
        version = ".".join(parts[1:3]) if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit() else ""
        return f"Claude {family} {version}".strip()

    @staticmethod
    def _pick_catalog_model(catalog: list[dict], token: str, fallback: str) -> str:
        needle = token.lower()
        for item in catalog:
            model_id = str(item.get("id", ""))
            if needle in model_id.lower():
                return model_id
        return fallback

    async def refresh_provider_catalogs(self) -> dict[str, str]:
        """Refresh every registered provider's catalog.

        Returns a status map keyed by provider name, with one of:

          • ``"updated:<N>"``   — catalog changed and was persisted.
          • ``"unchanged:<N>"`` — fetch succeeded but nothing changed.
          • ``"skipped:not-registered"`` — provider disabled in config /
            failed its liveness check, so no refresh attempted.
          • ``"skipped:empty"``  — fetch returned no models (unreachable,
            401, or misconfigured). See ~/.orb/run.log for the exception.

        Callers (the `orb models refresh` CLI handler) use this to print a
        line per provider so the user can see exactly which ones moved.
        """
        cfg = load_config()
        providers_cfg = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
        status: dict[str, str] = {}
        updated = False

        # Static catalog — always succeeds because it's hardcoded.
        OPENAI_CODEX_CATALOG: list[dict] = [
            {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini", "local": False},
            {"id": "gpt-5.4",      "label": "GPT-5.4",      "local": False},
        ]
        OPENAI_CODEX_DEFAULTS: dict[str, str] = {
            "cloud_lite":   "gpt-5.4-mini",
            "cloud_fast":   "gpt-5.4-mini",
            "cloud_strong": "gpt-5.4",
        }

        fetchers: list[tuple[str, Any, list[dict] | None, dict[str, str] | None]] = [
            ("anthropic",    self._fetch_anthropic_catalog, None,                 None),
            ("openai-codex", None,                          OPENAI_CODEX_CATALOG, OPENAI_CODEX_DEFAULTS),
            ("ollama",       self._fetch_ollama_catalog,    None,                 None),
            ("vmlx",         self._fetch_vmlx_catalog,      None,                 None),
            ("omlx",         self._fetch_omlx_catalog,      None,                 None),
        ]

        for name, fetcher, static_catalog, static_defaults in fetchers:
            if name not in self._all_providers:
                status[name] = "skipped:not-registered"
                continue

            if static_catalog is not None:
                catalog, defaults = static_catalog, static_defaults or {}
            else:
                catalog, defaults = await fetcher()

            if not catalog:
                status[name] = "skipped:empty"
                continue

            entry = dict(providers_cfg.get(name) or {})
            changed = entry.get("catalog") != catalog or entry.get("default_models") != defaults
            if changed:
                entry["catalog"] = catalog
                entry["default_models"] = defaults
                entry["refreshed_at"] = int(time.time())
                providers_cfg[name] = entry
                updated = True
                status[name] = f"updated:{len(catalog)}"
            else:
                status[name] = f"unchanged:{len(catalog)}"

        if updated:
            cfg["providers"] = providers_cfg
            save_config(cfg)

        return status

    async def _fetch_anthropic_catalog(self) -> tuple[list[dict], dict[str, str]]:
        from orb.cli.auth import _anthropic_headers, get_anthropic_key
        from orb.llm.types import ANTHROPIC_HAIKU_MODEL, ANTHROPIC_OPUS_MODEL, ANTHROPIC_SONNET_MODEL

        key = get_anthropic_key()
        if not key:
            return [], {}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers=_anthropic_headers(key),
                    timeout=15.0,
                )
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception as exc:
            logger.warning("Failed to refresh Anthropic model catalog: %s", exc)
            return [], {}

        catalog = []
        for item in data:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            catalog.append({
                "id": model_id,
                "label": self._anthropic_label_for_model(model_id),
                "local": False,
            })

        defaults = {
            "cloud_lite": self._pick_catalog_model(catalog, "haiku", ANTHROPIC_HAIKU_MODEL),
            "cloud_fast": self._pick_catalog_model(catalog, "sonnet", ANTHROPIC_SONNET_MODEL),
            "cloud_strong": self._pick_catalog_model(catalog, "opus", ANTHROPIC_OPUS_MODEL),
        }
        return catalog, defaults

    async def _fetch_ollama_catalog(self) -> tuple[list[dict], dict[str, str]]:
        from orb.llm.registry import _ollama_base_url

        provider_cfg = self._provider_config_entry("ollama")
        endpoint = str(provider_cfg.get("base_url") or _ollama_base_url()).rstrip("/")
        if endpoint.endswith("/v1"):
            endpoint = endpoint[:-3]
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{endpoint}/api/tags", timeout=5.0)
            resp.raise_for_status()
            data = resp.json().get("models") or []
        except Exception:
            return [], {}

        catalog = []
        seen: set[str] = set()
        for item in data:
            model_id = str(item.get("name") or "").strip()
            if not model_id:
                continue
            if model_id.endswith("-4k:latest"):
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            catalog.append({"id": model_id, "label": model_id, "local": True})

        defaults = {
            "local_small": self._pick_catalog_model(catalog, "9b", "qwen3.5:9b"),
            "local_medium": self._pick_catalog_model(catalog, "27b", "qwen3.5:27b"),
            "local_large": self._pick_catalog_model(catalog, "27b", "qwen3.5:27b"),
        }
        return catalog, defaults

    async def _fetch_vmlx_catalog(self) -> tuple[list[dict], dict[str, str]]:
        from orb.llm.registry import _vmlx_base_url, _vmlx_api_key

        provider_cfg = self._provider_config_entry("vmlx")
        endpoint = str(provider_cfg.get("base_url") or _vmlx_base_url()).rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint = f"{endpoint}/v1"
        headers = {}
        # Prefer an explicit config key; fall back to VMLX_API_KEY env var via
        # the shared helper so `models refresh` behaves like the runtime.
        api_key = provider_cfg.get("api_key") or _vmlx_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{endpoint}/models", timeout=5.0, headers=headers or None)
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception as exc:
            logger.warning("Failed to refresh vmlx catalog at %s: %s", endpoint, exc)
            return [], {}

        catalog = []
        seen: set[str] = set()
        for item in data:
            model_id = str(item.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            catalog.append({"id": model_id, "label": model_id, "local": True})

        defaults = {
            "local_small": self._pick_catalog_model(catalog, "7b", catalog[0]["id"] if catalog else "qwen"),
            "local_medium": self._pick_catalog_model(catalog, "14b", catalog[0]["id"] if catalog else "qwen"),
            "local_large": self._pick_catalog_model(catalog, "32b", catalog[-1]["id"] if catalog else "qwen"),
        }
        if catalog:
            defaults["local_small"] = self._pick_catalog_model(catalog, "7b", catalog[0]["id"])
            defaults["local_medium"] = self._pick_catalog_model(catalog, "14b", catalog[min(1, len(catalog) - 1)]["id"])
            defaults["local_large"] = self._pick_catalog_model(catalog, "32b", catalog[-1]["id"])
        return catalog, defaults

    async def _fetch_omlx_catalog(self) -> tuple[list[dict], dict[str, str]]:
        from orb.llm.registry import _omlx_base_url, _omlx_api_key

        provider_cfg = self._provider_config_entry("omlx")
        endpoint = str(provider_cfg.get("base_url") or _omlx_base_url()).rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint = f"{endpoint}/v1"
        headers = {}
        # Prefer an explicit config key; fall back to OMLX_API_KEY env var via
        # the shared helper so `models refresh` behaves like the runtime.
        api_key = provider_cfg.get("api_key") or _omlx_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{endpoint}/models", timeout=5.0, headers=headers or None)
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception as exc:
            logger.warning("Failed to refresh omlx catalog at %s: %s", endpoint, exc)
            return [], {}

        catalog = []
        seen: set[str] = set()
        for item in data:
            model_id = str(item.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            catalog.append({"id": model_id, "label": model_id, "local": True})

        defaults = {
            "local_small": self._pick_catalog_model(catalog, "7b", catalog[0]["id"] if catalog else "qwen"),
            "local_medium": self._pick_catalog_model(catalog, "14b", catalog[0]["id"] if catalog else "qwen"),
            "local_large": self._pick_catalog_model(catalog, "32b", catalog[-1]["id"] if catalog else "qwen"),
        }
        if catalog:
            defaults["local_small"] = self._pick_catalog_model(catalog, "7b", catalog[0]["id"])
            defaults["local_medium"] = self._pick_catalog_model(catalog, "14b", catalog[min(1, len(catalog) - 1)]["id"])
            defaults["local_large"] = self._pick_catalog_model(catalog, "32b", catalog[-1]["id"])
        return catalog, defaults

    def settings_payload(self) -> dict:
        model_payload = self.models_payload()
        models = model_payload.get("models", [])
        available = sorted(self._all_providers.keys())
        provider_config = get_config("providers") or {}
        enabled_models_by_provider: dict[str, list[dict]] = {name: [] for name in available}
        for item in models:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            if not provider or provider == "auto" or provider not in enabled_models_by_provider:
                continue
            enabled_models_by_provider[provider].append({
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or item.get("id") or ""),
                "local": bool(item.get("local")),
            })
        return {
            "available_providers": available,
            "providers": {
                name: {
                    "enabled": bool((provider_config.get(name) or {}).get("enabled", True)),
                    "active": name in self._providers,
                    "enabled_models": enabled_models_by_provider.get(name, []),
                }
                for name in available
            },
            "models": models,
        }

    def _dashboard_sessions_dir(self) -> Path:
        return Path.home() / ".orb" / "sessions"

    def _dashboard_session_path(self, session_id: str | None = None) -> Path:
        return self._dashboard_sessions_dir() / f"{session_id or self._conversation_session.session_id}.json"

    def _load_dashboard_snapshot(self, session_id: str | None = None) -> dict | None:
        path = self._dashboard_session_path(session_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load dashboard snapshot %s: %s", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def _write_dashboard_snapshot(self, payload: dict, session_id: str | None = None) -> None:
        path = self._dashboard_session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    def _load_dashboard_file_changes(self, session_id: str | None = None) -> list[dict]:
        snapshot = self._load_dashboard_snapshot(session_id) or {}
        file_changes = snapshot.get("file_changes") or []
        return file_changes if isinstance(file_changes, list) else []

    def _dashboard_snapshot_payload(self, *, session_id: str | None = None, run_active: bool | None = None) -> dict:
        payload = self.state.to_init_event()
        resolved_session_id = session_id or payload.get("session_id") or self._conversation_session.session_id
        payload["session_id"] = resolved_session_id
        payload["file_changes"] = self._load_dashboard_file_changes(resolved_session_id)
        payload["run_active"] = self.running if run_active is None else bool(run_active)
        # Expose session-level lock + workdir so the UI can show "pinned"
        # topology/models and disable re-classification affordances.
        payload["session"] = {
            "id": self._conversation_session.session_id,
            "workdir": self._conversation_session.workdir,
            "locked_topology": self._conversation_session.locked_topology,
            "locked_agent_models": dict(self._conversation_session.locked_agent_models),
            "locked_model_pin": self._conversation_session.locked_model_pin,
        }
        if self._last_trace is not None:
            payload["run_trace"] = self._last_trace.to_dict()
            payload["run_trace_summary"] = self._last_trace.summary()
        return payload

    @staticmethod
    def _merge_dashboard_snapshot(live_payload: dict, snapshot: dict | None) -> dict:
        if not snapshot:
            return live_payload

        merged = dict(snapshot)
        merged.update(live_payload)

        for key in ("agents", "edges", "messages", "activity_events", "plan_steps", "file_changes"):
            live_value = live_payload.get(key)
            if isinstance(live_value, list) and live_value:
                merged[key] = live_value
            elif key in snapshot:
                merged[key] = snapshot[key]

        for key in ("completed", "final_result", "final_agent", "final_diff", "session_turn", "session_generation", "workdir"):
            live_value = live_payload.get(key)
            if live_value not in ("", None, [], {}):
                merged[key] = live_value
            elif key in snapshot:
                merged[key] = snapshot[key]

        live_plan = live_payload.get("plan") or {}
        snapshot_plan = snapshot.get("plan") or {}
        merged_plan = dict(snapshot_plan)
        merged_plan.update(live_plan)
        for key in ("query", "agent_complexity", "agent_models", "neighbors", "positions", "graph_view", "routing", "workdir"):
            live_value = live_plan.get(key)
            if live_value not in ("", None, [], {}):
                merged_plan[key] = live_value
            elif key in snapshot_plan:
                merged_plan[key] = snapshot_plan[key]
        live_topology = live_plan.get("topology") or {}
        snapshot_topology = snapshot_plan.get("topology") or {}
        merged_topology = dict(snapshot_topology)
        merged_topology.update(live_topology)
        for key in ("id", "label", "description"):
            if live_topology.get(key) not in ("", None):
                merged_topology[key] = live_topology[key]
            elif key in snapshot_topology:
                merged_topology[key] = snapshot_topology[key]
        merged_plan["topology"] = merged_topology
        merged["plan"] = merged_plan

        live_stats = live_payload.get("stats") or {}
        snapshot_stats = snapshot.get("stats") or {}
        merged_stats = dict(snapshot_stats)
        merged_stats.update(live_stats)
        merged["stats"] = merged_stats

        merged["type"] = "init"
        return merged

    def _has_live_dashboard_state(self) -> bool:
        return any((
            self.state.agents,
            self.state.messages,
            self.state.activity_events,
            self.state.plan_steps,
            self.state.final_result,
            self.state.final_diff,
        ))

    def _persist_dashboard_snapshot(self, *, run_active: bool | None = None) -> None:
        session_id = self._conversation_session.session_id
        if not session_id:
            return
        try:
            self._write_dashboard_snapshot(
                self._dashboard_snapshot_payload(session_id=session_id, run_active=run_active),
                session_id=session_id,
            )
        except OSError as exc:
            logger.warning("Failed to persist dashboard snapshot: %s", exc)

    def _trace_dir(self) -> Path:
        return Path.cwd() / ".orb" / "traces"

    def _trace_session_index_dir(self) -> Path:
        return self._trace_dir() / "by-session"

    def _trace_session_index_path(self, session_id: str) -> Path:
        return self._trace_session_index_dir() / f"{session_id}.json"

    def _load_trace_session_index(self, session_id: str) -> dict:
        path = self._trace_session_index_path(session_id)
        if not path.exists():
            return {"session_id": session_id, "runs": []}
        try:
            payload = json.loads(path.read_text())
        except (OSError, JSONDecodeError, ValueError, TypeError):
            return {"session_id": session_id, "runs": []}
        if not isinstance(payload, dict):
            return {"session_id": session_id, "runs": []}
        payload.setdefault("session_id", session_id)
        payload.setdefault("runs", [])
        return payload

    def _persist_trace_session_index(self, trace: RunTrace) -> None:
        session_id = trace.session_id or str(trace.metadata.get("session_id") or "")
        if not session_id:
            return
        payload = self._load_trace_session_index(session_id)
        runs = [
            item for item in (payload.get("runs") or [])
            if isinstance(item, dict) and item.get("run_id") != trace.run_id
        ]
        runs.append(trace.summary())
        runs.sort(key=lambda item: float(item.get("last_event_at") or item.get("updated_at") or 0.0), reverse=True)
        payload["runs"] = runs
        payload["updated_at"] = time.time()
        path = self._trace_session_index_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

    def _persist_run_trace(self) -> None:
        if self._last_trace is None:
            return
        try:
            self._last_trace.save(self._trace_dir() / f"{self._last_trace.run_id}.json")
            self._persist_trace_session_index(self._last_trace)
        except OSError as exc:
            logger.warning("Failed to persist run trace: %s", exc)

    def list_trace_sessions(self) -> dict:
        sessions_dir = self._workspace_sessions_dir()
        summaries_by_id: dict[str, dict] = {}
        session_paths = sorted(sessions_dir.glob("*.json")) if sessions_dir.exists() else []
        for path in session_paths:
            try:
                session = ConversationSession.load(path)
            except (OSError, JSONDecodeError, ValueError, TypeError):
                continue
            trace_index = self._load_trace_session_index(session.session_id)
            runs = [
                item for item in (trace_index.get("runs") or [])
                if isinstance(item, dict)
            ]
            summaries_by_id[session.session_id] = {
                "session_id": session.session_id,
                "generation": session.generation,
                "user_turns": session.user_turn_count(),
                "updated_at": session.updated_at,
                "run_count": len(runs),
                "last_run_at": runs[0].get("last_event_at") if runs else None,
                "active": session.session_id == self._conversation_session.session_id,
            }
        index_dir = self._trace_session_index_dir()
        index_paths = sorted(index_dir.glob("*.json")) if index_dir.exists() else []
        for path in index_paths:
            try:
                payload = json.loads(path.read_text())
            except (OSError, JSONDecodeError, ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            session_id = str(payload.get("session_id") or path.stem)
            if not session_id or session_id in summaries_by_id:
                continue
            runs = [item for item in (payload.get("runs") or []) if isinstance(item, dict)]
            last_updated = max(
                [float(item.get("last_event_at") or 0.0) for item in runs] or [0.0]
            )
            summaries_by_id[session_id] = {
                "session_id": session_id,
                "generation": 1,
                "user_turns": 0,
                "updated_at": last_updated,
                "run_count": len(runs),
                "last_run_at": runs[0].get("last_event_at") if runs else None,
                "active": session_id == self._conversation_session.session_id,
            }
        summaries = list(summaries_by_id.values())
        summaries.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
        return {
            "sessions": summaries,
            "current_session_id": self._conversation_session.session_id,
        }

    def list_session_traces(self, session_id: str) -> dict:
        trace_index = self._load_trace_session_index(session_id)
        return {
            "session_id": session_id,
            "runs": [
                item for item in (trace_index.get("runs") or [])
                if isinstance(item, dict)
            ],
        }

    def get_trace_payload(self, run_id: str) -> dict | None:
        path = self._trace_dir() / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            trace = RunTrace.load(path)
        except (OSError, JSONDecodeError, ValueError, TypeError):
            return None
        return {
            "trace": trace.to_dict(),
            "summary": trace.summary(),
            "path": str(path),
        }

    def _persist_dashboard_file_change(self, *, path: str, agent: str, content: str, old_content: str = "") -> None:
        session_id = self._conversation_session.session_id
        if not session_id:
            return
        snapshot = self._load_dashboard_snapshot(session_id) or self._dashboard_snapshot_payload(
            session_id=session_id,
            run_active=self.running,
        )
        existing = {
            str(change.get("path")): dict(change)
            for change in (snapshot.get("file_changes") or [])
            if isinstance(change, dict) and change.get("path")
        }
        existing[path] = {
            "path": path,
            "agent": agent,
            "content": content,
            "old_content": old_content,
        }
        snapshot["file_changes"] = list(existing.values())
        snapshot["run_active"] = self.running
        try:
            self._write_dashboard_snapshot(snapshot, session_id=session_id)
        except OSError as exc:
            logger.warning("Failed to persist dashboard file change: %s", exc)

    def _clear_dashboard_file_changes(self, session_id: str | None = None) -> None:
        resolved_session_id = session_id or self._conversation_session.session_id
        if not resolved_session_id:
            return
        snapshot = self._load_dashboard_snapshot(resolved_session_id) or self._dashboard_snapshot_payload(
            session_id=resolved_session_id,
            run_active=self.running,
        )
        snapshot["file_changes"] = []
        try:
            self._write_dashboard_snapshot(snapshot, session_id=resolved_session_id)
        except OSError as exc:
            logger.warning("Failed to clear dashboard file changes: %s", exc)

    def _resolved_session_path(self) -> Path:
        return self._session_path or self._default_session_path(self._conversation_session.session_id)

    def _workspace_state_dir(self) -> Path:
        return Path.cwd() / ".orb"

    def _workspace_sessions_dir(self) -> Path:
        return self._workspace_state_dir() / "sessions"

    def _workspace_current_session_path(self) -> Path:
        return self._workspace_state_dir() / "current_session"

    def _default_session_path(self, session_id: str) -> Path:
        return self._workspace_sessions_dir() / f"{session_id}.json"

    def _load_session(self) -> ConversationSession:
        if self._session_path_explicit:
            path = self._resolved_session_path()
        else:
            current_path = self._workspace_current_session_path()
            session_id = ""
            try:
                if current_path.exists():
                    session_id = current_path.read_text().strip()
            except OSError:
                session_id = ""
            path = self._default_session_path(session_id) if session_id else (Path.cwd() / ".orb" / "session.json")
        if not path.exists():
            return ConversationSession()
        try:
            return ConversationSession.load(path)
        except (OSError, JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load conversation session %s: %s", path, exc)
            return ConversationSession()

    def _persist_session(self) -> None:
        try:
            path = self._resolved_session_path()
            self._conversation_session.save(path)
            if not self._session_path_explicit:
                current_path = self._workspace_current_session_path()
                current_path.parent.mkdir(parents=True, exist_ok=True)
                current_path.write_text(self._conversation_session.session_id)
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

    def current_init_event(self, session_id: str | None = None) -> dict:
        self._sync_session_state()
        requested_session_id = (session_id or "").strip()
        current_session_id = self.state.session_id or self._conversation_session.session_id
        current_snapshot = self._load_dashboard_snapshot(current_session_id)

        if requested_session_id:
            if requested_session_id != current_session_id or not self.running:
                snapshot = self._load_dashboard_snapshot(requested_session_id)
                if snapshot:
                    snapshot["type"] = "init"
                    snapshot["run_active"] = bool(self.running and requested_session_id == current_session_id)
                    return snapshot

        if not self.running and not self._has_live_dashboard_state():
            snapshot = current_snapshot
            if snapshot:
                snapshot["type"] = "init"
                snapshot["run_active"] = False
                return snapshot

        live_payload = self._dashboard_snapshot_payload(
            session_id=requested_session_id or current_session_id,
            run_active=self.running,
        )
        if requested_session_id and requested_session_id == current_session_id:
            return self._merge_dashboard_snapshot(live_payload, current_snapshot)
        if not requested_session_id:
            return self._merge_dashboard_snapshot(live_payload, current_snapshot)
        return live_payload

    async def _record_plan_step(self, stage: str, title: str, detail: str) -> None:
        from web.state import PlanStepRecord

        elapsed = round(time.time() - self.state.start_time, 2)
        self.state.plan_steps.append(PlanStepRecord(stage=stage, title=title, detail=detail, elapsed=elapsed))
        if len(self.state.plan_steps) > 20:
            self.state.plan_steps = self.state.plan_steps[-20:]
        self._persist_dashboard_snapshot()
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
        if self._last_trace is not None:
            self._last_trace.record_human_override(
                action="inject_message",
                message=resolved_text,
                data={"target": resolved_target, "message_id": msg.id},
            )
            self._persist_run_trace()
        self._persist_session()
        self._sync_session_state()
        self._persist_dashboard_snapshot()

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
        agent_models: dict[str, str] | None = None,
        workdir: str | None = None,
    ) -> tuple[int, dict]:
        from orb.topologies import normalize_topology_id

        if not self._providers:
            return 500, {"ok": False, "error": "Server has no providers configured"}
        if self.running:
            return 200, {"ok": False, "error": "Run already in progress"}

        # If a workdir is supplied for this run (or the session already has
        # one pinned), chdir the daemon to it so tools + file writes land in
        # the right repo. Empty string means "use wherever we are now".
        target_workdir = (workdir or self._conversation_session.workdir or "").strip()
        if target_workdir:
            expanded = Path(target_workdir).expanduser()
            if not expanded.exists() or not expanded.is_dir():
                return 400, {"ok": False, "error": f"Workdir does not exist or is not a directory: {expanded}"}
            resolved = str(expanded.resolve())
            try:
                os.chdir(resolved)
            except OSError as exc:
                return 500, {"ok": False, "error": f"Failed to chdir to {resolved}: {exc}"}
            self._conversation_session.workdir = resolved

        self.state.reset()
        self._last_result = None
        self._run_transcript = RunTranscript(session=self._conversation_session)
        self._last_trace = RunTrace(
            session_id=self._conversation_session.session_id,
            metadata={"session_id": self._conversation_session.session_id},
        )
        requested_target, query = self._resolve_conversation_target(
            query,
            default_target="coordinator",
        )

        # Session topology lock — once the session has planned its first
        # run, follow-ups should reuse the same topology AND per-node model
        # map without re-running the classifier. This also handles the
        # common UI case where a follow-up /api/start passes the locked
        # topology id explicitly rather than "auto": we still want to skip
        # re-classification in that case.
        explicit_topology = topology != "auto"
        manual_models = bool(agent_models)
        if self._conversation_session.locked_topology and not manual_models:
            same_as_lock = (
                not explicit_topology
                or topology == self._conversation_session.locked_topology
            )
            if same_as_lock:
                topology = self._conversation_session.locked_topology
                explicit_topology = True
                if not model_pin or model_pin == "auto":
                    model_pin = self._conversation_session.locked_model_pin or "auto"
                # Feed the locked per-agent model map through manual mode so
                # _manual_prediction runs instead of predict_topology.
                if self._conversation_session.locked_agent_models:
                    agent_models = dict(self._conversation_session.locked_agent_models)
                    manual_models = True

        logger.info(
            "run start session=%s requested_topology=%s requested_target=%s model_pin=%s manual_models=%s workdir=%s query=%s",
            self._conversation_session.session_id,
            topology,
            requested_target,
            model_pin,
            manual_models,
            self._conversation_session.workdir or "<cwd>",
            query[:240].replace("\n", " "),
        )
        self.state.run_query = query
        self._sync_session_state()
        self._last_trace.record_stage_start("planning", message="run planning started")
        await self._record_plan_step("planning", "Starting run planning", "Analyzing task, topology, and per-agent model allocation.")

        if manual_models and explicit_topology:
            # Manual mode — skip the classifier entirely. Synthesize a
            # predicted payload from the caller-supplied topology + models.
            predicted = self._manual_prediction(
                topology=normalize_topology_id(topology),
                agent_models=agent_models,
                model_pin=model_pin,
            )
        else:
            predicted = await self.predict_topology(query, model_pin=model_pin, requested_topology=topology)
        selected_topology = normalize_topology_id(topology) if topology != "auto" else predicted.get("topology", "triad")
        routing_payload = {
            "task_type": str(predicted.get("task_type") or ""),
            "reason": str(predicted.get("reason") or ""),
            "summary": str(predicted.get("summary") or ""),
            "routing_mode": str(predicted.get("routing_mode") or ""),
            "classifier_model": str(predicted.get("classifier_model") or ""),
            "classifier_provider": str(predicted.get("classifier_provider") or ""),
            "signals": dict(predicted.get("signals") or {}),
            "candidates": list(predicted.get("candidates") or []),
            "escalation_allowed": bool(predicted.get("escalation_allowed")),
            "stop_early_allowed": bool(predicted.get("stop_early_allowed")),
            "escalation_reason": str(predicted.get("escalation_reason") or ""),
            "stop_early_reason": str(predicted.get("stop_early_reason") or ""),
            "requested_topology": str(predicted.get("requested_topology") or topology or "auto"),
        }
        self._last_trace.record_topology_choice(
            selected_topology,
            reason=str(predicted.get("reason") or predicted.get("description") or ""),
            task_type=str(predicted.get("task_type") or ""),
            candidates=[option["topology"] for option in (predicted.get("options") or []) if isinstance(option, dict) and option.get("topology")],
            data={
                "complexity": int(predicted.get("complexity", 50)),
                "requested_target": requested_target,
                "routing_mode": str(predicted.get("routing_mode") or ""),
                "classifier_model": str(predicted.get("classifier_model") or ""),
                "classifier_provider": str(predicted.get("classifier_provider") or ""),
                "signals": dict(predicted.get("signals") or {}),
                "candidate_details": list(predicted.get("candidates") or []),
                "escalation_allowed": bool(predicted.get("escalation_allowed")),
                "stop_early_allowed": bool(predicted.get("stop_early_allowed")),
                "escalation_reason": str(predicted.get("escalation_reason") or ""),
                "stop_early_reason": str(predicted.get("stop_early_reason") or ""),
                "summary": str(predicted.get("summary") or ""),
            },
        )
        self._last_trace.record_stage_finish("planning", status="ok", message="run planning finished")
        await self._record_plan_step(
            "routing",
            "Classified task",
            f"{predicted.get('task_type') or 'unknown'} · {predicted.get('summary') or predicted.get('reason') or ''}".strip(),
        )
        await self._record_plan_step(
            "topology",
            "Selected topology",
            f"{predicted.get('label') or selected_topology}: {predicted.get('reason') or predicted.get('description') or ''}".strip(),
        )
        topology_label, topology_description, agent_positions = self._topology_meta(selected_topology)
        graph_view = self._topology_graph_view(selected_topology)
        agent_complexity = dict(predicted.get("agent_complexity") or {})
        overall_complexity = int(predicted.get("complexity", 50))
        fallback_model_map = self._build_agent_model_map(
            overall_complexity,
            model_pin=model_pin,
            agent_complexity=agent_complexity,
            topology_id=selected_topology,
        )
        agent_model_map, _agent_model_reasons = self._validate_agent_model_assignments(
            selected_topology,
            predicted.get("agent_assignments") if isinstance(predicted.get("agent_assignments"), dict) else None,
            fallback_model_map,
        )
        self._last_trace.record_stage_start("allocation", message="model allocation started")
        await self._record_plan_step(
            "allocator",
            "Pinned per-node models",
            ", ".join(f"{aid}={cfg.model_id}" for aid, cfg in agent_model_map.items()),
        )
        self._last_trace.record_stage_finish(
            "allocation",
            status="ok",
            message="model allocation finished",
            data={"agent_models": {aid: cfg.model_id for aid, cfg in agent_model_map.items()}},
        )
        resolved_agent_models = {role: cfg.model_id for role, cfg in agent_model_map.items()}
        # Pin the topology + agent-model map onto the session so follow-up
        # runs reuse the same allocation instead of re-classifying.
        self._conversation_session.locked_topology = selected_topology
        self._conversation_session.locked_agent_models = dict(resolved_agent_models)
        self._conversation_session.locked_model_pin = model_pin or "auto"
        self.state.topology_id = selected_topology
        self.state.topology_label = topology_label
        self.state.topology_description = topology_description
        self.state.agent_complexity = agent_complexity
        self.state.agent_models = resolved_agent_models
        self.state.agent_positions = agent_positions
        self.state.graph_view = graph_view
        self.state.routing = routing_payload
        logger.info(
            "run planning session=%s topology=%s task_type=%s complexity=%s classifier_provider=%s classifier_model=%s",
            self._conversation_session.session_id,
            selected_topology,
            predicted.get("task_type") or "",
            overall_complexity,
            predicted.get("classifier_provider") or "",
            predicted.get("classifier_model") or "",
        )
        logger.info(
            "run allocation session=%s models=%s",
            self._conversation_session.session_id,
            ", ".join(f"{aid}={cfg.provider}:{cfg.model_id}" for aid, cfg in agent_model_map.items()),
        )
        self._persist_run_trace()
        self._persist_dashboard_snapshot()
        self._clear_dashboard_file_changes()
        planning_init = self.current_init_event(session_id=self._conversation_session.session_id) | {"run_active": True}
        await self._broadcast(json.dumps(planning_init))
        self._run_task = asyncio.create_task(
            self._run_orchestrator(
                query,
                selected_topology,
                initial_target=requested_target,
                model_pin=model_pin,
                complexity=overall_complexity,
                agent_complexity=agent_complexity,
                agent_model_map=agent_model_map,
                trace_recorder=self._last_trace,
            )
        )
        self._run_task.add_done_callback(
            lambda t: logger.error("Run task failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )
        return 200, {
            "ok": True,
            "session_id": self._conversation_session.session_id,
            "session_generation": self._conversation_session.generation,
            "session_turn": self._conversation_session.user_turn_count(),
            "init": planning_init,
        }

    async def stop_run(self) -> dict:
        if self.running:
            logger.info("run stop session=%s", self._conversation_session.session_id)
            if self._last_trace is not None:
                self._last_trace.record_human_override(
                    action="stop_run",
                    message="run stopped by user",
                )
                self._persist_run_trace()
            self._run_task.cancel()
            self._persist_dashboard_snapshot(run_active=False)
            await self._broadcast(json.dumps({"type": "stopped"}))
            return {"ok": True}
        return {"ok": False, "error": "No run in progress"}

    async def new_session(self, *, workdir: str | None = None) -> tuple[int, dict]:
        if self.running:
            return 409, {"ok": False, "error": "Cannot start a new session while a run is in progress"}

        resolved_workdir = ""
        if workdir is not None and workdir.strip():
            expanded = Path(workdir.strip()).expanduser()
            if not expanded.exists() or not expanded.is_dir():
                return 400, {"ok": False, "error": f"Workdir does not exist or is not a directory: {expanded}"}
            resolved_workdir = str(expanded.resolve())
            try:
                os.chdir(resolved_workdir)
            except OSError as exc:
                return 500, {"ok": False, "error": f"Failed to chdir to {resolved_workdir}: {exc}"}

        self.state.reset()
        self._agents = {}
        self._last_result = None
        self._conversation_session = ConversationSession(workdir=resolved_workdir)
        if not self._session_path_explicit:
            self._session_path = self._default_session_path(self._conversation_session.session_id)
        self._run_transcript = RunTranscript(session=self._conversation_session)
        self._persist_session()
        self._sync_session_state()
        self._persist_dashboard_snapshot(run_active=False)
        logger.info(
            "session new session=%s workdir=%s",
            self._conversation_session.session_id,
            resolved_workdir or "<cwd>",
        )
        return 200, {
            "ok": True,
            "session_id": self._conversation_session.session_id,
            "session_generation": self._conversation_session.generation,
            "session_turn": self._conversation_session.user_turn_count(),
            "init": self.current_init_event(session_id=self._conversation_session.session_id),
        }

    def models_payload(self) -> dict:
        from orb.llm.types import ANTHROPIC_PROVIDER

        models = [{"id": "auto", "label": "Auto-select", "provider": "auto", "local": False}]
        if "anthropic" in self._providers:
            for item in self._provider_configured_model_entries("anthropic", local=False):
                models.append({
                    "id": item["id"],
                    "label": item["label"],
                    "provider": ANTHROPIC_PROVIDER,
                    "local": False,
                })
        if "openai-codex" in self._providers:
            for item in self._provider_configured_model_entries("openai-codex", local=False):
                models.append({
                    "id": item["id"],
                    "label": item["label"],
                    "provider": "openai-codex",
                    "local": False,
                })
        if "ollama" in self._providers:
            for item in self._provider_configured_model_entries("ollama", local=True):
                models.append({
                    "id": item["id"],
                    "label": item["label"],
                    "provider": "ollama",
                    "local": True,
                })
        if "vmlx" in self._providers:
            for item in self._provider_configured_model_entries("vmlx", local=True):
                models.append({
                    "id": item["id"],
                    "label": item["label"],
                    "provider": "vmlx",
                    "local": True,
                })
        if "omlx" in self._providers:
            for item in self._provider_configured_model_entries("omlx", local=True):
                models.append({
                    "id": item["id"],
                    "label": item["label"],
                    "provider": "omlx",
                    "local": True,
                })
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

    def _manual_prediction(
        self,
        *,
        topology: str,
        agent_models: dict[str, str],
        model_pin: str,
    ) -> dict:
        """Synthesize a topology-prediction payload for manual mode.

        When the caller drives both the topology and the per-agent model
        pins, we skip the classifier entirely. This returns the same shape
        ``predict_topology`` would have produced so the rest of
        ``start_run`` stays on one code path.
        """
        from orb.topologies import normalize_topology_id
        topology_id = normalize_topology_id(topology)
        topo = self._available_topologies().get(topology_id)
        label = topo.label if topo else topology_id
        description = topo.description if topo else ""
        # Build the agent_assignments dict the runtime expects
        agent_assignments: dict[str, dict] = {}
        for role, model_id in (agent_models or {}).items():
            if not role or not model_id:
                continue
            agent_assignments[str(role).strip()] = {
                "provider": "",  # resolved later in _validate_agent_model_assignments
                "model": str(model_id).strip(),
            }
        return {
            "topology": topology_id,
            "label": label,
            "description": description,
            "options": self._topology_options(topology_id),
            "task_type": "manual",
            "reason": "Manual mode — caller supplied topology and per-agent models.",
            "summary": "Manual topology + model selection",
            "signals": {"manual": True},
            "candidates": [],
            "escalation_allowed": False,
            "stop_early_allowed": False,
            "escalation_reason": "",
            "stop_early_reason": "Manual run",
            "requested_topology": topology_id,
            "routing_mode": "manual",
            "classifier_model": "",
            "classifier_provider": "",
            "complexity": 50,
            "agent_complexity": {},
            "agent_assignments": agent_assignments,
            "agent_models": {role: data.get("model", "") for role, data in agent_assignments.items()},
            "model_pin": model_pin,
        }

    async def predict_topology(
        self,
        query: str,
        model_pin: str = "auto",
        requested_topology: str = "auto",
    ) -> dict:
        available_topologies = self._available_topologies()
        if not query:
            first_id, topo = next(iter(available_topologies.items()))
            return {
                "topology": first_id,
                "label": topo.label,
                "description": topo.description,
                "options": self._topology_options(first_id),
                "task_type": "simple_direct",
                "reason": "Empty query defaults to the first approved topology.",
                "summary": "No task provided yet",
                "signals": {"word_count": 0},
                "candidates": [],
                "escalation_allowed": False,
                "stop_early_allowed": True,
                "escalation_reason": "",
                "stop_early_reason": "No task provided yet.",
                "requested_topology": requested_topology,
                "routing_mode": "default",
                "classifier_model": "",
                "classifier_provider": "",
                "complexity": 0,
                "agent_complexity": {},
                "agent_models": {},
                "agent_assignments": {},
            }

        classification = await self._topology_classifier.classify(
            query=query,
            requested_topology=requested_topology,
            model_pin=model_pin,
            topologies=available_topologies,
        )
        topology_id = classification.topology_id
        complexity = classification.complexity
        agent_complexity = self._derive_agent_complexity(topology_id, complexity)
        heuristic_map = self._build_agent_model_map(
            complexity,
            model_pin=model_pin,
            agent_complexity=agent_complexity,
            topology_id=topology_id,
        )
        agent_model_map, _agent_model_reasons = await self._llm_assign_agent_models(
            query,
            topology_id,
            complexity,
            agent_complexity,
            heuristic_map,
        )
        options = self._topology_options(
            topology_id,
            candidates=[
                {
                    "topology": candidate.topology_id,
                    "score": candidate.score,
                    "reason": candidate.reason,
                }
                for candidate in classification.candidates
            ],
        )
        return {
            "topology": topology_id,
            "label": classification.label,
            "description": classification.description,
            "options": options,
            "task_type": classification.task_type,
            "reason": classification.reason,
            "summary": classification.summary,
            "signals": dict(classification.signals),
            "candidates": [
                {
                    "topology": candidate.topology_id,
                    "score": candidate.score,
                    "reason": candidate.reason,
                }
                for candidate in classification.candidates
            ],
            "escalation_allowed": classification.escalation_allowed,
            "stop_early_allowed": classification.stop_early_allowed,
            "escalation_reason": classification.escalation_reason,
            "stop_early_reason": classification.stop_early_reason,
            "requested_topology": classification.requested_topology,
            "routing_mode": classification.routing_mode,
            "classifier_model": classification.classifier_model,
            "classifier_provider": classification.classifier_provider,
            "complexity": complexity,
            "agent_complexity": agent_complexity,
            "agent_models": {role: cfg.model_id for role, cfg in agent_model_map.items()},
            "agent_assignments": {
                role: {"provider": cfg.provider, "model": cfg.model_id}
                for role, cfg in agent_model_map.items()
            },
        }

    def _derive_agent_complexity(self, topology_id: str, complexity: int) -> dict[str, int]:
        topo = self._available_topologies().get(topology_id)
        if topo is None:
            return {}
        derived: dict[str, int] = {}
        for agent_id, agent in topo.agents.items():
            blended = int(round((int(agent.base_complexity) * 0.55) + (int(complexity) * 0.45)))
            derived[agent_id] = max(0, min(100, blended))
        return derived

    def _build_agent_model_map(
        self,
        complexity: int,
        model_pin: str = "auto",
        agent_complexity: dict | None = None,
        topology_id: str = "triad",
    ) -> dict:
        from orb.llm.types import (
            ANTHROPIC_PROVIDER,
            ModelConfig,
            ModelTier,
            OPENAI_CODEX_PROVIDER,
            OLLAMA_PROVIDER,
            OMLX_PROVIDER,
            VMLX_PROVIDER,
        )

        has_ollama = "ollama" in self._providers
        has_vmlx = "vmlx" in self._providers
        has_omlx = "omlx" in self._providers
        has_anthropic = "anthropic" in self._providers
        has_codex = "openai-codex" in self._providers

        def local(provider: str, tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider=provider)

        def ant(tier: ModelTier, model_id: str) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=model_id, provider=ANTHROPIC_PROVIDER)

        force_provider: str | None = None
        if model_pin and model_pin != "auto":
            if "claude" in model_pin:
                force_provider = "anthropic"
            elif model_pin.startswith("gpt-5.4"):
                force_provider = "openai-codex"
            elif "qwen" in model_pin or "llama" in model_pin:
                force_provider = "ollama" if has_ollama else "omlx" if has_omlx else "vmlx" if has_vmlx else "ollama"

        provider_available = {
            "anthropic": has_anthropic,
            "openai-codex": has_codex,
            "ollama": has_ollama,
            "vmlx": has_vmlx,
            "omlx": has_omlx,
        }
        if force_provider and not provider_available.get(force_provider):
            logger.warning("Forced provider '%s' not available; falling back to auto", force_provider)
            force_provider = None

        anthropic_haiku = self._provider_default_model("anthropic", "cloud_lite")
        anthropic_sonnet = self._provider_default_model("anthropic", "cloud_fast")
        anthropic_opus = self._provider_default_model("anthropic", "cloud_strong")
        codex_default = self._provider_default_model("openai-codex", "cloud_fast")
        ollama_small = self._provider_default_model("ollama", "local_small")
        ollama_medium = self._provider_default_model("ollama", "local_medium")
        vmlx_small = self._provider_default_model("vmlx", "local_small")
        vmlx_medium = self._provider_default_model("vmlx", "local_medium")
        omlx_small = self._provider_default_model("omlx", "local_small")
        omlx_medium = self._provider_default_model("omlx", "local_medium")
        use_ant = has_anthropic and force_provider in (None, "anthropic")
        use_codex = has_codex and force_provider in (None, "openai-codex")
        q9 = local(OLLAMA_PROVIDER, ModelTier.LOCAL_SMALL, ollama_small) if has_ollama and force_provider in (None, "ollama") else None
        q27 = local(OLLAMA_PROVIDER, ModelTier.LOCAL_MEDIUM, ollama_medium) if has_ollama and force_provider in (None, "ollama") else None
        vsmall = local(VMLX_PROVIDER, ModelTier.LOCAL_SMALL, vmlx_small) if has_vmlx and force_provider in (None, "vmlx") else None
        vmedium = local(VMLX_PROVIDER, ModelTier.LOCAL_MEDIUM, vmlx_medium) if has_vmlx and force_provider in (None, "vmlx") else None
        osmall = local(OMLX_PROVIDER, ModelTier.LOCAL_SMALL, omlx_small) if has_omlx and force_provider in (None, "omlx") else None
        omedium = local(OMLX_PROVIDER, ModelTier.LOCAL_MEDIUM, omlx_medium) if has_omlx and force_provider in (None, "omlx") else None

        def codex(tier: ModelTier) -> ModelConfig:
            return ModelConfig(tier=tier, model_id=codex_default, provider=OPENAI_CODEX_PROVIDER)

        haiku = (ant(ModelTier.CLOUD_LITE, anthropic_haiku) if use_ant else
                 codex(ModelTier.CLOUD_LITE) if use_codex else None)
        sonnet = (ant(ModelTier.CLOUD_FAST, anthropic_sonnet) if use_ant else
                  codex(ModelTier.CLOUD_FAST) if use_codex else None)
        opus = (ant(ModelTier.CLOUD_STRONG, anthropic_opus) if use_ant else
                codex(ModelTier.CLOUD_STRONG) if use_codex else None)

        def best(*choices):
            return next((c for c in choices if c is not None), None)

        def pick(score: int):
            if score <= 25:
                return best(q9, osmall, vsmall, haiku, q27, omedium, vmedium, sonnet, opus)
            if score <= 50:
                return best(q27, omedium, vmedium, haiku, sonnet, q9, osmall, vsmall, opus)
            if score <= 70:
                return best(sonnet, haiku, q27, omedium, vmedium, opus)
            if score <= 75:
                return best(sonnet, haiku, opus)
            return best(opus, sonnet)

        def pick_for_role(category: str, score: int):
            if category == "entry":
                if score <= 35:
                    return best(q9, osmall, vsmall, haiku, q27, omedium, vmedium, sonnet, opus)
                return best(q27, omedium, vmedium, haiku, sonnet, q9, osmall, vsmall, opus)
            if category == "implementation":
                if score <= 35:
                    return best(q27, omedium, vmedium, sonnet, haiku, opus)
                if score <= 70:
                    return best(sonnet, q27, omedium, vmedium, haiku, opus)
                return best(opus, sonnet)
            if category == "review":
                if score <= 35:
                    return best(q27, omedium, vmedium, sonnet, haiku, opus)
                if score <= 70:
                    return best(sonnet, q27, omedium, vmedium, haiku, opus)
                return best(opus, sonnet)
            if category == "validation":
                if score <= 30:
                    return best(q9, osmall, vsmall, haiku, q27, omedium, vmedium, sonnet, opus)
                if score <= 50:
                    return best(q27, omedium, vmedium, haiku, sonnet, q9, osmall, vsmall, opus)
                return best(sonnet, q27, omedium, vmedium, haiku, opus)
            if category == "discovery":
                if score <= 40:
                    return best(q27, omedium, vmedium, sonnet, haiku, opus)
                if score <= 75:
                    return best(sonnet, q27, omedium, vmedium, haiku, opus)
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
                alt_candidates = [c for c in [opus, sonnet, haiku, q27, vmedium, q9, vsmall] if c is not None]
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
        from orb.llm.types import ModelTier, ModelConfig

        if "openai-codex" in self._providers:
            model_id = self._provider_default_model("openai-codex", "cloud_fast")
            if self._provider_model_enabled("openai-codex", model_id):
                return ModelConfig(ModelTier.CLOUD_FAST, model_id, "openai-codex")
        if "anthropic" in self._providers:
            model_id = self._provider_default_model("anthropic", "cloud_fast")
            if self._provider_model_enabled("anthropic", model_id):
                return ModelConfig(ModelTier.CLOUD_FAST, model_id, "anthropic")
        return None

    def _planner_model_config(self):
        from orb.llm.types import DEFAULT_MODELS, ModelConfig, ModelTier

        candidates: list[tuple[int, int, ModelConfig]] = []
        if "omlx" in self._providers:
            model_id = self._provider_default_model("omlx", "local_small")
            if self._provider_model_enabled("omlx", model_id):
                candidates.append((0, 0, ModelConfig(ModelTier.LOCAL_SMALL, model_id, "omlx")))
        if "vmlx" in self._providers:
            model_id = self._provider_default_model("vmlx", "local_small")
            if self._provider_model_enabled("vmlx", model_id):
                candidates.append((0, 1, ModelConfig(ModelTier.LOCAL_SMALL, model_id, "vmlx")))
        if "ollama" in self._providers:
            model_id = self._provider_default_model("ollama", "local_small")
            if self._provider_model_enabled("ollama", model_id):
                candidates.append((0, 2, ModelConfig(ModelTier.LOCAL_SMALL, model_id, "ollama")))
        if "openai-codex" in self._providers:
            model_id = self._provider_default_model("openai-codex", "cloud_lite")
            if self._provider_model_enabled("openai-codex", model_id):
                candidates.append((1, 0, ModelConfig(ModelTier.CLOUD_LITE, model_id, "openai-codex")))
        if "anthropic" in self._providers:
            model_id = self._provider_default_model("anthropic", "cloud_lite")
            if self._provider_model_enabled("anthropic", model_id):
                candidates.append((1, 1, ModelConfig(ModelTier.CLOUD_LITE, model_id, "anthropic")))
        if "openai" in self._providers:
            candidates.append((1, 2, ModelConfig(ModelTier.CLOUD_LITE, "gpt-4o", "openai")))
        override_candidates = [
            cfg for cfg in (
                (self._model_overrides or {}).get(ModelTier.LOCAL_SMALL),
                (self._model_overrides or {}).get(ModelTier.CLOUD_LITE),
                (self._model_overrides or {}).get(ModelTier.CLOUD_FAST),
                DEFAULT_MODELS.get(ModelTier.LOCAL_SMALL),
                DEFAULT_MODELS.get(ModelTier.CLOUD_LITE),
                DEFAULT_MODELS.get(ModelTier.CLOUD_FAST),
            )
            if cfg is not None and getattr(cfg, "provider", None) in self._providers
        ]
        for cfg in override_candidates:
            tier = getattr(cfg, "tier", ModelTier.CLOUD_LITE)
            bucket = 0 if tier == ModelTier.LOCAL_SMALL else 1
            candidates.append((bucket, 99, cfg))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _available_model_choices(self) -> list[dict]:
        choices: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in self.models_payload().get("models", []):
            if not isinstance(item, dict):
                continue
            provider_name = str(item.get("provider") or "")
            model_id = str(item.get("id") or "")
            if not provider_name or not model_id or provider_name == "auto" or provider_name not in self._providers:
                continue
            key = (provider_name, model_id)
            if key in seen:
                continue
            seen.add(key)
            label = str(item.get("label") or model_id)
            description = "enabled local model" if item.get("local") else "enabled cloud model"
            choices.append({
                "provider": provider_name,
                "model": model_id,
                "label": label,
                "description": description,
                "local": bool(item.get("local")),
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

    async def _run_orchestrator(
        self,
        query: str,
        topology: str,
        initial_target: str = "coordinator",
        model_pin: str = "auto",
        complexity: int = 50,
        agent_complexity: dict | None = None,
        agent_model_map: dict | None = None,
        trace_recorder: RunTrace | None = None,
    ) -> None:
        from web.bridge import DashboardBridge
        from web.state import ActivityRecord

        self._turn_count += 1
        bridge = DashboardBridge(self.state, self._broadcast, persist_state=self._persist_dashboard_snapshot)
        effective_overrides = dict(self._model_overrides or {})
        agent_model_map = agent_model_map or self._build_agent_model_map(
            complexity, model_pin, agent_complexity, topology_id=topology
        )
        trace_recorder = trace_recorder or RunTrace(
            session_id=self._conversation_session.session_id,
            metadata={"session_id": self._conversation_session.session_id},
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
            trace_recorder=trace_recorder,
            tier_override=self._tier_override,
            agent_model_map=agent_model_map or None,
        )
        self._last_trace = trace_recorder
        self._last_trace.record_stage_start(
            "execution",
            message="orchestrator execution started",
            data={"topology_id": topology, "entry_agent": initial_target},
        )
        self._persist_run_trace()

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

        async def on_agent_activity(agent_id: str, activity: str, details: dict | None = None) -> None:
            elapsed = round(time.time() - self.state.start_time, 2)
            self.state.activity_events.append(ActivityRecord(
                agent=agent_id,
                activity=activity,
                elapsed=elapsed,
                details=dict(details or {}),
            ))
            if len(self.state.activity_events) > 100:
                self.state.activity_events = self.state.activity_events[-100:]
            self._persist_dashboard_snapshot()
            detail_summary = ""
            if details:
                target = str(details.get("to") or "")
                payload = str(details.get("content") or details.get("payload") or "")
                if target:
                    detail_summary = f" to={target}"
                if payload:
                    detail_summary += f" payload={payload[:160].replace(chr(10), ' ')}"
            logger.info(
                "dashboard event=agent_activity agent=%s elapsed=%.2fs activity=%s%s",
                agent_id,
                elapsed,
                activity,
                detail_summary,
            )
            await self._broadcast(json.dumps({
                "type": "agent_activity",
                "agent": agent_id,
                "activity": activity,
                "elapsed": elapsed,
                "details": dict(details or {}),
            }))

        async def on_agent_heartbeat(agent_id: str, payload: dict) -> None:
            await bridge.on_agent_heartbeat(agent_id, payload)

        for agent in orchestrator.agents.values():
            agent._on_activity = on_agent_activity
            agent._on_heartbeat = on_agent_heartbeat
            agent._shared_transcript = self._run_transcript

        def _make_file_write_cb(aid: str):
            def cb(_, path: str, content: str, old_content: str = "") -> None:
                self._persist_dashboard_file_change(
                    path=path,
                    agent=aid,
                    content=content,
                    old_content=old_content,
                )
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
            self._persist_dashboard_snapshot(run_active=False)
            if final_result:
                logger.info(
                    "run complete session=%s agent=%s elapsed=%.2fs routed=%s result=%s",
                    self._conversation_session.session_id,
                    final_agent_id or "",
                    elapsed,
                    self.state.message_count,
                    final_result[:240].replace("\n", " "),
                )
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

        if self._last_trace is not None:
            self._last_trace.record_stage_finish(
                "execution",
                status="ok" if result else "error",
                message="orchestrator execution finished",
                data={"completed": bool(result), "message_count": self.state.message_count},
            )
            self._persist_run_trace()
        self._persist_dashboard_snapshot(run_active=False)
        self._last_result = result
