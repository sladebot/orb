from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".orb" / "config.json"

_DEFAULTS: dict[str, Any] = {
    "local_models": True,
    "providers": {
        "anthropic": {"enabled": True},
        "openai-codex": {"enabled": True},
        "ollama": {"enabled": True},
    },
}

_BOOL_TRUE  = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


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
            if isinstance(value, dict) and "enabled" in value:
                normalized[name]["enabled"] = bool(value["enabled"])
            elif isinstance(value, bool):
                normalized[name]["enabled"] = value
        return normalized

    if isinstance(preferred_providers, list) and preferred_providers:
        preferred = {str(item) for item in preferred_providers}
        normalized = {name: {"enabled": name in preferred} for name in defaults}
        if any(item["enabled"] for item in normalized.values()):
            return normalized

    return defaults


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
