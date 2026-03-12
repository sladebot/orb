from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

console = Console()

AGENT_STYLES = {
    "coordinator": ("magenta", "Coordinator"),
    "coder":       ("cyan",    "Coder"),
    "reviewer":    ("yellow",  "Reviewer"),
    "reviewer_a":  ("yellow",  "Reviewer A"),
    "reviewer_b":  ("dark_orange", "Reviewer B"),
    "tester":      ("green",   "Tester"),
}

# Skip showing these as standalone results (they're interim completions)
SKIP_RESULT_PREFIX = "Consensus:"


def pick_primary_result(completions: dict[str, str]) -> tuple[str | None, str]:
    preferred = ["coder", "reviewer", "reviewer_a", "reviewer_b", "tester", "coordinator"]
    for agent_id in preferred:
        result = completions.get(agent_id, "")
        if result and not result.startswith(SKIP_RESULT_PREFIX) and result != "[shutdown]":
            return agent_id, result
    for agent_id, result in completions.items():
        if result and not result.startswith(SKIP_RESULT_PREFIX) and result != "[shutdown]":
            return agent_id, result
    return None, ""


def print_header() -> None:
    console.print(Panel(
        "[bold magenta]orb[/bold magenta]  [dim]—  LLM Agent Collaboration Network[/dim]",
        border_style="blue",
    ))


def print_result(completions: dict[str, str], message_count: int, timed_out: bool) -> None:
    console.print()
    if timed_out:
        console.print("[bold red]⚠  Run timed out[/bold red]")
    console.print(f"[dim]Messages routed: {message_count}[/dim]")
    console.print()

    primary_id, primary_result = pick_primary_result(completions)
    worker_results = [
        (aid, result)
        for aid, result in completions.items()
        if result
        and not result.startswith(SKIP_RESULT_PREFIX)
        and result != "[shutdown]"
        and aid != primary_id
    ]

    if primary_result:
        agent_id, result = primary_id, primary_result
        color, label = AGENT_STYLES.get(agent_id, ("magenta", agent_id.title()))
        console.print(Panel(
            Markdown(result),
            title=f"[bold {color}]Final Result[/bold {color}]",
            border_style=color,
            padding=(1, 2),
        ))
    elif not worker_results:
        console.print("[dim]No results.[/dim]")
        return

    if worker_results:
        if primary_result:
            console.print()
            console.print(Rule("[dim]Worker Results[/dim]", style="dim"))
            console.print()
        for agent_id, result in worker_results:
            color, label = AGENT_STYLES.get(agent_id, ("white", agent_id.title()))
            console.print(Panel(
                Markdown(result),
                title=f"[{color}]{label}[/{color}]",
                border_style="dim",
                padding=(0, 1),
            ))
            console.print()


def print_error(msg: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {msg}")
