"""Small schema-validated theme/config layer for the Orb TUI.

This is intentionally not a plugin runtime.  It provides a stable semantic
palette, a tiny config resolver, and safe CSS/markup token generation for the
Textual REPL TUI.  The token names mirror dashboard color semantics so the web
surface can adopt the same names without changing event behavior.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping

_DEFAULT_THEME = "orb-dark"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class TuiTheme:
    """Resolved semantic color tokens for the Orb TUI."""

    name: str
    surface: dict[str, str]
    border: dict[str, str]
    text: dict[str, str]
    accent: dict[str, str]
    agent: dict[str, str]

    def token(self, group: str, key: str, fallback: str = "#c4ced9") -> str:
        values = getattr(self, group, None)
        if isinstance(values, dict):
            return values.get(key, fallback)
        return fallback


@dataclass(frozen=True)
class ResolvedTuiTheme:
    """Result of resolving user TUI config."""

    name: str
    theme: TuiTheme
    warnings: list[str]


_BUILTIN_THEMES: dict[str, TuiTheme] = {
    "orb-dark": TuiTheme(
        name="orb-dark",
        surface={
            "bg": "#0b1016",
            "panel": "#141a22",
            "raised": "#0e141c",
            "block": "#0f141c",
            "palette": "#0e141b",
            "active": "#141e2d",
        },
        border={
            "subtle": "#1b2330",
            "focus": "#94bfff",
            "input": "#3966a8",
            "block": "#3a4552",
            "modal": "#2f3b4a",
        },
        text={
            "primary": "#ecf1f6",
            "secondary": "#c4ced9",
            "muted": "#8796a7",
            "dim": "#6b7685",
        },
        accent={
            "primary": "#94bfff",
            "success": "#86d8ab",
            "warn": "#f0c982",
            "error": "#f3afa7",
        },
        agent={
            "coordinator": "#94bfff",
            "coder": "#c4ced9",
            "reviewer": "#f0c982",
            "reviewer_a": "#f0c982",
            "reviewer_b": "#f5b56b",
            "tester": "#86d8ab",
            "user": "#94bfff",
            "system": "#8796a7",
        },
    ),
    "orb-high-contrast": TuiTheme(
        name="orb-high-contrast",
        surface={
            "bg": "#000000",
            "panel": "#101010",
            "raised": "#050505",
            "block": "#080808",
            "palette": "#050505",
            "active": "#1a1a1a",
        },
        border={
            "subtle": "#808080",
            "focus": "#ffffff",
            "input": "#bdbdbd",
            "block": "#bdbdbd",
            "modal": "#ffffff",
        },
        text={
            "primary": "#ffffff",
            "secondary": "#eeeeee",
            "muted": "#d0d0d0",
            "dim": "#a8a8a8",
        },
        accent={
            "primary": "#ffffff",
            "success": "#00ff87",
            "warn": "#ffd75f",
            "error": "#ff5f5f",
        },
        agent={
            "coordinator": "#ffffff",
            "coder": "#eeeeee",
            "reviewer": "#ffd75f",
            "reviewer_a": "#ffd75f",
            "reviewer_b": "#ffaf00",
            "tester": "#00ff87",
            "user": "#ffffff",
            "system": "#d0d0d0",
        },
    ),
}

DEFAULT_TUI_THEME = _BUILTIN_THEMES[_DEFAULT_THEME]
BUILTIN_TUI_THEME_NAMES = tuple(_BUILTIN_THEMES)


def _is_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_COLOR_RE.fullmatch(value))


def _copy_theme(theme: TuiTheme, *, name: str | None = None) -> TuiTheme:
    return TuiTheme(
        name=name or theme.name,
        surface=dict(theme.surface),
        border=dict(theme.border),
        text=dict(theme.text),
        accent=dict(theme.accent),
        agent=dict(theme.agent),
    )


def _coerce_tui_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    tui = config.get("tui", {})
    return tui if isinstance(tui, Mapping) else {}


def resolve_tui_theme(config: Mapping[str, Any] | None = None) -> ResolvedTuiTheme:
    """Resolve Orb config into a safe TUI theme.

    Supported config shape:

    ``{"tui": {"theme": "orb-dark", "theme_overrides": {"accent": {"primary": "#..."}}}}``

    Unknown theme names fall back to ``orb-dark``.  Invalid color strings are
    ignored per-token, with warnings suitable for a non-blocking system whisper.
    """

    tui = _coerce_tui_config(config)
    requested = str(tui.get("theme") or _DEFAULT_THEME).strip() or _DEFAULT_THEME
    warnings: list[str] = []
    base = _BUILTIN_THEMES.get(requested)
    resolved_name = requested
    if base is None:
        warnings.append(f'theme "{requested}" not found; using {_DEFAULT_THEME}')
        base = _BUILTIN_THEMES[_DEFAULT_THEME]
        resolved_name = _DEFAULT_THEME

    theme = _copy_theme(base)
    overrides = tui.get("theme_overrides")
    if isinstance(overrides, Mapping):
        for group_name in ("surface", "border", "text", "accent", "agent"):
            group_overrides = overrides.get(group_name)
            group = getattr(theme, group_name)
            if not isinstance(group_overrides, Mapping):
                continue
            for key, value in group_overrides.items():
                key = str(key)
                token_path = f"{group_name}.{key}"
                if key not in group:
                    warnings.append(f"theme token {token_path} is not supported; using default")
                    continue
                if not _is_color(value):
                    warnings.append(f"theme token {token_path} has invalid color; using default")
                    continue
                group[key] = str(value).lower()

    return ResolvedTuiTheme(name=resolved_name, theme=theme, warnings=warnings)


def load_resolved_tui_theme() -> ResolvedTuiTheme:
    """Load Orb CLI config and resolve the configured TUI theme."""

    try:
        from orb.cli.config import load_config

        return resolve_tui_theme(load_config())
    except Exception as exc:  # noqa: BLE001 - theme loading must not block TUI startup
        return ResolvedTuiTheme(
            name=_DEFAULT_THEME,
            theme=_copy_theme(DEFAULT_TUI_THEME),
            warnings=[f"could not load TUI theme config; using {_DEFAULT_THEME}: {exc}"],
        )


def build_tui_css(theme: TuiTheme = DEFAULT_TUI_THEME) -> str:
    """Generate Textual CSS from validated semantic TUI tokens."""

    s = theme.surface
    b = theme.border
    t = theme.text
    a = theme.accent
    return f"""
