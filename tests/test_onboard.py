from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orb.cli import config as config_mod
from orb.cli import onboard as onboard_mod


# ── helpers ──────────────────────────────────────────────────────────────────

def _inputs(monkeypatch, answers: list[str]) -> list[str]:
    """Feed a scripted sequence of answers into input() calls."""
    calls: list[str] = []
    queue = list(answers)

    def _fake_input(prompt: str = "") -> str:
        calls.append(prompt)
        if not queue:
            raise EOFError("onboard flow asked for more input than tests provided")
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
    assert onboard_mod._pick_provider() == "ollama"


def test_pick_provider_by_name(monkeypatch):
    _inputs(monkeypatch, ["anthropic"])
    assert onboard_mod._pick_provider() == "anthropic"


def test_pick_provider_quit(monkeypatch):
    _inputs(monkeypatch, ["q"])
    assert onboard_mod._pick_provider() is None


def test_pick_provider_retries_on_bad_input(monkeypatch):
    _inputs(monkeypatch, ["99", "nonsense", "2"])
    assert onboard_mod._pick_provider() == "openai-codex"


# ── _toggle_models ───────────────────────────────────────────────────────────

def test_toggle_models_starts_from_existing_enabled_flags(monkeypatch):
    _inputs(monkeypatch, [""])  # confirm without toggling
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
    state = onboard_mod._toggle_models("ollama", catalog, cfg)
    assert state["qwen3.5:9b"]["enabled"] is True
    assert state["qwen3.5:27b"]["enabled"] is False


def test_toggle_models_flips_individual_entries(monkeypatch):
    _inputs(monkeypatch, ["2", ""])   # toggle #2 off, then confirm
    cfg = {"providers": {"ollama": {"models": {}}}}
    catalog = _catalog(["a:latest", "b:latest", "c:latest"])
    state = onboard_mod._toggle_models("ollama", catalog, cfg)
    assert state["a:latest"]["enabled"] is True
    assert state["b:latest"]["enabled"] is False
    assert state["c:latest"]["enabled"] is True


def test_toggle_models_all_none_shortcuts(monkeypatch):
    _inputs(monkeypatch, ["n", "1", ""])   # disable all, re-enable #1, confirm
    cfg = {"providers": {"ollama": {"models": {}}}}
    catalog = _catalog(["a", "b", "c"])
    state = onboard_mod._toggle_models("ollama", catalog, cfg)
    assert state["a"]["enabled"] is True
    assert state["b"]["enabled"] is False
    assert state["c"]["enabled"] is False


# ── _pick_defaults ───────────────────────────────────────────────────────────

def test_pick_defaults_keeps_on_empty(monkeypatch):
    # Four prompts now: the "same for all?" y/n, plus three tiers.
    _inputs(monkeypatch, ["n", "", "", ""])
    catalog = _catalog(["m1", "m2"])
    current = {"local_small": "m1", "local_medium": "m2", "local_large": "m2"}
    result = onboard_mod._pick_defaults("ollama", catalog, current)
    assert result == current


def test_pick_defaults_overrides_by_name(monkeypatch):
    _inputs(monkeypatch, ["n", "m2", "", ""])
    catalog = _catalog(["m1", "m2"])
    current = {"local_small": "m1", "local_medium": "m2", "local_large": "m2"}
    result = onboard_mod._pick_defaults("ollama", catalog, current)
    assert result["local_small"] == "m2"


def test_pick_defaults_overrides_by_number(monkeypatch):
    _inputs(monkeypatch, ["n", "", "1", ""])
    catalog = _catalog(["m1", "m2"])
    current = {"local_small": "m1", "local_medium": "m2", "local_large": "m2"}
    result = onboard_mod._pick_defaults("ollama", catalog, current)
    assert result["local_medium"] == "m1"


def test_pick_defaults_same_for_all_applies_one_pick_to_every_tier(monkeypatch):
    _inputs(monkeypatch, ["y", "2"])   # "use same? yes" → pick #2 once
    catalog = _catalog(["m1", "m2", "m3"])
    current = {"local_small": "m1", "local_medium": "m1", "local_large": "m1"}
    result = onboard_mod._pick_defaults("ollama", catalog, current)
    assert result["local_small"] == "m2"
    assert result["local_medium"] == "m2"
    assert result["local_large"] == "m2"


def test_pick_defaults_same_for_all_by_name(monkeypatch):
    _inputs(monkeypatch, ["yes", "m3"])
    catalog = _catalog(["m1", "m2", "m3"])
    current = {"local_small": "m1", "local_medium": "m1", "local_large": "m1"}
    result = onboard_mod._pick_defaults("ollama", catalog, current)
    assert set(result.values()) == {"m3"}


# ── _probe_local / reachability ──────────────────────────────────────────────

