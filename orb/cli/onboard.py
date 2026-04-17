"""Interactive `orb onboard` flow.

One guided setup that covers every provider Orb supports: pick provider →
auth (if cloud) / reachability probe (if local) → refresh catalog →
toggle enabled models → pick default per tier → save to
`~/.orb/config.json`. Composes the lower-level building blocks in
`orb.cli.auth`, `orb.cli.config`, and the runtime's catalog-refresh RPC.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import auth as auth_cli
from . import config as config_cli


PROVIDER_META: dict[str, dict[str, Any]] = {
    "anthropic": {
        "kind": "cloud",
        "label": "Anthropic (Claude)",
        "tiers": ("cloud_lite", "cloud_fast", "cloud_strong"),
    },
    "openai-codex": {
        "kind": "cloud",
        "label": "OpenAI Codex",
        "tiers": ("cloud_lite", "cloud_fast", "cloud_strong"),
    },
    "ollama": {
        "kind": "local",
        "label": "Ollama",
        "tiers": ("local_small", "local_medium", "local_large"),
    },
    "vmlx": {
        "kind": "local",
        "label": "vLLM (OpenAI-compat)",
        "tiers": ("local_small", "local_medium", "local_large"),
    },
    "omlx": {
        "kind": "local",
        "label": "OpenAI-compatible (omlx)",
        "tiers": ("local_small", "local_medium", "local_large"),
    },
}

PROVIDER_ORDER: list[str] = list(PROVIDER_META.keys())


# ── tiny input helpers ───────────────────────────────────────────────────────

def _prompt(msg: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        raw = ""
    return raw or default


# ── local reachability probe ─────────────────────────────────────────────────

def _resolve_local_endpoint(provider: str, cfg: dict[str, Any]) -> str:
    """Resolve a local provider's base URL, preferring the saved config over
    the registry default."""
    from orb.llm.registry import _ollama_base_url, _vmlx_base_url, _omlx_base_url

    providers = cfg.get("providers") or {}
    entry = providers.get(provider) or {}
    configured = str(entry.get("base_url") or "").strip()
    if configured:
        return configured.rstrip("/")

    fallbacks = {
        "ollama": _ollama_base_url,
        "vmlx": _vmlx_base_url,
        "omlx": _omlx_base_url,
    }
    fn = fallbacks.get(provider)
    return fn().rstrip("/") if fn else ""


def _probe_local(provider: str, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Return `(reachable, probe_url)` for a local provider.

    Hits the same catalog endpoint the runtime uses when refreshing
    (ollama = `/api/tags`, vmlx/omlx = `/v1/models`). A 2s timeout keeps the
    provider list snappy even when a server is down.
    """
    if PROVIDER_META.get(provider, {}).get("kind") != "local":
        return False, ""
    base = _resolve_local_endpoint(provider, cfg or config_cli.load_config())
    if not base:
        return False, ""

    root = base[:-3].rstrip("/") if base.endswith("/v1") else base

    if provider == "ollama":
        probe_url = f"{root}/api/tags"
    else:  # vmlx, omlx — OpenAI-compatible
        probe_url = f"{root}/v1/models"

    try:
        resp = httpx.get(probe_url, timeout=2.0)
        return resp.status_code < 500, probe_url
    except Exception:
        return False, probe_url


# ── auth / reachability status ───────────────────────────────────────────────

def _auth_status(provider: str, cfg: dict[str, Any] | None = None) -> str:
    meta = PROVIDER_META[provider]
    if meta["kind"] == "local":
        reachable, endpoint = _probe_local(provider, cfg)
        if reachable and endpoint:
            return f"running at {endpoint}"
        if endpoint:
            return f"not reachable ({endpoint})"
        return "local"
    if provider == "anthropic":
        return "authed" if auth_cli.get_anthropic_key() else "not authed"
    if provider == "openai-codex":
        creds = auth_cli.load_credentials("openai") or {}
        return "authed" if (creds.get("api_key") or creds.get("access_token")) else "not authed"
    return "unknown"


