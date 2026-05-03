from __future__ import annotations

from orb.cli import config as orb_config


def test_default_tui_theme_preserves_current_orb_dark_tokens():
    from orb.cli.tui_theme import resolve_tui_theme

    resolved = resolve_tui_theme({})

    assert resolved.name == "orb-dark"
    assert resolved.theme.agent["coordinator"] == "#94bfff"
    assert resolved.theme.agent["coder"] == "#c4ced9"
    assert resolved.theme.surface["bg"] == "#0b1016"
    assert resolved.theme.accent["success"] == "#86d8ab"
    assert resolved.warnings == []


def test_builtin_high_contrast_theme_can_be_selected_from_orb_config():
    from orb.cli.tui_theme import resolve_tui_theme

    resolved = resolve_tui_theme({"tui": {"theme": "orb-high-contrast"}})

    assert resolved.name == "orb-high-contrast"
    assert resolved.theme.surface["bg"] != "#0b1016"
    assert resolved.theme.accent["primary"] == "#ffffff"
    assert resolved.warnings == []


def test_unknown_theme_name_falls_back_to_default_with_warning():
    from orb.cli.tui_theme import resolve_tui_theme

    resolved = resolve_tui_theme({"tui": {"theme": "missing-theme"}})

    assert resolved.name == "orb-dark"
    assert resolved.theme.agent["coordinator"] == "#94bfff"
    assert resolved.warnings == ['theme "missing-theme" not found; using orb-dark']


def test_custom_theme_invalid_color_tokens_fall_back_safely():
    from orb.cli.tui_theme import resolve_tui_theme

    resolved = resolve_tui_theme({
        "tui": {
            "theme": "orb-dark",
            "theme_overrides": {
                "accent": {"primary": "[#ff00ff]boom[/]", "success": "#00ff00"},
                "agent": {"coder": "not-a-color"},
            },
        }
    })

    assert resolved.theme.accent["primary"] == "#94bfff"
    assert resolved.theme.accent["success"] == "#00ff00"
    assert resolved.theme.agent["coder"] == "#c4ced9"
    assert any("accent.primary" in warning for warning in resolved.warnings)
    assert any("agent.coder" in warning for warning in resolved.warnings)


def test_generated_tui_css_uses_validated_theme_tokens_only():
    from orb.cli.tui_theme import build_tui_css, resolve_tui_theme

    resolved = resolve_tui_theme({
        "tui": {
            "theme_overrides": {"surface": {"panel": "[red]bad[/]"}},
        }
    })

    css = build_tui_css(resolved.theme)

    assert "[red]bad[/]" not in css
    assert "#141a22" in css


def test_orb_config_normalizes_tui_theme_field(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"tui": {"theme": "orb-high-contrast"}}')
    monkeypatch.setattr(orb_config, "CONFIG_PATH", cfg_path)

    cfg = orb_config.load_config()

    assert cfg["tui"]["theme"] == "orb-high-contrast"


def test_repl_tui_resolves_theme_from_orb_config(monkeypatch, tmp_path):
    from orb.cli.tui_repl import OrbReplTUI

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"tui": {"theme": "orb-high-contrast"}}')
    monkeypatch.setattr(orb_config, "CONFIG_PATH", cfg_path)

    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)

    assert app.theme_name == "orb-high-contrast"
    assert "Screen { background: #000000; }" in app.CSS


def test_repl_tui_keeps_invalid_theme_warning_for_non_blocking_whisper(monkeypatch, tmp_path):
    from orb.cli.tui_repl import OrbReplTUI

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"tui": {"theme": "missing-theme"}}')
    monkeypatch.setattr(orb_config, "CONFIG_PATH", cfg_path)

    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)

    assert app.theme_name == "orb-dark"
    assert app.theme_warnings == ['theme "missing-theme" not found; using orb-dark']