Screen {{ background: {s['bg']}; }}

#strip {{
    dock: top;
    height: auto;
    min-height: 1;
    padding: 0 1;
    color: {t['muted']};
    background: {s['panel']};
}}
#main {{ layout: horizontal; }}
#rail {{
    width: 28;
    min-width: 22;
    padding: 0 1;
    background: {s['bg']};
    border-right: solid {b['subtle']};
}}
#repl-col {{ width: 1fr; layout: vertical; }}
#stream {{
    padding: 1 2 0 2;
    overflow-y: auto;
    height: 1fr;
}}
#composer {{
    dock: bottom;
    height: auto;
    padding: 0 1;
    border-top: solid {b['subtle']};
    background: {s['raised']};
}}
#query-input {{
    height: 3;
    border: round {b['input']};
    background: {s['panel']};
    padding: 0 1;
    color: {t['primary']};
}}
#query-input:focus {{ border: round {b['focus']}; }}
.rail-section {{ margin-bottom: 1; }}
.rail-heading {{
    color: {t['dim']};
    text-style: bold;
}}
.rail-line {{
    color: {t['secondary']};
}}
.turn {{
    margin: 1 0 0 0;
}}
.turn-head {{ color: {t['muted']}; }}
.turn-body {{ color: {t['secondary']}; }}
.whisper {{
    color: {t['dim']};
    margin: 0;
    padding: 0 2;
}}
.block {{
    background: {s['block']};
    border-left: thick {b['block']};
    padding: 0 1;
    margin: 1 0 0 2;
    color: {t['secondary']};
}}
.block-hdr {{ color: {t['muted']}; }}
.block-ok  {{ color: {a['success']}; }}
.block-run {{ color: {a['primary']}; }}
.block-err {{ color: {a['error']}; }}
.composer-hint {{
    color: {t['dim']};
    padding: 0 1;
}}
.milestone {{
    color: {t['dim']};
    margin: 1 0 0 0;
    padding: 0 2;
}}
#live-bar {{
    height: auto;
    min-height: 1;
    color: {t['muted']};
    padding: 0 1;
}}
#slash-palette {{
    height: auto;
    padding: 0 1;
    background: {s['palette']};
    border: tall {b['subtle']};
    color: {t['muted']};
}}
.hidden {{ display: none; }}
.block-accept {{
    color: {t['muted']};
    padding: 0 1;
    margin: 0 0 0 2;
}}
"""


def dashboard_token_reference(theme: TuiTheme = DEFAULT_TUI_THEME) -> dict[str, dict[str, str]]:
    """Return semantic token names the dashboard should mirror.

    The dashboard still owns its CSS/classes in this PR; this map documents the
    shared contract for a follow-up without changing browser behavior.
    """

    return {
        "surface": deepcopy(theme.surface),
        "border": deepcopy(theme.border),
        "text": deepcopy(theme.text),
        "accent": deepcopy(theme.accent),
        "agent": deepcopy(theme.agent),
    }
