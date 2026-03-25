from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".orb" / "config.json"

_DEFAULTS: dict[str, Any] = {
    "local_models": True,
    "providers": {
        "anthropic": {
            "enabled": True,
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
            "enabled": True,
            "models": {},
            "default_models": {
                "cloud_lite": "gpt-5.4-mini",
                "cloud_fast": "gpt-5.4-mini",
                "cloud_strong": "gpt-5.4",
            },
        },
        "ollama": {
            "enabled": True,
            "models": {},
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
    cfg = load_config()
    max_len = max((len(k) for k in _DEFAULTS), default=0)
    for key, default in _DEFAULTS.items():
        val = cfg.get(key, default)
        source = "default" if key not in cfg else "config"
        print(f"  {key:<{max_len}}  =  {str(val).lower():<6}  ({source})")
