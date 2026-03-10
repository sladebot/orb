from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from ..llm.registry import build_providers
from ..llm.types import ModelTier, ModelConfig, DEFAULT_MODELS
from ..orchestrator.types import OrchestratorConfig
from ..topologies.triad import create_triad
from .display import print_header, print_result, print_error
from .repl import run_repl



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orb",
        description="LLM Agent Collaboration Network",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    # orb auth <provider>
    auth_parser = subparsers.add_parser("auth", help="Authenticate with a provider")
    auth_sub = auth_parser.add_subparsers(dest="auth_provider")
    openai_auth = auth_sub.add_parser("openai", help="Log in with OpenAI via OAuth or store API key")
    openai_auth.add_argument("--api-key", metavar="SK", help="Store an API key directly (skips OAuth)")
    auth_sub.add_parser("status", help="Show current auth status")
    auth_sub.add_parser("logout", help="Revoke stored OpenAI credentials")

    # Main run args (attached to the root parser so 'orb <query>' still works)
    parser.add_argument("query", nargs="?", help="Task query (omit for interactive mode)")
    parser.add_argument("-i", "--interactive", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--trace", action="store_true", default=True, help="Show real-time message routing")
    parser.add_argument("--no-trace", action="store_true", help="Disable tracing")
    parser.add_argument("--budget", type=int, default=200, help="Global message budget")
    parser.add_argument("--timeout", type=float, default=600.0, help="Timeout in seconds")
    parser.add_argument("--max-depth", type=int, default=10, help="Max message hop depth")
    parser.add_argument("--model", type=str, help="Override default cloud model")
    parser.add_argument("--local-only", action="store_true", help="Use only local models")
    parser.add_argument("--cloud-only", action="store_true", help="Use only cloud models")
    parser.add_argument("--ollama-model", type=str, default=os.environ.get("OLLAMA_MODEL"), help="Ollama model to use for all local tiers (e.g. qwen3.5:9b)")
    parser.add_argument("--dashboard", action="store_true", help="Launch live web dashboard")
    parser.add_argument("--dashboard-port", type=int, default=8080, help="Dashboard server port")
    parser.add_argument("--dev", action="store_true", help="Dev mode: auto-restart on file changes")
    parser.add_argument("--topology", choices=["triangle", "dual-review"], default="triangle", help="Agent topology to use")
    parser.add_argument("--verbose", "-v", action="store_true", default=True, help="Enable verbose logging (default: on)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose logging")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()

    # ── auth subcommand ───────────────────────────────────────────────────────
    if args.subcommand == "auth":
        from .auth import auth_openai, auth_status, revoke_openai_token
        provider = args.auth_provider
        if provider == "openai":
            api_key = getattr(args, "api_key", None)
            if api_key:
                from .auth import _save_credentials, CREDS_PATH
                _save_credentials("openai", {"api_key": api_key})
                print(f"Key stored at {CREDS_PATH}")
            else:
                await auth_openai()
        elif provider == "status" or provider is None:
            await auth_status()
        elif provider == "logout":
            revoke_openai_token()
            print("OpenAI credentials revoked.")
        else:
            print(f"Unknown auth provider: {provider}")
        return

    if args.verbose and not args.quiet:
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        logging.basicConfig(level=logging.DEBUG, format=fmt)
        # Silence noisy third-party libraries
        for noisy in ("httpx", "httpcore", "anthropic", "openai", "asyncio", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING)

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

    model_overrides: dict[ModelTier, ModelConfig] = {}

    if args.ollama_model:
        for tier in (ModelTier.LOCAL_SMALL, ModelTier.LOCAL_MEDIUM, ModelTier.LOCAL_LARGE):
            model_overrides[tier] = ModelConfig(tier=tier, model_id=args.ollama_model, provider="ollama")

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
        model_overrides[ModelTier.CLOUD_FAST] = override_config
        model_overrides[ModelTier.CLOUD_STRONG] = override_config

    tier_override = None
    if args.local_only:
        tier_override = ModelTier.LOCAL_MEDIUM
    elif args.cloud_only:
        tier_override = ModelTier.CLOUD_FAST

    # --dashboard without a query: serve the UI and wait for the browser to start a run
    if args.dashboard and args.query is None and not args.interactive:
        from web.server import DashboardServer
        from web.state import DashboardState

        dash_state = DashboardState()
        dashboard_server = DashboardServer(dash_state, port=args.dashboard_port)
        dashboard_server.set_providers(
            providers=providers,
            config=config,
            model_overrides=model_overrides or None,
            tier_override=tier_override,
        )

        await dashboard_server.start()

        print_header()
        print(f"  Dashboard running at http://localhost:{args.dashboard_port}")
        print("  Open the URL in your browser, type a task, and click Run.")
        print("  Press Ctrl-C to shut down.\n")

        # Keep server alive until Ctrl-C
        stop_event = asyncio.Event()
        try:
            import signal
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows fallback — will still stop on KeyboardInterrupt

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await dashboard_server.stop()
        return

    if args.interactive or args.query is None:
        await run_repl(
            providers=providers,
            config=config,
            model_overrides=model_overrides or None,
            trace=trace,
            tier_override=tier_override,
        )
    else:
        print_header()
        if args.topology == "dual-review":
            from ..topologies.dual_review import create_dual_review
            orchestrator = create_dual_review(
                providers=providers,
                config=config,
                model_overrides=model_overrides or None,
                trace=False,  # we register our own listener below
                tier_override=tier_override,
            )
        else:
            orchestrator = create_triad(
                providers=providers,
                config=config,
                model_overrides=model_overrides or None,
                trace=False,  # we register our own listener below
                tier_override=tier_override,
            )

        live_display = None
        dashboard_server = None

        if not args.dashboard and trace:
            from .live_display import LiveDisplay
            live_display = LiveDisplay(budget=args.budget)
            # Pass topology/model info for the header (use defaults since no LLM prediction here)
            topo_label = args.topology
            # Build a quick model map showing what each role will use
            agent_models = {
                aid: a.config.pinned_model.model_id if a.config.pinned_model else ""
                for aid, a in orchestrator.agents.items()
            }
            agent_models = {k: v for k, v in agent_models.items() if v}
            live_display.set_topology_info(
                topology=topo_label,
                complexity=None,
                agent_models=agent_models,
            )
            orchestrator.bus.on_event(live_display.on_event)
            live_display.start()

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

            dashboard_server.set_agents(orchestrator.agents)
            await dashboard_server.start()

            print(f"  Dashboard running at http://localhost:{args.dashboard_port}")

            # Hook bridge into orchestrator's completion tracking
            original_on_complete = orchestrator._on_agent_complete
            async def wrapped_on_complete(agent_id, result):
                await bridge.on_agent_complete(agent_id, result)
                await original_on_complete(agent_id, result)
            orchestrator._on_agent_complete = wrapped_on_complete

        result = await orchestrator.run(args.query)

        if live_display:
            live_display.stop()

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


def _dev_watcher(forward_args: list[str]) -> None:
    """Watch source files and restart the orb process on changes."""
    import subprocess
    import time
    from pathlib import Path

    root = Path(__file__).parent.parent.parent  # repo root
    watch_dirs = [root / "orb", root / "web"]
    watch_exts = {".py", ".js", ".css", ".html"}

    def mtimes() -> dict[str, float]:
        out: dict[str, float] = {}
        for d in watch_dirs:
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.suffix in watch_exts and "__pycache__" not in str(f):
                    try:
                        out[str(f)] = f.stat().st_mtime
                    except OSError:
                        pass
        return out

    cmd = [sys.executable, "-m", "orb.cli.main"] + forward_args
    print(f"  [dev] Watching orb/ and web/ for changes…")
    print(f"  [dev] Starting: {' '.join(cmd[2:])}\n")

    proc: subprocess.Popen | None = None
    last = mtimes()

    def start() -> subprocess.Popen:
        return subprocess.Popen(cmd)

    try:
        proc = start()
        while True:
            time.sleep(0.8)
            if proc.poll() is not None:
                print("\n  [dev] Process exited — restarting…")
                proc = start()
                last = mtimes()
                continue

            current = mtimes()
            changed_file = next(
                (p for p, m in current.items() if last.get(p) != m),
                next((p for p in last if p not in current), None),
            )
            last = current
            if changed_file:
                name = Path(changed_file).name
                print(f"\n  [dev] {name} changed — restarting…")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                time.sleep(0.3)
                proc = start()
    except KeyboardInterrupt:
        print("\n  [dev] Shutting down…")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> None:
    if "--dev" in sys.argv:
        forward = [a for a in sys.argv[1:] if a != "--dev"]
        _dev_watcher(forward)
        return
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
