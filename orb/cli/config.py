from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".orb" / "config.json"

_DEFAULTS: dict[str, Any] = {
    "local_models": True,
}

_BOOL_TRUE  = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def load_config() -> dict[str, Any]:
    cfg = dict(_DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


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


def local_models_enabled() -> bool:
    return bool(get("local_models"))


def show_config() -> None:
    cfg = load_config()
    max_len = max(len(k) for k in _DEFAULTS)
    for key, default in _DEFAULTS.items():
        val = cfg.get(key, default)
        source = "default" if key not in cfg else "config"
        print(f"  {key:<{max_len}}  =  {str(val).lower():<6}  ({source})")
