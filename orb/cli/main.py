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
from .repl import run_repl



LOG_FILE = os.path.join(os.path.expanduser("~"), ".orb", "run.log")
DAEMON_STATE_FILE = os.path.join(os.path.expanduser("~"), ".orb", "daemon.json")
DEFAULT_DAEMON_PORT = 1337
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
    return (workspace_root or Path.cwd()) / ".orb" / "traces"


def _trace_index_dir(workspace_root: Path | None = None) -> Path:
    return _trace_dir(workspace_root) / "by-session"


def _trace_roots() -> list[Path]:
    roots = [Path.cwd()]
    daemon_root = _managed_daemon_workdir()
    if daemon_root is not None and daemon_root not in roots:
        roots.append(daemon_root)
    return roots


def _current_session_id() -> str:
    for root in _trace_roots():
        path = root / ".orb" / "current_session"
        try:
            session_id = path.read_text().strip()
        except OSError:
            session_id = ""
        if session_id:
            return session_id
    return ""


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

    subparsers.add_parser("onboard", help="Interactive onboarding for auth and common settings")
    subparsers.add_parser("configure", help="Interactive provider + model setup (auth, catalog refresh, per-tier defaults)")
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
    topology_choices = ["auto"] + get_loader().list_ids()

    tui_parser = subparsers.add_parser("tui", help="Attach the terminal UI to a running Orb daemon")
    tui_parser.add_argument("query", nargs="?", help="Optional task query to start on the connected daemon")
    tui_parser.add_argument("--connect", type=str, default=None, help=f"Orb daemon URL (default: {DEFAULT_CONNECT_URL})")
    tui_parser.add_argument("--port", type=int, default=None, help="Orb daemon port shorthand for localhost connects")
    tui_parser.add_argument("--topology", choices=topology_choices, default="auto", help="Requested topology when starting a new run")
    tui_parser.add_argument("--budget", type=int, default=200, help="Requested budget when starting a new run")
    tui_parser.add_argument("--logs", action="store_true", help="Show live log panel in TUI")
    tui_parser.add_argument("--exit-after-run", action="store_true", help="Exit automatically after a non-interactive run completes")
    dashboard_parser = subparsers.add_parser("dashboard", help="Open the dashboard for a running Orb daemon")
    dashboard_parser.add_argument("query", nargs="?", help="Optional task query to start on the connected daemon")
    dashboard_parser.add_argument("--connect", type=str, default=None, help=f"Orb daemon URL (default: {DEFAULT_CONNECT_URL})")
    dashboard_parser.add_argument("--topology", choices=topology_choices, default="auto", help="Requested topology when starting a new run")
    dashboard_parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    daemon_common = argparse.ArgumentParser(add_help=False)
    daemon_common.add_argument("--host", default=None, help="Daemon bind host")
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
    daemon_parser.add_argument("--host", default="127.0.0.1", help="Daemon bind host (default: 127.0.0.1)")
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


def _resolve_connect_url(url: str | None, port: int | None = None) -> str:
    if url:
        return _normalize_connect_url(url)
    if port is not None:
        return _normalize_connect_url(f"http://127.0.0.1:{port}")
    return _normalize_connect_url(None)


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
    if workdir:
        path = Path(workdir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="orb-daemon-", dir="/tmp")).resolve()


def _daemon_state_path() -> Path:
    return Path(DAEMON_STATE_FILE)


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
        with urlopen(f"http://{probe_host}:{port}/api/state", timeout=0.75) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False
    return body.get("type") == "init"


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
            await runtime.refresh_provider_catalogs()

            cfg = load_config()
            providers_cfg = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
            print("Updated provider catalogs in ~/.orb/config.json")
            for provider_name in ("anthropic", "openai-codex", "ollama", "vmlx", "omlx"):
                entry = providers_cfg.get(provider_name) if isinstance(providers_cfg, dict) else None
                if not isinstance(entry, dict):
                    continue
                catalog = entry.get("catalog") or []
                defaults = entry.get("default_models") or {}
                if catalog:
                    print(f"  {provider_name:<13} {len(catalog)} models")
                if isinstance(defaults, dict) and defaults:
                    summary = ", ".join(f"{k}={v}" for k, v in defaults.items())
                    print(f"    defaults: {summary}")
            return
        print_error("Unknown models command")
        sys.exit(1)

    if args.subcommand == "onboard":
        from .onboard import run_onboarding
        await run_onboarding()
        return

    if args.subcommand == "configure":
        from .configure import run_configure
        await run_configure()
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
            host = args.host or "127.0.0.1"
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
            host = args.host or "127.0.0.1"
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

        daemon_host = args.host or "127.0.0.1"
        daemon_port = args.port or DEFAULT_DAEMON_PORT
        daemon_workdir = _resolve_daemon_workdir(getattr(args, "workdir", None))
        os.chdir(daemon_workdir)
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

        await attach_tui(
            connect_url=_resolve_connect_url(args.connect, getattr(args, "port", None)),
            topology=args.topology,
            budget=args.budget,
            show_logs=args.logs,
            initial_query=args.query,
            exit_after_run=args.exit_after_run,
        )
        return

    if args.subcommand == "dashboard":
        import aiohttp
        import webbrowser

        base = _resolve_connect_url(args.connect)

        if args.query:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/start", json={
                    "query": args.query,
                    "topology": args.topology,
                }) as resp:
                    payload = await resp.json()
                    if not payload.get("ok"):
                        print_error(payload.get("error", "Failed to start run"))
                        sys.exit(1)
                    print("  Started run on daemon.")
        if not args.no_open:
            webbrowser.open(base)
        print(f"  Open dashboard at {base}")
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
