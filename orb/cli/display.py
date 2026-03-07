from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

AGENT_STYLES = {
    "coder": ("cyan", "Coder"),
    "reviewer": ("yellow", "Reviewer"),
    "tester": ("green", "Tester"),
}


def print_header() -> None:
    console.print(Panel(
        "[bold]Orb[/bold] — LLM Agent Collaboration Network",
        style="blue",
    ))


def print_result(completions: dict[str, str], message_count: int, timed_out: bool) -> None:
    console.print()
    if timed_out:
        console.print("[bold red]Run timed out[/bold red]")

    console.print(f"[dim]Total messages routed: {message_count}[/dim]")
    console.print()

    for agent_id, result in completions.items():
        color, label = AGENT_STYLES.get(agent_id, ("white", agent_id))
        console.print(Panel(
            result,
            title=f"[bold {color}]{label} Result[/bold {color}]",
            border_style=color,
        ))


def print_error(msg: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {msg}")
