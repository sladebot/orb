from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".orb" / "config.json"

# Every provider ships disabled — `orb onboard` is what flips an entry to
# `enabled: True` after the user authenticates (cloud) or we confirm the
# server is reachable (local). The `models` and `default_models` maps are
# kept as suggestions that pre-fill the onboard toggle / tier prompts; they
# are dormant until the provider is enabled.
_DEFAULTS: dict[str, Any] = {
    "local_models": True,
    "tui": {"theme": "orb-dark"},
    "providers": {
        "anthropic": {
            "enabled": False,
            "models": {
                "claude-haiku-4-5-20251001": {"enabled": True},
                "claude-sonnet-4-20250514": {"enabled": False},
                "claude-opus-4-20250514": {"enabled": False},
            },
            "default_models": {
                "cloud_lite": "claude-haiku-4-5-20251001",
                "cloud_fast": "claude-haiku-4-5-20251001",
                "cloud_strong": "claude-haiku-4-5-20251001",
            },
        },
        "openai-codex": {
            "enabled": False,
            "models": {},
            "default_models": {
                "cloud_lite": "gpt-5.4-mini",
                "cloud_fast": "gpt-5.5",
                "cloud_strong": "gpt-5.5",
            },
        },
        "ollama": {
            "enabled": False,
            "models": {},
        },
        "vmlx": {
            "enabled": False,
            "models": {},
            "base_url": "http://localhost:1234/v1",
        },
        "omlx": {
            "enabled": False,
            "models": {},
            "base_url": "http://localhost:8000/v1",
        },
    },
}

_BOOL_TRUE  = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _canonical_model_id(provider: str, model_id: str) -> str:
    model_id = str(model_id or "").strip()
    if not model_id:
        return ""
    aliases = {
        "anthropic": {
            "claude-sonnet-4-6": "claude-sonnet-4-20250514",
            "claude-opus-4-6": "claude-opus-4-20250514",
        },
        "ollama": {
            "qwen3.5-9b-4k:latest": "qwen3.5:9b",
            "qwen3.5-27b-4k:latest": "qwen3.5:27b",
        },
    }
    return aliases.get(provider, {}).get(model_id, model_id)


def _skip_model_id(provider: str, model_id: str) -> bool:
    return False


