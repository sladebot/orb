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



LOG_FILE = os.path.join(os.path.expanduser("~"), ".orb", "run.log")
_LEVEL_COLORS = {
    "DEBUG":    "\033[2m",       # dim
    "INFO":     "\033[36m",      # cyan
    "WARNING":  "\033[33m",      # yellow
    "ERROR":    "\033[31m",      # red
    "CRITICAL": "\033[1;31m",    # bold red
}
_RESET = "\033[0m"


def _setup_log_file(fmt: str) -> None:
    from logging.handlers import RotatingFileHandler
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)


def _cmd_logs(args: argparse.Namespace) -> None:
    import collections

    if args.clear:
        open(LOG_FILE, "w").close()
        print(f"Cleared {LOG_FILE}")
        return

    if not os.path.exists(LOG_FILE):
        print(f"No log file yet. Run orb first. ({LOG_FILE})")
        return

    min_level = getattr(logging, args.level, logging.DEBUG)
    follow = args.follow and not args.no_follow

    def _matches(line: str) -> bool:
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            if f" {lvl} " in line or f" {lvl}\t" in line:
                return getattr(logging, lvl, 0) >= min_level
        return min_level <= logging.DEBUG  # unknown format — show if DEBUG

    def _colorize(line: str) -> str:
        for lvl, color in _LEVEL_COLORS.items():
            if f" {lvl} " in line or f" {lvl}\t" in line:
                return f"{color}{line}{_RESET}"
        return line

    # Print last N lines first
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        tail = collections.deque(f, maxlen=args.lines)
    for line in tail:
        line = line.rstrip()
        if _matches(line):
            print(_colorize(line))

    if not follow:
        return

    # Follow mode — stream new lines
    print(f"\033[2m--- following {LOG_FILE} (Ctrl+C to stop) ---{_RESET}")
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # seek to end
            while True:
                line = f.readline()
                if line:
                    line = line.rstrip()
                    if _matches(line):
                        print(_colorize(line), flush=True)
                else:
                    import time
                    time.sleep(0.1)
    except KeyboardInterrupt:
        pass


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
    anthropic_auth = auth_sub.add_parser("anthropic", help="Guide Claude setup-token auth or store an Anthropic API key")
    anthropic_auth.add_argument("--api-key", metavar="SK", help="Anthropic API key (sk-ant-api...)")
    anthropic_auth.add_argument("--oauth-token", metavar="SK", help="Claude subscription OAuth token (sk-ant-oat...)")
    auth_sub.add_parser("status", help="Show current auth status")
    auth_sub.add_parser("logout", help="Revoke all stored credentials")

    # orb logs
    logs_parser = subparsers.add_parser("logs", help="Stream logs from a running orb process")
    logs_parser.add_argument("-f", "--follow", action="store_true", default=True, help="Follow log output (default: on)")
    logs_parser.add_argument("--no-follow", action="store_true", help="Print existing logs and exit")
    logs_parser.add_argument("-n", "--lines", type=int, default=50, help="Number of past lines to show (default: 50)")
    logs_parser.add_argument("--level", choices=["DEBUG","INFO","WARNING","ERROR"], default="DEBUG", help="Minimum log level to show")
    logs_parser.add_argument("--clear", action="store_true", help="Clear the log file")

    # orb config [get|set|show]
    cfg_parser = subparsers.add_parser("config", help="View or change persistent settings")
    cfg_sub = cfg_parser.add_subparsers(dest="config_action")
    cfg_sub.add_parser("show", help="Print all settings")
    cfg_get = cfg_sub.add_parser("get", help="Get a single setting")
    cfg_get.add_argument("key", help="Setting name (e.g. local-models)")
    cfg_set = cfg_sub.add_parser("set", help="Change a setting")
    cfg_set.add_argument("key", help="Setting name (e.g. local-models)")
    cfg_set.add_argument("value", help="New value (e.g. false)")

    subparsers.add_parser("onboard", help="Interactive onboarding for auth and common settings")

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
    parser.add_argument("--topology", choices=["auto", "triangle", "dual-review"], default="auto", help="Agent topology to use")
    parser.add_argument("--tui", action="store_true", help="Launch interactive terminal TUI")
    parser.add_argument("--logs", action="store_true", help="Show live log panel in TUI (requires --tui)")
    parser.add_argument("--exit-after-run", action="store_true", help="Exit automatically after a non-interactive run completes")
    parser.add_argument("--verbose", "-v", action="store_true", default=True, help="Enable verbose logging (default: on)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose logging")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()

    # ── auth subcommand ───────────────────────────────────────────────────────
    if args.subcommand == "auth":
        from .auth import auth_openai, auth_anthropic, auth_status, revoke_openai_token, revoke_anthropic_key, CREDS_PATH
        provider = args.auth_provider
        if provider == "openai":
            api_key = getattr(args, "api_key", None)
            if api_key:
                from .auth import _save_credentials
                _save_credentials("openai", {"api_key": api_key})
                print(f"OpenAI key stored at {CREDS_PATH}")
            else:
                await auth_openai()
        elif provider == "anthropic":
            credential = getattr(args, "oauth_token", None) or getattr(args, "api_key", None)
            await auth_anthropic(credential)
        elif provider == "status" or provider is None:
            await auth_status()
        elif provider == "logout":
            revoke_openai_token()
            revoke_anthropic_key()
            print("All stored credentials revoked.")
        else:
            print(f"Unknown auth provider: {provider}")
        return

    # ── config subcommand ─────────────────────────────────────────────────────
    if args.subcommand == "config":
        from .config import get, set_value, show_config
        action = getattr(args, "config_action", None) or "show"
        if action == "show" or action is None:
            show_config()
        elif action == "get":
            key = args.key.replace("-", "_")
            print(get(key))
        elif action == "set":
            key = args.key.replace("-", "_")
            try:
                set_value(key, args.value)
                print(f"  {key} = {args.value}")
            except (KeyError, ValueError) as exc:
                print_error(str(exc))
                sys.exit(1)
        return

    if args.subcommand == "onboard":
        from .onboard import run_onboarding
        await run_onboarding()
        return

    # ── logs subcommand ───────────────────────────────────────────────────────
    if args.subcommand == "logs":
        _cmd_logs(args)
        return

    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    if args.verbose and not args.quiet:
        logging.basicConfig(level=logging.DEBUG, format=fmt)
        for noisy in ("httpx", "httpcore", "anthropic", "openai", "asyncio", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING)

    # Always write to ~/.orb/run.log so 'orb logs' can stream it
    _setup_log_file(fmt)

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

    # --tui: TUI is always a frontend; backend always runs (port exposed only with --dashboard)
    if args.tui:
        from .tui import run_tui_with_dashboard, run_tui
        if args.dashboard:
            await run_tui_with_dashboard(
                providers=providers,
                config=config,
                model_overrides=model_overrides or None,
                tier_override=tier_override,
                topology=args.topology,
                budget=args.budget,
                dashboard_port=args.dashboard_port,
                show_logs=args.logs,
                initial_query=args.query,
                exit_after_run=args.exit_after_run,
            )
        else:
            # No public dashboard: backend on loopback-only port 18080
            run_tui(
                providers=providers,
                config=config,
                model_overrides=model_overrides or None,
                tier_override=tier_override,
                topology=args.topology,
                budget=args.budget,
                show_logs=args.logs,
                initial_query=args.query,
                exit_after_run=args.exit_after_run,
            )
        return

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
        live_display = None
        dashboard_server = None
        orchestrator = None

        if not args.dashboard and trace:
            if args.topology == "dual-review":
                from ..topologies.dual_review import create_dual_review
                orchestrator = create_dual_review(
                    providers=providers,
                    config=config,
                    model_overrides=model_overrides or None,
                    trace=False,
                    tier_override=tier_override,
                )
            else:
                orchestrator = create_triad(
                    providers=providers,
                    config=config,
                    model_overrides=model_overrides or None,
                    trace=False,
                    tier_override=tier_override,
                )
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

            print(f"  Dashboard running at http://localhost:{args.dashboard_port}")
            status, payload = await dashboard_server.runtime.start_run(
                args.query,
                args.topology,
            )
            if not payload.get("ok"):
                print_error(payload.get("error", "Failed to start run"))
                await dashboard_server.stop()
                sys.exit(1)
            await dashboard_server.runtime.wait_for_run()
            result = dashboard_server.runtime.last_result
        else:
            result = await orchestrator.run(args.query)

        if live_display:
            live_display.stop()

        if dashboard_server:
            # Keep dashboard open for a bit so user can inspect
            if not args.exit_after_run:
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
