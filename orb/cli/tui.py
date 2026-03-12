from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Any

from rich.text import Text as RichText
from textual.app import App, ComposeResult, Screen
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TUIMessage
from textual.reactive import reactive
from textual import events, on
from textual.widgets import Footer, Label, RichLog, Static, TextArea

from .display import pick_primary_result

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

TOPOLOGY_LABELS: dict[str, str] = {
    "triangle": "Triad",
    "dual-review": "Dual Review",
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


def _topology_label(topology: str) -> str:
    return TOPOLOGY_LABELS.get(topology, topology.replace("-", " ").title())


def _graph_node_status(info: "AgentInfo", tick_count: int) -> tuple[str, str]:
    if info.activity_text.startswith("⏳ Waiting for user"):
        return "?", "yellow"
    if info.activity_text.startswith("wrote "):
        return "✎", "cyan"
    if info.status == "running":
        return SPINNERS[tick_count % len(SPINNERS)], STATUS_COLOR.get(info.status, "green")
    return STATUS_ICON.get(info.status, "○"), STATUS_COLOR.get(info.status, "dim")


# ─── State ───────────────────────────────────────────────────────────────────

@dataclass
class AgentInfo:
    agent_id: str
    role: str
    status: str = "idle"
    model: str = ""
    msg_count: int = 0
    complexity_score: int = 0        # last complexity score from model selector
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
        Binding("y",      "copy_result",    "Copy result"),
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
                 elapsed: float, msg_count: int, diff: str = "") -> None:
        super().__init__()
        self._task = task
        self._completions = completions
        self._elapsed = elapsed
        self._msg_count = msg_count
        self._diff = diff

    def compose(self) -> ComposeResult:
        yield Static(id="rs-header")
        with VerticalScroll(id="rs-body"):
            yield RichLog(id="rs-log", highlight=True, markup=True, wrap=True)
        yield Static(id="rs-footer")

    def on_mount(self) -> None:
        hdr = self.query_one("#rs-header", Static)
        t = RichText()
        t.append("✓ Run Complete\n", style="bold green")
        t.append(f"  Task: {self._task[:80]}\n", style="dim")
        t.append(f"  {self._msg_count} messages  ·  {self._elapsed:.1f}s", style="dim")
        hdr.update(t)

        ftr = self.query_one("#rs-footer", Static)
        ftr.update("[dim][q/Esc] back  [s] save  [y] copy result  drag to select text[/dim]")

        log = self.query_one("#rs-log", RichLog)
        primary_id, primary_result = pick_primary_result(self._completions)
        supporting_results = [
            (agent_id, result)
            for agent_id, result in self._completions.items()
            if result and agent_id != primary_id
        ]

        if primary_result:
            color = AGENT_COLORS.get(primary_id or "", "white")
            label = AGENT_LABELS.get(primary_id or "", "Final Result")
            log.write("[bold green]══ Final Result ═══════════════════════════[/bold green]")
            log.write(f"[bold {color}]{label}[/bold {color}]")
            log.write(primary_result)
            log.write("")

        # ── Files changed (git diff) ──────────────────────────────────────
        if self._diff:
            from ..cli.diff_capture import parse_diff_files
            files = parse_diff_files(self._diff)
            log.write("[bold yellow]── Files Changed ────────────────────────[/bold yellow]")
            for f in files:
                log.write(f"  [cyan]{f['path']}[/cyan]  [dim]{f['stat']}[/dim]")
            log.write("")
            log.write("[bold yellow]── Diff ─────────────────────────────────[/bold yellow]")
            for line in self._diff.splitlines():
                if line.startswith("diff --git") or line.startswith("index "):
                    log.write(f"[dim]{line}[/dim]")
                elif line.startswith("--- ") or line.startswith("+++ "):
                    log.write(f"[bold]{line}[/bold]")
                elif line.startswith("@@"):
                    log.write(f"[cyan]{line}[/cyan]")
                elif line.startswith("+"):
                    log.write(f"[green]{line}[/green]")
                elif line.startswith("-"):
                    log.write(f"[red]{line}[/red]")
                else:
                    log.write(f"[dim]{line}[/dim]")
            log.write("")
        else:
            log.write("[dim]No file changes detected (git diff empty).[/dim]\n")

        # ── Supporting results ────────────────────────────────────────────
        if supporting_results:
            log.write("[bold white]── Supporting Results ───────────────────[/bold white]")
        elif not primary_result:
            log.write("[dim]No agent results captured.[/dim]")

        ordered = [a for a in AGENT_ORDER if any(a == agent_id for agent_id, _ in supporting_results)]
        ordered += [a for a, _ in supporting_results if a not in AGENT_ORDER]
        for agent_id in ordered:
            result = self._completions[agent_id]
            color  = AGENT_COLORS.get(agent_id, "white")
            label  = AGENT_LABELS.get(agent_id, agent_id.title())
            log.write(f"\n[bold {color}]── {label}[/bold {color}]")
            log.write(result)

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()

    def action_copy_result(self) -> None:
        """Copy the primary worker result to clipboard."""
        _, text = pick_primary_result(self._completions)
        if text:
            self.app.copy_to_clipboard(text)
            self.app.notify("Result copied to clipboard", severity="information", timeout=2)
        else:
            self.app.notify("Nothing to copy", severity="warning", timeout=2)

    def action_save_results(self) -> None:
        import datetime
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"orb_result_{ts}.md"
        lines = [f"# Orb Result\n\n**Task:** {self._task}\n\n"
                 f"**Elapsed:** {self._elapsed:.1f}s  "
                 f"**Messages:** {self._msg_count}\n\n"]
        if self._diff:
            lines.append(f"## Files Changed\n\n```diff\n{self._diff}\n```\n\n")
        for agent_id, result in self._completions.items():
            label = AGENT_LABELS.get(agent_id, agent_id.title())
            lines.append(f"## {label}\n\n{result}\n\n")
        with open(path, "w") as f:
            f.writelines(lines)
        log = self.query_one("#rs-log", RichLog)
        log.write(f"\n[green]Saved to {path}[/green]")