def _print_providers(cfg: dict[str, Any]) -> None:
    providers = cfg.get("providers") or {}
    print("\nAvailable providers:")
    for i, name in enumerate(PROVIDER_ORDER, 1):
        meta = PROVIDER_META[name]
        entry = providers.get(name) or {}
        enabled = bool(entry.get("enabled"))
        mark = "✓" if enabled else " "
        status = _auth_status(name, cfg)
        print(f"  [{mark}] {i}. {name:<13} {meta['kind']:<6} · {status}")


# ── provider selection ───────────────────────────────────────────────────────

def _pick_provider() -> str | None:
    """Prompt until the user picks a known provider or quits."""
    while True:
        raw = _prompt("Pick a provider (number, name, or q to quit)")
        lowered = raw.lower()
        if lowered in {"q", "quit", "exit"}:
            return None
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(PROVIDER_ORDER):
                return PROVIDER_ORDER[idx]
            print(f"  Out of range: {raw}")
            continue
        if raw in PROVIDER_ORDER:
            return raw
        print(f"  Unknown selection: {raw!r}")


# ── auth / reachability gate ─────────────────────────────────────────────────

async def _ensure_authed(provider: str) -> bool:
    meta = PROVIDER_META[provider]
    if meta["kind"] == "local":
        reachable, endpoint = _probe_local(provider)
        if reachable:
            print(f"{provider}: reachable at {endpoint}")
            return True
        if endpoint:
            print(f"{provider}: not reachable at {endpoint}. Start the server and try again.")
        else:
            print(f"{provider}: no base_url configured; set it in ~/.orb/config.json first.")
        proceed = _prompt("Proceed anyway (catalog will likely be empty)? [y/N]", default="n").lower()
        return proceed in {"y", "yes"}

    if _auth_status(provider) == "authed":
        return True
    choice = _prompt(f"{provider} is not authenticated. Run auth now? [Y/n]", default="y").lower()
    if choice not in {"y", "yes"}:
        return False
    if provider == "anthropic":
        await auth_cli.auth_anthropic()
    elif provider == "openai-codex":
        await auth_cli.auth_openai()
    return _auth_status(provider) == "authed"


# ── catalog refresh ──────────────────────────────────────────────────────────

async def _refresh_catalog() -> None:
    """Ask the runtime to refetch every provider's model catalog.

    Imports live inside the function so the module stays light for tests
    (which monkeypatch `_refresh_catalog` to a no-op).
    """
    from web.state import DashboardState
    from orb.runtime.graph_runtime import GraphRuntime
    from orb.llm.registry import build_providers
    from orb.orchestrator.types import OrchestratorConfig

    runtime = GraphRuntime(DashboardState())
    runtime.configure(
        providers=build_providers(local_only=False, cloud_only=False),
        config=OrchestratorConfig(timeout=60.0, budget=200, max_depth=10),
        model_overrides=None,
        tier_override=None,
    )
    await runtime.refresh_provider_catalogs()


# ── model toggle ─────────────────────────────────────────────────────────────

def _toggle_models(
    provider: str,
    catalog: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, dict[str, bool]]:
    """Interactive checkbox for catalog models.

    Returns a new `models` map ({id: {"enabled": bool}}) for the provider.
    """
    providers = cfg.get("providers") or {}
    current = (providers.get(provider) or {}).get("models") or {}

    state: dict[str, bool] = {}
    for item in catalog:
        mid = str(item.get("id") or "")
        if not mid:
            continue
        cur = current.get(mid)
        if isinstance(cur, dict):
            state[mid] = bool(cur.get("enabled", True))
        elif isinstance(cur, bool):
            state[mid] = cur
        else:
            state[mid] = True  # enabled-by-default for new catalog entries

    while True:
        print()
        print(f"Models for {provider} — toggle by number, 'a' to enable all, 'n' to disable all, Enter to confirm.")
        for i, item in enumerate(catalog, 1):
            mid = str(item.get("id") or "")
            label = item.get("label") or mid
            mark = "✓" if state.get(mid, False) else " "
            print(f"  [{mark}] {i:>2}. {label}")

        raw = _prompt("Toggle")
        if not raw:
            break
        if raw.lower() == "a":
            for mid in state:
                state[mid] = True
            continue
        if raw.lower() == "n":
            for mid in state:
                state[mid] = False
            continue
        for token in raw.split():
            if not token.isdigit():
                continue
            idx = int(token) - 1
            if 0 <= idx < len(catalog):
                mid = str(catalog[idx].get("id") or "")
                if mid:
                    state[mid] = not state.get(mid, False)

    return {mid: {"enabled": enabled} for mid, enabled in state.items()}


