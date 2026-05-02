import asyncio
import time

from orb.cli import auth


def test_save_anthropic_key_stores_setup_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CREDS_PATH", tmp_path / "credentials.json")

    auth.save_anthropic_key("sk-ant-oat01-example-token")

    stored = auth.load_credentials("anthropic")
    assert stored == {"setup_token": "sk-ant-oat01-example-token"}


def test_save_anthropic_key_stores_api_key_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CREDS_PATH", tmp_path / "credentials.json")

    auth.save_anthropic_key("sk-ant-api03-example-token")

    stored = auth.load_credentials("anthropic")
    assert stored == {"api_key": "sk-ant-api03-example-token"}


def test_auth_anthropic_prompts_for_claude_setup_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth, "CREDS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("builtins.input", lambda _: "sk-ant-oat01-pasted-from-claude")

    asyncio.run(auth.auth_anthropic())

    out = capsys.readouterr().out
    assert "claude setup-token" in out
    stored = auth.load_credentials("anthropic")
    assert stored == {"setup_token": "sk-ant-oat01-pasted-from-claude"}


def test_get_anthropic_key_supports_legacy_oauth_field(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CREDS_PATH", tmp_path / "credentials.json")
    auth._save_credentials("anthropic", {"oauth_token": "sk-ant-oat01-legacy"})

    assert auth.get_anthropic_key() == "sk-ant-oat01-legacy"


def test_anthropic_headers_for_api_key():
    headers = auth._anthropic_headers("sk-ant-api03-example")
    assert headers["x-api-key"] == "sk-ant-api03-example"
    assert "Authorization" not in headers


def test_anthropic_headers_for_oauth_token():
    headers = auth._anthropic_headers("sk-ant-oat01-example")
    assert headers["Authorization"] == "Bearer sk-ant-oat01-example"
    assert headers["anthropic-beta"] == auth._ANTHROPIC_OAUTH_BETAS


def test_auth_status_prints_model_matrix(monkeypatch, capsys):
    monkeypatch.setattr(auth, "load_credentials", lambda provider: {"api_key": "sk-test-openai"} if provider == "openai" else {"api_key": "sk-ant-api03-test"})
    monkeypatch.setattr(auth, "get_anthropic_key", lambda: "sk-ant-api03-test")
    monkeypatch.setattr(auth.os, "environ", {"OLLAMA_BASE_URL": "http://127.0.0.1:11434"})
    monkeypatch.setattr(
        "orb.cli.config.load_config",
        lambda: {
            "providers": {
                "anthropic": {
                    "default_models": {
                        "cloud_lite": auth.ANTHROPIC_HAIKU_MODEL,
                        "cloud_fast": auth.ANTHROPIC_SONNET_MODEL,
                        "cloud_strong": auth.ANTHROPIC_OPUS_MODEL,
                    }
                },
                "openai-codex": {
                    "default_models": {
                        "cloud_lite": "gpt-5.4-mini",
                        "cloud_fast": "gpt-5.5",
                        "cloud_strong": "gpt-5.5",
                    }
                },
                "ollama": {
                    "default_models": {
                        "local_small": "qwen3.5:9b",
                        "local_medium": "qwen3.5:27b",
                        "local_large": "qwen3.5:27b",
                    }
                },
            }
        },
    )

    async def _true(*_args, **_kwargs):
        return True

    async def _anthropic_matrix(*_args, **_kwargs):
        return {
            auth.ANTHROPIC_HAIKU_MODEL: True,
            auth.ANTHROPIC_SONNET_MODEL: False,
            auth.ANTHROPIC_OPUS_MODEL: True,
        }

    async def _ollama_matrix(*_args, **_kwargs):
        return {"qwen3.5:9b": True, "qwen3.5:27b": False}

    monkeypatch.setattr(auth, "_check_openai_key", _true)
    monkeypatch.setattr(auth, "_check_openai_key_model", _true)
    monkeypatch.setattr(auth, "_check_anthropic", _true)
    monkeypatch.setattr(auth, "_check_anthropic_model_matrix", _anthropic_matrix)
    monkeypatch.setattr(auth, "_check_ollama_models", _ollama_matrix)

    asyncio.run(auth.auth_status())

    out = capsys.readouterr().out
    assert "Claude Haiku 4.5" in out
    assert "Claude Sonnet 4" in out
    assert "Claude Opus 4" in out
    assert "qwen3.5:9b" in out
    assert "qwen3.5:27b" in out


def test_auth_status_refreshes_expired_openai_oauth(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth, "CREDS_PATH", tmp_path / "credentials.json")
    auth._save_credentials(
        "openai",
        {
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": int(time.time()) - 10,
            "email": "user@example.test",
        },
    )
    monkeypatch.setattr(auth, "load_credentials", auth.load_credentials)
    monkeypatch.setattr(auth, "get_anthropic_key", lambda: None)
    monkeypatch.setattr(auth.os, "environ", {})
    monkeypatch.setattr(
        "orb.cli.config.load_config",
        lambda: {
            "providers": {
                "openai-codex": {
                    "default_models": {
                        "cloud_fast": "gpt-5.5",
                    }
                },
            }
        },
    )

    def _refresh(creds):
        updated = dict(creds)
        updated["access_token"] = "new-token"
        updated["expires_at"] = int(time.time()) + 3600
        auth._save_credentials("openai", updated)
        return updated

    async def _check_oauth(token):
        assert token == "new-token"
        return True

    async def _check_model(token, model_id):
        assert token == "new-token"
        assert model_id == "gpt-5.5"
        return True

    monkeypatch.setattr(auth, "refresh_openai_token", _refresh)
    monkeypatch.setattr(auth, "_check_openai_oauth", _check_oauth)
    monkeypatch.setattr(auth, "_check_openai_oauth_model", _check_model)

    asyncio.run(auth.auth_status())

    out = capsys.readouterr().out
    assert "OAuth token  user@example.test  (expires in" in out
    assert "expired" not in out
    assert "gpt-5.5" in out
