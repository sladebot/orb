from __future__ import annotations

import argparse
import asyncio
import os
import sys

from ..llm.providers import AnthropicProvider, OpenAIProvider, OllamaProvider
from ..llm.types import ModelTier, ModelConfig, DEFAULT_MODELS
from ..orchestrator.types import OrchestratorConfig
from ..topologies.triangle import create_triangle
from .display import print_header, print_result, print_error
from .repl import run_repl


def build_providers(
    local_only: bool = False,
    cloud_only: bool = False,
) -> dict[str, object]:
    providers = {}

    if not local_only:
        if os.environ.get("ANTHROPIC_API_KEY"):
            providers["anthropic"] = AnthropicProvider()
        if os.environ.get("OPENAI_API_KEY"):
            providers["openai"] = OpenAIProvider()

    if not cloud_only:
        providers["ollama"] = OllamaProvider()

    return providers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orb",
        description="LLM Agent Collaboration Network",
    )
    parser.add_argument("query", nargs="?", help="Task query (omit for interactive mode)")
    parser.add_argument("-i", "--interactive", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--trace", action="store_true", default=True, help="Show real-time message routing")
    parser.add_argument("--no-trace", action="store_true", help="Disable tracing")
    parser.add_argument("--budget", type=int, default=200, help="Global message budget")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds")
    parser.add_argument("--max-depth", type=int, default=10, help="Max message hop depth")
    parser.add_argument("--model", type=str, help="Override default cloud model")
    parser.add_argument("--local-only", action="store_true", help="Use only local models")
    parser.add_argument("--cloud-only", action="store_true", help="Use only cloud models")
    parser.add_argument("--dashboard", action="store_true", help="Launch live web dashboard")
    parser.add_argument("--dashboard-port", type=int, default=8080, help="Dashboard server port")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()

    trace = args.trace and not args.no_trace
    providers = build_providers(
        local_only=args.local_only,
        cloud_only=args.cloud_only,
    )

    if not providers:
        print_error(
            "No LLM providers available. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
            "or ensure Ollama is running locally."
        )
        sys.exit(1)

    config = OrchestratorConfig(
        timeout=args.timeout,
        budget=args.budget,
        max_depth=args.max_depth,
    )

    model_overrides = None
    if args.model:
        # Determine provider from model name
        if "claude" in args.model:
            provider = "anthropic"
        elif "gpt" in args.model:
            provider = "openai"
        else:
            provider = "ollama"
        override_config = ModelConfig(
            tier=ModelTier.CLOUD_FAST,
            model_id=args.model,
            provider=provider,
        )
        model_overrides = {ModelTier.CLOUD_FAST: override_config, ModelTier.CLOUD_STRONG: override_config}

    tier_override = None
    if args.local_only:
        tier_override = ModelTier.LOCAL_MEDIUM
    elif args.cloud_only:
        tier_override = ModelTier.CLOUD_FAST

    if args.interactive or args.query is None:
        await run_repl(
            providers=providers,
            config=config,
            model_overrides=model_overrides,
            trace=trace,
            tier_override=tier_override,
        )
    else:
        print_header()
        orchestrator = create_triangle(
            providers=providers,
            config=config,
            model_overrides=model_overrides,
            trace=trace,
            tier_override=tier_override,
        )

        dashboard_server = None
        if args.dashboard:
            from web.server import DashboardServer
            from web.bridge import DashboardBridge
            from web.state import DashboardState

            dash_state = DashboardState()
            dashboard_server = DashboardServer(dash_state, port=args.dashboard_port)
            bridge = DashboardBridge(dash_state, dashboard_server.broadcast)

            # Set up bridge with topology info
            agent_roles = {aid: a.config.role for aid, a in orchestrator.agents.items()}
            bridge.setup_agents(agent_roles)
            bridge.setup_edges(
                [(e.a, e.b) for e in orchestrator.bus.graph.edges]
            )
            bridge.setup_budget(config.budget)

            # Wire bridge into bus events
            orchestrator.bus.on_event(bridge.on_message_routed)

            # Wire completion callbacks through bridge
            for agent in orchestrator.agents.values():
                original_cb = agent._on_complete
                async def make_complete_cb(bridge_ref, orig):
                    async def cb(agent_id, result):
                        await bridge_ref.on_agent_complete(agent_id, result)
                        if orig:
                            r = orig(agent_id, result)
                            if asyncio.iscoroutine(r):
                                await r
                    return cb
                # This will be overwritten by orchestrator.run() anyway,
                # so we hook into the orchestrator instead

            await dashboard_server.start()

            import webbrowser
            webbrowser.open(f"http://localhost:{args.dashboard_port}")

            # Hook bridge into orchestrator's completion tracking
            original_on_complete = orchestrator._on_agent_complete
            async def wrapped_on_complete(agent_id, result):
                await bridge.on_agent_complete(agent_id, result)
                await original_on_complete(agent_id, result)
            orchestrator._on_agent_complete = wrapped_on_complete

        result = await orchestrator.run(args.query)

        if dashboard_server:
            # Keep dashboard open for a bit so user can inspect
            from rich.prompt import Prompt
            Prompt.ask("\n[dim]Dashboard running. Press Enter to shut down[/dim]")
            await dashboard_server.stop()

        if result.error:
            print_error(result.error)
            sys.exit(1)
        else:
            print_result(result.completions, result.message_count, result.timed_out)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
