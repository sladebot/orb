from __future__ import annotations

from time import time
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..messaging.message import Message, MessageType


AGENT_COLORS: dict[str, str] = {
    "coder": "cyan",
    "reviewer": "yellow",
    "tester": "green",
}

AGENT_LABELS: dict[str, str] = {
    "coder": "Coder",
    "reviewer": "Reviewer",
    "tester": "Tester",
}

STATUS_STYLES: dict[str, str] = {
    "idle": "dim",
    "waiting": "dim yellow",
    "running": "bold green",
    "completed": "bold cyan",
    "error": "bold red",
}


class LiveDisplay:
    """Rich Live TUI that shows agent status, message feed, and run stats."""

    MAX_FEED = 8

    def __init__(self, budget: int = 200) -> None:
        self._budget = budget
        self._start_time = time()

        # agent_id -> {status, model, msg_count}
        self._agents: dict[str, dict[str, Any]] = {}
        # recent messages (last MAX_FEED)
        self._feed: list[dict] = []
        # overall stats
        self._routed = 0

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
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._start_time = time()
        self._live.start(refresh=True)

    def stop(self) -> None:
        # Do one last render before stopping so the final state is visible.
        self._live.update(self._render())
        self._live.refresh()
        self._live.stop()

    # ------------------------------------------------------------------
    # Event callback — compatible with bus.on_event(callback) signature
    # ------------------------------------------------------------------

    def on_event(self, event_name: str, msg: Message) -> None:
        elapsed = time() - self._start_time

        # Infer agent info from the message
        for agent_id in (msg.from_, msg.to):
            if agent_id and agent_id not in ("user", "orchestrator", "[COMPLETE]"):
                if agent_id not in self._agents:
                    self._agents[agent_id] = {
                        "status": "idle",
                        "model": "",
                        "msg_count": 0,
                    }

        # Update sender model if present
        model = msg.metadata.get("model", "")
        if msg.from_ in self._agents and model:
            self._agents[msg.from_]["model"] = model

        # Determine status transitions
        if event_name == "injected":
            # Initial task injected — mark receiver as running
            if msg.to in self._agents:
                self._agents[msg.to]["status"] = "running"
        elif event_name == "routed":
            if msg.type == MessageType.COMPLETE:
                if msg.from_ in self._agents:
                    self._agents[msg.from_]["status"] = "completed"
                if msg.to in self._agents:
                    self._agents[msg.to]["status"] = "completed"
            else:
                # Sender just sent a message — mark as active
                if msg.from_ in self._agents:
                    self._agents[msg.from_]["status"] = "running"
                    self._agents[msg.from_]["msg_count"] += 1
                # Receiver is now waiting/running
                if msg.to in self._agents:
                    current = self._agents[msg.to]["status"]
                    if current not in ("completed",):
                        self._agents[msg.to]["status"] = "waiting"
            self._routed += 1

        # Build feed entry
        if msg.type == MessageType.COMPLETE:
            to_label = "[COMPLETE]"
        else:
            to_label = msg.to

        preview = msg.payload[:100].replace("\n", " ")
        self._feed.append({
            "elapsed": elapsed,
            "from_": msg.from_,
            "to": to_label,
            "model": model,
            "preview": preview,
            "event": event_name,
        })
        # Keep only last MAX_FEED entries
        if len(self._feed) > self.MAX_FEED:
            self._feed = self._feed[-self.MAX_FEED:]

        self._live.update(self._render())

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="agents", size=7),
            Layout(name="feed", minimum_size=4),
            Layout(name="stats", size=3),
        )
        return layout

    def _render(self) -> Layout:
        self._layout["agents"].update(self._render_agents())
        self._layout["feed"].update(self._render_feed())
        self._layout["stats"].update(self._render_stats())
        return self._layout

    def _render_agents(self) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Agent", style="bold", min_width=10)
        table.add_column("Model", style="dim", min_width=24)
        table.add_column("Status", min_width=12)
        table.add_column("Messages", justify="right", min_width=8)

        # Sort agents consistently
        agent_order = ["coder", "reviewer", "tester"]
        all_ids = agent_order + [k for k in self._agents if k not in agent_order]

        for agent_id in all_ids:
            if agent_id not in self._agents:
                continue
            info = self._agents[agent_id]
            color = AGENT_COLORS.get(agent_id.lower(), "white")
            label = AGENT_LABELS.get(agent_id.lower(), agent_id.title())
            status = info["status"]
            status_style = STATUS_STYLES.get(status, "")
            model_short = info["model"].split("/")[-1] if info["model"] else "—"

            # Status indicator
            status_icons = {
                "idle": "○ idle",
                "waiting": "◔ waiting",
                "running": "● running",
                "completed": "✓ done",
                "error": "✗ error",
            }
            status_text = Text(status_icons.get(status, status), style=status_style)

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
                if i > 0:
                    lines.append("\n")
                from_color = AGENT_COLORS.get(entry["from_"].lower(), "white")
                to_label = entry["to"]
                to_color = (
                    "bold green"
                    if to_label == "[COMPLETE]"
                    else AGENT_COLORS.get(to_label.lower(), "white")
                )
                model_str = f" ({entry['model']})" if entry["model"] else ""

                lines.append(f"[{entry['elapsed']:5.1f}s] ", style="dim")
                lines.append(entry["from_"], style=from_color)
                lines.append(model_str, style="dim")
                lines.append(" → ", style="dim")
                lines.append(to_label, style=to_color)
                lines.append(": ", style="dim")
                lines.append(f'"{entry["preview"]}"', style="italic")

        return Panel(lines, title="[bold]Message Feed[/bold]", border_style="blue", padding=(0, 1))

    def _render_stats(self) -> Panel:
        elapsed = time() - self._start_time
        budget_used = self._routed
        budget_remaining = max(0, self._budget - budget_used)

        bar = Text()
        bar.append("  Messages routed: ", style="dim")
        bar.append(str(self._routed), style="bold")
        bar.append("   |   Budget remaining: ", style="dim")
        bar.append(str(budget_remaining), style="bold cyan" if budget_remaining > 20 else "bold red")
        bar.append("   |   Elapsed: ", style="dim")
        bar.append(f"{elapsed:.1f}s", style="bold")

        return Panel(bar, title="[bold]Stats[/bold]", border_style="blue", padding=(0, 0))
