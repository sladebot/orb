"""Tests for GraphRuntime.refresh_provider_catalogs' status-map return.

The map lets `orb models refresh` tell the user, per provider, what
happened: updated, unchanged, or skipped (and why). Before this fix,
the fetchers swallowed errors and the CLI silently omitted providers
that failed — which is how the omlx 401 bug stayed hidden.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orb.cli import config as config_mod
from orb.runtime.graph_runtime import GraphRuntime


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    return cfg_path


def _make_runtime(providers: list[str]) -> GraphRuntime:
    from web.state import DashboardState

    rt = GraphRuntime(DashboardState())
    # Bypass `configure()` — we just need `_all_providers` populated with the
    # keys the method checks against.
    rt._all_providers = {name: object() for name in providers}
    return rt


@pytest.mark.asyncio
async def test_status_reports_skipped_for_unregistered_providers(isolated_config, monkeypatch):
    runtime = _make_runtime([])  # no providers registered at all
    status = await runtime.refresh_provider_catalogs()
    for name in ("anthropic", "openai-codex", "ollama", "vmlx", "omlx"):
        assert status[name] == "skipped:not-registered"


@pytest.mark.asyncio
async def test_status_reports_skipped_empty_when_fetch_returns_nothing(isolated_config, monkeypatch):
    runtime = _make_runtime(["omlx"])
    monkeypatch.setattr(runtime, "_fetch_omlx_catalog", AsyncMock(return_value=([], {})))
    status = await runtime.refresh_provider_catalogs()
    assert status["omlx"] == "skipped:empty"


@pytest.mark.asyncio
async def test_status_reports_updated_when_catalog_changes(isolated_config, monkeypatch):
    runtime = _make_runtime(["omlx"])
    catalog = [{"id": "m1", "label": "m1", "local": True}]
    defaults = {"local_small": "m1", "local_medium": "m1", "local_large": "m1"}
    monkeypatch.setattr(runtime, "_fetch_omlx_catalog", AsyncMock(return_value=(catalog, defaults)))

    status = await runtime.refresh_provider_catalogs()
    assert status["omlx"] == "updated:1"

    saved = json.loads(isolated_config.read_text())
    assert saved["providers"]["omlx"]["catalog"] == catalog


@pytest.mark.asyncio
async def test_status_reports_unchanged_when_catalog_matches(isolated_config, monkeypatch):
    catalog = [{"id": "m1", "label": "m1", "local": True}]
    defaults = {"local_small": "m1", "local_medium": "m1", "local_large": "m1"}
    # Seed the config with a matching catalog so the refresh finds no diff.
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps({
        "providers": {
            "omlx": {"enabled": True, "catalog": catalog, "default_models": defaults},
        },
    }))

    runtime = _make_runtime(["omlx"])
    monkeypatch.setattr(runtime, "_fetch_omlx_catalog", AsyncMock(return_value=(catalog, defaults)))
    status = await runtime.refresh_provider_catalogs()
    assert status["omlx"] == "unchanged:1"


@pytest.mark.asyncio
async def test_openai_codex_static_catalog_still_runs(isolated_config, monkeypatch):
    runtime = _make_runtime(["openai-codex"])
    status = await runtime.refresh_provider_catalogs()
    assert status["openai-codex"].startswith("updated:") or status["openai-codex"].startswith("unchanged:")
