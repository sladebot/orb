from __future__ import annotations

import asyncio

from rich.console import Console
from rich.prompt import Prompt

from ..llm.client import LLMClient
from ..llm.types import ModelTier, ModelConfig
from ..orchestrator.types import OrchestratorConfig
from ..topologies import create_orchestrator, get_loader
from .display import print_header, print_result, print_error

console = Console()


async def run_repl(
    providers: dict[str, LLMClient],
    config: OrchestratorConfig,
    model_overrides: dict[ModelTier, ModelConfig] | None = None,
    trace: bool = True,
    tier_override: ModelTier | None = None,
    topology: str = "triangle",
) -> None:
    print_header()
    console.print("[dim]Type your query, or 'quit' to exit.[/dim]")
    console.print("[dim]Commands: /topology <name>, /list-topologies, /reload-topologies[/dim]\n")

    current_topology = topology

    while True:
        try:
            query = Prompt.ask("[bold blue]>[/bold blue]")
        except (EOFError, KeyboardInterrupt):
            break

        stripped = query.strip().lower()
        if stripped in ("quit", "exit", "q"):
            break

        if stripped.startswith("/topology ") or stripped.startswith("use topology "):
            new_topo = stripped.split()[-1]
            loader = get_loader()
            if new_topo in loader.list_ids():
                current_topology = new_topo
                console.print(f"[green]Switched to topology: {new_topo}[/green]")
            else:
                console.print(f"[red]Unknown topology: {new_topo}[/red]")
                console.print(f"[dim]Available: {', '.join(loader.list_ids())}[/dim]")
            continue

        if stripped == "/list-topologies":
            loader = get_loader()
            for tid in loader.list_ids():
                topo = loader.get(tid)
                marker = " [bold cyan]*[/bold cyan]" if tid == current_topology else ""
                console.print(f"  [bold]{tid}[/bold]: {topo.label} — {topo.description}{marker}")
            continue

        if stripped == "/reload-topologies":
            loader = get_loader()
            loader.load(force=True)
            console.print("[green]Topologies reloaded[/green]")
            continue

        if not query.strip():
            continue

        orchestrator = create_orchestrator(
            current_topology,
            providers=providers,
            config=config,
            model_overrides=model_overrides,
            trace=trace,
            tier_override=tier_override,
        )

        result = await orchestrator.run(query)

        if result.error:
            print_error(result.error)
        else:
            print_result(result.completions, result.message_count, result.timed_out)

        console.print()