def load_config() -> dict[str, Any]:
    cfg = dict(_DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
            cfg.update(raw)
        except Exception:
            pass
    cfg["providers"] = _normalized_provider_config(cfg.get("providers"), cfg.get("preferred_providers"))
    return cfg


def _normalized_provider_config(
    providers: Any,
    preferred_providers: Any = None,
) -> dict[str, dict[str, bool]]:
    defaults = {
        name: dict(value)
        for name, value in _DEFAULTS["providers"].items()
    }

    if isinstance(providers, dict):
        normalized = {name: dict(value) for name, value in defaults.items()}
        for name, value in providers.items():
            if name not in normalized:
                continue
            if isinstance(value, dict):
                for extra_key, extra_value in value.items():
                    if extra_key == "enabled":
                        normalized[name]["enabled"] = bool(extra_value)
                    elif extra_key == "models":
                        normalized[name]["models"] = _normalized_model_config(name, extra_value)
                    elif extra_key == "default_models":
                        normalized[name]["default_models"] = _normalized_default_models(name, extra_value)
                    elif extra_key == "catalog":
                        normalized[name]["catalog"] = _normalized_catalog(name, extra_value)
                    else:
                        normalized[name][extra_key] = extra_value
            elif isinstance(value, bool):
                normalized[name]["enabled"] = value
        return normalized

    if isinstance(preferred_providers, list) and preferred_providers:
        preferred = {str(item) for item in preferred_providers}
        normalized = {name: {"enabled": name in preferred} for name in defaults}
        if any(item["enabled"] for item in normalized.values()):
            return normalized

    return {
        name: _normalized_provider_defaults(name, value)
        for name, value in defaults.items()
    }


def _normalized_provider_defaults(provider: str, value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    normalized["models"] = _normalized_model_config(provider, normalized.get("models"))
    if "default_models" in normalized:
        normalized["default_models"] = _normalized_default_models(provider, normalized.get("default_models"))
    if "catalog" in normalized:
        normalized["catalog"] = _normalized_catalog(provider, normalized.get("catalog"))
    return normalized


def _normalized_model_config(provider: str, models: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(models, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for model_id, value in models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        if _skip_model_id(provider, model_id):
            continue
        key = _canonical_model_id(provider, model_id)
        if isinstance(value, bool):
            normalized[key] = {"enabled": value}
            continue
        if not isinstance(value, dict):
            continue
        entry = dict(value)
        entry["enabled"] = bool(entry.get("enabled", True))
        normalized[key] = entry
    return normalized


def _normalized_default_models(provider: str, default_models: Any) -> dict[str, str]:
    if not isinstance(default_models, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, model_id in default_models.items():
        raw = str(model_id or "")
        if provider == "ollama" and raw == "qwen3.5-9b-4k:latest":
            canonical = "qwen3.5:9b"
        elif provider == "ollama" and raw == "qwen3.5-27b-4k:latest":
            canonical = "qwen3.5:27b"
        else:
            canonical = _canonical_model_id(provider, raw)
        if canonical:
            normalized[str(key)] = canonical
    return normalized


def _normalized_catalog(provider: str, catalog: Any) -> list[dict[str, Any]]:
    if not isinstance(catalog, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in catalog:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or "")
        if _skip_model_id(provider, raw_id):
            continue
        canonical_id = _canonical_model_id(provider, raw_id)
        if not canonical_id or canonical_id in seen:
            continue
        entry = dict(item)
        entry["id"] = canonical_id
        if not entry.get("label"):
            entry["label"] = canonical_id
        normalized.append(entry)
        seen.add(canonical_id)
    return normalized


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(0o600)


def get(key: str) -> Any:
    return load_config().get(key, _DEFAULTS.get(key))


def provider_model_enabled(provider: str, model_id: str, *, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or load_config()
    providers = cfg.get("providers") if isinstance(cfg, dict) else {}
    entry = providers.get(provider) or {} if isinstance(providers, dict) else {}
    models = entry.get("models") or {} if isinstance(entry, dict) else {}
    canonical = _canonical_model_id(provider, model_id)
    if not canonical:
        return False
    if not isinstance(models, dict) or not models:
        return True
    value = models.get(canonical)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return True


def provider_catalog(provider: str, *, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    providers = cfg.get("providers") if isinstance(cfg, dict) else {}
    entry = providers.get(provider) or {} if isinstance(providers, dict) else {}
    catalog = entry.get("catalog") or [] if isinstance(entry, dict) else []
    filtered: list[dict[str, Any]] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        model_id = _canonical_model_id(provider, item.get("id") or "")
        if not model_id or not provider_model_enabled(provider, model_id, cfg=cfg):
            continue
        normalized = dict(item)
        normalized["id"] = model_id
        if not normalized.get("label"):
            normalized["label"] = model_id
        filtered.append(normalized)
    return filtered


def provider_default_model(provider: str, key: str) -> str:
    cfg = load_config()
    providers = cfg.get("providers") if isinstance(cfg, dict) else {}
    entry = providers.get(provider) or {} if isinstance(providers, dict) else {}
    defaults = entry.get("default_models") or {} if isinstance(entry, dict) else {}
    candidate = _canonical_model_id(provider, defaults.get(key) or "")
    if candidate and provider_model_enabled(provider, candidate, cfg=cfg):
        return candidate

    catalog = provider_catalog(provider, cfg=cfg)
    if catalog:
        return str(catalog[0].get("id") or "")

    models = entry.get("models") or {} if isinstance(entry, dict) else {}
    for model_id in models:
        canonical = _canonical_model_id(provider, model_id)
        if canonical and provider_model_enabled(provider, canonical, cfg=cfg):
            return canonical

    return ""


def set_value(key: str, value: str) -> None:
    if key not in _DEFAULTS:
        raise KeyError(f"Unknown config key: {key!r}")
    cfg = load_config()
    expected_type = type(_DEFAULTS[key])
    if expected_type is bool:
        lo = value.lower()
        if lo in _BOOL_TRUE:
            cfg[key] = True
        elif lo in _BOOL_FALSE:
            cfg[key] = False
        else:
            raise ValueError(f"Expected boolean (true/false/on/off), got: {value!r}")
    elif expected_type is int:
        cfg[key] = int(value)
    else:
        cfg[key] = value
    save_config(cfg)


def set_config_value(key: str, value: Any) -> None:
    if key not in _DEFAULTS:
        raise KeyError(f"Unknown config key: {key!r}")
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def local_models_enabled() -> bool:
    return bool(get("local_models"))


def show_config() -> None:
    """Pretty-printed config dump.

    Top-level scalar keys render as an aligned table; each provider gets
    its own block showing enabled state, base_url (for local), model
    catalog with per-model enabled marks, and tier defaults.
    """
    cfg = load_config()

    print()
    print("Orb config — " + str(CONFIG_PATH))
    print()

    print("Settings")
    scalar_keys = [k for k in _DEFAULTS if k != "providers"]
    key_width = max((len(k) for k in scalar_keys), default=0)
    for key in scalar_keys:
        val = cfg.get(key, _DEFAULTS[key])
        source = "config" if key in cfg else "default"
        print(f"  {key:<{key_width}}  =  {_format_scalar(val):<6}  ({source})")

    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict):
        print("\nProviders: (invalid)")
        return

    enabled_providers = [
        (name, providers.get(name) or {})
        for name in _DEFAULTS["providers"]
        if isinstance(providers.get(name), dict) and providers.get(name, {}).get("enabled")
    ]

    if not enabled_providers:
        print("\nProviders: none enabled — run `orb onboard` to configure one.")
        return

    print("\nProviders")
    for name, entry in enabled_providers:
        _print_provider_block(name, entry)


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _print_provider_block(name: str, entry: dict[str, Any]) -> None:
    enabled = bool(entry.get("enabled"))
    mark = "✓" if enabled else " "
    header = f"  [{mark}] {name}"
    extras: list[str] = []
    base_url = entry.get("base_url")
    if base_url:
        extras.append(str(base_url))
    refreshed = entry.get("refreshed_at")
    if isinstance(refreshed, (int, float)) and refreshed > 0:
        extras.append(_format_refreshed(refreshed))
    if extras:
        header += "   " + " · ".join(extras)
    print(header)

    models_map = entry.get("models") if isinstance(entry.get("models"), dict) else {}
    catalog = entry.get("catalog") if isinstance(entry.get("catalog"), list) else []
    defaults = entry.get("default_models") if isinstance(entry.get("default_models"), dict) else {}

    if catalog:
        # Use catalog as the source of model rows so we can show labels;
        # overlay enablement from `models` map.
        print("        models:")
        id_width = max((len(str(item.get("id") or "")) for item in catalog), default=0)
        for item in catalog:
            mid = str(item.get("id") or "")
            if not mid:
                continue
            status_entry = models_map.get(mid)
            if isinstance(status_entry, dict):
                m_enabled = bool(status_entry.get("enabled", True))
            elif isinstance(status_entry, bool):
                m_enabled = status_entry
            else:
                m_enabled = True
            m_mark = "✓" if m_enabled else " "
            label = item.get("label")
            suffix = f"  — {label}" if label and label != mid else ""
            print(f"          [{m_mark}] {mid:<{id_width}}{suffix}")
    elif models_map:
        # No catalog yet (never refreshed); fall back to the enable map.
        print("        models:")
        id_width = max((len(k) for k in models_map), default=0)
        for mid, value in models_map.items():
            if isinstance(value, dict):
                m_enabled = bool(value.get("enabled", True))
            elif isinstance(value, bool):
                m_enabled = value
            else:
                m_enabled = True
            m_mark = "✓" if m_enabled else " "
            print(f"          [{m_mark}] {mid:<{id_width}}")
    else:
        print("        models: (no catalog — run `orb onboard` or `orb models refresh`)")

    if defaults:
        print("        defaults:")
        tier_width = max((len(str(k)) for k in defaults), default=0)
        for tier, model_id in defaults.items():
            print(f"          {str(tier):<{tier_width}}  →  {model_id}")


def _format_refreshed(ts: float) -> str:
    import datetime as _dt
    try:
        return "refreshed " + _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "refreshed ?"
