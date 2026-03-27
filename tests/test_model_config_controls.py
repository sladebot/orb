from __future__ import annotations

import json

from orb.cli import config as config_cli
from orb.llm.types import ANTHROPIC_HAIKU_MODEL, ANTHROPIC_OPUS_MODEL, ANTHROPIC_SONNET_MODEL
from orb.runtime import graph_runtime as runtime_mod
from orb.runtime.graph_runtime import GraphRuntime


def test_load_config_normalizes_model_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "providers": {
            "anthropic": {
                "enabled": True,
                "models": {
                    ANTHROPIC_SONNET_MODEL: False,
                    ANTHROPIC_OPUS_MODEL: {"enabled": True, "label": "keep"},
                },
            },
        },
    }))
    monkeypatch.setattr(config_cli, "CONFIG_PATH", config_path)

    cfg = config_cli.load_config()

    assert cfg["providers"]["anthropic"]["models"][ANTHROPIC_SONNET_MODEL]["enabled"] is False
    assert cfg["providers"]["anthropic"]["models"][ANTHROPIC_OPUS_MODEL]["enabled"] is True
    assert cfg["providers"]["openai-codex"]["enabled"] is True
    assert cfg["providers"]["ollama"]["models"] == {}
    assert cfg["providers"]["vmlx"]["enabled"] is False


def test_load_config_canonicalizes_stale_model_aliases(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "providers": {
            "anthropic": {
                "models": {
                    "claude-sonnet-4-6": {"enabled": False},
                },
                "default_models": {
                    "cloud_fast": "claude-sonnet-4-6",
                },
                "catalog": [
                    {"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "local": False},
                ],
            },
            "ollama": {
                "models": {
                    "qwen3.5-9b-4k:latest": {"enabled": True},
                },
                "default_models": {
                    "local_small": "qwen3.5-9b-4k:latest",
                },
                "catalog": [
                    {"id": "qwen3.5-9b-4k:latest", "label": "qwen3.5-9b-4k:latest", "local": True},
                    {"id": "qwen3.5:9b", "label": "qwen3.5:9b", "local": True},
                ],
            },
        },
    }))
    monkeypatch.setattr(config_cli, "CONFIG_PATH", config_path)

    cfg = config_cli.load_config()

    assert cfg["providers"]["anthropic"]["models"][ANTHROPIC_SONNET_MODEL]["enabled"] is False
    assert cfg["providers"]["anthropic"]["default_models"]["cloud_fast"] == ANTHROPIC_SONNET_MODEL
    assert cfg["providers"]["anthropic"]["catalog"][0]["id"] == ANTHROPIC_OPUS_MODEL
    assert cfg["providers"]["ollama"]["models"]["qwen3.5:9b"]["enabled"] is True
    assert cfg["providers"]["ollama"]["default_models"]["local_small"] == "qwen3.5:9b"
    assert cfg["providers"]["ollama"]["catalog"] == [{"id": "qwen3.5:9b", "label": "qwen3.5-9b-4k:latest", "local": True}]


def test_default_config_matches_seeded_user_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "missing-config.json"
    monkeypatch.setattr(config_cli, "CONFIG_PATH", config_path)

    cfg = config_cli.load_config()

    assert cfg["local_models"] is True
    assert cfg["providers"]["anthropic"]["models"][ANTHROPIC_HAIKU_MODEL]["enabled"] is True
    assert cfg["providers"]["anthropic"]["models"][ANTHROPIC_SONNET_MODEL]["enabled"] is False
    assert cfg["providers"]["anthropic"]["models"][ANTHROPIC_OPUS_MODEL]["enabled"] is False
    assert cfg["providers"]["anthropic"]["default_models"]["cloud_fast"] == ANTHROPIC_HAIKU_MODEL
    assert cfg["providers"]["openai-codex"]["enabled"] is True
    assert cfg["providers"]["ollama"]["enabled"] is True
    assert cfg["providers"]["vmlx"]["enabled"] is False


def test_runtime_configure_drops_provider_with_no_enabled_models(monkeypatch):
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: {
            "anthropic": {
                "enabled": True,
                "models": {
                    ANTHROPIC_HAIKU_MODEL: {"enabled": False},
                    ANTHROPIC_SONNET_MODEL: {"enabled": False},
                    ANTHROPIC_OPUS_MODEL: {"enabled": False},
                },
            },
        } if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime.configure(
        providers={"anthropic": object()},
        config=None,
        model_overrides=None,
        tier_override=None,
    )

    assert runtime._providers == {}  # noqa: SLF001


def test_models_payload_excludes_disabled_models(monkeypatch):
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: {
            "anthropic": {
                "enabled": True,
                "models": {
                    ANTHROPIC_SONNET_MODEL: {"enabled": False},
                },
            },
        } if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"anthropic": object()}  # noqa: SLF001

    payload = runtime.models_payload()
    model_ids = {item["id"] for item in payload["models"]}

    assert ANTHROPIC_HAIKU_MODEL in model_ids
    assert ANTHROPIC_SONNET_MODEL not in model_ids
    assert ANTHROPIC_OPUS_MODEL in model_ids


def test_build_agent_model_map_falls_back_when_default_model_is_disabled(monkeypatch):
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: {
            "anthropic": {
                "enabled": True,
                "models": {
                    ANTHROPIC_SONNET_MODEL: {"enabled": False},
                },
            },
        } if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"anthropic": object()}  # noqa: SLF001

    model_map = runtime._build_agent_model_map(  # noqa: SLF001
        complexity=40,
        topology_id="triad",
        agent_complexity={
            "coordinator": 30,
            "coder": 40,
            "reviewer": 30,
            "tester": 35,
        },
    )

    assert model_map["coder"].model_id != ANTHROPIC_SONNET_MODEL
    assert model_map["reviewer"].model_id != ANTHROPIC_SONNET_MODEL


def test_models_payload_includes_vmlx_catalog_models(monkeypatch):
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: {
            "vmlx": {
                "enabled": True,
                "catalog": [
                    {"id": "Qwen/Qwen2.5-Coder-7B-Instruct", "label": "Qwen 2.5 Coder 7B", "local": True},
                ],
            },
        } if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"vmlx": object()}  # noqa: SLF001

    payload = runtime.models_payload()
    entries = {(item["provider"], item["id"]) for item in payload["models"]}

    assert ("vmlx", "Qwen/Qwen2.5-Coder-7B-Instruct") in entries


def test_build_agent_model_map_uses_vmlx_when_it_is_the_only_local_provider(monkeypatch):
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: {
            "vmlx": {
                "enabled": True,
                "catalog": [
                    {"id": "Qwen/Qwen2.5-Coder-7B-Instruct", "label": "Qwen 2.5 Coder 7B", "local": True},
                    {"id": "Qwen/Qwen2.5-Coder-14B-Instruct", "label": "Qwen 2.5 Coder 14B", "local": True},
                ],
                "default_models": {
                    "local_small": "Qwen/Qwen2.5-Coder-7B-Instruct",
                    "local_medium": "Qwen/Qwen2.5-Coder-14B-Instruct",
                    "local_large": "Qwen/Qwen2.5-Coder-14B-Instruct",
                },
            },
        } if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"vmlx": object()}  # noqa: SLF001

    model_map = runtime._build_agent_model_map(  # noqa: SLF001
        complexity=30,
        topology_id="triad",
        agent_complexity={
            "coordinator": 20,
            "coder": 30,
            "reviewer": 30,
            "tester": 25,
        },
    )

    assert model_map["coordinator"].provider == "vmlx"
    assert model_map["coder"].provider == "vmlx"
