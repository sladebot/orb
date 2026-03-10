from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from time import time
from typing import Any

from rich.text import Text as RichText
from textual.app import App, ComposeResult, Screen
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TUIMessage
from textual.reactive import reactive
from textual.widgets import Footer, Input, Label, RichLog, Static
from textual import on, work

from ..messaging.message import Message as OrbMessage, MessageType
from ..llm.types import ModelTier, ModelConfig
from ..orchestrator.types import OrchestratorConfig

# ─── Constants ───────────────────────────────────────────────────────────────

AGENT_COLORS: dict[str, str] = {
    "coordinator": "magenta",
    "coder":       "cyan",
    "reviewer":    "yellow",
    "reviewer_a":  "yellow",
    "reviewer_b":  "dark_orange",
    "tester":      "green",
}

AGENT_LABELS: dict[str, str] = {
    "coordinator": "Coordinator",
    "coder":       "Coder",
    "reviewer":    "Reviewer",
    "reviewer_a":  "Reviewer A",
    "reviewer_b":  "Reviewer B",
    "tester":      "Tester",
}

AGENT_ORDER = ["coordinator", "coder", "reviewer", "reviewer_a", "reviewer_b", "tester"]

STATUS_ICON: dict[str, str] = {
    "idle":      "○",
    "waiting":   "◔",
    "running":   "●",
    "completed": "✓",
    "error":     "✗",
}

STATUS_COLOR: dict[str, str] = {
    "idle":      "dim",
    "waiting":   "yellow",
    "running":   "green",
    "completed": "cyan",
    "error":     "red",
}

MSG_TYPE_COLOR: dict[str, str] = {
    "task":     "yellow",
    "response": "white",
    "feedback": "orange1",
    "complete": "green",
    "system":   "dim",
}

SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

AGENT_KEY_MAP: dict[str, str] = {
    "coordinator": "1",
    "coder":       "2",
    "reviewer":    "3",
    "reviewer_a":  "4",
    "reviewer_b":  "5",
    "tester":      "6",
}

# ─── Graph layout definitions ─────────────────────────────────────────────────
# Each layout is a list of rows; each row is a list of segments.
# Segment types:
#   {"t": "text", "s": "style"}       — static text
#   {"node": "agent_id"}               — live agent node (icon + label)
#   {"edge": ("from","to"), "t":"─"}  — edge char, highlighted when active

TOPOLOGY_LAYOUTS: dict[str, list[list[dict]]] = {
    "triangle": [
        [{"t": "       "}, {"node": "coordinator"}],
        [{"t": "            "}, {"edge": ("coordinator", "coder"), "t": "│"}],
        [{"t": "  "}, {"node": "coder"}, {"t": "  "},
         {"edge": ("coder", "reviewer"), "t": "───  "},
         {"node": "reviewer"}],
        [{"t": "  "}, {"edge": ("coder", "tester"), "t": "│"},
         {"t": "                 "}, {"edge": ("reviewer", "tester"), "t": "│"}],
        [{"t": "  "}, {"node": "tester"}, {"t": "  "},
         {"edge": ("tester", "reviewer"), "t": "─────────────╯"}],
    ],
    "dual-review": [
        [{"t": "         "}, {"node": "coordinator"}],
        [{"t": "              "}, {"edge": ("coordinator", "coder"), "t": "│"}],
        [{"t": "         "}, {"node": "coder"}],
        [{"t": "        "}, {"edge": ("coder", "reviewer_a"), "t": "╱"},
         {"t": "     "},
         {"edge": ("coder", "reviewer_b"), "t": "╲"}],
        [{"t": "  "}, {"node": "reviewer_a"}, {"t": "  "},
         {"edge": ("reviewer_a", "reviewer_b"), "t": "───  "},
         {"node": "reviewer_b"}],
        [{"t": "       "}, {"edge": ("reviewer_a", "tester"), "t": "╲"},
         {"t": "     "},
         {"edge": ("reviewer_b", "tester"), "t": "╱"}],
        [{"t": "          "}, {"node": "tester"}],
    ],
}


def _short_model(model_id: str) -> str:
    if not model_id:
        return ""
    m = re.search(r"claude-([a-z]+)", model_id, re.I)
    if m:
        return m.group(1)
    return model_id[:10]


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


def _budget_bar(routed: int, budget: int, width: int = 10) -> tuple[str, str]:
    """Returns (bar_str, style) showing budget consumption."""
    pct = min(routed / budget, 1.0) if budget else 0
    filled = round(pct * width)
    bar = "▓" * filled + "░" * (width - filled)
    style = "red" if pct > 0.8 else "yellow" if pct > 0.5 else "green"
    return bar, style


# ─── State ───────────────────────────────────────────────────────────────────

@dataclass
class AgentInfo:
    agent_id: str
    role: str
    status: str = "idle"
    model: str = ""
    msg_count: int = 0
    activity_text: str = ""          # live activity from _emit callback
    messages: list[dict] = field(default_factory=list)   # full history for detail pane
    result: str = ""
    status_since: float = field(default_factory=time)    # when status last changed

    def set_status(self, status: str) -> None:
        if status != self.status:
            if self.status == "running" and status != "running":
                self.activity_text = ""
            self.status = status
            self.status_since = time()

    @property
    def time_in_state(self) -> float:
        return time() - self.status_since


