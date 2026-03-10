from __future__ import annotations

import asyncio

from rich.console import Console
from rich.prompt import Prompt

from ..llm.client import LLMClient
from ..llm.types import ModelTier, ModelConfig
from ..orchestrator.types import OrchestratorConfig
from ..topologies.triad import create_triad
from .display import print_header, print_result, print_error

console = Console()


async def run_repl(
    providers: dict[str, LLMClient],
    config: OrchestratorConfig,
    model_overrides: dict[ModelTier, ModelConfig] | None = None,
    trace: bool = True,
    tier_override: ModelTier | None = None,
) -> None:
    print_header()
    console.print("[dim]Type your query, or 'quit' to exit.[/dim]\n")

    while True:
        try:
            query = Prompt.ask("[bold blue]>[/bold blue]")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("quit", "exit", "q"):
            break

        if not query.strip():
            continue

        orchestrator = create_triad(
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