# ── tier defaults ────────────────────────────────────────────────────────────

def _pick_defaults(
    provider: str,
    catalog: list[dict[str, Any]],
    current_defaults: dict[str, Any],
) -> dict[str, str]:
    meta = PROVIDER_META[provider]
    enabled_ids = [str(item.get("id") or "") for item in catalog if item.get("id")]
    if not enabled_ids:
        return dict(current_defaults)

    print("\nPick defaults per tier (Enter to keep, name or number to change):")
    print(f"  Available: {', '.join(enabled_ids)}")

    new_defaults = {str(k): str(v) for k, v in (current_defaults or {}).items() if v}
    for tier in meta["tiers"]:
        suggested = new_defaults.get(tier) if new_defaults.get(tier) in enabled_ids else enabled_ids[0]
        raw = _prompt(f"  {tier}", default=suggested)
        if raw in enabled_ids:
            new_defaults[tier] = raw
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(enabled_ids):
                new_defaults[tier] = enabled_ids[idx]
                continue
        new_defaults[tier] = suggested
    return new_defaults


# ── per-provider driver ──────────────────────────────────────────────────────

async def configure_provider(provider: str) -> None:
    if provider not in PROVIDER_META:
        print(f"  Unknown provider: {provider!r}")
        return

    if not await _ensure_authed(provider):
        print(f"Skipped {provider}.")
        return

    print(f"\nRefreshing catalog for {provider}…")
    try:
        await _refresh_catalog()
    except Exception as exc:  # pragma: no cover - surfaced to the user
        print(f"  catalog refresh failed: {exc}")

    cfg = config_cli.load_config()
    providers = cfg.get("providers") or {}
    entry = dict(providers.get(provider) or {})
    catalog = entry.get("catalog") or []

    if not catalog:
        print(f"  No catalog entries found for {provider}. Is it reachable / authed?")
        return

    entry["enabled"] = True
    models_map = _toggle_models(provider, catalog, cfg)
    entry["models"] = models_map

    enabled_catalog = [
        item for item in catalog
        if models_map.get(str(item.get("id") or ""), {}).get("enabled", False)
    ]
    if not enabled_catalog:
        print("  No models selected; leaving provider disabled.")
        entry["enabled"] = False
    else:
        entry["default_models"] = _pick_defaults(
            provider, enabled_catalog, entry.get("default_models") or {}
        )

    providers[provider] = entry
    cfg["providers"] = providers
    config_cli.save_config(cfg)

    enabled_count = sum(1 for v in models_map.values() if v.get("enabled"))
    defaults = entry.get("default_models") or {}
    summary = ", ".join(f"{k}={v}" for k, v in defaults.items()) or "—"
    print(f"\n  Saved: {provider} enabled={entry['enabled']} · "
          f"{enabled_count}/{len(catalog)} models · defaults: {summary}")


# ── top-level entry point ────────────────────────────────────────────────────

async def run_onboarding() -> None:
    print("Orb onboarding — interactive provider / model setup")
    while True:
        cfg = config_cli.load_config()
        _print_providers(cfg)
        provider = _pick_provider()
        if not provider:
            break
        await configure_provider(provider)

        again = _prompt("\nConfigure another provider? [y/N]", default="n").lower()
        if again not in {"y", "yes"}:
            break
    print("\nDone. Run `orb config show` to review, or restart the daemon to apply.")
