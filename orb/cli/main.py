from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv
load_dotenv()

from ..llm.registry import build_providers
from ..llm.types import ModelTier, ModelConfig, DEFAULT_MODELS
from ..orchestrator.types import OrchestratorConfig
from ..topologies import create_orchestrator, get_loader
from .display import print_header, print_result, print_error
from . import paths as orb_paths
from .repl import run_repl



LOG_FILE = os.path.join(os.path.expanduser("~"), ".orb", "run.log")
# Kept for legacy references / tests that compare ``.endswith(".orb/daemon.json")``;
# the authoritative path is ``orb_paths.daemon_state_file()``.
DAEMON_STATE_FILE = str(orb_paths.daemon_state_file())
DEFAULT_DAEMON_PORT = 1337
DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_CONNECT_URL = f"http://127.0.0.1:{DEFAULT_DAEMON_PORT}"
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


def _managed_daemon_workdir() -> Path | None:
    state = _load_daemon_state() or {}
    workdir = str(state.get("workdir") or "").strip()
    if not workdir:
        return None
    path = Path(workdir).expanduser()
    return path if path.exists() else None


def _trace_dir(workspace_root: Path | None = None) -> Path:
    """Trace directory for a session state root.

    ``workspace_root`` is now a per-session dir under
    ``~/.orb/daemon/sessions/{sid}/``; traces live directly at
    ``{root}/traces/``.
    """
    return (workspace_root or orb_paths.daemon_home()) / "traces"


def _trace_index_dir(workspace_root: Path | None = None) -> Path:
    return _trace_dir(workspace_root) / "by-session"


