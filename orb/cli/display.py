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

# Agents whose result is the canonical "final answer"
SYNTHESIS_AGENTS = {"coordinator"}

# Skip showing these as standalone results (they're interim completions)
SKIP_RESULT_PREFIX = "Consensus:"


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

    # Separate synthesis result from worker results
    synthesis_result: tuple[str, str] | None = None
    worker_results: list[tuple[str, str]] = []

    for agent_id, result in completions.items():
        if not result or result.startswith(SKIP_RESULT_PREFIX):
            continue
        if agent_id in SYNTHESIS_AGENTS:
            synthesis_result = (agent_id, result)
        else:
            worker_results.append((agent_id, result))

    # If no synthesis agent (simple topology), treat all as worker results
    if not synthesis_result and not worker_results:
        worker_results = [(aid, r) for aid, r in completions.items() if r]

    # Show synthesis result prominently
    if synthesis_result:
        agent_id, result = synthesis_result
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

    # Show worker results as collapsible sub-panels if present
    if worker_results:
        if synthesis_result:
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
