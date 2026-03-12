from __future__ import annotations

from time import time
from typing import Any

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ..messaging.message import Message, MessageType


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

STATUS_STYLES: dict[str, str] = {
    "idle":      "dim",
    "waiting":   "dim yellow",
    "running":   "bold green",
    "completed": "bold cyan",
    "error":     "bold red",
}

STATUS_ICONS: dict[str, str] = {
    "idle":      "○ idle",
    "waiting":   "◔ waiting",
    "running":   "● running",
    "completed": "✓ done",
    "error":     "✗ error",
}

# Canonical display order
AGENT_ORDER = ["coordinator", "coder", "reviewer", "reviewer_a", "reviewer_b", "tester"]


def _short_model(model_id: str) -> str:
    """claude-haiku-4-5-20251001 → haiku, qwen3.5:27b → qwen:27b"""
    if not model_id:
        return "—"
    import re
    m = re.search(r"claude-([a-z]+)", model_id, re.I)
    if m:
        return m.group(1).lower()
    return model_id


class LiveDisplay:
    """Rich Live TUI showing agent status, message feed, run stats, and final result."""

    MAX_FEED = 18

    def __init__(self, budget: int = 200) -> None:
        self._budget = budget
        self._start_time = time()

        # agent_id -> {status, model, msg_count}
        self._agents: dict[str, dict[str, Any]] = {}
        # recent messages (last MAX_FEED)
        self._feed: list[dict] = []
        # overall stats
        self._routed = 0

        # Optional topology/complexity info shown in header
        self._topology: str = ""
        self._complexity: int | None = None
        self._agent_models: dict[str, str] = {}   # role -> model_id

        self._console = Console()
        self._layout = self._build_layout()
        self._live = Live(
            self._layout,
            console=self._console,
            refresh_per_second=4,
            screen=False,
            transient=False,
        )

    # ------------------------------------------------------------------
    # Public setup helpers
    # ------------------------------------------------------------------

    def set_topology_info(
        self,
        topology: str,
        complexity: int | None = None,
        agent_models: dict[str, str] | None = None,
    ) -> None:
        """Call before start() to show topology/complexity in the header."""
        self._topology = topology
        self._complexity = complexity
        self._agent_models = agent_models or {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._start_time = time()
        self._live.start(refresh=True)

    def stop(self) -> None:
        # Mark any agents still mid-run as completed before the final render
        for info in self._agents.values():
            if info["status"] not in ("completed", "error"):
                info["status"] = "completed"
        self._live.update(self._render())
        self._live.refresh()
        self._live.stop()

    # ------------------------------------------------------------------
    # Event callback — compatible with bus.on_event(callback) signature
    # ------------------------------------------------------------------

    def on_event(self, event_name: str, msg: Message) -> None:
        elapsed = time() - self._start_time

        # Ensure all participants have an entry
        for agent_id in (msg.from_, msg.to):
            if agent_id and agent_id not in ("user", "orchestrator", "[COMPLETE]"):
                if agent_id not in self._agents:
                    self._agents[agent_id] = {"status": "idle", "model": "", "msg_count": 0}

        # Update model if present
        model = msg.metadata.get("model", "")
        if msg.from_ in self._agents and model:
            self._agents[msg.from_]["model"] = model

        # Status transitions
        if event_name == "injected":
            if msg.to in self._agents:
                self._agents[msg.to]["status"] = "running"
        elif event_name == "routed":
            if msg.type == MessageType.COMPLETE:
                if msg.from_ in self._agents:
                    self._agents[msg.from_]["status"] = "completed"
                if msg.to in self._agents:
                    self._agents[msg.to]["status"] = "completed"
            else:
                if msg.from_ in self._agents:
                    self._agents[msg.from_]["status"] = "running"
                    self._agents[msg.from_]["msg_count"] += 1
                if msg.to in self._agents:
                    if self._agents[msg.to]["status"] not in ("completed",):
                        self._agents[msg.to]["status"] = "waiting"
            self._routed += 1

        # Feed entry
        to_label = "[COMPLETE]" if msg.type == MessageType.COMPLETE else msg.to
        preview = msg.payload[:120].replace("\n", " ")
        self._feed.append({
            "elapsed": elapsed,
            "from_":   msg.from_,
            "to":      to_label,
            "model":   model,
            "preview": preview,
            "type":    msg.type.value,
        })
        if len(self._feed) > self.MAX_FEED:
            self._feed = self._feed[-self.MAX_FEED:]

        self._live.update(self._render())

    # ------------------------------------------------------------------
    # Layout & rendering
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="agents", size=8),
            Layout(name="feed"),
            Layout(name="stats", size=3),
        )
        return layout

    def _render(self) -> Layout:
        self._layout["header"].update(self._render_header())
        self._layout["agents"].update(self._render_agents())
        self._layout["feed"].update(self._render_feed())
        self._layout["stats"].update(self._render_stats())
        return self._layout

    def _render_header(self) -> Panel:
        t = Text()
        t.append("orb", style="bold magenta")
        t.append("  —  LLM Agent Collaboration Network", style="dim")

        if self._topology or self._complexity is not None:
            t.append("\n")
            if self._topology:
                topo_label = "Dual Review" if "dual" in self._topology else "Triad"
                t.append(f"Topology: ", style="dim")
                t.append(topo_label, style="bold cyan")
                t.append("   ", style="dim")
            if self._complexity is not None:
                c = self._complexity
                color = "red" if c >= 75 else "yellow" if c >= 50 else "green"
                t.append("Complexity: ", style="dim")
                t.append(str(c), style=f"bold {color}")
                t.append("/100", style="dim")

        if self._agent_models:
            t.append("\n")
            for i, (role, mid) in enumerate(self._agent_models.items()):
                if i:
                    t.append("  ")
                color = AGENT_COLORS.get(role, "white")
                t.append(f"{role}: ", style=f"dim {color}")
                t.append(_short_model(mid), style=color)

        return Panel(t, border_style="blue", padding=(0, 1))

    def _render_agents(self) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Agent",    style="bold", min_width=12)
        table.add_column("Model",    style="dim",  min_width=20)
        table.add_column("Status",                 min_width=12)
        table.add_column("Messages", justify="right", min_width=8)

        ordered = AGENT_ORDER + [k for k in self._agents if k not in AGENT_ORDER]
        for agent_id in ordered:
            if agent_id not in self._agents:
                continue
            info  = self._agents[agent_id]
            color = AGENT_COLORS.get(agent_id, "white")
            label = AGENT_LABELS.get(agent_id, agent_id.title())

            # Use pinned model from agent_models map if the agent hasn't reported one yet
            model_display = info["model"] or self._agent_models.get(agent_id, "")
            model_short   = _short_model(model_display)

            status      = info["status"]
            status_text = Text(STATUS_ICONS.get(status, status), style=STATUS_STYLES.get(status, ""))

            table.add_row(
                Text(label, style=color),
                model_short,
                status_text,
                str(info["msg_count"]),
            )

        return Panel(table, title="[bold]Agents[/bold]", border_style="blue", padding=(0, 1))

    def _render_feed(self) -> Panel:
        lines = Text()
        if not self._feed:
            lines.append("Waiting for messages…", style="dim italic")
        else:
            for i, entry in enumerate(self._feed):
                if i:
                    lines.append("\n")

                from_color = AGENT_COLORS.get(entry["from_"].lower(), "white")
                to_label   = entry["to"]
                to_color   = (
                    "bold green"
                    if to_label == "[COMPLETE]"
                    else AGENT_COLORS.get(to_label.lower(), "white")
                )
                model_str = f" [{_short_model(entry['model'])}]" if entry["model"] else ""
                type_label = entry.get("type", "")
                type_badge = f" {{{type_label}}}" if type_label not in ("", "response") else ""

                lines.append(f"[{entry['elapsed']:5.1f}s] ", style="dim")
                lines.append(entry["from_"],               style=from_color)
                lines.append(model_str,                    style="dim")
                lines.append(type_badge,                   style="dim cyan")
                lines.append(" → ",                        style="dim")
                lines.append(to_label,                     style=to_color)
                lines.append(": ",                         style="dim")
                lines.append(f'"{entry["preview"]}"',      style="italic")

        return Panel(lines, title="[bold]Message Feed[/bold]", border_style="blue", padding=(0, 1))

    def _render_stats(self) -> Panel:
        elapsed           = time() - self._start_time
        budget_remaining  = max(0, self._budget - self._routed)
        color             = "bold cyan" if budget_remaining > 20 else "bold red"

        bar = Text()
        bar.append("  Messages routed: ", style="dim")
        bar.append(str(self._routed),       style="bold")
        bar.append("   |   Budget remaining: ", style="dim")
        bar.append(str(budget_remaining),   style=color)
        bar.append("   |   Elapsed: ",      style="dim")
        bar.append(f"{elapsed:.1f}s",        style="bold")

        return Panel(bar, title="[bold]Stats[/bold]", border_style="blue", padding=(0, 0))