# ─── TUI Messages ─────────────────────────────────────────────────────────────

class OrbBusEvent(TUIMessage):
    def __init__(self, event_name: str, msg: OrbMessage) -> None:
        super().__init__()
        self.event_name = event_name
        self.orb_msg = msg


class OrbRunComplete(TUIMessage):
    def __init__(self, error: str | None = None,
                 completions: dict[str, str] | None = None) -> None:
        super().__init__()
        self.error = error
        self.completions = completions or {}


class OrbAgentComplete(TUIMessage):
    def __init__(self, agent_id: str, result: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.result = result


class OrbActivityUpdate(TUIMessage):
    def __init__(self, agent_id: str, text: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.text = text


class OrbLogRecord(TUIMessage):
    def __init__(self, level: str, name: str, message: str) -> None:
        super().__init__()
        self.level   = level
        self.name    = name
        self.message = message


# ─── Result Screen ────────────────────────────────────────────────────────────

class ResultScreen(Screen):
    """Full-screen overlay showing all agent results after run completes."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("q",      "dismiss_screen", "Back"),
        Binding("s",      "save_results",   "Save to file"),
    ]

    DEFAULT_CSS = """
    ResultScreen {
        background: #0d1117;
        layout: vertical;
    }
    #rs-header {
        height: 4;
        background: #161b22;
        border-bottom: solid #21262d;
        padding: 1 2;
    }
    #rs-body {
        height: 1fr;
    }
    #rs-log {
        padding: 1 2;
    }
    #rs-footer {
        height: 1;
        background: #161b22;
        border-top: solid #21262d;
        color: #8b949e;
        padding: 0 2;
    }
    """

    def __init__(self, task: str, completions: dict[str, str],
                 elapsed: float, msg_count: int) -> None:
        super().__init__()
        self._task = task
        self._completions = completions
        self._elapsed = elapsed
        self._msg_count = msg_count

    def compose(self) -> ComposeResult:
        yield Static(id="rs-header")
        with VerticalScroll(id="rs-body"):
            yield RichLog(id="rs-log", highlight=True, markup=True, wrap=True)
        yield Static(id="rs-footer")

    def on_mount(self) -> None:
        # Header
        hdr = self.query_one("#rs-header", Static)
        t = RichText()
        t.append("✓ Run Complete\n", style="bold green")
        t.append(f"  Task: {self._task[:80]}\n", style="dim")
        t.append(f"  {self._msg_count} messages  ·  {self._elapsed:.1f}s", style="dim")
        hdr.update(t)

        # Footer
        ftr = self.query_one("#rs-footer", Static)
        ftr.update("[dim][q/Esc] back  [s] save to file[/dim]")

        # Results per agent
        log = self.query_one("#rs-log", RichLog)
        ordered = [a for a in AGENT_ORDER if a in self._completions]
        ordered += [a for a in self._completions if a not in AGENT_ORDER]
        for agent_id in ordered:
            result = self._completions[agent_id]
            color  = AGENT_COLORS.get(agent_id, "white")
            label  = AGENT_LABELS.get(agent_id, agent_id.title())
            log.write(f"[bold {color}]── {label} ──────────────────────────[/bold {color}]")
            log.write(result)
            log.write("")

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()

    def action_save_results(self) -> None:
        import datetime
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"orb_result_{ts}.md"
        lines = [f"# Orb Result\n\n**Task:** {self._task}\n\n"
                 f"**Elapsed:** {self._elapsed:.1f}s  "
                 f"**Messages:** {self._msg_count}\n\n"]
        for agent_id, result in self._completions.items():
            label = AGENT_LABELS.get(agent_id, agent_id.title())
            lines.append(f"## {label}\n\n{result}\n\n")
        with open(path, "w") as f:
            f.writelines(lines)
        log = self.query_one("#rs-log", RichLog)
        log.write(f"\n[green]Saved to {path}[/green]")


# ─── Widgets ──────────────────────────────────────────────────────────────────

class HeaderBar(Static):
    """Top stats bar with budget progress bar."""
    _v: reactive[int] = reactive(0)

    def __init__(self, state: "OrbTUI", **kw: Any) -> None:
        super().__init__(**kw)
        self._state = state

    def watch__v(self, _: int) -> None:
        self.refresh()

    def render(self) -> RichText:
        s = self._state
        t = RichText()
        t.append("  ORB", style="bold magenta")
        t.append("  │  ", style="dim")

        # Run status badge
        badge_style = {
            "Waiting":  ("dim",        "  ○ Ready   "),
            "Running":  ("bold green", "  ● Running "),
            "Complete": ("bold cyan",  "  ✓ Done    "),
            "Error":    ("bold red",   "  ✗ Error   "),
        }.get(s._run_status, ("white", s._run_status))
        t.append(badge_style[1], style=badge_style[0])
        t.append("  │  ", style="dim")

        # Budget bar
        bar, bar_style = _budget_bar(s._routed, s._budget, width=12)
        t.append(f"msgs {s._routed}", style="bold white")
        t.append(f"/{s._budget}  ", style="dim")
        t.append(bar, style=bar_style)

        if s._run_start:
            elapsed = time() - s._run_start
            t.append(f"  {elapsed:.1f}s", style="dim")

        if s._selected_agent and s._selected_agent in s._agents:
            label = AGENT_LABELS.get(s._selected_agent, s._selected_agent)
            color = AGENT_COLORS.get(s._selected_agent, "white")
            t.append("  │  @", style="dim")
            t.append(label, style=f"bold {color}")

        if s._run_status == "Complete":
            t.append("  │  ", style="dim")
            t.append("r=results  s=save", style="dim cyan")

        if s._dashboard_server is not None:
            t.append(f"  │  ::{s._dashboard_port}", style="dim")

        return t

    def bump(self) -> None:
        self._v += 1


class GraphPanel(Static):
    """
    Live graph panel — only rendered once agents are running.
    Shows topology structure with live edge highlighting and agent status.
    """
    _v: reactive[int] = reactive(0)

    def __init__(self, state: "OrbTUI", **kw: Any) -> None:
        super().__init__(**kw)
        self._state = state

    def watch__v(self, _: int) -> None:
        self.refresh()

    def render(self) -> RichText:  # noqa: C901
        s = self._state
        t = RichText()

        if not s._agents:
            t.append("\n  Waiting for task…\n", style="dim italic")
            t.append("  Type below and press Enter to start.\n\n", style="dim")
            t.append("  Topologies available:\n", style="dim")
            t.append("  --topology triangle    ", style="dim")
            t.append("Coordinator → Coder ↔ Reviewer ↔ Tester\n", style="dim cyan")
            t.append("  --topology dual-review ", style="dim")
            t.append("Coder → Reviewer A + Reviewer B\n", style="dim cyan")
            return t

        # ── Topology graph ─────────────────────────────────────────────
        layout = TOPOLOGY_LAYOUTS.get(s._topology_name, [])
        for row in layout:
            for seg in row:
                if "node" in seg:
                    agent_id = seg["node"]
                    info = s._agents.get(agent_id)
                    if not info:
                        t.append(f"[{agent_id}]", style="dim")
                        continue
                    icon   = STATUS_ICON.get(info.status, "○")
                    i_clr  = STATUS_COLOR.get(info.status, "dim")
                    color  = AGENT_COLORS.get(agent_id, "white")
                    label  = AGENT_LABELS.get(agent_id, agent_id)
                    sel    = agent_id == s._selected_agent
                    key    = AGENT_KEY_MAP.get(agent_id, "")
                    name_s = f"bold {color}" + (" reverse" if sel else "")
                    # spinner for running
                    if info.status == "running":
                        spin = SPINNERS[s._tick_count % len(SPINNERS)]
                        t.append(spin + " ", style=f"bold {i_clr}")
                    else:
                        t.append(icon + " ", style=f"bold {i_clr}")
                    t.append(label, style=name_s)
                    if key:
                        t.append(f"[{key}]", style="dim")
                elif "edge" in seg:
                    a, b   = seg["edge"]
                    chars  = seg.get("t", "─")
                    fwd    = s._active_edges.get((a, b), 0)
                    rev    = s._active_edges.get((b, a), 0)
                    active = max(fwd, rev) >= s._tick_count
                    style  = "bold bright_cyan" if active else "dim"
                    t.append(chars, style=style)
                else:
                    t.append(seg.get("t", ""), style=seg.get("s", "dim"))
            t.append("\n")

        # ── Agent roster ───────────────────────────────────────────────
        t.append("\n  " + "─" * 42 + "\n\n", style="dim")
        ordered = [a for a in AGENT_ORDER if a in s._agents]
        ordered += [a for a in s._agents if a not in AGENT_ORDER]

        for agent_id in ordered:
            info   = s._agents[agent_id]
            color  = AGENT_COLORS.get(agent_id, "white")
            label  = AGENT_LABELS.get(agent_id, agent_id.title())
            icon   = STATUS_ICON.get(info.status, "○")
            i_clr  = STATUS_COLOR.get(info.status, "dim")
            key    = AGENT_KEY_MAP.get(agent_id, "")
            sel    = agent_id == s._selected_agent

            # Status line
            t.append("  ")
            if info.status == "running":
                spin = SPINNERS[s._tick_count % len(SPINNERS)]
                t.append(spin + " ", style=f"bold {i_clr}")
            else:
                t.append(icon + " ", style=f"bold {i_clr}")

            name_s = f"bold {color}" + (" reverse" if sel else "")
            t.append(label, style=name_s)

            meta: list[str] = []
            if info.model:
                meta.append(_short_model(info.model))
            if info.msg_count:
                meta.append(f"✉{info.msg_count}")
            if info.status in ("running", "waiting") and info.time_in_state > 1:
                meta.append(f"{info.time_in_state:.0f}s")
            if key:
                meta.append(f"[{key}]")
            if meta:
                t.append("  " + " · ".join(meta), style="dim")
            t.append("\n")

            # Activity line (live from _emit or last message)
            activity = info.activity_text or (
                info.messages[-1].get("preview", "") if info.messages else ""
            )
            if activity:
                t.append("     ⎿ ", style="dim")
                t.append(_truncate(activity, 58) + "\n", style="dim italic")

        return t

    def bump(self) -> None:
        self._v += 1


class ModeBar(Static):
    """1-line bar showing run mode and agent shortcuts."""
    _v: reactive[int] = reactive(0)

    def __init__(self, state: "OrbTUI", **kw: Any) -> None:
        super().__init__(**kw)
        self._state = state

    def watch__v(self, _: int) -> None:
        self.refresh()

    def render(self) -> RichText:
        s = self._state
        t = RichText()

        if s._run_status == "Running":
            t.append("  ↪ inject", style="bold cyan")
            t.append(" · new input goes to coordinator  ", style="dim")
        elif s._run_status == "Complete":
            t.append("  ✓ done", style="bold cyan")
            t.append(" · enter new task  ", style="dim")
        else:
            t.append("  ○ ready", style="dim")
            t.append(" · type a task  ", style="dim")

        # Agent shortcuts
        if s._agents:
            ordered = [a for a in AGENT_ORDER if a in s._agents]
            for agent_id in ordered:
                info  = s._agents.get(agent_id)
                color = AGENT_COLORS.get(agent_id, "white")
                icon  = STATUS_ICON.get(info.status if info else "idle", "○")
                i_clr = STATUS_COLOR.get(info.status if info else "idle", "dim")
                key   = AGENT_KEY_MAP.get(agent_id, "")
                label = AGENT_LABELS.get(agent_id, agent_id)
                sel   = agent_id == s._selected_agent
                name_s = f"bold {color}" + (" underline" if sel else "")
                t.append(f" {icon}", style=f"bold {i_clr}")
                t.append(f"{label}", style=name_s)
                if key:
                    t.append(f"[{key}]", style="dim")
                t.append("  ", style="dim")
        else:
            t.append("@mention or 1–6 to inspect agents", style="dim")

        return t

    def bump(self) -> None:
        self._v += 1


# ─── Log handler ─────────────────────────────────────────────────────────────

class TUILogHandler(logging.Handler):
    """Redirects Python log records into the TUI log panel."""

    def __init__(self, app: "OrbTUI") -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._app.post_message(OrbLogRecord(
                level=record.levelname,
                name=record.name,
                message=self.format(record),
            ))
        except Exception:
            pass


# ─── Main App ─────────────────────────────────────────────────────────────────

class OrbTUI(App[None]):

    CSS = """
    Screen {
        layout: vertical;
        background: #0d1117;
    }

    #header-bar {
        height: 1;
        background: #161b22;
        border-bottom: solid #21262d;
    }

    #body {
        layout: horizontal;
        height: 1fr;
    }

    /* Left: graph + feed stacked */
    #left-panel {
        width: 1fr;
        layout: vertical;
    }

    #graph-panel {
        height: auto;
        padding: 1 1;
        border-bottom: solid #21262d;
        background: #0d1117;
    }

    #feed-scroll {
        height: 1fr;
    }

    #message-feed {
        padding: 0 2;
    }

    /* Right: detail pane */
    #detail-pane {
        width: 46;
        background: #0d1117;
        border-left: solid #21262d;
        display: none;
        layout: vertical;
    }

    #detail-pane.visible {
        display: block;
    }

    #detail-header {
        height: 2;
        background: #161b22;
        border-bottom: solid #21262d;
        padding: 0 1;
    }

    #detail-scroll {
        height: 1fr;
    }

    #detail-log {
        padding: 0 1;
    }

    /* Bottom bars */
    #mode-bar {
        height: 1;
        background: #0d1117;
        border-top: solid #21262d;
        color: #8b949e;
    }

    #query-bar {
        height: 3;
        layout: horizontal;
        background: #161b22;
        border-top: solid #21262d;
        align: left middle;
        padding: 0 1;
    }

    #query-label {
        width: 4;
        color: #484f58;
    }

    #query-input {
        width: 1fr;
        background: #0d1117;
        border: solid #30363d;
        color: #c9d1d9;
    }

    #query-input:focus {
        border: solid #388bfd;
    }

    #query-input.inject-mode {
        border: solid #0d9373;
    }

    Footer {
        background: #161b22;
        color: #484f58;
    }

    /* Log panel (--logs mode) */
    #log-panel {
        height: 10;
        background: #0a0d12;
        border-top: solid #21262d;
        display: none;
    }

    #log-panel.visible {
        display: block;
    }

    #log-panel-header {
        height: 1;
        background: #161b22;
        padding: 0 2;
        color: #484f58;
    }

    #log-feed {
        height: 1fr;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+c",  "quit",                    "Quit"),
        Binding("escape",  "deselect",                "Deselect"),
        Binding("tab",     "next_agent",              "Next agent"),
        Binding("slash",   "focus_input",             "Focus input", show=False),
        Binding("r",       "show_results",            "Results"),
        Binding("ctrl+k",  "cancel_run",              "Cancel run"),
        Binding("ctrl+l",  "clear_feed",              "Clear feed"),
        Binding("1", "select('coordinator')",         show=False),
        Binding("2", "select('coder')",               show=False),
        Binding("3", "select('reviewer')",            show=False),
        Binding("4", "select('reviewer_a')",          show=False),
        Binding("5", "select('reviewer_b')",          show=False),
        Binding("6", "select('tester')",              show=False),
    ]

    def __init__(
        self,
        providers: dict,
        config: OrchestratorConfig,
        model_overrides: dict | None = None,
        tier_override: ModelTier | None = None,
        topology: str = "triangle",
        budget: int = 200,
        dashboard_server: Any = None,
        dashboard_port: int = 8080,
        show_logs: bool = False,
    ) -> None:
        super().__init__()
        self._providers        = providers
        self._config           = config
        self._model_overrides  = model_overrides
        self._tier_override    = tier_override
        self._topology_name    = topology
        self._budget           = budget
        self._dashboard_server = dashboard_server
        self._dashboard_port   = dashboard_port
        self._show_logs        = show_logs
        self._dashboard_state: Any = None

        # Run state
        self._agents: dict[str, AgentInfo] = {}
        self._detail_feed: list[dict] = []
        self._routed: int = 0
        self._run_start: float | None = None
        self._run_status: str = "Waiting"
        self._selected_agent: str | None = None
        self._current_orchestrator: Any = None
        self._completions: dict[str, str] = {}
        self._last_query: str = ""
        self._last_elapsed: float = 0.0

        # Animation state
        self._tick_count: int = 0
        self._active_edges: dict[tuple[str, str], int] = {}  # edge -> expiry tick
        self._elapsed_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield HeaderBar(state=self, id="header-bar")

        with Horizontal(id="body"):
            with Vertical(id="left-panel"):
                yield GraphPanel(state=self, id="graph-panel")
                with VerticalScroll(id="feed-scroll"):
                    yield RichLog(id="message-feed", highlight=True, markup=True, wrap=True)

            with Vertical(id="detail-pane"):
                yield Static(id="detail-header")
                with VerticalScroll(id="detail-scroll"):
                    yield RichLog(id="detail-log", highlight=True, markup=True, wrap=True)

        yield ModeBar(state=self, id="mode-bar")

        with Vertical(id="log-panel"):
            yield Static(" 📋 Logs", id="log-panel-header")
            yield RichLog(id="log-feed", highlight=False, markup=True, wrap=True)

        with Horizontal(id="query-bar"):
            yield Label(" >  ", id="query-label")
            yield Input(
                placeholder="Describe a task… or @agentname  ·  tab=next  r=results  1-6=inspect",
                id="query-input",
            )

        yield Footer()

    def on_mount(self) -> None:
        feed = self.query_one("#message-feed", RichLog)
        feed.write("[dim]Ready. Type a task and press Enter.[/dim]")
        self.query_one("#query-input", Input).focus()
        self._elapsed_task = asyncio.create_task(self._tick())

        if self._show_logs:
            self.query_one("#log-panel").add_class("visible")
            handler = TUILogHandler(self)
            handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
            self._log_handler = handler
        else:
            self._log_handler = None

    # ── Tick / animation ──────────────────────────────────────────────────────

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self._tick_count += 1
            self.query_one("#header-bar",  HeaderBar).bump()
            self.query_one("#graph-panel", GraphPanel).bump()
            self.query_one("#mode-bar",    ModeBar).bump()

    def _refresh_all(self) -> None:
        self.query_one("#header-bar",  HeaderBar).bump()
        self.query_one("#graph-panel", GraphPanel).bump()
        self.query_one("#mode-bar",    ModeBar).bump()

    # ── Input handling ────────────────────────────────────────────────────────

    @on(Input.Submitted, "#query-input")
    async def on_query_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw:
            return
        event.input.clear()
        event.input.remove_class("inject-mode")

        # @mention — select agent, run remainder as task
        mention = re.match(r'^@(\w+)\s*', raw)
        if mention:
            agent_id  = mention.group(1).lower()
            remainder = raw[mention.end():].strip()
            self.action_select(agent_id)
            if not remainder:
                return
            raw = remainder

        # Mid-run: inject to coordinator
        if self._current_orchestrator is not None and self._run_status == "Running":
            await self._inject_to_coordinator(raw)
            return

        # New run
        self._start_new_run(raw)

    @on(Input.Changed, "#query-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        inp = event.input
        if self._run_status == "Running":
            inp.add_class("inject-mode")
        else:
            inp.remove_class("inject-mode")

    def _start_new_run(self, query: str) -> None:
        self._last_query      = query
        self._agents          = {}
        self._detail_feed     = []
        self._completions     = {}
        self._routed          = 0
        self._run_start       = time()
        self._run_status      = "Running"
        self._active_edges    = {}
        self._current_orchestrator = None

        # Reset detail pane
        self.query_one("#detail-log", RichLog).clear()

        feed = self.query_one("#message-feed", RichLog)
        feed.clear()
        feed.write(f"[bold cyan]▶ Task:[/bold cyan] {query}\n")

        self._refresh_all()
        self._run_query(query)

    async def _inject_to_coordinator(self, text: str) -> None:
        orch  = self._current_orchestrator
        entry = orch.config.entry_agent if orch else "coordinator"
        if entry not in orch.agents:
            entry = next(iter(orch.agents), None)
        if not entry:
            return
        msg = OrbMessage(from_="user", to=entry,
                         type=MessageType.TASK, payload=text)
        await orch.agents[entry].channel.send(msg)
        feed  = self.query_one("#message-feed", RichLog)
        color = AGENT_COLORS.get(entry, "white")
        feed.write(
            f"[dim]↪ user →[/dim] [{color}]{entry}[/{color}][dim]: {text}[/dim]"
        )

    # ── Run worker ────────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _run_query(self, query: str) -> None:
        from ..topologies.triad import create_triad
        try:
            from ..topologies.dual_review import create_dual_review
            has_dual = True
        except ImportError:
            has_dual = False

        try:
            if self._topology_name == "dual-review" and has_dual:
                orchestrator = create_dual_review(
                    providers=self._providers, config=self._config,
                    model_overrides=self._model_overrides,
                    tier_override=self._tier_override, trace=False,
                )
            else:
                orchestrator = create_triad(
                    providers=self._providers, config=self._config,
                    model_overrides=self._model_overrides,
                    tier_override=self._tier_override, trace=False,
                )

            self._current_orchestrator = orchestrator

            # Initialise agents in state
            for agent_id, agent in orchestrator.agents.items():
                self._agents[agent_id] = AgentInfo(
                    agent_id=agent_id, role=agent.config.role
                )

            # Wire on_activity callbacks
            for agent_id, agent in orchestrator.agents.items():
                aid = agent_id
                def make_cb(a: str):
                    def cb(_, text: str) -> None:
                        self.post_message(OrbActivityUpdate(a, text))
                    return cb
                agent._on_activity = make_cb(aid)

            # Detect topology from actual graph edges
            self._topology_name = (
                "dual-review"
                if "reviewer_a" in orchestrator.agents
                else "triangle"
            )

            orchestrator.bus.on_event(self._on_bus_event)

            # Dashboard bridge
            if self._dashboard_server is not None:
                try:
                    from web.bridge import DashboardBridge
                    dash_state = self._dashboard_state
                    dash_state.reset()
                    bridge = DashboardBridge(dash_state, self._dashboard_server.broadcast)
                    agent_roles = {aid: a.config.role for aid, a in orchestrator.agents.items()}
                    bridge.setup_agents(agent_roles)
                    bridge.setup_edges([(e.a, e.b) for e in orchestrator.bus.graph.edges])
                    bridge.setup_budget(self._config.budget)
                    orchestrator.bus.on_event(bridge.on_message_routed)
                    self._dashboard_server.set_agents(orchestrator.agents)

                    orig = orchestrator._on_agent_complete
                    async def patched_bridge(aid: str, res: str) -> None:
                        await bridge.on_agent_complete(aid, res)
                        self.post_message(OrbAgentComplete(aid, res))
                        await orig(aid, res)
                    orchestrator._on_agent_complete = patched_bridge
                except Exception:
                    self._wire_plain_complete(orchestrator)
            else:
                self._wire_plain_complete(orchestrator)

            self._refresh_all()
            run_result = await orchestrator.run(query)
            self.post_message(OrbRunComplete(
                error=run_result.error,
                completions=dict(run_result.completions),
            ))

        except Exception as e:
            self.post_message(OrbRunComplete(error=f"{type(e).__name__}: {e}"))

    def _wire_plain_complete(self, orchestrator: Any) -> None:
        orig = orchestrator._on_agent_complete
        async def patched(aid: str, res: str) -> None:
            self.post_message(OrbAgentComplete(aid, res))
            await orig(aid, res)
        orchestrator._on_agent_complete = patched

    # ── Bus events ────────────────────────────────────────────────────────────

    def _on_bus_event(self, event_name: str, msg: OrbMessage) -> None:
        self.post_message(OrbBusEvent(event_name, msg))

    def on_orb_bus_event(self, event: OrbBusEvent) -> None:  # noqa: C901
        event_name = event.event_name
        msg        = event.orb_msg

        if msg.type == MessageType.SYSTEM:
            return

        elapsed = time() - (self._run_start or time())

        for agent_id in (msg.from_, msg.to):
            if agent_id and agent_id not in ("user", "orchestrator", "[COMPLETE]"):
                if agent_id not in self._agents:
                    self._agents[agent_id] = AgentInfo(agent_id=agent_id, role=agent_id)

        model = msg.metadata.get("model", "")
        if msg.from_ in self._agents and model:
            self._agents[msg.from_].model = model

        # Mark edge as active (expires in 3 ticks ≈ 1.5s)
        if event_name == "routed" and msg.type != MessageType.COMPLETE:
            key = (msg.from_, msg.to)
            self._active_edges[key] = self._tick_count + 3

        # Status transitions
        if event_name == "injected":
            if msg.to in self._agents:
                self._agents[msg.to].set_status("running")
        elif event_name == "routed":
            if msg.type == MessageType.COMPLETE:
                for aid in (msg.from_, msg.to):
                    if aid in self._agents:
                        self._agents[aid].set_status("completed")
            else:
                if msg.from_ in self._agents:
                    info = self._agents[msg.from_]
                    info.set_status("running")
                    info.msg_count += 1
                if msg.to in self._agents:
                    info = self._agents[msg.to]
                    if info.status != "completed":
                        info.set_status("waiting")
            self._routed += 1

        # Feed entry
        to_label = "[COMPLETE]" if msg.type == MessageType.COMPLETE else msg.to
        preview  = msg.payload[:500].replace("\n", " ")
        entry = {
            "elapsed": elapsed, "from_": msg.from_, "to": to_label,
            "model": model, "preview": preview, "type": msg.type.value,
        }
        self._detail_feed.append(entry)

        # Add to agent message history
        for aid in (msg.from_, to_label):
            if aid in self._agents:
                self._agents[aid].messages.append(entry)

        self._write_feed(entry)

        # Update detail pane if this involves the selected agent
        if self._selected_agent and (
            msg.from_ == self._selected_agent or msg.to == self._selected_agent
        ):
            self._append_to_detail(entry)

        self._refresh_all()

    def on_orb_log_record(self, event: OrbLogRecord) -> None:
        try:
            log = self.query_one("#log-feed", RichLog)
        except Exception:
            return
        level_color = {
            "DEBUG":    "dim",
            "INFO":     "cyan",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold red",
        }.get(event.level, "white")
        log.write(
            f"[{level_color}]{event.level:<8}[/{level_color}]"
            f"[dim] {event.message}[/dim]"
        )

    def on_orb_activity_update(self, event: OrbActivityUpdate) -> None:
        if event.agent_id in self._agents:
            self._agents[event.agent_id].activity_text = event.text
        self.query_one("#graph-panel", GraphPanel).bump()

    def on_orb_agent_complete(self, event: OrbAgentComplete) -> None:
        if event.agent_id in self._agents:
            self._agents[event.agent_id].set_status("completed")
            self._agents[event.agent_id].result = event.result
            self._agents[event.agent_id].activity_text = ""
        self._refresh_all()

    def on_orb_run_complete(self, event: OrbRunComplete) -> None:
        self._current_orchestrator = None
        self._completions = event.completions

        if self._run_start:
            self._last_elapsed = time() - self._run_start

        feed = self.query_one("#message-feed", RichLog)

        if event.error:
            self._run_status = "Error"
            feed.write(f"\n[bold red]✗ Error:[/bold red] {event.error}")
        else:
            self._run_status = "Complete"
            for info in self._agents.values():
                if info.status not in ("completed", "error"):
                    info.set_status("completed")
                info.activity_text = ""

            feed.write(
                f"\n[bold green]✓ Complete[/bold green]"
                f"[dim]  {self._routed} messages · {self._last_elapsed:.1f}s"
                f"  press [/dim][cyan]r[/cyan][dim] for full results[/dim]"
            )

            # Auto-select coordinator to show synthesis result
            if "coordinator" in self._agents and not self._selected_agent:
                self.action_select("coordinator")

        self.query_one("#query-input", Input).focus()
        self._refresh_all()

    # ── Feed ──────────────────────────────────────────────────────────────────

    def _write_feed(self, entry: dict) -> None:
        feed    = self.query_one("#message-feed", RichLog)
        from_id = entry["from_"]
        to_id   = entry["to"]
        f_color = AGENT_COLORS.get(str(from_id).lower(), "white")
        t_color = "green" if to_id == "[COMPLETE]" else AGENT_COLORS.get(str(to_id).lower(), "white")
        model_s = f" [{_short_model(entry['model'])}]" if entry["model"] else ""
        ttype   = entry.get("type", "")
        t_clr   = MSG_TYPE_COLOR.get(ttype, "white")
        badge   = f" [{t_clr}]{{{ttype}}}[/{t_clr}]" if ttype not in ("", "response") else ""
        preview = _truncate(str(entry.get("preview", "")), 90)

        feed.write(
            f"[dim][{entry['elapsed']:5.1f}s][/dim] "
            f"[{f_color}]{from_id}[/{f_color}]"
            f"[dim]{model_s}[/dim]{badge}"
            f"[dim] → [/dim]"
            f"[{t_color}]{to_id}[/{t_color}]"
            f"[dim]:  [/dim]"
            f"[dim]\"{preview}\"[/dim]"
        )

    def _append_to_detail(self, entry: dict) -> None:
        """Append one entry to the detail log."""
        log     = self.query_one("#detail-log", RichLog)
        info    = self._agents.get(self._selected_agent)
        if not info:
            return
        from_id = entry["from_"]
        to_id   = entry["to"]
        elapsed = entry.get("elapsed", 0.0)
        color   = AGENT_COLORS.get(self._selected_agent, "white")

        if from_id == self._selected_agent:
            direction, other, arrow_s = "→", to_id, "bold cyan"
        else:
            direction, other, arrow_s = "←", from_id, "bold magenta"

        other_label = AGENT_LABELS.get(other, other)
        other_color = AGENT_COLORS.get(str(other).lower(), "white")
        ttype = entry.get("type", "")
        t_clr = MSG_TYPE_COLOR.get(ttype, "dim")

        log.write(
            f"[dim][{elapsed:4.1f}s][/dim] "
            f"[{arrow_s}]{direction}[/{arrow_s}] "
            f"[{other_color}]{other_label}[/{other_color}]"
            f"[dim] {{{ttype}}}[/dim]"
        )
        # Full content (not truncated in detail pane)
        full = entry.get("preview", "")
        log.write(f"[dim]{full[:400]}[/dim]\n")

    def _populate_detail_pane(self) -> None:
        """Rebuild detail pane from scratch for the selected agent."""
        if not self._selected_agent:
            return
        log  = self.query_one("#detail-log", RichLog)
        log.clear()
        info = self._agents.get(self._selected_agent)
        if not info:
            return

        # Write all historical messages involving this agent
        agent_entries = [
            e for e in self._detail_feed
            if e.get("from_") == self._selected_agent
            or e.get("to") == self._selected_agent
        ]
        for entry in agent_entries:
            self._append_to_detail(entry)

        if info.result:
            log.write(f"\n[bold green]── Result ──────────────────────[/bold green]")
            log.write(info.result[:1000])

    def _update_detail_header(self) -> None:
        hdr = self.query_one("#detail-header", Static)
        if not self._selected_agent:
            hdr.update("")
            return
        info  = self._agents.get(self._selected_agent)
        color = AGENT_COLORS.get(self._selected_agent, "white")
        label = AGENT_LABELS.get(self._selected_agent, self._selected_agent)
        icon  = STATUS_ICON.get(info.status if info else "idle", "○")
        i_clr = STATUS_COLOR.get(info.status if info else "idle", "dim")
        t = RichText()
        t.append(f" {icon} ", style=f"bold {i_clr}")
        t.append(f"{label}", style=f"bold {color}")
        if info and info.model:
            t.append(f"  {_short_model(info.model)}", style="dim")
        if info and info.msg_count:
            t.append(f"  ✉{info.msg_count}", style="dim")
        t.append(f"\n [dim]Esc to close[/dim]")
        hdr.update(t)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_select(self, agent_id: str) -> None:
        pane = self.query_one("#detail-pane")
        if self._selected_agent == agent_id:
            self._selected_agent = None
            pane.remove_class("visible")
        else:
            self._selected_agent = agent_id
            pane.add_class("visible")
            self._populate_detail_pane()
            self._update_detail_header()
        self._refresh_all()

    def action_next_agent(self) -> None:
        active = [a for a in AGENT_ORDER if a in self._agents]
        active += [a for a in self._agents if a not in AGENT_ORDER]
        if not active:
            return
        if self._selected_agent not in active:
            self.action_select(active[0])
        else:
            idx = active.index(self._selected_agent)
            self.action_select(active[(idx + 1) % len(active)])

    def action_deselect(self) -> None:
        self._selected_agent = None
        self.query_one("#detail-pane").remove_class("visible")
        self._refresh_all()

    def action_focus_input(self) -> None:
        self.query_one("#query-input", Input).focus()

    def action_show_results(self) -> None:
        if self._run_status != "Complete" or not self._completions:
            return
        self.push_screen(ResultScreen(
            task=self._last_query,
            completions=self._completions,
            elapsed=self._last_elapsed,
            msg_count=self._routed,
        ))

    def action_cancel_run(self) -> None:
        if self._current_orchestrator:
            # Signal cancellation by setting the completion event
            try:
                self._current_orchestrator._completion_event.set()
            except Exception:
                pass
        self._run_status = "Waiting"
        self._current_orchestrator = None
        feed = self.query_one("#message-feed", RichLog)
        feed.write("\n[dim]Run cancelled.[/dim]")
        self._refresh_all()

    def action_clear_feed(self) -> None:
        self.query_one("#message-feed", RichLog).clear()

    def action_quit(self) -> None:
        if self._elapsed_task:
            self._elapsed_task.cancel()
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)
        self.exit()


