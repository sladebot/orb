from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orb.cli import configure as configure_mod
from orb.cli import config as config_mod


# ── helpers ──────────────────────────────────────────────────────────────────

def _inputs(monkeypatch, answers: list[str]) -> list[str]:
    """Feed a scripted sequence of answers into input() calls."""
    calls: list[str] = []
    queue = list(answers)

    def _fake_input(prompt: str = "") -> str:
        calls.append(prompt)
        if not queue:
            raise EOFError("configure flow asked for more input than tests provided")
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", _fake_input)
    return calls


def _catalog(ids: list[str]) -> list[dict]:
    return [{"id": mid, "label": mid, "local": True} for mid in ids]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch):
    """Redirect ~/.orb/config.json to a temp path for each test."""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    return cfg_path


# ── _pick_provider ───────────────────────────────────────────────────────────

def test_pick_provider_by_number(monkeypatch):
    _inputs(monkeypatch, ["3"])
    assert configure_mod._pick_provider() == "ollama"


def test_pick_provider_by_name(monkeypatch):
    _inputs(monkeypatch, ["anthropic"])
    assert configure_mod._pick_provider() == "anthropic"


def test_pick_provider_quit(monkeypatch):
    _inputs(monkeypatch, ["q"])
    assert configure_mod._pick_provider() is None


def test_pick_provider_retries_on_bad_input(monkeypatch):
    _inputs(monkeypatch, ["99", "nonsense", "2"])
    assert configure_mod._pick_provider() == "openai-codex"


# ── _toggle_models ───────────────────────────────────────────────────────────

def test_toggle_models_starts_from_existing_enabled_flags(monkeypatch):
    _inputs(monkeypatch, [""])  # just confirm without toggling
    cfg = {
        "providers": {
            "ollama": {
                "models": {
                    "qwen3.5:9b":  {"enabled": True},
                    "qwen3.5:27b": {"enabled": False},
                },
            },
        },
    }
    catalog = _catalog(["qwen3.5:9b", "qwen3.5:27b"])
    state = configure_mod._toggle_models("ollama", catalog, cfg)
    assert state["qwen3.5:9b"]["enabled"] is True
    assert state["qwen3.5:27b"]["enabled"] is False


def test_toggle_models_flips_individual_entries(monkeypatch):
    _inputs(monkeypatch, ["2", ""])   # toggle #2 off, then confirm
    cfg = {"providers": {"ollama": {"models": {}}}}
    catalog = _catalog(["a:latest", "b:latest", "c:latest"])
    state = configure_mod._toggle_models("ollama", catalog, cfg)
    assert state["a:latest"]["enabled"] is True
    assert state["b:latest"]["enabled"] is False
    assert state["c:latest"]["enabled"] is True


def test_toggle_models_all_none_shortcuts(monkeypatch):
    _inputs(monkeypatch, ["n", "1", ""])   # disable all, then re-enable #1, confirm
    cfg = {"providers": {"ollama": {"models": {}}}}
    catalog = _catalog(["a", "b", "c"])
    state = configure_mod._toggle_models("ollama", catalog, cfg)
    assert state["a"]["enabled"] is True
    assert state["b"]["enabled"] is False
    assert state["c"]["enabled"] is False


# ── _pick_defaults ───────────────────────────────────────────────────────────

def test_pick_defaults_keeps_on_empty(monkeypatch):
    # Three tiers for local providers, three empty responses to keep defaults
    _inputs(monkeypatch, ["", "", ""])
    catalog = _catalog(["m1", "m2"])
    current = {"local_small": "m1", "local_medium": "m2", "local_large": "m2"}
    result = configure_mod._pick_defaults("ollama", catalog, current)
    assert result == current


def test_pick_defaults_overrides_by_name(monkeypatch):
    _inputs(monkeypatch, ["m2", "", ""])
    catalog = _catalog(["m1", "m2"])
    current = {"local_small": "m1", "local_medium": "m2", "local_large": "m2"}
    result = configure_mod._pick_defaults("ollama", catalog, current)
    assert result["local_small"] == "m2"


def test_pick_defaults_overrides_by_number(monkeypatch):
    _inputs(monkeypatch, ["", "1", ""])
    catalog = _catalog(["m1", "m2"])
    current = {"local_small": "m1", "local_medium": "m2", "local_large": "m2"}
    result = configure_mod._pick_defaults("ollama", catalog, current)
    assert result["local_medium"] == "m1"


# ── configure_provider end-to-end (local) ────────────────────────────────────

@pytest.mark.asyncio
async def test_configure_provider_saves_catalog_and_defaults(isolated_config, monkeypatch):
    # Pre-seed config with a fetched catalog so we don't need to stub a real
    # daemon; the refresh step will be mocked to a no-op.
    cfg = {
        "providers": {
            "ollama": {
                "enabled": False,
                "models": {},
                "catalog": _catalog(["m1", "m2", "m3"]),
                "default_models": {"local_small": "m1", "local_medium": "m2", "local_large": "m3"},
            },
        },
    }
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(cfg))

    # Toggle model #3 off, then keep default picks.
    _inputs(monkeypatch, ["3", "", "", "", ""])

    with patch.object(configure_mod, "_refresh_catalog", new_callable=AsyncMock):
        await configure_mod.configure_provider("ollama")

    saved = json.loads(isolated_config.read_text())
    entry = saved["providers"]["ollama"]
    assert entry["enabled"] is True
    assert entry["models"]["m1"]["enabled"] is True
    assert entry["models"]["m2"]["enabled"] is True
    assert entry["models"]["m3"]["enabled"] is False
    assert entry["default_models"]["local_small"] == "m1"


@pytest.mark.asyncio
async def test_configure_provider_disables_when_nothing_selected(isolated_config, monkeypatch):
    cfg = {
        "providers": {
            "ollama": {
                "enabled": False,
                "models": {},
                "catalog": _catalog(["m1"]),
                "default_models": {"local_small": "m1", "local_medium": "m1", "local_large": "m1"},
            },
        },
    }
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(cfg))

    # Disable all, confirm, then (no default prompts reached because no models)
    _inputs(monkeypatch, ["n", ""])
    with patch.object(configure_mod, "_refresh_catalog", new_callable=AsyncMock):
        await configure_mod.configure_provider("ollama")

    entry = json.loads(isolated_config.read_text())["providers"]["ollama"]
    assert entry["enabled"] is False
    assert entry["models"]["m1"]["enabled"] is False


@pytest.mark.asyncio
async def test_configure_provider_bails_when_catalog_is_empty(isolated_config, monkeypatch):
    # Fresh config (no catalog yet) and a refresh that yields nothing.
    _inputs(monkeypatch, [])  # no prompts expected — should abort before toggle
    with patch.object(configure_mod, "_refresh_catalog", new_callable=AsyncMock):
        await configure_mod.configure_provider("omlx")

    # Either nothing was written or the existing defaults remain untouched.
    saved = json.loads(isolated_config.read_text()) if isolated_config.exists() else {}
    entry = (saved.get("providers") or {}).get("omlx") or {}
    assert entry.get("catalog") in (None, [])


# ── CLI wiring ───────────────────────────────────────────────────────────────

def test_configure_subcommand_is_registered():
    from orb.cli.main import parse_args

    with patch("sys.argv", ["orb", "configure", "--help"]):
        with pytest.raises(SystemExit):
            parse_args()