def _trace_roots() -> list[Path]:
    """Every session's state dir — the CLI scans these to find trace files."""
    sessions_dir = orb_paths.daemon_sessions_dir()
    if not sessions_dir.exists():
        return []
    return sorted(
        (p for p in sessions_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _current_session_id() -> str:
    """Return the most-recently-active session id, or "".

    Previously read from a ``<workdir>/.orb/current_session`` pointer;
    now the daemon's registry is the single source of truth.
    """
    registry_path = orb_paths.daemon_registry_file()
    if not registry_path.exists():
        return ""
    try:
        registry = json.loads(registry_path.read_text())
    except Exception:
        return ""
    if not isinstance(registry, dict) or not registry:
        return ""
    # Registry insertion order ~= session-creation order; the last key is
    # the most recent. This is sufficient for `orb trace --current-session`.
    return next(reversed(registry), "")


def _latest_trace_path(session_id: str | None = None) -> Path | None:
    candidates: list[Path] = []
    for root in _trace_roots():
        trace_dir = _trace_dir(root)
        if session_id:
            index_path = _trace_index_dir(root) / f"{session_id}.json"
            if index_path.exists():
                try:
                    payload = json.loads(index_path.read_text())
                except Exception:
                    payload = {}
                for item in payload.get("runs") or []:
                    if isinstance(item, dict) and item.get("run_id"):
                        candidate = trace_dir / f"{item['run_id']}.json"
                        if candidate.is_file():
                            candidates.append(candidate)
        elif trace_dir.exists():
            candidates.extend(path for path in trace_dir.glob("*.json") if path.is_file())
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _list_session_runs(session_id: str) -> list[dict]:
    for root in _trace_roots():
        index_path = _trace_index_dir(root) / f"{session_id}.json"
        if not index_path.exists():
            continue
        try:
            payload = json.loads(index_path.read_text())
        except Exception:
            return []
        runs = payload.get("runs") or []
        return [item for item in runs if isinstance(item, dict)]
    return []


def _resolve_trace_session_arg(args: argparse.Namespace) -> str:
    if getattr(args, "current_session", False):
        return _current_session_id()
    return (getattr(args, "session", None) or "").strip()


def _format_trace_event_line(event: dict) -> str:
    kind = str(event.get("kind") or "unknown")
    actor = str(event.get("actor") or "")
    target = str(event.get("target") or "")
    stage = str(event.get("stage") or "")
    status = str(event.get("status") or "")
    message = str(event.get("message") or "").replace("\n", " ").strip()
    parts = [kind]
    if actor:
        parts.append(f"actor={actor}")
    if target:
        parts.append(f"target={target}")
    if stage:
        parts.append(f"stage={stage}")
    if status:
        parts.append(f"status={status}")
    line = " | ".join(parts)
    if message:
        line += f" | {message[:160]}"
    return line


def _cmd_trace(args: argparse.Namespace) -> None:
    from orb.tracing import RunTrace

    action = getattr(args, "trace_action", None) or "latest"
    session_id = _resolve_trace_session_arg(args)

    if action == "list":
        if not session_id:
            print("No session selected. Use --session <session_id> or --current-session.")
            return
        runs = _list_session_runs(session_id)
        if not runs:
            print(f"No traces recorded for session {session_id}.")
            return
        print(f"Session {session_id}")
        for run in runs:
            outcome = "unknown"
            if run.get("success") is True:
                outcome = "success"
            elif run.get("success") is False:
                outcome = "failure"
            print(
                f"{run.get('run_id', '')}  "
                f"{run.get('topology_id', 'unknown')}  "
                f"{outcome}  "
                f"events={run.get('event_count', 0)}"
            )
        return

    if action == "show":
        run_id = (getattr(args, "run_id", None) or "").strip()
        if not run_id:
            print_error("Missing run id")
            sys.exit(1)
        path = next(
            (_trace_dir(root) / f"{run_id}.json" for root in _trace_roots() if (_trace_dir(root) / f"{run_id}.json").exists()),
            None,
        )
        if path is None or not path.exists():
            print_error(f"Trace not found: {run_id}")
            sys.exit(1)
        trace = RunTrace.load(path)
        if getattr(args, "path", False):
            print(path)
            return
        if getattr(args, "json", False):
            print(trace.to_json())
            return
        print(f"Trace: {path}")
        print(trace.summary_text())
        return

    if action == "tail":
        if not session_id:
            print("No session selected. Use --session <session_id> or --current-session.")
            return
        print(f"Following traces for session {session_id}. Press Ctrl-C to stop.")
        last_run_id = ""
        seen_count = 0
        try:
            while True:
                path = _latest_trace_path(session_id=session_id)
                if path is not None:
                    trace = RunTrace.load(path)
                    if trace.run_id != last_run_id:
                        last_run_id = trace.run_id
                        seen_count = 0
                        print(f"\nRun {trace.run_id} | topology={trace.topology_choice() or 'unknown'}")
                    events = trace.to_dict().get("events") or []
                    if len(events) > seen_count:
                        for event in events[seen_count:]:
                            if isinstance(event, dict):
                                print(_format_trace_event_line(event))
                        seen_count = len(events)
                time.sleep(getattr(args, "interval", 0.5) or 0.5)
        except KeyboardInterrupt:
            return

    if action != "latest":
        print_error("Unknown trace command")
        sys.exit(1)

    path = _latest_trace_path(session_id=session_id or None)
    if path is None:
        print("No trace files found. Run Orb first. (.orb/traces)")
        return

    trace = RunTrace.load(path)
    if getattr(args, "path", False):
        print(path)
        return
    if getattr(args, "json", False):
        print(trace.to_json())
        return

    summary = trace.summary()
    print(f"Latest trace: {path}")
    print(trace.summary_text())
    if summary.get("agent_ids"):
        print(f"Agents: {', '.join(summary['agent_ids'])}")
    if summary.get("counts_by_kind"):
        counts = ", ".join(f"{k}={v}" for k, v in sorted(summary["counts_by_kind"].items()))
        print(f"Events: {counts}")
    if summary.get("stage_latencies"):
        latencies = ", ".join(
            f"{stage}={duration:.2f}s"
            for stage, duration in sorted(summary["stage_latencies"].items())
        )
        print(f"Stage Latencies: {latencies}")


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

    models_parser = subparsers.add_parser("models", help="Inspect or refresh provider model catalogs")
    models_sub = models_parser.add_subparsers(dest="models_action")
    models_sub.add_parser("refresh", help="Fetch latest provider model catalogs into ~/.orb/config.json")

    subparsers.add_parser(
        "onboard",
        help="Interactive provider + model setup (auth, catalog refresh, per-tier defaults)",
    )
    trace_parser = subparsers.add_parser("trace", help="Inspect persisted run traces")
    trace_sub = trace_parser.add_subparsers(dest="trace_action")
    trace_latest = trace_sub.add_parser("latest", help="Show the latest trace")
    trace_latest.add_argument("--json", action="store_true", help="Print the full trace JSON")
    trace_latest.add_argument("--path", action="store_true", help="Print only the trace file path")
    trace_latest.add_argument("--session", type=str, help="Restrict to a specific session id")
    trace_latest.add_argument("--current-session", action="store_true", help="Restrict to the current workspace session")
    trace_list = trace_sub.add_parser("list", help="List runs for a session")
    trace_list.add_argument("--session", type=str, help="Session id to inspect")
    trace_list.add_argument("--current-session", action="store_true", help="Use the current workspace session")
    trace_tail = trace_sub.add_parser("tail", help="Tail trace events for a session")
    trace_tail.add_argument("--session", type=str, help="Session id to inspect")
    trace_tail.add_argument("--current-session", action="store_true", help="Use the current workspace session")
    trace_tail.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds")
    trace_show = trace_sub.add_parser("show", help="Show a specific run trace")
    trace_show.add_argument("run_id", help="Run id")
    trace_show.add_argument("--json", action="store_true", help="Print the full trace JSON")
    trace_show.add_argument("--path", action="store_true", help="Print only the trace file path")
    topologies_parser = subparsers.add_parser("topologies", help="Manage user topology definitions")
    topologies_sub = topologies_parser.add_subparsers(dest="topologies_action")
    topologies_init = topologies_sub.add_parser("init", help="Create ~/.orb/topologies.yaml from the bundled sample")
    topologies_init.add_argument("--force", action="store_true", help="Overwrite ~/.orb/topologies.yaml if it already exists")

    # ── memory subcommand ─────────────────────────────────────────────────────
    memory_parser = subparsers.add_parser("memory", help="Manage the persistent memory vault")
    memory_sub = memory_parser.add_subparsers(dest="memory_action")
    init_p = memory_sub.add_parser("init", help="Initialize the memory vault structure")
    init_p.add_argument(
        "--vault-path",
        type=str,
        default="~/.orb/vault",
        help="Path to the vault (default: ~/.orb/vault)",
    )
    memory_sub.add_parser("status", help="Report vault health (page count, tags, last write)").add_argument(
        "--vault-path", type=str, default="~/.orb/vault", help="Path to the vault (default: ~/.orb/vault)",
    )
    prune_p = memory_sub.add_parser("prune", help="Archive page(s) to memories/").add_argument(
        "--vault-path", type=str, default="~/.orb/vault", help="Path to the vault (default: ~/.orb/vault)",
    )
    prune_p.add_argument("page_title", help="Page title to archive (matches wiki/*.md stem)")

    sessions_parser = subparsers.add_parser("sessions", help="Manage Orb sessions (list, show, remove, prune)")
    sessions_parser.add_argument("--connect", type=str, default=None, help=f"Orb daemon URL (default: {DEFAULT_CONNECT_URL})")
    sessions_parser.add_argument("--port", type=int, default=None, help="Daemon port shorthand for localhost connects")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_action")
    sessions_sub.add_parser("list", help="List sessions the daemon knows about (active + persisted on disk)")
    sessions_show = sessions_sub.add_parser("show", help="Print a session's summary + run state")
    sessions_show.add_argument("session_id", help="Session id to inspect")
    sessions_rm = sessions_sub.add_parser("rm", help="Delete one or more sessions by id")
    sessions_rm.add_argument("session_ids", nargs="+", help="Session ids to remove")
    sessions_rm.add_argument("--keep-disk", action="store_true", help="Remove from daemon registry but keep the on-disk transcript")
    sessions_prune = sessions_sub.add_parser("prune", help="Remove terminal (idle/completed/errored) sessions; add --all to include in-flight")
    sessions_prune.add_argument("--all", action="store_true", help="Also delete in-flight sessions (cancels any running task)")
    sessions_prune.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    sessions_prune.add_argument("--keep-disk", action="store_true", help="Keep on-disk transcripts")

    tui_parser = subparsers.add_parser("tui", help="Attach the terminal UI to a running Orb daemon")
    tui_parser.add_argument("query", nargs="?", help="Optional task query to start on the connected daemon")
    tui_parser.add_argument("--connect", type=str, default=None, help=f"Orb daemon URL (default: {DEFAULT_CONNECT_URL})")
    tui_parser.add_argument("--port", type=int, default=None, help="Orb daemon port shorthand for localhost connects")
    tui_parser.add_argument("--budget", type=int, default=200, help="Requested budget when starting a new run")
    tui_parser.add_argument("--logs", action="store_true", help="Show live log panel in TUI")
    tui_parser.add_argument("--exit-after-run", action="store_true", help="Exit automatically after a non-interactive run completes")
    tui_parser.add_argument("--workdir", type=str, default=None, help="Scope the session to this folder (default: current working directory)")
    tui_parser.add_argument("--no-prompt", action="store_true", help="Skip the startup topology prompt; default to 'triad' (non-interactive CI default)")
    tui_parser.add_argument(
        "--review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require y/n approval before each agent file write (default: on). Use --no-review to let agents write directly.",
    )
    dashboard_parser = subparsers.add_parser("dashboard", help="Open the dashboard for a running Orb daemon")
    dashboard_parser.add_argument("query", nargs="?", help="Optional task query to start on the connected daemon")
    dashboard_parser.add_argument("--connect", type=str, default=None, help=f"Orb daemon URL (default: {DEFAULT_CONNECT_URL})")
    dashboard_parser.add_argument("--workdir", type=str, default=None, help="Scope the dashboard session to this folder")
    dashboard_parser.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="role=model_id",
        help="Manual per-node model pin (repeatable). Passes through to the daemon as agent_models in the run body.",
    )
    dashboard_parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    daemon_common = argparse.ArgumentParser(add_help=False)
    daemon_common.add_argument("--host", default=DEFAULT_DAEMON_HOST, help="Daemon bind host (default: 127.0.0.1; pass --host 0.0.0.0 to listen on all interfaces)")
    daemon_common.add_argument("--port", type=int, default=None, help="Daemon bind port")
    daemon_common.add_argument("--workdir", type=str, help="Daemon workspace directory")
    daemon_common.add_argument(
        "--local-only",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Use only local models in the daemon",
    )
    daemon_common.add_argument(
        "--cloud-only",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Use only cloud models in the daemon",
    )
    daemon_parser = subparsers.add_parser("daemon", help="Run Orb backend daemon with API, WebSocket, and dashboard")
    daemon_parser.add_argument("--host", default=DEFAULT_DAEMON_HOST, help="Daemon bind host (default: 127.0.0.1; pass --host 0.0.0.0 to listen on all interfaces)")
    daemon_parser.add_argument("--port", type=int, default=DEFAULT_DAEMON_PORT, help=f"Daemon bind port (default: {DEFAULT_DAEMON_PORT})")
    daemon_parser.add_argument("--workdir", type=str, help="Daemon workspace directory (default: create a fresh /tmp/orb-daemon-* dir each start)")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_action")
    daemon_sub.add_parser("run", parents=[daemon_common], help="Run the daemon in the foreground")
    daemon_sub.add_parser("start", parents=[daemon_common], help="Start the daemon in the background")
    daemon_sub.add_parser("restart", parents=[daemon_common], help="Restart the managed daemon")
    daemon_sub.add_parser("stop", parents=[daemon_common], help="Stop the managed daemon")
    daemon_sub.add_parser("status", parents=[daemon_common], help="Show managed daemon status")

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
    parser.add_argument("--connect", type=str, help="Connect TUI or dashboard client to an existing Orb daemon URL")
    parser.add_argument("--dev", action="store_true", help="Dev mode: auto-restart on file changes")
    parser.add_argument("--verbose", "-v", action="store_true", default=True, help="Enable verbose logging (default: on)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose logging")
    return parser.parse_args()


def _normalize_connect_url(url: str | None, default: str = DEFAULT_CONNECT_URL) -> str:
    base = (url or default).rstrip("/")
    if "://" not in base:
        base = f"http://{base}"
    return base


async def _cmd_sessions(args: argparse.Namespace) -> None:
    """Handle `orb sessions list/show/rm/prune`.

    Sessions live in two places:
      - the daemon's in-memory `RuntimeManager` registry
      - JSON transcripts on disk under `~/.orb/sessions/*.json`

    The CLI talks to the daemon's v1 API for live state and also
    scrubs the on-disk files when the user asks to remove or prune.
    That way cleaning up doesn't leave dangling transcripts.
    """
    import aiohttp

    action = getattr(args, "sessions_action", None)
    base = _resolve_connect_url(getattr(args, "connect", None), getattr(args, "port", None))

    async def _api(session: aiohttp.ClientSession, method: str, path: str, **kwargs) -> tuple[int, dict]:
        try:
            async with session.request(method, f"{base}{path}", **kwargs) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = {"ok": False, "error": f"HTTP {resp.status}"}
                return resp.status, body
        except aiohttp.ClientError as exc:
            return 0, {"ok": False, "error": f"Cannot reach daemon at {base}: {exc}"}

    def _sessions_dir() -> Path:
        return Path.home() / ".orb" / "sessions"

    def _disk_session_ids() -> list[str]:
        d = _sessions_dir()
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def _rm_disk(session_id: str) -> bool:
        p = _sessions_dir() / f"{session_id}.json"
        try:
            p.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def _short(sid: str) -> str:
        return sid[:8] if len(sid) > 8 else sid

    if action == "list":
        async with aiohttp.ClientSession() as http:
            status, body = await _api(http, "GET", "/api/v1/sessions")
        if status == 0:
            print_error(body.get("error", "Failed to reach daemon"))
            sys.exit(1)
        live = {s["session_id"]: s for s in (body.get("data") or {}).get("sessions", [])}
        disk_ids = set(_disk_session_ids())
        all_ids = sorted(set(live.keys()) | disk_ids, key=lambda sid: (sid not in live, sid))

        if not all_ids:
            print("  No sessions found.")
            return
        header = f"  {'SESSION':<10}  {'STATE':<10}  {'WORKDIR':<40}  {'TOPOLOGY':<12}  NOTES"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for sid in all_ids:
            info = live.get(sid)
            if info:
                state = info.get("run_state") or "?"
                workdir = info.get("workdir") or "<daemon-cwd>"
                topology = info.get("locked_topology") or "auto"
                note = ""
            else:
                state = "persisted"
                workdir = "—"
                topology = "—"
                note = "on-disk only (daemon doesn't have it loaded)"
            print(f"  {_short(sid):<10}  {state:<10}  {workdir[:40]:<40}  {topology:<12}  {note}")
        print()
        print(f"  Total: {len(all_ids)}  ({len(live)} active, {len(disk_ids - set(live))} persisted-only)")
        return

    if action == "show":
        sid = args.session_id
        # Resolve short prefixes against the live registry + disk files.
        async with aiohttp.ClientSession() as http:
            _, list_body = await _api(http, "GET", "/api/v1/sessions")
        live_ids = [s["session_id"] for s in (list_body.get("data") or {}).get("sessions", [])]
        all_ids = sorted(set(live_ids) | set(_disk_session_ids()))
        matches = [i for i in all_ids if i == sid or i.startswith(sid)]
        if len(matches) == 1:
            sid = matches[0]
        elif len(matches) > 1:
            print_error(f"Ambiguous session prefix {sid!r} matches {len(matches)} sessions:")
            for m in matches[:10]:
                print(f"    {m}")
            sys.exit(1)
        async with aiohttp.ClientSession() as http:
            status, body = await _api(http, "GET", f"/api/v1/sessions/{sid}")
        if status == 404:
            # Try falling back to on-disk transcript
            disk_path = _sessions_dir() / f"{sid}.json"
            if disk_path.exists():
                import json as _json
                data = _json.loads(disk_path.read_text())
                print(f"  Session:    {sid}")
                print(f"   (on-disk only, not loaded by the daemon)")
                print(f"  Workdir:    {data.get('workdir') or '—'}")
                print(f"  Topology:   {data.get('locked_topology') or '(not set)'}")
                print(f"  Generation: {data.get('generation') or 1}")
                print(f"  Turns:      {len(data.get('turns') or [])}")
                return
            print_error(f"No session with id {sid}")
            sys.exit(1)
        if status == 0:
            print_error(body.get("error", "Failed to reach daemon"))
            sys.exit(1)
        data = (body.get("data") or {})
        print(f"  Session:    {data.get('session_id')}")
        print(f"  State:      {data.get('run_state')}")
        print(f"  Workdir:    {data.get('workdir') or '<daemon-cwd>'}")
        print(f"  Topology:   {data.get('locked_topology') or '(not set)'}")
        print(f"  Turn:       {data.get('turn')}")
        print(f"  Generation: {data.get('generation')}")
        return

    if action == "rm":
        removed = 0
        async with aiohttp.ClientSession() as http:
            for sid in args.session_ids:
                status, body = await _api(http, "DELETE", f"/api/v1/sessions/{sid}")
                if status == 200:
                    print(f"  ✓ {_short(sid)}  removed from daemon registry")
                    removed += 1
                elif status == 404:
                    print(f"  ·  {_short(sid)}  not in daemon registry")
                else:
                    print_error(f"  ✗ {_short(sid)}  {body.get('error', 'unknown error')}")
                    continue
                if not args.keep_disk and _rm_disk(sid):
                    print(f"     and deleted ~/.orb/sessions/{sid}.json")
        print(f"\n  Done — {removed} session(s) removed from daemon.")
        return

    if action == "prune":
        # Gather live sessions. Filter to terminal-state unless --all.
        async with aiohttp.ClientSession() as http:
            status, body = await _api(http, "GET", "/api/v1/sessions")
            if status == 0:
                print_error(body.get("error", "Failed to reach daemon"))
                sys.exit(1)
            live_sessions = (body.get("data") or {}).get("sessions", [])

        terminal_states = {"idle", "completed", "errored"}
        targets = [
            s for s in live_sessions
            if args.all or (s.get("run_state") in terminal_states)
        ]
        disk_extras = [
            sid for sid in _disk_session_ids()
            if sid not in {s["session_id"] for s in live_sessions}
        ]

        if not targets and not disk_extras:
            print("  Nothing to prune — no terminal sessions in the registry or stale files on disk.")
            return

        print(f"  Would remove {len(targets)} session(s) from the daemon:")
        for s in targets:
            sid = s["session_id"]
            print(f"    {_short(sid)}  state={s.get('run_state')}  workdir={s.get('workdir') or '<cwd>'}")
        if disk_extras:
            print(f"\n  And {len(disk_extras)} orphaned transcript(s) on disk:")
            for sid in disk_extras[:10]:
                print(f"    ~/.orb/sessions/{sid}.json")
            if len(disk_extras) > 10:
                print(f"    … and {len(disk_extras) - 10} more")

        if not args.yes:
            try:
                confirm = input("\n  Proceed? [y/N]: ").strip().lower()
            except EOFError:
                confirm = "n"
            if confirm != "y":
                print("  Aborted.")
                return

        async with aiohttp.ClientSession() as http:
            for s in targets:
                sid = s["session_id"]
                await _api(http, "DELETE", f"/api/v1/sessions/{sid}")
                if not args.keep_disk:
                    _rm_disk(sid)
        if not args.keep_disk:
            for sid in disk_extras:
                _rm_disk(sid)
        print(f"\n  Pruned {len(targets)} active + {len(disk_extras) if not args.keep_disk else 0} on-disk session(s).")
        return

    print_error("Specify a sessions action: list | show <id> | rm <id...> | prune")
    sys.exit(1)


def _resolve_connect_url(url: str | None, port: int | None = None) -> str:
    if url:
        return _normalize_connect_url(url)
    if port is not None:
        return _normalize_connect_url(f"http://127.0.0.1:{port}")
    return _normalize_connect_url(None)


_DEFAULT_TOPOLOGY_PICK = "triad"


def _prompt_topology_choice(topology_choices: list[str]) -> str:
    """Interactive prompt used by `orb tui` on startup.

    Returns the selected topology id. Deliberately omits ``auto`` —
    picking auto would invoke the LLM classifier on every submit, adding
    a 3–15s blank window per run. Forcing a concrete choice routes
    through the no-LLM ``_manual_prediction`` fast path in the runtime.
    If you genuinely want classifier-driven routing, POST directly to
    ``/api/v1/sessions`` with ``topology: "auto"``.

    EOF / empty input defaults to ``triad`` (a balanced multi-agent
    starting point). The default can be changed by setting
    ``_DEFAULT_TOPOLOGY_PICK`` — any value in ``topology_choices`` works.
    """
    print()
    print("  How should Orb route turns in this session?")
    print()
    ordered: list[str] = []
    seen: set[str] = set()
    for tid in topology_choices:
        if tid == "auto" or tid in seen:
            continue
        seen.add(tid)
        ordered.append(tid)
    if not ordered:
        # Defensive: should never happen with the default loader set.
        return _DEFAULT_TOPOLOGY_PICK
    default = _DEFAULT_TOPOLOGY_PICK if _DEFAULT_TOPOLOGY_PICK in seen else ordered[0]
    for i, tid in enumerate(ordered, 1):
        marker = "  (default)" if tid == default else ""
        print(f"    {i}. {tid}{marker}")
    print()
    while True:
        try:
            raw = input(f"  Pick [1-{len(ordered)}] or press Enter for {default}: ").strip()
        except EOFError:
            return default
        if not raw:
            return default
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(ordered):
                return ordered[idx - 1]
        if raw in ordered:
            return raw
        print(f"  Please enter a number 1-{len(ordered)}, a topology id, or blank for {default}.")


def _init_topologies_file(force: bool = False) -> Path:
    from orb.topologies.loader import USER_TOPOLOGIES_PATH

    target = USER_TOPOLOGIES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists. Use --force to overwrite it.")
    sample = resources.files("orb").joinpath("sample-topology.yaml").read_text()
    target.write_text(sample)
    return target


def _resolve_daemon_workdir(workdir: str | None) -> Path:
    """The daemon always anchors at ``~/.orb/daemon/``.

    The ``workdir`` argument is kept for CLI back-compat but is ignored —
    a fixed anchor means session indexes (registry.json), session
    snapshots, and traces live in one predictable place across restarts
    and across daemon-spawning commands.
    """
    # ``workdir`` is accepted for CLI back-compat but deliberately unused.
    _ = workdir
    return orb_paths.ensure_daemon_home()


def _daemon_state_path() -> Path:
    return orb_paths.daemon_state_file()


def _load_daemon_state() -> dict | None:
    path = _daemon_state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _save_daemon_state(*, pid: int, host: str, port: int, workdir: Path) -> None:
    path = _daemon_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": pid,
        "host": host,
        "port": port,
        "workdir": str(workdir),
        "started_at": time.time(),
    }))