class HelpScreen(Screen):
    """Compact full-screen keybinding reference."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("q", "dismiss_screen", "Back"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        background: #0d1117;
        layout: vertical;
    }
    #help-header {
        height: 3;
        background: #161b22;
        border-bottom: solid #21262d;
        padding: 1 2;
    }
    #help-body {
        height: 1fr;
    }
    #help-log {
        padding: 1 2;
    }
    #help-footer {
        height: 1;
        background: #161b22;
        border-top: solid #21262d;
        color: #8b949e;
        padding: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="help-header")
        with VerticalScroll(id="help-body"):
            yield RichLog(id="help-log", highlight=True, markup=True, wrap=True)
        yield Static(id="help-footer")

    def on_mount(self) -> None:
        self.query_one("#help-header", Static).update(
            "[bold white]Orb TUI Help[/bold white]\n[dim]Keybindings for navigation, replies, and results[/dim]"
        )
        self.query_one("#help-footer", Static).update("[dim][q/Esc] back[/dim]")
        log = self.query_one("#help-log", RichLog)
        sections = [
            ("Run Control", [
                ("Enter", "Submit the current task or reply"),
                ("Ctrl+K", "Stop the active run"),
                ("R", "Open the results screen after a run"),
                ("Y", "Copy the selected or primary result"),
            ]),
            ("Navigation", [
                ("/", "Focus the input composer"),
                ("Tab", "Cycle through agents"),
                ("1-6", "Inspect a specific agent"),
                ("Esc", "Close the current inspector or overlay"),
            ]),
            ("Reply Mode", [
                ("Ctrl+G", "Clear the current reply draft"),
                ("Agent question banner", "Shows who is waiting for input"),
                ("Reply composer", "Sends your next message back to that agent"),
            ]),
            ("Workspace", [
                ("Ctrl+L", "Clear the main feed"),
                ("Drag to select", "Copy any visible terminal text"),
            ]),
        ]
        for title, rows in sections:
            log.write(f"[bold white]{title}[/bold white]")
            for key, desc in rows:
                log.write(f"  [bold cyan]{key:<22}[/bold cyan] {desc}")
            log.write("")

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


# ─── Widgets ──────────────────────────────────────────────────────────────────

class QueryInput(TextArea):
    """TextArea that submits on Enter but preserves newlines from paste.

    Typing Enter → submit.  Pasting multi-line text → newlines are kept and
    the whole block is submitted when Enter is pressed.
    """

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            await self.app.action_submit_input()
        # All other keys (incl. ctrl+enter as alternative) pass through normally


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
        active_agent = s._awaiting_user or next(
            (aid for aid, info in s._agents.items() if info.status == "running"),
            None,
        )
        elapsed = time() - s._run_start if s._run_start else s._last_elapsed

        t.append("  ORB", style="bold magenta")
        t.append(f"  [{_topology_label(s._topology_name)}]", style="dim")
        t.append("  │  ", style="dim")

        if s._awaiting_user:
            t.append("USER INPUT", style="bold black on yellow")
        else:
            badge_style = {
                "Waiting": ("dim", "READY"),
                "Idle": ("dim", "READY"),
                "Running": ("bold green", "RUNNING"),
                "Complete": ("bold cyan", "DONE"),
                "Error": ("bold red", "ERROR"),
            }.get(s._run_status, ("white", s._run_status.upper()))
            t.append(badge_style[1], style=badge_style[0])

        if active_agent:
            label = AGENT_LABELS.get(active_agent, active_agent)
            color = AGENT_COLORS.get(active_agent, "white")
            t.append("  │  ", style="dim")
            t.append("active ", style="dim")
            t.append(label, style=f"bold {color}")

        bar, bar_style = _budget_bar(s._routed, s._budget, width=10)
        t.append("  │  ", style="dim")
        t.append(f"{elapsed:.1f}s", style="bold white")
        t.append(f"  {s._routed}/{s._budget} msgs  ", style="dim")
        t.append(bar, style=bar_style)

        if s._selected_agent and s._selected_agent in s._agents:
            label = AGENT_LABELS.get(s._selected_agent, s._selected_agent)
            color = AGENT_COLORS.get(s._selected_agent, "white")
            t.append("  │  ", style="dim")
            t.append("inspect ", style="dim")
            t.append(label, style=f"bold {color}")

        if getattr(s, "_server_port", None):
            t.append("  │  ", style="dim")
            t.append(f"::{s._server_port}", style="dim")

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
                    icon, i_clr = _graph_node_status(info, s._tick_count)
                    color  = AGENT_COLORS.get(agent_id, "white")
                    label  = AGENT_LABELS.get(agent_id, agent_id)
                    sel    = agent_id == s._selected_agent
                    key    = AGENT_KEY_MAP.get(agent_id, "")
                    name_s = f"bold {color}" + (" reverse" if sel else "")
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
            icon, i_clr = _graph_node_status(info, s._tick_count)
            key    = AGENT_KEY_MAP.get(agent_id, "")
            sel    = agent_id == s._selected_agent

            t.append("  ")
            t.append(icon + " ", style=f"bold {i_clr}")

            name_s = f"bold {color}" + (" reverse" if sel else "")
            t.append(label, style=name_s)

            meta: list[str] = []
            if info.model:
                meta.append(_short_model(info.model))
            if info.msg_count:
                meta.append(f"✉{info.msg_count}")
            if info.complexity_score:
                meta.append(f"⚡{info.complexity_score}")
            if info.status in ("running", "waiting") and info.time_in_state > 1:
                meta.append(f"{info.time_in_state:.0f}s")
            if info.activity_text.startswith("⏳ Waiting for user"):
                meta.append("ASK")
            elif info.activity_text.startswith("wrote "):
                meta.append("FILE")
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

        if s._awaiting_user:
            label = AGENT_LABELS.get(s._awaiting_user, s._awaiting_user)
            t.append("  ↩ reply", style="bold yellow")
            t.append(f" · your next input goes to {label}  ", style="dim")
        elif s._run_status == "Running":
            t.append("  ↪ inject", style="bold cyan")
            t.append(" · mid-run input goes to coordinator  ", style="dim")
        elif s._run_status in ("Complete", "Idle") and s._turn_count > 0:
            t.append("  ✓ done", style="bold cyan")
            t.append(" · type a follow-up or new task  ", style="dim")
        else:
            t.append("  ○ ready", style="dim")
            t.append(" · type a task  ", style="dim")

        t.append("│  / focus  ctrl+k stop  r results  ? help  ", style="dim")

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
            t.append("@mention or 1-6 to inspect agents", style="dim")

        return t

    def bump(self) -> None:
        self._v += 1


# ─── Code panel ──────────────────────────────────────────────────────────────

# Simple extension→language hint for syntax coloring
_EXT_LANG: dict[str, str] = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "tsx": "tsx", "jsx": "jsx", "go": "go", "rs": "rust",
    "java": "java", "kt": "kotlin", "swift": "swift",
    "c": "c", "cpp": "c++", "h": "c", "hpp": "c++",
    "sh": "bash", "zsh": "bash", "bash": "bash",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "md": "markdown", "html": "html",
    "css": "css", "sql": "sql", "rb": "ruby",
}


def _lang_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_LANG.get(ext, "text")


def _colorize_code(lines: list[str], lang: str) -> list[str]:
    """Very lightweight token coloring for common patterns."""
    KW = {"def", "class", "return", "import", "from", "if", "else", "elif",
          "for", "while", "with", "as", "try", "except", "raise", "pass",
          "async", "await", "yield", "lambda", "in", "not", "and", "or",
          "True", "False", "None", "const", "let", "var", "function",
          "export", "default", "interface", "type", "extends", "implements"}
    out = []
    for raw in lines:
        stripped = raw.rstrip()
        # Comments
        if stripped.lstrip().startswith(("#", "//", "--")):
            out.append(f"[dim]{stripped}[/dim]")
            continue
        # Simple keyword coloring
        result = ""
        for word in re.split(r"(\W+)", stripped):
            if word in KW:
                result += f"[bold cyan]{word}[/bold cyan]"
            elif re.match(r'^"[^"]*"$|^\'[^\']*\'$', word):
                result += f"[green]{word}[/green]"
            elif re.match(r'^\d+(\.\d+)?$', word):
                result += f"[yellow]{word}[/yellow]"
            else:
                result += word
        out.append(result)
    return out


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
    # Disable Textual's mouse capture so the terminal can handle text selection.
    # Keyboard navigation (1-6 for agents, tab, etc.) still works fully.
    ENABLE_MOUSE = False

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
        height: 3;
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

    #question-banner {
        display: none;
        layout: vertical;
        background: #1a1f14;
        border-top: solid #5b4a17;
        border-bottom: solid #5b4a17;
        padding: 0 1;
    }

    #question-banner.visible {
        display: block;
    }

    #question-banner-header {
        height: 1;
        color: #f2cc60;
    }

    #question-banner-body {
        color: #e6edf3;
        padding: 0 0 1 0;
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
        height: auto;
        min-height: 1;
        max-height: 8;
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

    #query-input.user-reply-mode {
        border: solid #e3b341;
    }

    Footer {
        background: #161b22;
        color: #484f58;
    }

    /* Code panel */
    #code-panel {
        width: 60;
        background: #0d1117;
        border-left: solid #21262d;
        display: none;
        layout: vertical;
    }

    #code-panel.visible {
        display: block;
    }

    #code-panel-header {
        height: 1;
        background: #161b22;
        border-bottom: solid #21262d;
        padding: 0 1;
        color: #8b949e;
    }

    #code-scroll {
        height: 1fr;
    }

    #code-log {
        padding: 0 1;
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
        Binding("ctrl+c",     "quit",         "Quit"),
        Binding("escape",     "deselect",     "Deselect"),
        Binding("tab",        "next_agent",   "Next agent"),
        Binding("slash",      "focus_input",  "Focus input", show=False),
        Binding("r",          "show_results", "Results"),
        Binding("ctrl+k",     "cancel_run",   "Cancel run"),
        Binding("ctrl+l",     "clear_feed",   "Clear feed"),
        Binding("ctrl+g",     "cancel_reply", "Clear reply", show=False),
        Binding("y",          "copy_result",  "Copy result"),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("ctrl+enter", "submit_input", "Send", show=False),
        Binding("1", "select('coordinator')",         show=False),
        Binding("2", "select('coder')",               show=False),
        Binding("3", "select('reviewer')",            show=False),
        Binding("4", "select('reviewer_a')",          show=False),
        Binding("5", "select('reviewer_b')",          show=False),
        Binding("6", "select('tester')",              show=False),
    ]

    def __init__(
        self,
        server_port: int = 8080,
        topology: str = "triangle",
        show_logs: bool = False,
        initial_query: str | None = None,
        exit_after_run: bool = False,
    ) -> None:
        super().__init__()
        self._server_port   = server_port
        self._topology_name = topology
        self._show_logs     = show_logs
        self._initial_query = initial_query.strip() if initial_query else ""
        self._exit_after_run = exit_after_run
        self._initial_query_started = False

        # Run state (populated by WebSocket events from backend)
        self._agents: dict[str, AgentInfo] = {}
        self._detail_feed: deque[dict] = deque(maxlen=500)
        self._routed: int = 0
        self._run_start: float | None = None
        self._run_status: str = "Waiting"
        self._selected_agent: str | None = None
        self._completions: dict[str, str] = {}
        self._last_query: str = ""
        self._last_elapsed: float = 0.0
        self._last_diff: str = ""
        self._turn_count: int = 0

        # User-prompt handling — set when backend reports an agent waiting for user
        self._awaiting_user: str | None = None
        self._awaiting_user_question: str = ""

        # Animation state
        self._tick_count: int = 0
        self._active_edges: dict[tuple[str, str], int] = {}
        self._elapsed_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None

        # Shared HTTP session (created on mount, closed on unmount)
        self._http_session: Any = None

    def compose(self) -> ComposeResult:
        yield HeaderBar(state=self, id="header-bar")

        with Horizontal(id="body"):
            with Vertical(id="left-panel"):
                yield GraphPanel(state=self, id="graph-panel")
                with VerticalScroll(id="feed-scroll"):
                    yield RichLog(id="message-feed", highlight=True, markup=True, wrap=True)

            with Vertical(id="code-panel"):
                yield Static("", id="code-panel-header")
                with VerticalScroll(id="code-scroll"):
                    yield RichLog(id="code-log", highlight=False, markup=True, wrap=False)

            with Vertical(id="detail-pane"):
                yield Static(id="detail-header")
                with VerticalScroll(id="detail-scroll"):
                    yield RichLog(id="detail-log", highlight=True, markup=True, wrap=True)

        yield ModeBar(state=self, id="mode-bar")

        with Vertical(id="question-banner"):
            yield Static("", id="question-banner-header")
            yield Static("", id="question-banner-body")

        with Vertical(id="log-panel"):
            yield Static(" Logs  [dim]~/.orb/run.log  ·  orb logs -f to stream outside TUI[/dim]", id="log-panel-header")
            yield RichLog(id="log-feed", highlight=False, markup=True, wrap=True)

        with Horizontal(id="query-bar"):
            yield Label(" >  ", id="query-label")
            yield QueryInput(id="query-input", soft_wrap=True)

        yield Footer()

    async def on_mount(self) -> None:
        import aiohttp
        self._http_session = aiohttp.ClientSession()
        feed = self.query_one("#message-feed", RichLog)
        if self._initial_query:
            feed.write("[dim]Connecting to backend and starting task…[/dim]")
        else:
            feed.write("[dim]Ready. Type a task and press [bold]enter[/bold] to send. [bold]y[/bold]=copy result  drag to select &amp; copy text[/dim]")
            self.query_one("#query-input", TextArea).focus()
        self._elapsed_task = asyncio.create_task(self._tick())
        self._ws_task = asyncio.create_task(self._start_ws_client())

        if self._show_logs:
            self.query_one("#log-panel").add_class("visible")
            handler = TUILogHandler(self)
            handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
            self._log_handler = handler
        else:
            self._log_handler = None

    async def on_unmount(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()

    def _show_question_banner(self, agent_id: str, text: str) -> None:
        banner = self.query_one("#question-banner")
        header = self.query_one("#question-banner-header", Static)
        body = self.query_one("#question-banner-body", Static)
        label = AGENT_LABELS.get(agent_id, agent_id.title())
        header.update(
            f"[bold yellow]Replying to {label}[/bold yellow] [dim]Enter to send  Ctrl+G to clear draft[/dim]"
        )
        body.update(text)
        banner.add_class("visible")

    def _hide_question_banner(self) -> None:
        self.query_one("#question-banner").remove_class("visible")
        self.query_one("#question-banner-header", Static).update("")
        self.query_one("#question-banner-body", Static).update("")

    async def _auto_start_initial_query(self) -> None:
        if self._initial_query_started or not self._initial_query:
            return
        self._initial_query_started = True
        await asyncio.sleep(0)
        await self._start_new_run(self._initial_query)

    # ── Tick / animation ──────────────────────────────────────────────────────

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self._tick_count += 1
            # Evict edges whose animation window has expired
            self._active_edges = {
                k: v for k, v in self._active_edges.items() if v > self._tick_count
            }
            self.query_one("#header-bar",  HeaderBar).bump()
            self.query_one("#graph-panel", GraphPanel).bump()
            self.query_one("#mode-bar",    ModeBar).bump()
            if self._selected_agent:
                self._update_detail_header()

    def _refresh_all(self) -> None:
        self.query_one("#header-bar",  HeaderBar).bump()
        self.query_one("#graph-panel", GraphPanel).bump()
        self.query_one("#mode-bar",    ModeBar).bump()

    # ── Input handling ────────────────────────────────────────────────────────

    async def action_submit_input(self) -> None:
        """Submit the textarea (ctrl+enter)."""
        ta = self.query_one("#query-input", TextArea)
        raw = ta.text.strip()
        if not raw:
            return
        ta.clear()
        ta.remove_class("inject-mode")
        ta.remove_class("user-reply-mode")

        # @mention — select agent, run remainder as task
        mention = re.match(r'^@(\w+)\s*', raw)
        if mention:
            agent_id  = mention.group(1).lower()
            remainder = raw[mention.end():].strip()
            self.action_select(agent_id)
            if not remainder:
                return
            raw = remainder

        # Agent waiting for user — reply via HTTP inject
        if self._awaiting_user:
            target = self._awaiting_user
            self._awaiting_user = None
            self._awaiting_user_question = ""
            self.query_one("#query-label", Label).update(" >  ")
            self._hide_question_banner()
            await self._post_json("/api/inject", {"to": target, "message": raw})
            return

        # Mid-run: inject to entry agent
        if self._run_status == "Running":
            entry = next((aid for aid in self._agents if aid == "coordinator"),
                         next(iter(self._agents), "coordinator"))
            await self._post_json("/api/inject", {"to": entry, "message": raw})
            return

        # New run
        await self._start_new_run(raw)

    @on(TextArea.Changed, "#query-input")
    def on_input_changed(self, event: TextArea.Changed) -> None:
        ta = event.text_area
        if self._awaiting_user:
            ta.add_class("user-reply-mode")
            ta.remove_class("inject-mode")
        elif self._run_status == "Running":
            ta.add_class("inject-mode")
            ta.remove_class("user-reply-mode")
        else:
            ta.remove_class("inject-mode")
            ta.remove_class("user-reply-mode")

    def action_cancel_reply(self) -> None:
        if not self._awaiting_user:
            return
        ta = self.query_one("#query-input", TextArea)
        ta.clear()
        ta.focus()
        self.notify("Reply draft cleared", severity="information", timeout=2)

    async def _start_new_run(self, query: str) -> None:
        self._last_query   = query
        self._agents       = {}
        self._detail_feed  = deque(maxlen=500)
        self._completions  = {}
        self._routed       = 0
        self._run_start    = time()
        self._run_status   = "Running"
        self._active_edges = {}

        self.query_one("#detail-log", RichLog).clear()
        feed = self.query_one("#message-feed", RichLog)
        feed.clear()
        feed.write(f"[bold cyan]▶ Task:[/bold cyan] {query}\n")
        self._refresh_all()

        resp = await self._post_json("/api/start", {
            "query": query,
            "topology": self._topology_name,
        })
        if not resp.get("ok"):
            self._run_status = "Error"
            feed.write(f"\n[bold red]✗ Error:[/bold red] {resp.get('error', 'start failed')}")
            self._refresh_all()

    # ── WebSocket client ──────────────────────────────────────────────────────

    async def _post_json(self, path: str, payload: dict) -> dict:
        url = f"http://localhost:{self._server_port}{path}"
        try:
            async with self._http_session.post(url, json=payload) as resp:
                return await resp.json()
        except Exception as exc:
            logger.warning("POST %s failed: %s", path, exc)
            return {"ok": False, "error": str(exc)}

    async def _start_ws_client(self) -> None:
        import aiohttp
        url = f"ws://localhost:{self._server_port}/ws"
        while True:
            try:
                async with self._http_session.ws_connect(url, heartbeat=30) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                self._handle_server_event(json.loads(msg.data))
                            except json.JSONDecodeError as exc:
                                logger.debug("WS JSON decode error: %s", exc)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except Exception as exc:
                logger.debug("WS connection lost: %s", exc)
            await asyncio.sleep(1)  # reconnect backoff

    def _handle_server_event(self, data: dict) -> None:  # noqa: C901
        t = data.get("type")
        if t == "init":
            self._on_server_init(data)
        elif t == "message":
            self._on_server_message(data)
        elif t == "agent_status":
            self._on_server_agent_status(data)
        elif t == "agent_stats":
            self._on_server_agent_stats(data)
        elif t == "agent_activity":
            self._on_server_agent_activity(data)
        elif t == "complete":
            self._on_server_complete(data)
        elif t == "run_complete":
            self._on_server_run_complete(data)
        elif t == "file_write":
            self._on_server_file_write(data)
        elif t == "stopped":
            self._run_status = "Error"
            self.query_one("#message-feed", RichLog).write("\n[dim]Run cancelled.[/dim]")
            self._refresh_all()
        elif t == "stats":
            self._routed = data.get("message_count", self._routed)
            self._last_elapsed = data.get("elapsed", self._last_elapsed)

    def _on_server_init(self, data: dict) -> None:
        self._agents = {}
        self._detail_feed = deque(maxlen=500)
        self._completions = {}
        self._routed = 0
        self._active_edges = {}

        for agent_data in data.get("agents", []):
            aid  = agent_data["id"]
            info = AgentInfo(agent_id=aid, role=agent_data.get("role", aid))
            info.status           = agent_data.get("status", "idle")
            info.model            = agent_data.get("model", "")
            info.complexity_score = agent_data.get("complexity", 0)
            result = agent_data.get("completed_result", "")
            if result:
                info.result = result
                self._completions[aid] = result
            self._agents[aid] = info

        self._topology_name = "dual-review" if "reviewer_a" in self._agents else "triangle"

        stats = data.get("stats", {})
        self._routed       = stats.get("message_count", 0)
        self._last_elapsed = stats.get("elapsed", 0.0)

        run_active = data.get("run_active", False)
        completed  = data.get("completed", False)
        if run_active:
            self._run_status = "Running"
            self._run_start  = time() - stats.get("elapsed", 0)
        elif completed:
            self._run_status = "Idle"

        feed = self.query_one("#message-feed", RichLog)
        feed.clear()
        if self._last_query and run_active:
            feed.write(f"[bold cyan]▶ Task:[/bold cyan] {self._last_query}\n")
        for msg in data.get("messages", []):
            msg_type = msg.get("msg_type", "")
            if msg_type == "system":
                continue
            to_label = "[COMPLETE]" if msg_type == "complete" else msg.get("to", "")
            entry = {
                "elapsed": msg.get("elapsed", 0.0),
                "from_":   msg.get("from", ""),
                "to":      to_label,
                "model":   msg.get("model", ""),
                "preview": msg.get("content", "")[:120].replace("\n", " "),
                "payload": msg.get("content", ""),
                "type":    msg_type,
            }
            self._detail_feed.append(entry)
            for aid in (entry["from_"], to_label):
                if aid in self._agents:
                    self._agents[aid].messages.append(entry)
            self._write_feed(entry)

        if self._initial_query and not self._initial_query_started and not run_active:
            asyncio.create_task(self._auto_start_initial_query())

        self._refresh_all()

    def _on_server_message(self, data: dict) -> None:
        from_id  = data.get("from", "")
        to_id    = data.get("to", "")
        msg_type = data.get("msg_type", "")
        model    = data.get("model", "")
        content  = data.get("content", "")
        elapsed  = data.get("elapsed", 0.0)

        if msg_type == "system":
            return

        for aid in (from_id, to_id):
            if aid and aid not in ("user", "orchestrator", "[COMPLETE]"):
                if aid not in self._agents:
                    self._agents[aid] = AgentInfo(agent_id=aid, role=aid)

        if from_id in self._agents and model:
            self._agents[from_id].model = model

        if msg_type != "complete":
            self._active_edges[(from_id, to_id)] = self._tick_count + 3

        if msg_type == "complete":
            for aid in (from_id, to_id):
                if aid in self._agents:
                    self._agents[aid].set_status("completed")
        else:
            if from_id in self._agents:
                info = self._agents[from_id]
                info.set_status("running")
                info.msg_count += 1
            if to_id in self._agents:
                info = self._agents[to_id]
                if info.status != "completed":
                    info.set_status("waiting")
            self._routed += 1

        to_label = "[COMPLETE]" if msg_type == "complete" else to_id
        entry = {
            "elapsed": elapsed,
            "from_":   from_id,
            "to":      to_label,
            "model":   model,
            "preview": content[:120].replace("\n", " "),
            "payload": content,
            "type":    msg_type,
        }
        self._detail_feed.append(entry)
        for aid in (from_id, to_label):
            if aid in self._agents:
                self._agents[aid].messages.append(entry)
        self._write_feed(entry)

        if self._selected_agent and (
            from_id == self._selected_agent or to_id == self._selected_agent
        ):
            self._append_to_detail(entry)
            self.query_one("#detail-scroll").scroll_end(animate=False)

        self._refresh_all()

    def _on_server_agent_status(self, data: dict) -> None:
        aid    = data.get("agent", "")
        status = data.get("status", "")
        model  = data.get("model", "")
        if aid in self._agents:
            if status:
                self._agents[aid].set_status(status)
            if model:
                self._agents[aid].model = model
        if self._selected_agent == aid:
            self._update_detail_header()
        self._refresh_all()

    def _on_server_agent_stats(self, data: dict) -> None:
        aid        = data.get("agent", "")
        model      = data.get("model", "")
        msg_count  = data.get("msg_count")
        complexity = data.get("complexity")
        if aid in self._agents:
            if model:
                self._agents[aid].model = model
            if msg_count is not None:
                self._agents[aid].msg_count = msg_count
            if complexity is not None:
                self._agents[aid].complexity_score = int(complexity)
        self._refresh_all()

    def _on_server_agent_activity(self, data: dict) -> None:
        aid  = data.get("agent", "")
        text = data.get("activity", "")
        if aid in self._agents:
            self._agents[aid].activity_text = text
        self.query_one("#graph-panel", GraphPanel).bump()
        if aid == self._selected_agent:
            self._update_detail_header()

        if text.startswith("⏳ Waiting for user"):
            self._awaiting_user = aid
            self._awaiting_user_question = text
            feed = self.query_one("#message-feed", RichLog)
            feed.write(f"[bold yellow]{aid} asks user:[/bold yellow] {text}")
            self.query_one("#query-label", Label).update(f" ↩ {aid}: ")
            ta = self.query_one("#query-input", TextArea)
            ta.add_class("user-reply-mode")
            ta.remove_class("inject-mode")
            ta.focus()
            self._show_question_banner(aid, text)
            self.action_select(aid)
        elif text == "" and self._awaiting_user == aid:
            self._awaiting_user = None
            self._awaiting_user_question = ""
            self.query_one("#query-label", Label).update(" >  ")
            self.query_one("#query-input", TextArea).remove_class("user-reply-mode")
            self._hide_question_banner()

    def _on_server_complete(self, data: dict) -> None:
        aid    = data.get("agent", "")
        result = data.get("result", "")
        if aid in self._agents:
            self._agents[aid].set_status("completed")
            self._agents[aid].result        = result
            self._agents[aid].activity_text = ""
        if result and not result.startswith("Consensus:") and result != "[shutdown]":
            self._completions[aid] = result
        self._refresh_all()

    def _on_server_run_complete(self, data: dict) -> None:
        self._last_elapsed = data.get("elapsed", 0.0)
        self._turn_count   = data.get("session_turn", self._turn_count)
        self._last_diff    = data.get("diff", "")
        self._routed       = data.get("routed", self._routed)
        self._run_status   = "Idle"

        for info in self._agents.values():
            if info.status not in ("completed", "error"):
                info.set_status("completed")
            info.activity_text = ""

        sep  = "─" * 48
        feed = self.query_one("#message-feed", RichLog)
        feed.write(
            f"\n[dim]{sep}[/dim]\n"
            f"[dim]── Turn {self._turn_count} complete"
            f"  ({self._last_elapsed:.1f}s · {self._routed} messages) ──[/dim]\n"
            f"[dim]Type your next message or question…[/dim]\n"
        )

        if "coordinator" in self._agents and not self._selected_agent:
            self.action_select("coordinator")

        self.query_one("#query-input", TextArea).focus()
        self._refresh_all()
        if self._exit_after_run:
            self.call_after_refresh(self.action_quit)

    def _on_server_file_write(self, data: dict) -> None:
        import difflib
        agent_id    = data.get("agent", "")
        path        = data.get("path", "")
        content     = data.get("content", "")
        old_content = data.get("old_content", "")
        color  = AGENT_COLORS.get(agent_id, "white")
        is_new = old_content == ""

        new_lines  = content.splitlines(keepends=True)
        old_lines  = old_content.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        ))

        added   = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
        stat    = f"+{added} -{removed}" if not is_new else f"+{len(new_lines)} (new file)"

        feed = self.query_one("#message-feed", RichLog)
        feed.write(
            f"[{color}]{agent_id}[/{color}]"
            f"[dim] wrote [/dim][cyan]{path}[/cyan]"
            f"  [green]+{added}[/green] [red]-{removed}[/red]"
        )

        panel = self.query_one("#code-panel")
        panel.add_class("visible")
        hdr  = self.query_one("#code-panel-header", Static)
        mode = "new file" if is_new else "modified"
        hdr.update(
            f" [{color}]{agent_id}[/{color}]"
            f"[dim] · [/dim][cyan]{path}[/cyan]"
            f"[dim] · {mode} · [/dim]"
            f"[green]+{added}[/green][dim]/[/dim][red]-{removed}[/red]"
        )

        log = self.query_one("#code-log", RichLog)
        log.clear()
        if diff_lines:
            for line in diff_lines:
                line = line.rstrip("\n")
                if line.startswith("@@"):
                    log.write(f"[cyan]{line}[/cyan]")
                elif line.startswith("+++") or line.startswith("---"):
                    log.write(f"[bold]{line}[/bold]")
                elif line.startswith("+"):
                    log.write(f"[green]{line}[/green]")
                elif line.startswith("-"):
                    log.write(f"[red]{line}[/red]")
                else:
                    log.write(f"[dim]{line}[/dim]")
        else:
            for i, l in enumerate(new_lines, 1):
                log.write(f"[green]+{i:3} {l.rstrip()}[/green]")

        if agent_id in self._agents:
            self._agents[agent_id].activity_text = f"wrote {path} ({stat})"
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
        """Append one entry to the detail log with full payload."""
        log = self.query_one("#detail-log", RichLog)
        if not self._agents.get(self._selected_agent):
            return

        from_id = entry["from_"]
        to_id   = entry["to"]
        elapsed = entry.get("elapsed", 0.0)
        ttype   = entry.get("type", "")
        t_clr   = MSG_TYPE_COLOR.get(ttype, "dim")

        if from_id == self._selected_agent:
            direction   = "▶ OUT"
            other       = to_id
            arrow_style = "bold cyan"
        else:
            direction   = "◀ IN "
            other       = from_id
            arrow_style = "bold magenta"

        other_label = AGENT_LABELS.get(str(other).lower(), other)
        other_color = AGENT_COLORS.get(str(other).lower(), "white")
        model_s = f"  [{_short_model(entry['model'])}]" if entry.get("model") else ""

        # Header line
        log.write(
            f"[dim]{'─' * 38}[/dim]"
        )
        log.write(
            f"[{arrow_style}]{direction}[/{arrow_style}]"
            f"  [{other_color}]{other_label}[/{other_color}]"
            f"[dim]{model_s}  {{{ttype}}}  {elapsed:.1f}s[/dim]"
        )

        # Full payload — split into lines, preserve formatting
        full = entry.get("payload") or entry.get("preview", "")
        for line in full.splitlines():
            log.write(f"  [dim]{line}[/dim]")
        log.write("")

    def _populate_detail_pane(self) -> None:
        """Rebuild detail pane from scratch for the selected agent."""
        if not self._selected_agent:
            return
        log  = self.query_one("#detail-log", RichLog)
        log.clear()
        info = self._agents.get(self._selected_agent)
        if not info:
            return

        color = AGENT_COLORS.get(self._selected_agent, "white")
        label = AGENT_LABELS.get(self._selected_agent, self._selected_agent)

        agent_entries = [
            e for e in self._detail_feed
            if e.get("from_") == self._selected_agent
            or e.get("to") == self._selected_agent
        ]

        log.write(f"[bold {color}]■ {label}[/bold {color}] [dim]{info.role}[/dim]")
        log.write("")
        log.write("[bold white]Overview[/bold white]")
        log.write(f"  [dim]status[/dim]  {info.status}")
        log.write(f"  [dim]messages[/dim]  {len(agent_entries)}")
        if info.model:
            log.write(f"  [dim]model[/dim]  {info.model}")
        if info.complexity_score:
            log.write(f"  [dim]complexity[/dim]  {info.complexity_score}")

        if info.activity_text:
            log.write("")
            log.write("[bold white]Current Activity[/bold white]")
            log.write(f"  {info.activity_text}")

        log.write("")
        log.write("[bold white]Recent Messages[/bold white]")
        if agent_entries:
            for entry in agent_entries[-8:]:
                self._append_to_detail(entry)
        else:
            log.write("  [dim]No messages yet.[/dim]")

        if info.result:
            log.write("")
            log.write("[bold green]Result[/bold green]")
            for line in info.result.splitlines():
                log.write(f"  {line}")

        # Scroll to bottom so latest message is visible
        self.query_one("#detail-scroll").scroll_end(animate=False)

    def _update_detail_header(self) -> None:
        hdr = self.query_one("#detail-header", Static)
        if not self._selected_agent:
            hdr.update("")
            return
        info  = self._agents.get(self._selected_agent)
        color = AGENT_COLORS.get(self._selected_agent, "white")
        label = AGENT_LABELS.get(self._selected_agent, self._selected_agent)
        status = info.status if info else "idle"
        icon  = STATUS_ICON.get(status, "○")
        i_clr = STATUS_COLOR.get(status, "dim")

        t = RichText()
        # Line 1: icon + name + model + msg count
        if info and status == "running":
            spin = SPINNERS[self._tick_count % len(SPINNERS)]
            t.append(f" {spin} ", style=f"bold {i_clr}")
        else:
            t.append(f" {icon} ", style=f"bold {i_clr}")
        t.append(f"{label}", style=f"bold {color}")
        if info and info.model:
            t.append(f"  {_short_model(info.model)}", style="dim")
        if info and info.msg_count:
            t.append(f"  ✉{info.msg_count}", style="dim")
        t.append(f"  [{i_clr}]{status}[/{i_clr}]")
        # Line 2: time in state + live activity
        t.append("\n ")
        if info and info.time_in_state > 0.5:
            t.append(f"{info.time_in_state:.0f}s in state  ", style="dim")
        if info and info.activity_text:
            t.append(info.activity_text[:50], style="italic dim")
        else:
            t.append("Esc to close", style="dim")
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
        self.query_one("#query-input", TextArea).focus()

    def action_show_results(self) -> None:
        if self._run_status not in ("Complete", "Idle") or not self._completions:
            return
        self.push_screen(ResultScreen(
            task=self._last_query,
            completions=self._completions,
            elapsed=self._last_elapsed,
            msg_count=self._routed,
            diff=self._last_diff,
        ))

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_cancel_run(self) -> None:
        if self._run_status == "Running":
            await self._post_json("/api/stop", {})

    def action_clear_feed(self) -> None:
        self.query_one("#message-feed", RichLog).clear()

    def action_copy_result(self) -> None:
        """Copy the selected agent's result or the primary worker result."""
        text = ""

        # Prefer selected agent's last completion result
        if self._selected_agent and self._selected_agent in self._completions:
            text = self._completions[self._selected_agent]

        # Fall back to the primary worker result
        if not text and self._completions:
            _, text = pick_primary_result(self._completions)

        # Fall back to code panel content (last written file)
        if not text:
            try:
                log = self.query_one("#code-log", RichLog)
                # RichLog stores rendered lines; grab the plain text
                lines = [seg.text for line in log._lines for seg in line._spans]
                text = "".join(lines).strip()
            except Exception:
                pass

        if text:
            self.copy_to_clipboard(text)
            self.notify("Copied to clipboard", severity="information", timeout=2)
        else:
            self.notify("Nothing to copy yet", severity="warning", timeout=2)

    def action_quit(self) -> None:
        if self._elapsed_task:
            self._elapsed_task.cancel()
        if self._ws_task:
            self._ws_task.cancel()
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)
        self.exit()


