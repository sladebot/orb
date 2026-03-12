import asyncio

from orb.cli import onboard


def test_onboard_routes_to_anthropic_auth(monkeypatch, capsys):
    calls: list[str] = []
    answers = iter(["1", "5"])

    async def fake_auth_anthropic() -> None:
        calls.append("anthropic")

    monkeypatch.setattr(onboard.auth_cli, "auth_anthropic", fake_auth_anthropic)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    asyncio.run(onboard.run_onboarding())

    assert calls == ["anthropic"]
    assert "Onboarding complete." in capsys.readouterr().out


def test_onboard_updates_local_models(monkeypatch):
    answers = iter(["3", "2", "5"])
    set_calls: list[tuple[str, str]] = []

    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(onboard.config_cli, "local_models_enabled", lambda: True)
    monkeypatch.setattr(onboard.config_cli, "set_value", lambda key, value: set_calls.append((key, value)))

    asyncio.run(onboard.run_onboarding())

    assert set_calls == [("local_models", "false")]