def _clear_daemon_state(expected_pid: int | None = None) -> None:
    path = _daemon_state_path()
    if not path.exists():
        return
    if expected_pid is not None:
        state = _load_daemon_state()
        if state and int(state.get("pid", -1)) != expected_pid:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _daemon_command(
    host: str,
    port: int,
    workdir: Path,
    *,
    local_only: bool = False,
    cloud_only: bool = False,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "orb.cli.main",
    ]
    if local_only:
        cmd.append("--local-only")
    if cloud_only:
        cmd.append("--cloud-only")
    cmd.extend([
        "daemon",
        "run",
        "--host",
        host,
        "--port",
        str(port),
        "--workdir",
        str(workdir),
    ])
    return cmd


def _port_bind_error(host: str, port: int) -> OSError | None:
    probe_host = "" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe_host, port))
        except OSError as exc:
            return exc
    return None


def _port_in_use(host: str, port: int) -> bool:
    return _port_bind_error(host, port) is not None


def _find_listening_pid(port: int) -> int | None:
    try:
        proc = subprocess.run(
            ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    output = (proc.stdout or "").strip().splitlines()
    if not output:
        return None
    try:
        return int(output[0].strip())
    except ValueError:
        return None


def _port_looks_like_orb_daemon(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with urlopen(f"http://{probe_host}:{port}/api/v1/health", timeout=0.75) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False
    return body.get("ok") is True and body.get("code") == "HEALTHY"


def _start_managed_daemon(
    host: str,
    port: int,
    workdir: str | None,
    *,
    local_only: bool = False,
    cloud_only: bool = False,
) -> dict:
    active = _load_daemon_state()
    if active and _pid_is_alive(int(active.get("pid", -1))):
        raise RuntimeError(f"Orb daemon already running (pid {active['pid']})")
    bind_error = _port_bind_error(host, port)
    if bind_error is not None:
        if getattr(bind_error, "errno", None) in {13, 48, 98}:
            if bind_error.errno == 13:
                raise RuntimeError(
                    f"Port {port} requires elevated privileges or additional permissions."
                ) from bind_error
        raise RuntimeError(
            f"Port {port} is already in use. Stop the existing daemon or choose a different --port."
        ) from bind_error

    resolved_workdir = _resolve_daemon_workdir(workdir)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_handle = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        _daemon_command(
            host,
            port,
            resolved_workdir,
            local_only=local_only,
            cloud_only=cloud_only,
        ),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
        close_fds=True,
        # Pin the daemon's process CWD to its fixed anchor so code paths
        # that hit ``Path.cwd()`` (sandbox fallback, test harness, etc.)
        # never leak the user's launch shell into runtime state.
        cwd=str(resolved_workdir),
    )
    log_handle.close()
    time.sleep(0.5)
    if proc.poll() is not None:
        raise RuntimeError(f"Orb daemon failed to start (exit code {proc.returncode}). Check {LOG_FILE}")
    _save_daemon_state(pid=proc.pid, host=host, port=port, workdir=resolved_workdir)
    return {
        "pid": proc.pid,
        "host": host,
        "port": port,
        "workdir": str(resolved_workdir),
    }


async def _stop_managed_daemon(port: int = DEFAULT_DAEMON_PORT) -> bool:
    state = _load_daemon_state()
    if not state:
        pid = _find_listening_pid(port)
        if pid is None:
            print("  Orb daemon is not running.")
            return False
        if not _port_looks_like_orb_daemon("127.0.0.1", port):
            raise RuntimeError(
                f"Port {port} is occupied by pid {pid}, but it does not look like an Orb daemon."
            )
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _pid_is_alive(pid):
                print(f"  Stopped Orb daemon on port {port} (pid {pid}).")
                return True
            await asyncio.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not _pid_is_alive(pid):
                print(f"  Force-stopped Orb daemon on port {port} (pid {pid}).")
                return True
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Failed to stop Orb daemon pid {pid}")

    pid = int(state.get("pid", -1))
    if pid <= 0 or not _pid_is_alive(pid):
        _clear_daemon_state()
        print("  Orb daemon is not running.")
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            _clear_daemon_state(expected_pid=pid)
            print(f"  Stopped Orb daemon (pid {pid}).")
            return True
        await asyncio.sleep(0.1)

    os.kill(pid, signal.SIGKILL)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            _clear_daemon_state(expected_pid=pid)
            print(f"  Force-stopped Orb daemon (pid {pid}).")
            return True
        await asyncio.sleep(0.1)

    raise RuntimeError(f"Failed to stop Orb daemon pid {pid}")


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

    if args.subcommand == "models":
        action = getattr(args, "models_action", None) or "refresh"
        if action == "refresh":
            from web.state import DashboardState
            from orb.runtime.graph_runtime import GraphRuntime
            from .config import load_config

            runtime = GraphRuntime(DashboardState())
            runtime.configure(
                providers=build_providers(local_only=False, cloud_only=False),
                config=OrchestratorConfig(timeout=args.timeout, budget=args.budget, max_depth=args.max_depth),
                model_overrides=None,
                tier_override=None,
            )
            status = await runtime.refresh_provider_catalogs()

            cfg = load_config()
            providers_cfg = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
            print("Provider catalogs (~/.orb/config.json):")
            reasons = {
                "skipped:not-registered": "not registered (disabled in config or liveness check failed)",
                "skipped:empty":          "fetch returned no models (see ~/.orb/run.log)",
            }
            for provider_name in ("anthropic", "openai-codex", "ollama", "vmlx", "omlx"):
                st = status.get(provider_name, "skipped:not-registered")
                if st.startswith("updated:") or st.startswith("unchanged:"):
                    verb, count = st.split(":", 1)
                    print(f"  {provider_name:<13} {verb:<9} · {count} models")
                    entry = providers_cfg.get(provider_name) if isinstance(providers_cfg, dict) else None
                    defaults = entry.get("default_models") if isinstance(entry, dict) else None
                    if isinstance(defaults, dict) and defaults:
                        summary = ", ".join(f"{k}={v}" for k, v in defaults.items())
                        print(f"                defaults: {summary}")
                else:
                    reason = reasons.get(st, st)
                    print(f"  {provider_name:<13} skipped   · {reason}")
            return
        print_error("Unknown models command")
        sys.exit(1)

    if args.subcommand == "onboard":
        from .onboard import run_onboarding
        await run_onboarding()
        return

    if args.subcommand == "trace":
        _cmd_trace(args)
        return

    if args.subcommand == "topologies":
        if getattr(args, "topologies_action", None) == "init":
            try:
                target = _init_topologies_file(force=getattr(args, "force", False))
            except FileExistsError as exc:
                print_error(str(exc))
                sys.exit(1)
            print(f"Initialized topology file at {target}")
            return
        print_error("Unknown topologies command")
        sys.exit(1)

    # ── memory subcommand ─────────────────────────────────────────────────────
    if args.subcommand == "memory":
        from orb.memory_tools.cli import _handle_memory_command
        _handle_memory_command(args)
        return

    if args.subcommand == "sessions":
        await _cmd_sessions(args)
        return

    if args.subcommand == "daemon":
        daemon_action = getattr(args, "daemon_action", None)
        if daemon_action == "status":
            state = _load_daemon_state()
            if not state or not _pid_is_alive(int(state.get("pid", -1))):
                _clear_daemon_state()
                port = args.port or DEFAULT_DAEMON_PORT
                pid = _find_listening_pid(port)
                if pid is None:
                    print("  Orb daemon is not running.")
                    return
                if _port_looks_like_orb_daemon("127.0.0.1", port):
                    print(f"  Orb daemon is running on port {port} (pid {pid})")
                    print("  State:     unmanaged")
                    return
                print(f"  Port {port} is occupied by pid {pid}, but it is not an Orb daemon.")
                return
            print(f"  Orb daemon is running (pid {state['pid']})")
            print(f"  URL:       http://{state['host']}:{state['port']}")
            print(f"  Workspace: {state['workdir']}")
            return
        if daemon_action == "stop":
            await _stop_managed_daemon(args.port or DEFAULT_DAEMON_PORT)
            return
        if daemon_action == "restart":
            await _stop_managed_daemon(args.port or DEFAULT_DAEMON_PORT)
            host = args.host or DEFAULT_DAEMON_HOST
            port = args.port or DEFAULT_DAEMON_PORT
            info = _start_managed_daemon(
                host,
                port,
                args.workdir,
                local_only=args.local_only,
                cloud_only=args.cloud_only,
            )
            print(f"  Restarted Orb daemon (pid {info['pid']})")
            print(f"  URL:       http://{info['host']}:{info['port']}")
            print(f"  Workspace: {info['workdir']}")
            return
        if daemon_action == "start":
            host = args.host or DEFAULT_DAEMON_HOST
            port = args.port or DEFAULT_DAEMON_PORT
            info = _start_managed_daemon(
                host,
                port,
                args.workdir,
                local_only=args.local_only,
                cloud_only=args.cloud_only,
            )
            print(f"  Started Orb daemon (pid {info['pid']})")
            print(f"  URL:       http://{info['host']}:{info['port']}")
            print(f"  Workspace: {info['workdir']}")
            return

        from web.server import DashboardServer
        from web.state import DashboardState

        daemon_host = args.host or DEFAULT_DAEMON_HOST
        daemon_port = args.port or DEFAULT_DAEMON_PORT
        daemon_workdir = _resolve_daemon_workdir(getattr(args, "workdir", None))
        # No process-level chdir — each Session owns its workdir and the
        # sandbox gets it explicitly through the orchestrator factory.
        _save_daemon_state(pid=os.getpid(), host=daemon_host, port=daemon_port, workdir=daemon_workdir)

        dash_state = DashboardState()
        dashboard_server = DashboardServer(dash_state, host=daemon_host, port=daemon_port)
        dashboard_server.set_providers(
            providers=build_providers(
                local_only=args.local_only,
                cloud_only=args.cloud_only,
            ),
            config=OrchestratorConfig(timeout=args.timeout, budget=args.budget, max_depth=args.max_depth),
            model_overrides=None,
            tier_override=None,
        )

        await dashboard_server.start()
        print_header()
        print(f"  Orb daemon listening at http://{daemon_host}:{daemon_port}")
        print(f"  Workspace:  {daemon_workdir}")
        print("  TUI attach: orb tui --connect http://127.0.0.1:{0}".format(daemon_port))
        print(f"  Dashboard:  http://{daemon_host}:{daemon_port}")
        print("  Press Ctrl-C to shut down.\n")

        stop_event = asyncio.Event()
        try:
            import signal
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError):
            pass

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            _clear_daemon_state(expected_pid=os.getpid())
            await dashboard_server.stop()
        return

    # ── logs subcommand ───────────────────────────────────────────────────────
    if args.subcommand == "logs":
        _cmd_logs(args)
        return

    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    file_only_logging = args.subcommand in {"daemon", "tui"}
    if file_only_logging:
        level = logging.DEBUG if args.verbose and not args.quiet else logging.WARNING
        logging.basicConfig(level=level, format=fmt, handlers=[], force=True)
    elif args.verbose and not args.quiet:
        logging.basicConfig(level=logging.DEBUG, format=fmt, force=True)
        for noisy in ("httpx", "httpcore", "anthropic", "openai", "asyncio", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING, force=True)

    # Always write to ~/.orb/run.log so 'orb logs' can stream it
    _setup_log_file(fmt)

    if args.subcommand == "tui":
        from .tui import attach_tui

        connect_url = _resolve_connect_url(args.connect, getattr(args, "port", None))

        # Default workdir to the shell's current directory so the session is
        # scoped to the repo the user invoked `orb tui` from.
        workdir = args.workdir or str(Path.cwd())
        workdir = str(Path(workdir).expanduser().resolve())

        # Ask the user how to route runs. The picker deliberately omits
        # ``"auto"`` — picking auto would invoke the LLM classifier on
        # every submit, adding a 3–15s blank window per run. Forcing a
        # concrete topology choice routes through the no-LLM
        # ``_manual_prediction`` fast path in the runtime.
        if not getattr(args, "no_prompt", False) and sys.stdin.isatty():
            from orb.topologies import get_loader
            topo_choices = get_loader().list_ids()
            topology = _prompt_topology_choice(topo_choices)
        else:
            # Non-interactive fallback (--no-prompt, piped stdin, CI).
            # ``triad`` is the balanced multi-agent default; callers who
            # want something else should run interactively or POST
            # directly to the daemon with their chosen topology.
            topology = "triad"

        # attach_tui handles session creation itself via POST /api/v1/sessions
        # so each `orb tui` invocation gets its own runtime (like the
        # dashboard's New Session modal). Print the intent here so the
        # user sees the scoping before the TUI mounts.
        review = bool(getattr(args, "review", True))
        print(f"  Session scoped to: {workdir}")
        print(f"  Topology: {topology}")
        print(f"  Review mode: {'on (y/n before each file write)' if review else 'off (agents write directly)'}")

        await attach_tui(
            connect_url=connect_url,
            topology=topology,
            budget=args.budget,
            show_logs=args.logs,
            initial_query=args.query,
            exit_after_run=args.exit_after_run,
            workdir=workdir,
            approval_required=review,
        )
        return

    if args.subcommand == "dashboard":
        import aiohttp
        import webbrowser

        base = _resolve_connect_url(args.connect)

        # Parse --agent-model pairs: role=model_id (repeatable)
        agent_model_pins: dict[str, str] = {}
        for pair in getattr(args, "agent_model", []) or []:
            if "=" not in pair:
                print_error(f"--agent-model expects role=model_id, got: {pair}")
                sys.exit(1)
            role, _, model_id = pair.partition("=")
            role = role.strip()
            model_id = model_id.strip()
            if not role or not model_id:
                print_error(f"--agent-model expects role=model_id, got: {pair}")
                sys.exit(1)
            agent_model_pins[role] = model_id
        # ``--topology`` was removed from the CLI — the dashboard
        # subcommand now always launches runs with a concrete topology.
        # See ``_DEFAULT_TOPOLOGY_PICK`` for the picked default.

        async with aiohttp.ClientSession() as session:
            session_id: str | None = None
            if getattr(args, "workdir", None) or args.query or agent_model_pins:
                create_body: dict = {}
                if args.query or agent_model_pins:
                    create_body["topology"] = _DEFAULT_TOPOLOGY_PICK
                if agent_model_pins:
                    create_body["agent_models"] = agent_model_pins
                # Scope the session to a workdir first, if requested.
                workdir = None
                if getattr(args, "workdir", None):
                    workdir = str(Path(args.workdir).expanduser().resolve())
                    create_body["workdir"] = workdir
                async with session.post(f"{base}/api/v1/sessions", json=create_body) as resp:
                    payload = await resp.json()
                    if not payload.get("ok"):
                        print_error(payload.get("error", "Failed to create dashboard session"))
                        sys.exit(1)
                    data = payload.get("data") or {}
                    session_id = str(data.get("session_id") or "") or None
                if workdir:
                    print(f"  Session scoped to: {workdir}")

            if args.query:
                if not session_id:
                    print_error("Failed to create dashboard session")
                    sys.exit(1)
                start_body: dict = {
                    "query": args.query,
                    "topology": _DEFAULT_TOPOLOGY_PICK,
                }
                if agent_model_pins:
                    start_body["agent_models"] = agent_model_pins
                async with session.post(f"{base}/api/v1/sessions/{session_id}/runs", json=start_body) as resp:
                    payload = await resp.json()
                    if not payload.get("ok"):
                        print_error(payload.get("error", "Failed to start run"))
                        sys.exit(1)
                    print("  Started run on daemon.")

        dashboard_url = base
        if session_id:
            separator = "&" if "?" in base else "?"
            dashboard_url = f"{base}{separator}session={session_id}"
        if not args.no_open:
            webbrowser.open(dashboard_url)
        print(f"  Open dashboard at {dashboard_url}")
        return

    trace = args.trace and not args.no_trace
    providers = build_providers(
        local_only=args.local_only,
        cloud_only=args.cloud_only,
    )

    if not providers:
        print_error(
            "No LLM providers available. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
            "or ensure Ollama/VMLX/OMLX is running locally."
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
            provider = "openai-codex"
        else:
            provider = "ollama" if "ollama" in providers else "omlx" if "omlx" in providers else "vmlx" if "vmlx" in providers else "ollama"
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

    if args.interactive or args.query is None:
        await run_repl(
            providers=providers,
            config=config,
            model_overrides=model_overrides or None,
            trace=trace,
            tier_override=tier_override,
            topology="triad",
        )
    else:
        print_header()
        live_display = None
        orchestrator = None

        if trace:
            topology_id = "triad"
            orchestrator = create_orchestrator(
                topology_id,
                providers=providers,
                config=config,
                model_overrides=model_overrides or None,
                trace=False,
                tier_override=tier_override,
            )
            from .live_display import LiveDisplay
            live_display = LiveDisplay(budget=args.budget)
            # Direct CLI runs use the default local topology.
            topo_label = topology_id
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

        result = await orchestrator.run(args.query)

        if live_display:
            live_display.stop()

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