def _mock_httpx_get(monkeypatch, responses: dict[str, int | Exception]):
    """Patch httpx.get with a URL-keyed response table."""
    def _fake_get(url, *args, **kwargs):
        value = responses.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"unexpected GET to {url}")
        return SimpleNamespace(status_code=value)
    monkeypatch.setattr(onboard_mod.httpx, "get", _fake_get)


def test_probe_local_hits_ollama_tags_endpoint(monkeypatch):
    cfg = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:11434/api/tags": 200})
    state, url = onboard_mod._probe_local("ollama", cfg)
    assert state == "ok"
    assert url == "http://localhost:11434/api/tags"


def test_probe_local_hits_omlx_models_endpoint(monkeypatch):
    cfg = {"providers": {"omlx": {"base_url": "http://localhost:8000/v1"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:8000/v1/models": 200})
    state, url = onboard_mod._probe_local("omlx", cfg)
    assert state == "ok"
    assert url == "http://localhost:8000/v1/models"


def test_probe_local_returns_down_on_connection_error(monkeypatch):
    cfg = {"providers": {"vmlx": {"base_url": "http://localhost:1234/v1"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:1234/v1/models": ConnectionError("down")})
    state, url = onboard_mod._probe_local("vmlx", cfg)
    assert state == "down"
    assert url == "http://localhost:1234/v1/models"


def test_probe_local_returns_auth_on_401(monkeypatch):
    cfg = {"providers": {"omlx": {"base_url": "http://localhost:8000/v1"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:8000/v1/models": 401})
    state, url = onboard_mod._probe_local("omlx", cfg)
    assert state == "auth"
    assert url == "http://localhost:8000/v1/models"


def test_probe_local_sends_api_key_when_configured(monkeypatch):
    cfg = {"providers": {"omlx": {"base_url": "http://localhost:8000/v1", "api_key": "sk-x"}}}
    captured: dict = {}

    def _fake_get(url, *args, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(onboard_mod.httpx, "get", _fake_get)
    state, _ = onboard_mod._probe_local("omlx", cfg)
    assert state == "ok"
    assert captured["headers"] == {"Authorization": "Bearer sk-x"}


def test_probe_local_rejects_cloud_provider(monkeypatch):
    state, url = onboard_mod._probe_local("anthropic", {})
    assert state == "down"
    assert url == ""


def test_auth_status_local_reports_reachability(monkeypatch):
    cfg = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:11434/api/tags": 200})
    status = onboard_mod._auth_status("ollama", cfg)
    assert "running" in status and "http://localhost:11434" in status


def test_auth_status_local_reports_unreachable(monkeypatch):
    cfg = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:11434/api/tags": ConnectionError("no server")})
    status = onboard_mod._auth_status("ollama", cfg)
    assert "not reachable" in status


def test_auth_status_local_reports_auth_required(monkeypatch):
    cfg = {"providers": {"omlx": {"base_url": "http://localhost:8000/v1"}}}
    _mock_httpx_get(monkeypatch, {"http://localhost:8000/v1/models": 401})
    status = onboard_mod._auth_status("omlx", cfg)
    assert "auth required" in status


@pytest.mark.asyncio
async def test_ensure_authed_local_aborts_when_unreachable(monkeypatch):
    _inputs(monkeypatch, ["n"])  # decline the "proceed anyway" prompt
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("down", "http://x/y"))
    assert await onboard_mod._ensure_authed("ollama") is False


@pytest.mark.asyncio
async def test_ensure_authed_local_allows_override(monkeypatch):
    _inputs(monkeypatch, ["y"])
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("down", "http://x/y"))
    assert await onboard_mod._ensure_authed("ollama") is True


@pytest.mark.asyncio
async def test_ensure_authed_local_passes_when_reachable(monkeypatch):
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("ok", "http://x/y"))
    assert await onboard_mod._ensure_authed("ollama") is True


@pytest.mark.asyncio
async def test_ensure_authed_local_prompts_for_api_key_on_401(isolated_config, monkeypatch):
    _inputs(monkeypatch, ["sk-test-omlx"])

    # First probe returns auth-required; after the api_key is saved, the
    # follow-up probe (invoked from `_prompt_local_api_key`) returns ok.
    probes = iter([("auth", "http://localhost:8000/v1/models"), ("ok", "http://localhost:8000/v1/models")])
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: next(probes))

    result = await onboard_mod._ensure_authed("omlx")
    assert result is True
    saved = json.loads(isolated_config.read_text())
    assert saved["providers"]["omlx"]["api_key"] == "sk-test-omlx"


@pytest.mark.asyncio
async def test_ensure_authed_local_skip_on_empty_api_key(isolated_config, monkeypatch):
    _inputs(monkeypatch, [""])  # Just hit Enter → skip
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("auth", "http://x/y"))
    assert await onboard_mod._ensure_authed("omlx") is False


@pytest.mark.asyncio
async def test_ensure_authed_cloud_skips_when_already_authed_and_user_declines_reauth(monkeypatch):
    _inputs(monkeypatch, ["n"])
    monkeypatch.setattr(onboard_mod, "_auth_status", lambda *_a, **_k: "authed")
    ran: list[str] = []

    async def _should_not_run(provider):
        ran.append(provider)

    monkeypatch.setattr(onboard_mod, "_run_cloud_auth", _should_not_run)
    assert await onboard_mod._ensure_authed("anthropic") is True
    assert ran == []


@pytest.mark.asyncio
async def test_ensure_authed_cloud_runs_reauth_when_accepted(monkeypatch):
    _inputs(monkeypatch, ["y"])
    monkeypatch.setattr(onboard_mod, "_auth_status", lambda *_a, **_k: "authed")
    ran: list[str] = []

    async def _spy(provider):
        ran.append(provider)

    monkeypatch.setattr(onboard_mod, "_run_cloud_auth", _spy)
    assert await onboard_mod._ensure_authed("anthropic") is True
    assert ran == ["anthropic"]


@pytest.mark.asyncio
async def test_ensure_authed_cloud_runs_first_time_auth_when_accepted(monkeypatch):
    _inputs(monkeypatch, ["y"])
    statuses = iter(["not authed", "authed"])
    monkeypatch.setattr(onboard_mod, "_auth_status", lambda *_a, **_k: next(statuses))
    ran: list[str] = []

    async def _spy(provider):
        ran.append(provider)

    monkeypatch.setattr(onboard_mod, "_run_cloud_auth", _spy)
    assert await onboard_mod._ensure_authed("openai-codex") is True
    assert ran == ["openai-codex"]


@pytest.mark.asyncio
async def test_ensure_authed_cloud_returns_false_when_user_declines(monkeypatch):
    _inputs(monkeypatch, ["n"])
    monkeypatch.setattr(onboard_mod, "_auth_status", lambda *_a, **_k: "not authed")
    assert await onboard_mod._ensure_authed("openai-codex") is False


@pytest.mark.asyncio
async def test_run_cloud_auth_openai_api_key_path(monkeypatch):
    _inputs(monkeypatch, ["2", "sk-test-123"])
    saved: dict = {}

    def _fake_save(provider, data):
        saved[provider] = data

    monkeypatch.setattr(onboard_mod.auth_cli, "_save_credentials", _fake_save)
    # Prevent OAuth from running if branching goes wrong.
    monkeypatch.setattr(onboard_mod.auth_cli, "auth_openai", AsyncMock())

    await onboard_mod._run_cloud_auth("openai-codex")
    assert saved == {"openai": {"api_key": "sk-test-123"}}


@pytest.mark.asyncio
async def test_run_cloud_auth_openai_oauth_path(monkeypatch):
    _inputs(monkeypatch, ["1"])
    oauth = AsyncMock()
    monkeypatch.setattr(onboard_mod.auth_cli, "auth_openai", oauth)
    await onboard_mod._run_cloud_auth("openai-codex")
    oauth.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cloud_auth_anthropic_delegates(monkeypatch):
    anthropic = AsyncMock()
    monkeypatch.setattr(onboard_mod.auth_cli, "auth_anthropic", anthropic)
    await onboard_mod._run_cloud_auth("anthropic")
    anthropic.assert_awaited_once()


# ── configure_provider end-to-end ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_configure_provider_saves_catalog_and_defaults(isolated_config, monkeypatch):
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

    # Toggle model #3 off, confirm, answer "no" to same-for-all, then keep
    # each tier default.
    _inputs(monkeypatch, ["3", "", "n", "", "", ""])
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("ok", "http://x/y"))

    with patch.object(onboard_mod, "_refresh_catalog", new_callable=AsyncMock):
        await onboard_mod.configure_provider("ollama")

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

    _inputs(monkeypatch, ["n", ""])
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("ok", "http://x/y"))
    with patch.object(onboard_mod, "_refresh_catalog", new_callable=AsyncMock):
        await onboard_mod.configure_provider("ollama")

    entry = json.loads(isolated_config.read_text())["providers"]["ollama"]
    assert entry["enabled"] is False
    assert entry["models"]["m1"]["enabled"] is False


@pytest.mark.asyncio
async def test_configure_provider_bails_when_catalog_is_empty(isolated_config, monkeypatch):
    _inputs(monkeypatch, [])  # should abort before any prompt
    monkeypatch.setattr(onboard_mod, "_probe_local", lambda *_a, **_k: ("ok", "http://x/y"))
    with patch.object(onboard_mod, "_refresh_catalog", new_callable=AsyncMock):
        await onboard_mod.configure_provider("omlx")

    saved = json.loads(isolated_config.read_text()) if isolated_config.exists() else {}
    entry = (saved.get("providers") or {}).get("omlx") or {}
    assert entry.get("catalog") in (None, [])


# ── CLI wiring ───────────────────────────────────────────────────────────────

def test_onboard_subcommand_is_registered():
    from orb.cli.main import parse_args

    with patch("sys.argv", ["orb", "onboard", "--help"]):
        with pytest.raises(SystemExit):
            parse_args()