# ─── Entry points ─────────────────────────────────────────────────────────────

def run_tui(
    providers: dict,
    config: OrchestratorConfig,
    model_overrides: dict | None = None,
    tier_override: ModelTier | None = None,
    topology: str = "triangle",
    budget: int = 200,
    show_logs: bool = False,
) -> None:
    OrbTUI(
        providers=providers, config=config,
        model_overrides=model_overrides,
        tier_override=tier_override,
        topology=topology, budget=budget,
        show_logs=show_logs,
    ).run()


async def run_tui_with_dashboard(
    providers: dict,
    config: OrchestratorConfig,
    model_overrides: dict | None = None,
    tier_override: ModelTier | None = None,
    topology: str = "triangle",
    budget: int = 200,
    dashboard_port: int = 8080,
    show_logs: bool = False,
) -> None:
    from web.server import DashboardServer
    from web.state import DashboardState

    dash_state = DashboardState()
    dashboard_server = DashboardServer(dash_state, port=dashboard_port)
    dashboard_server.set_providers(
        providers=providers, config=config,
        model_overrides=model_overrides,
        tier_override=tier_override,
    )
    await dashboard_server.start()

    app = OrbTUI(
        providers=providers, config=config,
        model_overrides=model_overrides,
        tier_override=tier_override,
        topology=topology, budget=budget,
        dashboard_server=dashboard_server,
        dashboard_port=dashboard_port,
        show_logs=show_logs,
    )
    app._dashboard_state = dash_state
    await app.run_async()
    await dashboard_server.stop()