# ─── Entry points ─────────────────────────────────────────────────────────────

async def _launch(
    providers: dict,
    config: Any,
    model_overrides: dict | None,
    tier_override: Any,
    topology: str,
    show_logs: bool,
    server_port: int,
    server_host: str = "0.0.0.0",
    initial_query: str | None = None,
    exit_after_run: bool = False,
) -> None:
    from web.server import DashboardServer
    from web.state import DashboardState

    dash_state = DashboardState()
    server = DashboardServer(dash_state, host=server_host, port=server_port)
    server.set_providers(
        providers=providers, config=config,
        model_overrides=model_overrides, tier_override=tier_override,
    )
    await server.start()
    try:
        await OrbTUI(
            server_port=server_port,
            topology=topology,
            show_logs=show_logs,
            initial_query=initial_query,
            exit_after_run=exit_after_run,
        ).run_async()
    finally:
        await server.stop()


def run_tui(
    providers: dict,
    config: Any,
    model_overrides: dict | None = None,
    tier_override: Any = None,
    topology: str = "triangle",
    budget: int = 200,
    show_logs: bool = False,
    server_port: int = 18080,
    initial_query: str | None = None,
    exit_after_run: bool = False,
) -> None:
    """TUI-only mode: backend runs on a local-only port."""
    import asyncio
    asyncio.run(_launch(
        providers=providers, config=config,
        model_overrides=model_overrides, tier_override=tier_override,
        topology=topology, show_logs=show_logs,
        server_port=server_port, server_host="127.0.0.1",
        initial_query=initial_query, exit_after_run=exit_after_run,
    ))


async def run_tui_with_dashboard(
    providers: dict,
    config: Any,
    model_overrides: dict | None = None,
    tier_override: Any = None,
    topology: str = "triangle",
    budget: int = 200,
    dashboard_port: int = 8080,
    show_logs: bool = False,
    initial_query: str | None = None,
    exit_after_run: bool = False,
) -> None:
    """TUI + public dashboard: backend serves both the TUI and the browser."""
    await _launch(
        providers=providers, config=config,
        model_overrides=model_overrides, tier_override=tier_override,
        topology=topology, show_logs=show_logs,
        server_port=dashboard_port, server_host="0.0.0.0",
        initial_query=initial_query, exit_after_run=exit_after_run,
    )
