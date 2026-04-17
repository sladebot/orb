from __future__ import annotations

import json
from pathlib import Path

import pytest

from orb.cli import config as config_mod


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    return cfg_path


def _write(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))


def test_show_config_renders_scalar_table(capsys, isolated_config):
    _write(isolated_config, {"local_models": False, "providers": {}})
    config_mod.show_config()
    out = capsys.readouterr().out
    assert "Settings" in out
    assert "local_models" in out
    # boolean renders as a lowercase word, not "true" vs "True"
    assert "false" in out


def test_show_config_hides_disabled_providers(capsys, isolated_config):
    # Disable every known provider so the "none enabled" path is exercised.
    _write(
        isolated_config,
        {
            "providers": {
                name: {"enabled": False} for name in config_mod._DEFAULTS["providers"]
            },
        },
    )
    config_mod.show_config()
    out = capsys.readouterr().out
    for name in config_mod._DEFAULTS["providers"]:
        assert name not in out
    assert "none enabled" in out


def test_show_config_skips_disabled_providers_when_others_enabled(capsys, isolated_config):
    _write(
        isolated_config,
        {
            "providers": {
                "ollama":    {"enabled": True,  "models": {"m": {"enabled": True}}},
                "anthropic": {"enabled": False, "models": {}},
            },
        },
    )
    config_mod.show_config()
    out = capsys.readouterr().out
    assert "ollama" in out
    assert "anthropic" not in out


def test_show_config_renders_provider_blocks(capsys, isolated_config):
    _write(
        isolated_config,
        {
            "local_models": True,
            "providers": {
                "ollama": {
                    "enabled": True,
                    "base_url": "http://localhost:11434",
                    "models": {
                        "qwen3.5:9b":  {"enabled": True},
                        "qwen3.5:27b": {"enabled": False},
                    },
                    "catalog": [
                        {"id": "qwen3.5:9b",  "label": "Qwen 3.5 9B",  "local": True},
                        {"id": "qwen3.5:27b", "label": "Qwen 3.5 27B", "local": True},
                    ],
                    "default_models": {
                        "local_small":  "qwen3.5:9b",
                        "local_medium": "qwen3.5:27b",
                        "local_large":  "qwen3.5:27b",
                    },
                },
            },
        },
    )

    config_mod.show_config()
    out = capsys.readouterr().out

    assert "Providers" in out
    assert "ollama" in out
    assert "http://localhost:11434" in out
    # Enabled model renders with a checkmark, disabled with a blank mark
    assert "[✓] qwen3.5:9b" in out
    assert "[ ] qwen3.5:27b" in out
    # Labels are surfaced when they differ from the id
    assert "Qwen 3.5 9B" in out
    # Defaults map renders
    assert "local_small" in out and "→" in out


def test_show_config_handles_missing_catalog(capsys, isolated_config):
    _write(
        isolated_config,
        {
            "providers": {
                "omlx": {
                    "enabled": True,
                    "models": {"some-model": {"enabled": True}},
                },
            },
        },
    )
    config_mod.show_config()
    out = capsys.readouterr().out
    # Falls back to the model-map render when catalog is absent
    assert "[✓] some-model" in out


def test_show_config_notes_when_provider_has_no_catalog(capsys, isolated_config):
    _write(isolated_config, {"providers": {"vmlx": {"enabled": True}}})
    config_mod.show_config()
    out = capsys.readouterr().out
    assert "(no catalog" in out
