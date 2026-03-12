import asyncio

from orb.cli import auth


def test_save_anthropic_key_stores_setup_token_as_oauth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CREDS_PATH", tmp_path / "credentials.json")

    auth.save_anthropic_key("sk-ant-oat01-example-token")

    stored = auth.load_credentials("anthropic")
    assert stored == {"oauth_token": "sk-ant-oat01-example-token"}


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
    assert stored == {"oauth_token": "sk-ant-oat01-pasted-from-claude"}
