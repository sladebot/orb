"""REPL-stream TUI for Orb — matches the orb-design-system/orb-tui design.

Layout:

    ┌────────────────── status strip ────────────────────┐
    ├──────────┬─────────────────────────────────────────┤
    │  context │            REPL stream                  │
    │   rail   │  (scrollable turns + inline blocks)     │
    │          ├─────────────────────────────────────────┤
    │          │            composer                     │
    ├──────────┴─────────────────────────────────────────┤
    │                   keybind footer                   │
    └────────────────────────────────────────────────────┘

The business logic is a subset of ``orb/cli/tui.py`` — same WebSocket
event contract, same daemon-backed session. We only replace the widget
tree + rendering; event handlers dispatch into new widgets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from time import time
from typing import Any
from urllib.parse import urlparse

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static, TextArea

# Slash command catalog surfaced by the palette peek + /help.
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "all commands"),
    ("/clear", "clear the stream"),
    ("/stop", "stop the current run"),
    ("/resume", "pick a prior session"),
    ("/topology", "switch routing topology"),
    ("/quit", "exit the TUI"),
]

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD_PORT = 1337

# Design palette — matches orb-design-system/orb-tui/index.html tokens.
AGENT_STYLE: dict[str, str] = {
    "coordinator": "#94bfff",
    "coder":       "#c4ced9",
    "reviewer":    "#f0c982",
    "reviewer_a":  "#f0c982",
    "reviewer_b":  "#f5b56b",
    "tester":      "#86d8ab",
    "user":        "#94bfff",
    "system":      "#8796a7",
}
AGENT_LABELS: dict[str, str] = {
    "coordinator": "Coordinator",
    "coder":       "Coder",
    "reviewer":    "Reviewer",
    "reviewer_a":  "Reviewer A",
    "reviewer_b":  "Reviewer B",
    "tester":      "Tester",
    "user":        "you",
    "system":      "session",
}
TOPOLOGY_LABELS: dict[str, str] = {
    "solo": "solo",
    "triad": "triad",
    "dual-review": "dual-review",
    "hierarchy": "hierarchy",
}

SCOPE_RE = re.compile(r"@([\w./\-]+)")
SCOPE_MAX = 8


def _plan_progress(items: list[tuple[str, str]]) -> tuple[int, int, str]:
    """Return ``(done, total, meter)`` derived from plan_items.

    ``items`` is a list of ``(kind, title)`` where kind is one of
    ``done`` / ``now`` / ``todo``. The meter is a 6-cell bar using
    U+25B0 (▰) for filled and U+25B1 (▱) for empty cells, scaled
    proportionally to ``done/total``.
    """
    total = len(items)
    done = sum(1 for k, _ in items if k == "done")
    width = 6
    if total <= 0:
        return 0, 0, "▱" * width
    filled = round(done / total * width)
    filled = max(0, min(width, filled))
    return done, total, ("▰" * filled) + ("▱" * (width - filled))


def _render_topology_graph(state: "OrbReplTUI") -> list[str]:
    """Return a list of Textual-markup lines rendering the topology graph.

    Uses a static pretty-printed layout for bundled topologies
    (``solo`` / ``triad``). Unknown topologies fall back to the
    graph-view ``rows`` stored from the init payload, or a vertical
    list of agent ids.
    """
    topo = (state.topology or "").lower()
    agents = state.agents

    def _status_color(aid: str) -> str:
        info = agents.get(aid) or {}
        status = str(info.get("status") or "idle")
        base = AGENT_STYLE.get(aid) or AGENT_STYLE.get(info.get("role", "").lower()) or "#c4ced9"
        if status in ("running", "waiting"):
            return base
        if status == "completed":
            return "#86d8ab"
        if status in ("error", "errored"):
            return "#f3afa7"
        return "#6b7685"

    if topo == "solo":
        aid = next(iter(state.agent_order), "agent")
        color = _status_color(aid)
        return [
            f"   [{color}]┌────────┐[/]",
            f"   [{color}]│  {aid[:6]:<6}│[/]",
            f"   [{color}]└────────┘[/]",
        ]
    if topo == "triad":
        c_coord = _status_color("coordinator")
        c_coder = _status_color("coder")
        c_rev = _status_color("reviewer")
        c_test = _status_color("tester")
        dim = "#3a4552"
        return [
            f"     [{c_coord}]┌───────────┐[/]",
            f"     [{c_coord}]│ coordntr  │[/]",
            f"     [{c_coord}]└─────┬─────┘[/]",
            f"     [{dim}]      ▼      [/]",
            f"     [{c_coder}]┌───────────┐[/]",
            f"     [{c_coder}]│   coder   │[/]",
            f"     [{c_coder}]└───────────┘[/]",
            f"     [{dim}]     / \\     [/]",
            f"     [{dim}]┌───┘   └───┐[/]",
            f"     [{dim}]▼           ▼[/]",
            f"  [{c_rev}]┌────┐[/]      [{c_test}]┌────┐[/]",
            f"  [{c_rev}]│rev │[/]      [{c_test}]│test│[/]",
            f"  [{c_rev}]└────┘[/]      [{c_test}]└────┘[/]",
        ]
    # Fallback: use graph_rows from init, else list agents vertically.
    rows = getattr(state, "graph_rows", None) or []
    if rows:
        return [f"[#6b7685]{str(r)}[/]" for r in rows]
    out = []
    for aid in state.agent_order:
        color = _status_color(aid)
        out.append(f"[{color}]● {aid}[/]")
    if not out:
        out = ["[#6b7685](no topology)[/]"]
    return out


STATUS_META: dict[str, str] = {
    "idle":      "idle",
    "running":   "edit",
    "waiting":   "watch",
    "completed": "done",
    "error":     "error",
}

# Textual CSS — 2-pane grid, dark palette, minimal chrome.
CSS = """
Screen { background: #0b1016; }

#strip {
    dock: top;
    height: auto;
    min-height: 1;
    padding: 0 1;
    color: #8796a7;
    background: #141a22;
}
#main { layout: horizontal; }
#rail {
    width: 28;
    min-width: 22;
    padding: 0 1;
    background: #0b1016;
    border-right: solid #1b2330;
}
#repl-col { width: 1fr; layout: vertical; }
#stream {
    padding: 1 2 0 2;
    overflow-y: auto;
    height: 1fr;
}
#composer {
    dock: bottom;
    height: auto;
    padding: 0 1;
    border-top: solid #1b2330;
    background: #0e141c;
}
#query-input {
    height: 3;
    border: round #3966a8;
    background: #141a22;
    padding: 0 1;
    color: #ecf1f6;
}
#query-input:focus { border: round #94bfff; }
.rail-section { margin-bottom: 1; }
.rail-heading {
    color: #6b7685;
    text-style: bold;
}
.rail-line {
    color: #c4ced9;
}
.turn {
    margin: 1 0 0 0;
}
.turn-head { color: #8796a7; }
.turn-body { color: #c4ced9; }
.whisper {
    color: #6b7685;
    margin: 0;
    padding: 0 2;
}
.block {
    background: #0f141c;
    border-left: thick #3a4552;
    padding: 0 1;
    margin: 1 0 0 2;
    color: #c4ced9;
}
.block-hdr { color: #8796a7; }
.block-ok  { color: #86d8ab; }
.block-run { color: #94bfff; }
.block-err { color: #f3afa7; }
.composer-hint {
    color: #6b7685;
    padding: 0 1;
}
.milestone {
    color: #6b7685;
    margin: 1 0 0 0;
    padding: 0 2;
}
#live-bar {
    height: auto;
    min-height: 1;
    color: #8796a7;
    padding: 0 1;
}
#slash-palette {
    height: auto;
    padding: 0 1;
    background: #0e141b;
    border: tall #1b2330;
    color: #8796a7;
}
.hidden { display: none; }
.block-accept {
    color: #8796a7;
    padding: 0 1;
    margin: 0 0 0 2;
}
"""


class StatusStrip(Static):
    """Top-row status context: workdir · branch · topology · live pill · burn metrics."""

    def __init__(self, state: "OrbReplTUI") -> None:
        super().__init__(id="strip")
        self.state = state

    def refresh_content(self) -> None:
        s = self.state
        wd = s.workdir or "—"
        topo = s.topology or "—"
        live = s.live_text or "idle"
        sid = (s.session_id or "")[:8] or "—"
        # Derive plan N/M progress + meter bar from plan_items.
        done, total, meter = _plan_progress(s.plan_items)
        branch = getattr(s, "branch", "") or f"orb/s{sid}"
        # Row 1: brand / workdir / branch / topology / live pill
        row1 = (
            f"[bold #94bfff]ORB[/] · [#c4ced9]{wd}[/] · "
            f"[#8796a7]branch[/] [#c4ced9]{branch}[/] · "
            f"[#8796a7]topology[/] [#c4ced9]{topo}[/] · "
            f"[#86d8ab]●[/] [#c4ced9]{live}[/]"
        )
        # Row 2: plan meter + burn metrics + session id.
        burn = f"{s.elapsed:.1f}s · {s.message_count} tok · {s.budget_remaining} left"
        plan_str = f"[#8796a7]plan[/] [bold #c4ced9]{done}/{total}[/] [#94bfff]{meter}[/]"
        row2 = (
            f"{plan_str}   [#8796a7]{burn}[/] · "
            f"[#8796a7]session[/] [#c4ced9]{sid}[/]"
        )
        self.update(f"{row1}\n{row2}")


class ContextRail(Static):
    """Left rail: Agents · Plan · Changes · Scope sections."""

    def __init__(self, state: "OrbReplTUI") -> None:
        super().__init__(id="rail")
        self.state = state

    def refresh_content(self) -> None:
        s = self.state
        lines: list[str] = []
        lines.append("[bold #6b7685]AGENTS[/]  [dim]\\[1–5][/]")
        for aid in s.agent_order:
            info = s.agents.get(aid)
            if not info:
                continue
            color = AGENT_STYLE.get(aid) or AGENT_STYLE.get(info.get("role", "").lower()) or "#c4ced9"
            status = str(info.get("status") or "idle")
            meta = STATUS_META.get(status, status)
            dot = "●" if status in ("running", "waiting") else ("✓" if status == "completed" else "○")
            label = AGENT_LABELS.get(aid, aid.title())
            if status in ("running", "waiting"):
                # Current agent: accent marker + subtle background tint.
                lines.append(
                    f"[on rgb(20,30,45)][{color}]▎{dot} {label}[/]  "
                    f"[#6b7685]{meta}[/][/on rgb(20,30,45)]"
                )
            elif status == "completed":
                lines.append(f"  [#86d8ab]{dot}[/] [#c4ced9]{label}[/]  [#6b7685]{meta}[/]")
            else:
                lines.append(f"  [#6b7685]{dot} {label}[/]  [#6b7685]{meta}[/]")
        lines.append("")

        # Topology — small ASCII graph (static layout for bundled topologies).
        lines.append("[bold #6b7685]TOPOLOGY[/]  [dim]\\[g][/]")
        lines.extend(_render_topology_graph(s))
        lines.append("")

        # Plan — synthesized from plan_steps (done/now/todo) if present.
        lines.append("[bold #6b7685]PLAN[/]  [dim]\\[p][/]")
        if s.plan_items:
            for kind, text in s.plan_items:
                if kind == "done":
                    lines.append(f"[#86d8ab]✓[/] [#8796a7]{text}[/]")
                elif kind == "now":
                    lines.append(f"[#94bfff]●[/] [#c4ced9]{text}[/]")
                else:
                    lines.append(f"[#6b7685]○ {text}[/]")
        else:
            lines.append("[#6b7685](no plan yet)[/]")
        lines.append("")

        # Changes
        lines.append("[bold #6b7685]CHANGES[/]  [dim]\\[d][/]")
        if s.file_changes:
            for path, fc in s.file_changes.items():
                add = fc.get("added", 0)
                rem = fc.get("removed", 0)
                stat = f"[#86d8ab]+{add}[/]"
                if rem:
                    stat += f" [#f3afa7]−{rem}[/]"
                short = path if len(path) <= 22 else "…" + path[-21:]
                lines.append(f"[#c4ced9]{short}[/]  {stat}")
        else:
            lines.append("[#6b7685](no writes yet)[/]")
        lines.append("")

        # Scope — @-mentions the user has typed into the composer.
        lines.append("[bold #6b7685]SCOPE[/]  [dim]\\[@][/]")
        scope_paths = getattr(s, "scope_paths", None) or []
        if scope_paths:
            for path in scope_paths:
                short = path if len(path) <= 22 else "…" + path[-21:]
                lines.append(f"[#94bfff]@[/] [#c4ced9]{short}[/]")
        lines.append("[#6b7685]+ add with @[/]")

        self.update("\n".join(lines))


class ReplStream(VerticalScroll):
    """Scrollable list of turns (messages / tool blocks)."""

    def __init__(self) -> None:
        super().__init__(id="stream")

    def append_line(self, widget: Static) -> None:
        self.mount(widget)
        self.scroll_end(animate=False)


def _fmt_timestamp(ts: float) -> str:
    """``H:MM:SS`` short timestamp."""
    secs = max(0, int(ts))
    return f"{secs // 3600}:{(secs // 60) % 60:02d}:{secs % 60:02d}"


class Turn(Static):
    """One entry in the REPL stream.

    v2 design: a fixed-width left lane (``_LANE_WIDTH`` columns) carries
    the speaker, timestamp, and model stacked vertically. The body sits
    to the right, visually separated by a left border drawn per-line
    with ``│``. The lane width is modeled on the HTML reference's
    ``grid-template-columns: 88px 1fr`` — in monospace that's roughly
    a 10-char column so we keep the Textual layout tight but readable.
    """

    _LANE_WIDTH = 10  # chars before the │ separator

    def __init__(self, speaker: str, elapsed: float, body: str, *, model: str = "") -> None:
        super().__init__()
        color = AGENT_STYLE.get(speaker, "#8796a7")
        label = AGENT_LABELS.get(speaker, speaker or "?")
        ts = _fmt_timestamp(elapsed)
        mdl = (model or "").strip()

        # Left-lane cells. Pad/truncate each to the lane width so the
        # body column stays aligned even when ``label`` or ``model`` is
        # longer than ``_LANE_WIDTH``.
        lane_cells = [
            (label, color, True),   # speaker — agent color, bold
            (ts, "#6b7685", False),
            (mdl, "#6b7685", False) if mdl else ("", "", False),
        ]

        body_lines = (body or "").split("\n") or [""]

        out: list[str] = []
        rows = max(len(lane_cells), len(body_lines))
        for i in range(rows):
            lane_text, lane_color, lane_bold = (lane_cells[i] if i < len(lane_cells) else ("", "", False))
            body_line = body_lines[i] if i < len(body_lines) else ""
            # Truncate lane text to fit.
            cell = lane_text[: self._LANE_WIDTH]
            cell_padded = cell.ljust(self._LANE_WIDTH)
            if lane_text:
                style = f"bold {lane_color}" if lane_bold else lane_color
                lane_markup = f"[{style}]{cell_padded}[/]"
            else:
                lane_markup = cell_padded
            sep = f"[{color}]│[/]"
            out.append(f"{lane_markup} {sep} [#c4ced9]{body_line}[/]")

        self.update("\n".join(out))
        self.add_class("turn")


class Whisper(Static):
    """Compact single-line entry for internal events.

    Distinct from ``Turn``: no full lane, no speaker bar — just a dim
    agent-colored dot + text. Used for agent_activity pings, status
    transitions, and other progress signals the user wants to *see* but
    doesn't need to read word-for-word the way they read messages.
    """

    def __init__(self, speaker: str, elapsed: float, body: str) -> None:
        super().__init__()
        color = AGENT_STYLE.get(speaker, "#8796a7")
        label = AGENT_LABELS.get(speaker, speaker or "?")
        ts = _fmt_timestamp(elapsed)
        # Indent slightly so the whisper sits visually under the
        # preceding turn. Compact, one line, muted body.
        self.update(
            f"  [{color}]·[/] [#6b7685]{ts}[/] [#6b7685]{label}[/]  [#8796a7]{body}[/]"
        )
        self.add_class("whisper")


class ToolBlock(Static):
    """Inline tool-call block inside a turn — header + optional body.

    v2: every rendered line is prefixed with an agent-colored ``┃`` so
    the block visually "attaches" to its speaker. Optionally an
    ``accept`` bar is rendered as a footer row with kbd/label chips.
    """

    def __init__(
        self,
        *,
        glyph: str,
        label: str,
        meta: str = "",
        pill: str = "",
        pill_kind: str = "ok",
        body: str = "",
        agent: str = "",
        accept: list[str] | None = None,
    ) -> None:
        super().__init__()
        pill_color = {
            "ok":   "#86d8ab",
            "run":  "#94bfff",
            "warn": "#f0c982",
            "err":  "#f3afa7",
        }.get(pill_kind, "#8796a7")
        bar_color = AGENT_STYLE.get(agent, "#3a4552")

        head = f"[#c4ced9]{glyph}[/] [bold #ecf1f6]{label}[/]"
        if meta:
            head += f"  [#6b7685]{meta}[/]"
        if pill:
            # Wrap the pill in U+2595 ▕ / U+258F ▏ so it reads like a bracketed
            # capsule without confusing Textual's markup parser with literal
            # square-brackets inside a tag.
            head += f"  [{pill_color}]▕ {pill} ▏[/]"

        raw_lines = [head]
        if body:
            raw_lines.extend(body.split("\n"))

        # Prefix every line with an agent-colored thick vertical bar so
        # the block visually hangs off its speaker's color.
        prefix = f"[{bar_color}]┃[/] "
        lines = [f"{prefix}{line}" for line in raw_lines]

        # Accept bar — rendered as a separate footer row, same prefix,
        # so it reads as "part of the block" visually. Keys are shown
        # as `y/accept` style bindings, colored by positive/negative
        # semantics where recognizable.
        if accept:
            chips: list[str] = []
            for entry in accept:
                key, _, lbl = entry.partition("/")
                key = key.strip() or entry
                lbl = (lbl or entry).strip()
                tone = "#86d8ab"
                low_lbl = lbl.lower()
                if low_lbl.startswith("reject") or low_lbl.startswith("no") or low_lbl == "n":
                    tone = "#f3afa7"
                elif low_lbl.startswith("edit"):
                    tone = "#f0c982"
                chip = f"[bold {tone}]{key}[/][#6b7685]/[/][#c4ced9]{lbl}[/]"
                chips.append(chip)
            accept_row = f"{prefix}[#6b7685]apply?[/]  " + "  ".join(chips)
            lines.append(accept_row)

        self.update("\n".join(lines))
        self.add_class("block")


class Milestone(Static):
    """Silent separator emitted between plan steps in the REPL stream.

    Renders as a faint ``── step N · label ──────`` centered-ish line,
    mirroring the HTML reference's ``.mst`` block.
    """

    def __init__(self, step: int, label: str, *, width: int = 60) -> None:
        super().__init__()
        label = (label or "").strip() or "next step"
        core = f" step {step} · {label} "
        filler = max(4, width - len(core) - 4)
        text = (
            f"[#6b7685]── {core}[/]"
            f"[#3a4552]{'─' * filler}[/]"
        )
        self.update(text)
        self.add_class("milestone")


class LiveStatusBar(Static):
    """Live activity pill docked above the composer.

    Shows ``● agent · activity · 0.9s`` driven by ``state.live_text``
    plus an ``_activity_started`` timestamp for rolling elapsed.
    """

    def __init__(self, state: "OrbReplTUI") -> None:
        super().__init__(id="live-bar")
        self.state = state

    def refresh_content(self) -> None:
        s = self.state
        text = (s.live_text or "idle").strip()
        started = getattr(s, "_live_started", None)
        dur = ""
        if started is not None:
            delta = max(0.0, time() - float(started))
            dur = f" · {delta:.1f}s"
        # Distinguish agent (before ': ') from activity (after) if the
        # handler provided a formatted "agent: message" string.
        who, _, rest = text.partition(":")
        if rest:
            who = who.strip()
            color = AGENT_STYLE.get(who.lower(), "#94bfff")
            self.update(
                f"[{color}]●[/] [bold {color}]{who}[/] [#8796a7]· {rest.strip()}[/]"
                f"[#6b7685]{dur}[/]"
            )
        else:
            self.update(f"[#6b7685]●[/] [#8796a7]{text}[/][#6b7685]{dur}[/]")


class SlashPalette(Static):
    """Peek palette above the composer listing slash commands.

    Hidden by default via the ``hidden`` CSS class; shown when the
    composer text begins with ``/``. The currently-matched command
    (whichever prefix the user has typed) is highlighted.
    """

    def __init__(self) -> None:
        super().__init__(id="slash-palette")
        self.add_class("hidden")

    def refresh_for(self, text: str) -> None:
        if not text.startswith("/"):
            self.add_class("hidden")
            return
        query = text.split()[0].lower() if text.strip() else "/"
        # Exact-match first (so full ``/help`` highlights just /help);
        # otherwise prefix-match.
        exact = [cmd for cmd in SLASH_COMMANDS if cmd[0] == query]
        if exact:
            matches = exact
        else:
            matches = [cmd for cmd in SLASH_COMMANDS if cmd[0].startswith(query)]
        if not matches:
            matches = list(SLASH_COMMANDS)

        lines: list[str] = []
        for cmd, desc in matches:
            highlight = (cmd == query) or (len(matches) == 1)
            if highlight:
                lines.append(f"[bold #94bfff]{cmd:<12}[/] [#c4ced9]{desc}[/]")
            else:
                lines.append(f"[#94bfff]{cmd:<12}[/] [#6b7685]{desc}[/]")
        self.update("\n".join(lines))
        self.remove_class("hidden")


class ResumeSessionScreen(Screen):
    """Modal showing prior sessions; press a number to switch to one.

    Ported from the old ``orb/cli/tui.py`` picker. Capped at 9 entries
    so the user can press ``1``–``9`` to pick without a cursor; for
    longer histories the dashboard's Resume modal is the right tool.
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("q", "dismiss_screen", "Back"),
    ]

    DEFAULT_CSS = """
    ResumeSessionScreen {
        align: center middle;
        background: rgba(5, 8, 12, 0.72);
    }
    #resume-box {
        width: 88;
        height: auto;
        background: #11161d;
        border: round #2f3b4a;
        padding: 1 2;
    }
    """

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self._sessions = sessions[:9]
        for i in range(1, len(self._sessions) + 1):
            self._bindings.bind(str(i), f"pick({i})", show=False)

    def compose(self) -> ComposeResult:
        yield Static(id="resume-box")

    def on_mount(self) -> None:
        box = self.query_one("#resume-box", Static)
        if not self._sessions:
            box.update(
                "[bold white]Resume session[/bold white]\n\n"
                "[dim]No prior sessions found.[/dim]\n\n"
                "[dim]Esc[/dim] Close"
            )
            return
        lines = ["[bold white]Resume session[/bold white]"]
        lines.append("[dim]Press a number to switch; Esc to close.[/dim]\n")
        for i, s in enumerate(self._sessions, start=1):
            active = " [green]active[/green]" if s.get("active") else ""
            workdir = s.get("workdir") or "(no workdir)"
            sid = s.get("session_id", "")[:12]
            turns = s.get("user_turns", 0)
            topo = s.get("locked_topology") or ""
            meta = []
            if topo:
                meta.append(topo)
            if turns:
                meta.append(f"{turns} turn" + ("" if turns == 1 else "s"))
            meta_str = f"  [dim]({', '.join(meta)})[/dim]" if meta else ""
            lines.append(f"[dim]{i}[/dim]  {workdir}  [dim]{sid}…[/dim]{active}{meta_str}")
        box.update("\n".join(lines))

    async def action_pick(self, index: int) -> None:
        idx = int(index) - 1
        if not (0 <= idx < len(self._sessions)):
            return
        target = self._sessions[idx]
        app = self.app
        self.app.pop_screen()
        if hasattr(app, "_attach_to_session"):
            await app._attach_to_session(target.get("session_id", ""))  # noqa: SLF001

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class OrbReplTUI(App[None]):
    """Daemon-backed REPL TUI matching the orb-design-system/orb-tui design."""

    CSS = CSS
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+enter", "submit_input", "Send", show=False),
        Binding("escape", "cancel_input", "Cancel"),
        Binding("ctrl+r", "resume_session", "Resume"),
        Binding("ctrl+k", "cancel_run", "Stop", show=True),
        Binding("tab", "focus_input", "Focus", show=False),
        Binding("question_mark", "show_help", "?"),
    ]

    def __init__(
        self,
        *,
        server_host: str = "127.0.0.1",
        server_port: int = DEFAULT_DASHBOARD_PORT,
        server_scheme: str = "http",
        topology: str = "auto",
        budget: int = 200,
        show_logs: bool = False,
        initial_query: str | None = None,
        exit_after_run: bool = False,
        session_id: str = "",
    ) -> None:
        super().__init__()
        self._server_scheme = server_scheme
        self._server_host = server_host
        self._server_port = server_port
        self._topology = topology
        self._budget = budget
        self._initial_query = initial_query
        self._exit_after_run = exit_after_run

        # Event state (mirrors what the server broadcasts). Seeding
        # ``session_id`` here means the TUI can start routing inject /
        # run requests immediately to the freshly created session the
        # CLI wrapper minted — without waiting for the WS ``init``
        # broadcast.
        self.session_id: str = session_id or ""
        self.workdir: str = ""
        self.topology: str = topology
        self.live_text: str = ""
        self.elapsed: float = 0.0
        self.message_count: int = 0
        self.budget_remaining: int = budget
        self.agents: dict[str, dict] = {}
        self.agent_order: list[str] = []
        self.edges: list[tuple[str, str]] = []
        self.graph_rows: list[str] = []
        self.file_changes: dict[str, dict] = {}
        self.plan_items: list[tuple[str, str]] = []
        # @-mentions the user has typed into the composer (most-recent,
        # deduplicated). Capped at ``SCOPE_MAX`` so the rail stays compact.
        self.scope_paths: list[str] = []
        # Wall-clock when the current ``live_text`` was set; LiveStatusBar
        # renders an elapsed ``Ns`` pill driven off this + a refresh timer.
        self._live_started: float | None = None
        # Lifecycle state machine — mirrors the server's RunState enum.
        # When `idle | completed | errored`, the next submit starts a
        # fresh run via POST /runs. While in-flight, submits inject into
        # the active run via POST /runs/inject.
        self.run_state: str = "idle"
        self._t0 = time()
        self._rendered_turns: deque[str] = deque(maxlen=2048)
        self._http_session: Any = None
        self._ws_task: asyncio.Task | None = None

    # ── Widget tree ───────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield StatusStrip(self)
        with Horizontal(id="main"):
            yield ContextRail(self)
            with Vertical(id="repl-col"):
                yield ReplStream()
                with Vertical(id="composer"):
                    yield LiveStatusBar(self)
                    yield SlashPalette()
                    ta = TextArea(id="query-input")
                    ta.show_line_numbers = False
                    yield ta
                    yield Static(
                        "[#6b7685]^↵ send · ↵ newline · /plan · /rewind · /model · /help[/]",
                        classes="composer-hint",
                    )
        yield Footer()

    async def on_mount(self) -> None:
        import aiohttp
        self._http_session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._start_ws_client())
        self._refresh_chrome()
        # Tick the live bar's elapsed counter while an activity is in-flight.
        self.set_interval(0.5, self._tick_live_bar)
        self.query_one("#query-input", TextArea).focus()
        if self._initial_query:
            # Fire off after mount so the WS has a chance to connect.
            asyncio.create_task(self._submit_after_connect(self._initial_query))

    def _tick_live_bar(self) -> None:
        try:
            self.query_one(LiveStatusBar).refresh_content()
        except Exception:  # noqa: BLE001
            pass

    @on(TextArea.Changed, "#query-input")
    def _on_composer_changed(self, event: TextArea.Changed) -> None:
        """Toggle the slash-palette peek as the user types in the composer."""
        try:
            palette = self.query_one(SlashPalette)
        except Exception:  # noqa: BLE001
            return
        text = (event.text_area.text or "")
        palette.refresh_for(text)

    async def on_unmount(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
        if self._http_session is not None:
            await self._http_session.close()

    # ── WebSocket client ─────────────────────────────────────────────────

    async def _start_ws_client(self) -> None:
        import aiohttp
        ws_scheme = "wss" if self._server_scheme == "https" else "ws"
        url = f"{ws_scheme}://{self._server_host}:{self._server_port}/api/v1/ws"
        while True:
            try:
                async with self._http_session.ws_connect(url, heartbeat=30) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                self._handle_server_event(json.loads(msg.data))
                            except json.JSONDecodeError as exc:
                                logger.debug("WS JSON decode: %s", exc)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as exc:  # noqa: BLE001
                logger.debug("WS lost: %s", exc)
            await asyncio.sleep(1)

    # ── Server event dispatch ─────────────────────────────────────────────

    def _handle_server_event(self, data: dict) -> None:
        t = data.get("type")
        if t == "init":
            self._handle_init(data)
        elif t == "message":
            self._handle_message(data)
        elif t == "agent_status":
            self._handle_agent_status(data)
        elif t == "agent_activity":
            self._handle_agent_activity(data)
        elif t == "complete":
            self._handle_complete(data)
        elif t == "run_complete":
            self._handle_run_complete(data)
        elif t == "file_write":
            self._handle_file_write(data)
        elif t == "plan_step":
            self._handle_plan_step(data)
        elif t == "stats":
            self.message_count = int(data.get("message_count") or self.message_count)
            self.elapsed = float(data.get("elapsed") or self.elapsed)
            self.budget_remaining = max(0, self._budget - self.message_count)
            self._refresh_chrome()
        elif t == "run_state_changed":
            to_state = str(data.get("to") or data.get("state") or "").strip()
            from_state = str(data.get("from") or "").strip()
            if to_state:
                self.run_state = to_state
                # Surface transitions in the stream so users can see the
                # run's lifecycle (especially errors that would otherwise
                # just hang the UI). Similar to Claude Code's visible
                # agent progress.
                if to_state == "planning" and from_state in {"idle", "completed", "errored"}:
                    self._emit_turn("system", "[#94bfff]● run started[/]")
                elif to_state == "errored":
                    reason = str(data.get("reason") or data.get("error") or "")
                    msg = "[#f3afa7]✗ run errored[/]"
                    if reason:
                        msg += f"\n[#c4ced9]{reason[:400]}[/]"
                    self._emit_turn("system", msg)
                elif to_state == "stopping":
                    self._emit_turn("system", "[#f0c982]… stopping[/]")
                self._refresh_chrome()

    def _handle_init(self, data: dict) -> None:
        incoming_sid = data.get("session_id") or ""
        # Only wipe the stream when we're genuinely switching sessions.
        # The server re-broadcasts ``init`` at every run's planning stage
        # so the dashboard gets fresh agent/model info — but wiping the
        # stream there erases the user's just-sent message, and every
        # intermediate event that fired before the init landed. That was
        # the "chat disappears" bug.
        session_changed = bool(incoming_sid) and incoming_sid != self.session_id
        self.session_id = incoming_sid or self.session_id
        self.workdir = data.get("workdir") or self.workdir
        self.run_state = str(data.get("run_state") or "idle")
        topo = ((data.get("plan") or {}).get("topology") or {}).get("id")
        if topo:
            self.topology = TOPOLOGY_LABELS.get(topo, topo)
        self.agents = {}
        self.agent_order = []
        # Pull edges + optional graph-view rows from the plan payload so
        # the TOPOLOGY section can render a faithful view for unknown /
        # custom topologies.
        plan = data.get("plan") or {}
        topo_info = plan.get("topology") or {}
        edges_raw = topo_info.get("edges") or []
        parsed_edges: list[tuple[str, str]] = []
        for e in edges_raw:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                parsed_edges.append((str(e[0]), str(e[1])))
            elif isinstance(e, dict):
                src = e.get("from") or e.get("src") or ""
                dst = e.get("to") or e.get("dst") or ""
                if src and dst:
                    parsed_edges.append((str(src), str(dst)))
        self.edges = parsed_edges
        graph = topo_info.get("graph") or {}
        self.graph_rows = list(graph.get("rows") or [])
        for a in data.get("agents") or []:
            aid = a.get("id") or ""
            if not aid:
                continue
            self.agents[aid] = {
                "role": a.get("role") or aid,
                "status": a.get("status") or "idle",
                "model": a.get("model") or "",
            }
            self.agent_order.append(aid)
        stats = data.get("stats") or {}
        self.message_count = int(stats.get("message_count") or 0)
        self.elapsed = float(stats.get("elapsed") or 0.0)
        self.budget_remaining = max(0, self._budget - self.message_count)
        # Only clear when the user actually switched sessions. During a
        # run the server re-broadcasts init with fresh agent/model state;
        # wiping on every re-broadcast erases the user's in-flight
        # message + every intermediate event emitted so far.
        stream = self.query_one(ReplStream)
        if session_changed or not stream.children:
            stream.remove_children()
            self._emit_turn(
                "system",
                f"session started · workdir [#c4ced9]{self.workdir or '—'}[/] · topology [#c4ced9]{self.topology}[/]",
            )
            for m in (data.get("messages") or [])[-12:]:
                self._render_message(m)
        self._refresh_chrome()

    def _handle_message(self, data: dict) -> None:
        self._render_message(data)
        self.message_count += 1
        self.budget_remaining = max(0, self._budget - self.message_count)
        self._refresh_chrome()

    def _render_message(self, m: dict) -> None:
        frm = m.get("from") or "system"
        content = m.get("content") or ""
        self._emit_turn(frm, content, model=m.get("model") or "")

    def _handle_agent_status(self, data: dict) -> None:
        aid = data.get("agent") or ""
        status = data.get("status") or "idle"
        prev = self.agents.get(aid, {}).get("status")
        if aid in self.agents:
            self.agents[aid]["status"] = status
            if data.get("model"):
                self.agents[aid]["model"] = data.get("model")
        # Only emit on meaningful transitions so we don't flood the stream
        # when status gets re-broadcast with the same value. Status changes
        # are internal events — render as compact whispers, not full turns.
        if prev and prev != status and status in {"running", "completed", "error", "errored"}:
            glyph = "●" if status == "running" else "✓" if status == "completed" else "✗"
            tone = "#94bfff" if status == "running" else "#86d8ab" if status == "completed" else "#f3afa7"
            self._emit_whisper(aid, f"[{tone}]{glyph}[/] {status}")
        # Keep the LiveStatusBar pill fresh so a newly-running agent
        # surfaces before the next activity event.
        if status == "running":
            self.live_text = f"{AGENT_LABELS.get(aid, aid)}: running"
            self._live_started = time()
        self._refresh_chrome()

    def _handle_agent_activity(self, data: dict) -> None:
        aid = data.get("agent") or ""
        activity = data.get("activity") or ""
        details = data.get("details") or {}
        if activity.startswith("⏳ Waiting for user"):
            full = (details or {}).get("full_content") if isinstance(details, dict) else None
            prompt = full or activity.replace("⏳ Waiting for user:", "").strip()
            self.live_text = f"{AGENT_LABELS.get(aid, aid)} is waiting"
            self._live_started = time()
            self._emit_turn(aid, f"[#f0c982]? {prompt}[/]")
        elif activity:
            # Show the activity inline so users see intermediate progress
            # (classifier calls, reads, retries) — not just the final run
            # complete. Render as a compact whisper so it reads as
            # secondary to the user/agent conversation turns.
            self.live_text = f"{AGENT_LABELS.get(aid, aid)}: {activity}"
            self._live_started = time()
            self._emit_whisper(aid, activity)
        self._refresh_chrome()

    def _handle_complete(self, data: dict) -> None:
        aid = data.get("agent") or "system"
        result = data.get("result") or ""
        if result:
            self._emit_turn(aid, result[:600])

    def _handle_run_complete(self, data: dict) -> None:
        agent = data.get("agent") or "system"
        elapsed = float(data.get("elapsed") or self.elapsed)
        result = data.get("result") or ""
        self._emit_turn(agent, f"[#86d8ab]✓ run complete · {elapsed:.1f}s[/]\n{result[:800]}")
        self.live_text = "done"
        self._live_started = None
        self._refresh_chrome()
        if self._exit_after_run:
            self.call_after_refresh(self.action_quit)

    def _handle_file_write(self, data: dict) -> None:
        path = data.get("path") or ""
        if not path:
            return
        agent = data.get("agent") or ""
        old = data.get("old_content") or ""
        new = data.get("content") or ""
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        added = max(0, len(new_lines) - len(old_lines))
        removed = max(0, len(old_lines) - len(new_lines))
        # Over-count for non-length-diff cases — close enough for the rail display.
        self.file_changes[path] = {"agent": agent, "added": added, "removed": removed}
        self._refresh_chrome()
        # Render a tool block inline so the stream shows what was written.
        block_body = f"[#6b7685]wrote {len(new_lines)} lines · {len(new)} bytes[/]"
        # TODO: wire these keys to real accept/reject actions that hit
        # the daemon. For now they exist only to surface the v2 design.
        accept = ["y/accept", "a/accept all", "e/edit", "n/reject"]
        self._emit_block(
            agent,
            glyph="±",
            label="edit_file",
            meta=path,
            pill="applied",
            pill_kind="ok",
            body=block_body,
            accept=accept,
        )

    def _handle_plan_step(self, data: dict) -> None:
        title = data.get("title") or "Planning update"
        detail = data.get("detail") or ""
        # Emit the milestone separator BEFORE appending so the step
        # number reflects "this one" (1-indexed, matches the HTML ref).
        step_num = len(self.plan_items) + 1
        label = detail or title
        self._emit_milestone(step_num, label)
        self.plan_items.append(("now", title))
        # Promote previous 'now' to 'done'.
        promoted: list[tuple[str, str]] = []
        seen_new = False
        for kind, text in reversed(self.plan_items):
            if not seen_new and kind == "now":
                seen_new = True
                promoted.append((kind, text))
            elif kind == "now":
                promoted.append(("done", text))
            else:
                promoted.append((kind, text))
        self.plan_items = list(reversed(promoted))
        self._refresh_chrome()
        if detail:
            self._emit_turn("system", f"[#94bfff]plan[/] · {title} — {detail}")

    # ── Turn / block emission ────────────────────────────────────────────

    def _emit_whisper(self, speaker: str, body: str) -> None:
        """Mount a compact internal-event entry. See ``Whisper``."""
        try:
            stream = self.query_one(ReplStream)
        except Exception:  # noqa: BLE001
            return
        stream.mount(Whisper(speaker, self.elapsed, body))
        stream.scroll_end(animate=False)

    def _emit_turn(self, speaker: str, body: str, *, model: str = "") -> None:
        stream = self.query_one(ReplStream)
        stream.mount(Turn(speaker, self.elapsed, body, model=model))
        stream.scroll_end(animate=False)

    def _emit_block(
        self,
        agent: str,
        *,
        glyph: str,
        label: str,
        meta: str = "",
        pill: str = "",
        pill_kind: str = "ok",
        body: str = "",
        accept: list[str] | None = None,
    ) -> None:
        stream = self.query_one(ReplStream)
        color = AGENT_STYLE.get(agent, "#8796a7")
        head = Static(f"[{color}]◆[/] [bold {color}]{AGENT_LABELS.get(agent, agent)}[/]  [#6b7685]{_fmt_timestamp(self.elapsed)}[/]", classes="turn")
        stream.mount(head)
        stream.mount(
            ToolBlock(
                glyph=glyph,
                label=label,
                meta=meta,
                pill=pill,
                pill_kind=pill_kind,
                body=body,
                agent=agent,
                accept=accept,
            )
        )
        stream.scroll_end(animate=False)

    def _emit_milestone(self, step: int, label: str) -> None:
        """Emit a silent step separator into the stream."""
        stream = self.query_one(ReplStream)
        stream.mount(Milestone(step, label))
        stream.scroll_end(animate=False)
        stream.scroll_end(animate=False)

    def _track_scope_mentions(self, text: str) -> None:
        """Extract ``@path`` mentions from ``text`` and record them.

        Order-preserving dedup — once a path is in ``scope_paths`` it
        stays at its earliest position, so users don't see their rail
        re-order as they type. Capped at :data:`SCOPE_MAX` entries.
        """
        for match in SCOPE_RE.findall(text or ""):
            if match and match not in self.scope_paths:
                self.scope_paths.append(match)
        if len(self.scope_paths) > SCOPE_MAX:
            # Keep the most recent ones when we overflow.
            self.scope_paths = self.scope_paths[-SCOPE_MAX:]

    def _refresh_chrome(self) -> None:
        try:
            self.query_one(StatusStrip).refresh_content()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.query_one(ContextRail).refresh_content()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.query_one(LiveStatusBar).refresh_content()
        except Exception:  # noqa: BLE001
            pass

    # ── Actions ──────────────────────────────────────────────────────────

    async def action_submit_input(self) -> None:
        ta = self.query_one("#query-input", TextArea)
        text = (ta.text or "").strip()
        if not text:
            return
        # Track @-mentions for the SCOPE rail before we clear the textarea.
        self._track_scope_mentions(text)
        ta.text = ""
        # Slash commands — Claude-Code-style local shortcuts that don't
        # round-trip to the daemon.
        if text.startswith("/"):
            await self._run_slash_command(text)
            return
        if not self.session_id:
            self._emit_turn("system", "[#f3afa7]no session — reconnect to the daemon first[/]")
            return
        self._emit_turn("user", text)
        # If no run is in flight, start one. Inject only works while the
        # orchestrator is running — firing inject on an idle session is
        # the bug that made the TUI silently "freeze" after the first
        # Enter: the daemon just 409'd the inject and nothing else ever
        # happened.
        terminal_states = {"idle", "completed", "errored"}
        if self.run_state in terminal_states:
            start_url = self._session_url("/runs")
            payload: dict = {"query": text}
            # Prefer the topology the session is already locked to (set from
            # the init payload's plan.topology.id once the session has been
            # classified once). Falling back to the CLI-time topology means
            # a session started with --topology=auto used to keep sending
            # "auto" forever, so every follow-up run could re-classify.
            effective_topology = self.topology or self._topology
            if effective_topology and effective_topology != "auto":
                payload["topology"] = effective_topology
            try:
                async with self._http_session.post(start_url, json=payload) as resp:
                    body_text = await resp.text()
                    if resp.status >= 400:
                        self._emit_turn("system", f"[#f3afa7]start_run failed ({resp.status}): {body_text[:200]}[/]")
                        return
                # Optimistic: flip to 'planning' so subsequent submits inject
                # instead of double-starting. The server will broadcast the
                # real state shortly.
                self.run_state = "planning"
                self._refresh_chrome()
            except Exception as exc:  # noqa: BLE001
                self._emit_turn("system", f"[#f3afa7]start_run send failed: {exc}[/]")
            return
        # Run is in-flight — inject into the active run.
        inject_url = self._session_url("/runs/inject")
        try:
            async with self._http_session.post(
                inject_url,
                json={"to": "coordinator", "message": text},
            ) as resp:
                body_text = await resp.text()
                if resp.status >= 400:
                    self._emit_turn("system", f"[#f3afa7]inject failed ({resp.status}): {body_text[:200]}[/]")
        except Exception as exc:  # noqa: BLE001
            self._emit_turn("system", f"[#f3afa7]send failed: {exc}[/]")

    async def _run_slash_command(self, text: str) -> None:
        """Handle inline slash commands. Parity with Claude-Code ergonomics."""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in {"help", "?"}:
            self._emit_turn(
                "system",
                "[#c4ced9]commands:[/]\n"
                "  [#94bfff]/help[/]     show this\n"
                "  [#94bfff]/clear[/]    clear stream\n"
                "  [#94bfff]/stop[/]     stop current run\n"
                "  [#94bfff]/resume[/]   pick a prior session (same as ^r)\n"
                "  [#94bfff]/topology[/] <id>   change topology for new runs\n"
                "  [#94bfff]/quit[/]     exit",
            )
        elif cmd == "clear":
            self.query_one(ReplStream).remove_children()
            self._emit_turn("system", "[#8796a7]stream cleared[/]")
        elif cmd in {"stop", "cancel"}:
            await self.action_cancel_run()
        elif cmd == "resume":
            await self.action_resume_session()
        elif cmd == "topology":
            new = arg.strip().lower()
            if new in TOPOLOGY_LABELS:
                self._topology = new
                self._emit_turn("system", f"[#8796a7]topology set to[/] [#c4ced9]{new}[/] [#6b7685](applies to the next run)[/]")
            else:
                self._emit_turn(
                    "system",
                    f"[#f3afa7]unknown topology: {new!r}[/]\n[#8796a7]valid:[/] {', '.join(TOPOLOGY_LABELS)}",
                )
        elif cmd in {"quit", "exit"}:
            self.call_after_refresh(self.action_quit)
        else:
            self._emit_turn("system", f"[#f3afa7]unknown command:[/] /{cmd}")

    def action_cancel_input(self) -> None:
        ta = self.query_one("#query-input", TextArea)
        ta.text = ""

    def action_focus_input(self) -> None:
        self.query_one("#query-input", TextArea).focus()

    async def action_cancel_run(self) -> None:
        if not self.session_id:
            return
        try:
            async with self._http_session.post(self._session_url("/runs/stop")) as resp:
                await resp.read()
        except Exception:  # noqa: BLE001
            pass

    async def action_resume_session(self) -> None:
        """Open the session picker — lists known sessions from the daemon."""
        sessions: list[dict] = []
        try:
            url = f"{self._server_scheme}://{self._server_host}:{self._server_port}/api/v1/sessions?include=known"
            async with self._http_session.get(url) as resp:
                body = await resp.json()
            if isinstance(body, dict) and body.get("ok"):
                data = body.get("data") or {}
                candidates = data.get("sessions") or []
                current = self.session_id or ""
                sessions = [s for s in candidates if s.get("session_id") != current]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to list known sessions: %s", exc)
        self.push_screen(ResumeSessionScreen(sessions))

    async def _attach_to_session(self, session_id: str) -> None:
        """Switch the TUI's attached session.

        Fetches ``/api/v1/sessions/{sid}/state`` and dispatches the
        payload through ``_handle_init`` so every widget rebuilds
        against the new session. The WS client keeps running and
        picks up subsequent broadcasts via the per-session fanout.

        Refuses error envelopes (non-200 or ``ok: false``) without
        mutating ``session_id`` — otherwise a stale id from the
        picker would silently route every subsequent inject/run
        to a session that no longer exists.
        """
        if not session_id or session_id == self.session_id:
            return
        try:
            url = f"{self._server_scheme}://{self._server_host}:{self._server_port}/api/v1/sessions/{session_id}/state"
            async with self._http_session.get(url) as resp:
                status = resp.status
                body = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch session state for %s: %s", session_id, exc)
            return
        if status != 200 or not isinstance(body, dict) or body.get("ok") is False:
            logger.warning(
                "Refusing to attach to session %s: status=%s body=%s",
                session_id, status, body,
            )
            return
        payload = body.get("data") if "data" in body else body
        if not isinstance(payload, dict):
            return
        payload.setdefault("type", "init")
        payload.setdefault("session_id", session_id)
        self.session_id = session_id
        self._handle_init(payload)

    def action_show_help(self) -> None:
        self._emit_turn(
            "system",
            "[#8796a7]keys:[/] ^↵ send · esc cancel input · ^k stop · ^r resume · ^c quit · ? help",
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _session_url(self, suffix: str) -> str:
        base = f"{self._server_scheme}://{self._server_host}:{self._server_port}"
        if self.session_id:
            return f"{base}/api/v1/sessions/{self.session_id}{suffix}"
        return f"{base}/api/v1{suffix}"

    async def _submit_after_connect(self, query: str) -> None:
        # Wait briefly for the WS to surface a session_id before sending.
        for _ in range(50):
            if self.session_id:
                break
            await asyncio.sleep(0.1)
        ta = self.query_one("#query-input", TextArea)
        ta.text = query
        await self.action_submit_input()


async def attach_tui_repl(
    connect_url: str,
    topology: str = "auto",
    budget: int = 200,
    show_logs: bool = False,
    initial_query: str | None = None,
    exit_after_run: bool = False,
    session_id: str | None = None,
    workdir: str | None = None,
    agent_models: dict[str, str] | None = None,
) -> None:
    """Attach the REPL TUI to a running Orb daemon.

    Unless an existing ``session_id`` is supplied, a fresh session is
    minted via ``POST /api/v1/sessions`` before the TUI mounts — each
    ``orb tui`` invocation gets its own runtime, like the dashboard's
    "New Session" modal. The id is threaded straight into
    :class:`OrbReplTUI` so inject/run requests can start routing
    immediately (no waiting for the WS ``init`` event).
    """
    import aiohttp

    parsed = urlparse(connect_url if "://" in connect_url else f"http://{connect_url}")
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else DEFAULT_DASHBOARD_PORT)
    base = f"{scheme}://{host}:{port}"

    resolved_sid = session_id or ""
    if not resolved_sid:
        # Build the v1 POST body. Omit empty fields so the server picks
        # up its defaults (topology=auto, no workdir, etc.).
        post_body: dict[str, Any] = {}
        if workdir:
            post_body["workdir"] = workdir
        if topology and topology != "auto":
            post_body["topology"] = topology
        if agent_models:
            post_body["agent_models"] = agent_models
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(f"{base}/api/v1/sessions", json=post_body) as resp:
                    body = await resp.json()
            if isinstance(body, dict) and body.get("ok"):
                data = body.get("data") or {}
                resolved_sid = str(data.get("session_id") or "")
            else:
                logger.warning("Could not create session via /api/v1/sessions: %s", body)
        except Exception as exc:  # noqa: BLE001
            # Daemon unreachable — soft-fail so the TUI can mount and
            # surface the real connection error through the WS client.
            logger.warning("Failed to create session on startup: %s", exc)

    await OrbReplTUI(
        server_host=host,
        server_port=port,
        server_scheme=scheme,
        topology=topology,
        budget=budget,
        show_logs=show_logs,
        initial_query=initial_query,
        exit_after_run=exit_after_run,
        session_id=resolved_sid,
    ).run_async()
