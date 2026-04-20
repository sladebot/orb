from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from aiohttp import web
from json import JSONDecodeError

from orb.runtime import GraphRuntime, RuntimeManager
from .api_v1 import register_v1_routes
from .state import DashboardState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@web.middleware
async def _no_cache_middleware(request: web.Request, handler):
    response = await handler(request)
    if (
        request.path == "/"
        or request.path.startswith("/static/")
        or request.path.startswith("/api/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class DashboardServer:
    """Backend runtime server with API, WebSocket fanout, and dashboard assets."""

    def __init__(
        self,
        state: DashboardState,
        host: str = "0.0.0.0",
        port: int = 8080,
        runtime: GraphRuntime | None = None,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.runtime = runtime or GraphRuntime(state)
        # Multi-tenant control plane. The legacy routes still act on
        # `self.runtime` as the "default session"; v1 routes go through
        # the manager so harnesses can run multiple sessions concurrently.
        self.manager = RuntimeManager()
        self.manager._sessions[self.runtime._conversation_session.session_id] = self.runtime  # noqa: SLF001
        # Bridge the default session's broadcasts through the manager too,
        # so v1 subscribers see everything.
        self.runtime.subscribe(self.manager._forward_broadcast)  # noqa: SLF001
        self._app = web.Application(middlewares=[_no_cache_middleware])
        self._clients: dict[web.WebSocketResponse, str | None] = {}
        self._runner: web.AppRunner | None = None
        self._catalog_refresh_task: asyncio.Task | None = None

        # Legacy routes removed — every surface is under /api/v1/... now.
        # Handler methods are kept on this class because the v1 layer
        # still delegates to a few of them (git status, for instance).
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR)

        # v1 multi-tenant API — sits alongside the legacy routes during
        # the transition; frontend + TUI will switch over in Phase 4.
        register_v1_routes(self._app, self.manager, self)

    def set_agents(self, agents: dict) -> None:
        """Retained for compatibility; runtime owns live agents."""
        self.runtime._agents = agents

    def set_providers(self, providers: dict, config, model_overrides, tier_override) -> None:
        # Route provider config through the manager so every session
        # (default + any created later via POST /api/v1/sessions) gets
        # the same pool. `manager.configure` fans out to the existing
        # default session; create_session seeds the pool onto fresh ones.
        self.manager.configure(providers, config, model_overrides, tier_override)

    async def start(self) -> None:
        # Subscribe the WS broadcaster to the MANAGER's subscriber pool so
        # events from every session — not just the default one — reach
        # connected clients. The manager's _forward_broadcast re-emits
        # every per-session broadcast through this pool with the
        # originating session_id embedded in the JSON payload.
        self.manager.subscribe(self.broadcast)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("Dashboard server running at http://%s:%s", self.host, self.port)
        self._catalog_refresh_task = asyncio.create_task(self.runtime.refresh_provider_catalogs())
        self._catalog_refresh_task.add_done_callback(self._log_catalog_refresh_error)

        # Start topology file watcher for hot-reload
        from orb.topologies import get_watcher
        self._topology_watcher = get_watcher()
        self._topology_watcher.on_reload(self._on_topologies_reloaded)
        self._topology_watcher.start()

    @staticmethod
    def _log_catalog_refresh_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Background provider catalog refresh failed")

    async def _on_topologies_reloaded(self) -> None:
        await self.broadcast(json.dumps({"type": "topologies_reloaded"}))

    async def stop(self) -> None:
        if hasattr(self, "_topology_watcher"):
            self._topology_watcher.stop()
        if self._catalog_refresh_task is not None and not self._catalog_refresh_task.done():
            self._catalog_refresh_task.cancel()
        self.manager.unsubscribe(self.broadcast)
        await self.runtime.stop()
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, data: str) -> None:
        # Parse the payload's session_id (every GraphRuntime broadcast is
        # tagged) and fan out to clients whose filter matches. Clients with
        # no filter receive everything. Payloads without a session_id tag
        # (e.g. topology hot-reload pings) go to everyone.
        payload_session_id: str | None = None
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                raw = parsed.get("session_id")
                if isinstance(raw, str) and raw:
                    payload_session_id = raw
        except (ValueError, TypeError):
            pass
        closed = []
        for ws, client_filter in self._clients.items():
            if client_filter and payload_session_id and client_filter != payload_session_id:
                continue
            try:
                await ws.send_str(data)
            except (ConnectionResetError, RuntimeError):
                closed.append(ws)
        for ws in closed:
            self._clients.pop(ws, None)

    @staticmethod
    def _read_index_assets() -> tuple[str, str]:
        """Synchronous file I/O for the index page — call via asyncio.to_thread."""
        build_ts = str(max(
            int((STATIC_DIR / "style.css").stat().st_mtime),
            int((STATIC_DIR / "graph.js").stat().st_mtime),
            int((STATIC_DIR / "app.js").stat().st_mtime),
            int((STATIC_DIR / "index.html").stat().st_mtime),
        ))
        html = (STATIC_DIR / "index.html").read_text()
        return build_ts, html

    async def _index_handler(self, request: web.Request) -> web.Response:
        build_ts, html = await asyncio.to_thread(self._read_index_assets)
        html = html.replace("/static/style.css", f"/static/style.css?v={build_ts}")
        html = html.replace("/static/graph.js", f"/static/graph.js?v={build_ts}")
        html = html.replace("/static/app.js", f"/static/app.js?v={build_ts}")
        response = web.Response(text=html, content_type="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    async def _state_handler(self, request: web.Request) -> web.Response:
        session_id = request.rel_url.query.get("session", "").strip() or None
        return web.json_response(self.runtime.current_init_event(session_id=session_id))

    async def _inject_handler(self, request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 1_048_576:
            return web.json_response({"ok": False, "error": "Request too large"}, status=413)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        target_id = body.get("to", "").strip()
        text = body.get("message", "").strip()
        if not target_id:
            return web.json_response({"ok": False, "error": "Missing 'to' field"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "Missing 'message' field"}, status=400)

        status, payload = await self.runtime.inject_message(target_id, text)
        return web.json_response(payload, status=status)

    async def _start_handler(self, request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 1_048_576:
            return web.json_response({"ok": False, "error": "Request too large"}, status=413)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        query = (body.get("query") or "").strip()
        topology = (body.get("topology") or "auto").strip()
        model_pin = (body.get("model") or "auto").strip()
        workdir = (body.get("workdir") or "").strip() or None
        raw_agent_models = body.get("agent_models") or {}
        if not isinstance(raw_agent_models, dict):
            return web.json_response(
                {"ok": False, "error": "agent_models must be an object mapping role -> model_id"},
                status=400,
            )
        agent_models: dict[str, str] = {
            str(role).strip(): str(model_id).strip()
            for role, model_id in raw_agent_models.items()
            if str(role).strip() and str(model_id).strip()
        } or None
        if agent_models and (topology == "auto" or not topology):
            return web.json_response(
                {"ok": False, "error": "agent_models requires an explicit topology (not 'auto')"},
                status=400,
            )
        if not query:
            return web.json_response({"ok": False, "error": "Query must not be empty"}, status=400)
        from orb.topologies import get_loader, normalize_topology_id
        topology = normalize_topology_id(topology)
        valid_topologies = ["auto"] + get_loader().list_ids()
        if topology not in valid_topologies:
            return web.json_response(
                {"ok": False, "error": f"topology must be one of: {', '.join(valid_topologies)}"},
                status=400,
            )

        status, payload = await self.runtime.start_run(
            query,
            topology,
            model_pin=model_pin,
            agent_models=agent_models,
            workdir=workdir,
        )
        return web.json_response(payload, status=status)

    async def _stop_run_handler(self, request: web.Request) -> web.Response:
        return web.json_response(await self.runtime.stop_run())

    async def _new_session_handler(self, request: web.Request) -> web.Response:
        workdir: str | None = None
        if request.body_exists and request.content_length:
            try:
                body = await request.json()
            except (JSONDecodeError, UnicodeDecodeError, ValueError):
                body = None
            if isinstance(body, dict):
                raw = body.get("workdir")
                if isinstance(raw, str) and raw.strip():
                    workdir = raw.strip()
        status, payload = await self.runtime.new_session(workdir=workdir)
        return web.json_response(payload, status=status)

    async def _run_status_handler(self, request: web.Request) -> web.Response:
        return web.json_response({
            "run_state": self.runtime.run_state.value,
            "message_count": self.state.message_count,
        })

    async def _predict_topology_handler(self, request: web.Request) -> web.Response:
        q = request.rel_url.query.get("q", "").strip()
        model = request.rel_url.query.get("model", "auto").strip()
        return web.json_response(await self.runtime.predict_topology(q, model_pin=model))

    async def _models_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.models_payload())

    async def _settings_get_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.settings_payload())

    async def _fs_list_handler(self, request: web.Request) -> web.Response:
        """Directory listing endpoint for the dashboard's workspace picker.

        Returns subdirectories of the given `?path=` (defaulting to the user's
        home directory). Files are filtered out because only folders can be a
        workdir. Paths are always resolved, so the client receives absolute
        canonical paths it can send back to /api/session/new verbatim.
        """
        raw = (request.rel_url.query.get("path") or "").strip()
        show_hidden = request.rel_url.query.get("hidden", "").lower() in ("1", "true", "yes")
        try:
            root = Path(raw).expanduser() if raw else Path.home()
            root = root.resolve(strict=False)
            if not root.exists() or not root.is_dir():
                return web.json_response(
                    {"ok": False, "error": f"Not a directory: {root}"},
                    status=400,
                )
            entries: list[dict] = []
            for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                if not show_hidden and child.name.startswith("."):
                    continue
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": True,
                })
            parent = str(root.parent) if root.parent != root else ""
            return web.json_response({
                "ok": True,
                "path": str(root),
                "parent": parent,
                "home": str(Path.home()),
                "entries": entries,
            })
        except PermissionError as exc:
            return web.json_response(
                {"ok": False, "error": f"Permission denied: {exc}"},
                status=403,
            )
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"Failed to list path: {exc}"},
                status=500,
            )

    @staticmethod
    def _resolve_workdir(raw: str | None) -> Path | None:
        if not raw:
            return None
        try:
            path = Path(str(raw)).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return None
        if not path.exists() or not path.is_dir():
            return None
        return path

    @staticmethod
    def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _git_status(self, path: Path) -> dict:
        """Inspect a folder for git repo state — used by the dashboard."""
        info: dict = {
            "ok": True,
            "path": str(path),
            "is_git_repo": False,
            "branch": "",
            "remote_url": "",
            "github_slug": "",
            "has_uncommitted": False,
            "ahead": 0,
            "behind": 0,
        }
        try:
            inside = self._run_git(["rev-parse", "--is-inside-work-tree"], path)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            info["ok"] = False
            info["error"] = "git CLI unavailable or timed out"
            return info
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return info

        info["is_git_repo"] = True
        branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)
        if branch.returncode == 0:
            info["branch"] = branch.stdout.strip()
        remote = self._run_git(["remote", "get-url", "origin"], path)
        if remote.returncode == 0:
            info["remote_url"] = remote.stdout.strip()
            info["github_slug"] = self._github_slug_from_remote(info["remote_url"])
        porcelain = self._run_git(["status", "--porcelain"], path)
        info["has_uncommitted"] = bool(porcelain.stdout.strip())
        ahead_behind = self._run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            path,
        )
        if ahead_behind.returncode == 0 and ahead_behind.stdout.strip():
            parts = ahead_behind.stdout.strip().split()
            if len(parts) == 2:
                info["ahead"] = int(parts[0])
                info["behind"] = int(parts[1])
        return info

    @staticmethod
    def _github_slug_from_remote(url: str) -> str:
        """Return owner/repo from an origin URL, or '' if not a GitHub remote."""
        if not url:
            return ""
        match = re.match(r"(?:git@github\.com:|https?://github\.com/)([^/]+)/(.+?)(?:\.git)?/?$", url.strip())
        if not match:
            return ""
        return f"{match.group(1)}/{match.group(2)}"

    async def _fs_files_handler(self, request: web.Request) -> web.Response:
        """List workspace files so the dashboard can seed the Repository
        Changes panel with the user's existing repo state. Prefers
        `git ls-files` when available (respects .gitignore); otherwise
        walks the tree with a size cap and a standard ignore list.
        """
        raw = request.rel_url.query.get("path") or ""
        path = self._resolve_workdir(raw)
        if path is None:
            return web.json_response({"ok": False, "error": "path must point to an existing directory"}, status=400)
        limit = 800
        files: list[str] = []
        used_git = False
        try:
            result = self._run_git(["ls-files", "-z"], path)
            if result.returncode == 0:
                used_git = True
                files = [
                    name for name in result.stdout.split("\0")
                    if name and not name.startswith(".git/")
                ][:limit]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if not used_git:
            ignore_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".turbo", ".DS_Store"}
            for root, dirs, entries in __import__("os").walk(str(path)):
                dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                root_path = Path(root)
                for entry in entries:
                    if entry.startswith("."):
                        continue
                    full = root_path / entry
                    try:
                        rel = full.relative_to(path)
                    except ValueError:
                        continue
                    files.append(str(rel))
                    if len(files) >= limit:
                        break
                if len(files) >= limit:
                    break
            files.sort()
        return web.json_response({
            "ok": True,
            "path": str(path),
            "source": "git" if used_git else "walk",
            "files": files,
            "truncated": len(files) >= limit,
        })

    async def _fs_read_handler(self, request: web.Request) -> web.Response:
        """Return the text content of a file inside the session's workdir so
        the diff pane can show unchanged files. Refuses binary content and
        anything over 512KB to keep the browser responsive.
        """
        workdir_raw = request.rel_url.query.get("workdir") or ""
        rel_raw = request.rel_url.query.get("path") or ""
        if not rel_raw:
            return web.json_response({"ok": False, "error": "path is required"}, status=400)
        workdir = self._resolve_workdir(workdir_raw)
        if workdir is None:
            return web.json_response({"ok": False, "error": "workdir must point to an existing directory"}, status=400)
        try:
            # strict=True follows symlinks AND requires every component to
            # exist, so a link pointing outside the workdir resolves to its
            # real target and the relative_to() check below catches the escape.
            target = (workdir / rel_raw).resolve(strict=True)
        except (OSError, ValueError, FileNotFoundError):
            return web.json_response({"ok": False, "error": "invalid path"}, status=400)
        # Prevent path traversal outside the workdir
        try:
            target.relative_to(workdir)
        except ValueError:
            return web.json_response({"ok": False, "error": "path escapes workdir"}, status=400)
        # Belt-and-suspenders: re-check against realpath in case of any
        # remaining symlink trickery (e.g. TOCTOU or path component games).
        import os as _os
        real = Path(_os.path.realpath(str(target)))
        try:
            real.relative_to(workdir)
        except ValueError:
            return web.json_response({"ok": False, "error": "path escapes workdir"}, status=400)
        if not target.exists() or not target.is_file():
            return web.json_response({"ok": False, "error": "not a file"}, status=404)
        size = target.stat().st_size
        if size > 512 * 1024:
            return web.json_response({
                "ok": False,
                "error": f"file is too large ({size} bytes) to preview",
            }, status=413)
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return web.json_response({"ok": False, "error": "binary file"}, status=415)
        return web.json_response({
            "ok": True,
            "path": str(target),
            "relative": rel_raw,
            "content": content,
            "size": size,
        })

    async def _git_status_handler(self, request: web.Request) -> web.Response:
        raw = request.rel_url.query.get("path") or ""
        if not raw:
            return web.json_response(
                {"ok": False, "error": "path is required"},
                status=400,
            )
        path = self._resolve_workdir(raw)
        if path is None:
            return web.json_response(
                {"ok": False, "error": f"path must point to an existing directory: {raw}"},
                status=400,
            )
        info = self._git_status(path)
        return web.json_response(info)

    async def _git_init_handler(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)
        path = self._resolve_workdir(body.get("path"))
        if path is None:
            return web.json_response({"ok": False, "error": "path must point to an existing directory"}, status=400)
        try:
            result = self._run_git(["init"], path)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return web.json_response({"ok": False, "error": f"git CLI failed: {exc}"}, status=500)
        if result.returncode != 0:
            return web.json_response({"ok": False, "error": result.stderr.strip() or "git init failed"}, status=500)
        return web.json_response({"ok": True, "status": self._git_status(path)})

    async def _git_pr_url_handler(self, request: web.Request) -> web.Response:
        """Return a GitHub compare URL for the current branch vs. the repo's
        default remote branch. No push, no commit — callers open the URL in
        a new tab and finish the PR in GitHub. If the workdir isn't a GitHub
        repo, we return an error the dashboard can surface.
        """
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)
        path = self._resolve_workdir(body.get("path"))
        if path is None:
            return web.json_response({"ok": False, "error": "path must point to an existing directory"}, status=400)
        status = self._git_status(path)
        if not status.get("is_git_repo"):
            return web.json_response({"ok": False, "error": "Not a git repo"}, status=400)
        slug = status.get("github_slug")
        if not slug:
            return web.json_response({"ok": False, "error": f"Origin is not a GitHub remote: {status.get('remote_url') or '(none)'}"}, status=400)
        branch = status.get("branch") or "HEAD"
        # Try to resolve the default branch via `gh` if present, otherwise fall
        # back to `main`.
        default_branch = "main"
        try:
            rev = self._run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], path)
            if rev.returncode == 0 and rev.stdout.strip():
                default_branch = rev.stdout.strip().split("/")[-1]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        compare_url = f"https://github.com/{slug}/compare/{default_branch}...{branch}?expand=1"
        return web.json_response({
            "ok": True,
            "compare_url": compare_url,
            "branch": branch,
            "default_branch": default_branch,
            "github_slug": slug,
            "has_uncommitted": status.get("has_uncommitted", False),
        })

    async def _topologies_handler(self, request: web.Request) -> web.Response:
        from orb.topologies import get_loader

        loader = get_loader()
        topologies = []
        for tid in loader.list_ids():
            topo = loader.get(tid)
            edges = [
                {"source": a, "target": b}
                for a, b in (topo.edges or [])
            ]
            topologies.append({
                "id": tid,
                "label": topo.label,
                "description": topo.description,
                "agents": list(topo.agents.keys()),
                "edges": edges,
            })
        return web.json_response({"topologies": topologies})

    async def _trace_sessions_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.list_trace_sessions())

    async def _trace_session_runs_handler(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"].strip()
        if not session_id:
            return web.json_response({"error": "missing session id"}, status=400)
        return web.json_response(self.runtime.list_session_traces(session_id))

    async def _trace_run_handler(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"].strip()
        if not run_id:
            return web.json_response({"error": "missing run id"}, status=400)
        payload = self.runtime.get_trace_payload(run_id)
        if payload is None:
            return web.json_response({"error": "trace not found"}, status=404)
        return web.json_response(payload)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        session_id = request.rel_url.query.get("session", "").strip() or None
        self._clients[ws] = session_id
        try:
            logger.info("Dashboard client connected (%s total)", len(self._clients))
            try:
                await ws.send_str(json.dumps(self.runtime.current_init_event(session_id=session_id)))
            except (ConnectionResetError, RuntimeError):
                pass
            async for _msg in ws:
                pass
        finally:
            self._clients.pop(ws, None)
            logger.info("Dashboard client disconnected (%s total)", len(self._clients))
        return ws
