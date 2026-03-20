import asyncio

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
    assert "Claude Sonnet 4.6" in out
    assert "Claude Opus 4.6" in out
    assert "qwen3.5:9b" in out
    assert "qwen3.5:27b" in out
