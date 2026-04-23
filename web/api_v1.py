"""Multi-tenant v1 HTTP + WebSocket API for Orb.

Everything under ``/api/v1/sessions/{session_id}/...`` routes to a
specific :class:`~orb.runtime.graph_runtime.GraphRuntime` registered in
the :class:`~orb.runtime.manager.RuntimeManager`. The old top-level
``/api/*`` routes continue to work against a default session for the
dashboard + TUI during the transition; v1 is the surface external
harnesses (hermes, openclaw, etc.) should target.

Every v1 response uses the standard envelope:

    {"ok": bool, "code": "UPPER_SNAKE", "error"?: str, "data"?: dict}

Error responses set ``ok: false`` and include ``code`` + ``error`` but
never ``data``; success responses set ``ok: true`` with ``code`` and
``data``. HTTP status codes mirror the envelope for 400/404/409 cases.
"""

from __future__ import annotations

import asyncio
import logging
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from orb.runtime.manager import RuntimeManager
    from orb.runtime.graph_runtime import GraphRuntime

logger = logging.getLogger(__name__)


# ── Envelope helpers ─────────────────────────────────────────────────────


def ok(code: str, data: dict | None = None, *, status: int = 200) -> web.Response:
    return web.json_response(
        {"ok": True, "code": code, "data": data or {}},
        status=status,
    )


def err(code: str, message: str, *, status: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "code": code, "error": message},
        status=status,
    )


# ── Session lookup ───────────────────────────────────────────────────────


def _get_session(manager: "RuntimeManager", session_id: str) -> "GraphRuntime | None":
    # Prefer live registry; fall back to restoring from the persisted
    # daemon registry so a page refresh after a daemon restart transparently
    # resurrects the user's session instead of blanking the dashboard.
    runtime = manager.get_session(session_id)
    if runtime is not None:
        return runtime
    return manager.try_restore(session_id)


def _session_summary(runtime: "GraphRuntime") -> dict:
    cs = runtime._conversation_session  # noqa: SLF001
    return {
        "session_id": cs.session_id,
        "generation": cs.generation,
        "workdir": cs.workdir,
        "run_state": runtime.run_state.value,
        "turn": cs.user_turn_count(),
        "locked_topology": cs.locked_topology,
    }


# ── Routes ───────────────────────────────────────────────────────────────


def register_v1_routes(app: web.Application, manager: "RuntimeManager", server: Any) -> None:
    """Attach every v1 route to the aiohttp app.

    ``server`` is the enclosing :class:`DashboardServer`, needed only for
    the WebSocket client registry — v1 reuses the same broadcast fanout
    path as the legacy routes during the transition.
    """

    # ---- Health ----

    async def health(request: web.Request) -> web.Response:
        return ok("HEALTHY", {
            "active_sessions": len(manager.list_sessions()),
            "active_runs": manager.active_session_count(),
        })

    # ---- Sessions ----

    async def create_session(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return err("INVALID_BODY", "Request body must be a JSON object", status=400)
        workdir = (body.get("workdir") or "").strip() or None
        if workdir:
            path = Path(workdir).expanduser()
            if not path.exists() or not path.is_dir():
                return err("INVALID_WORKDIR", f"Workdir does not exist or is not a directory: {path}", status=400)
            workdir = str(path.resolve())

        # Optional pre-warm: explicit topology + per-node models get
        # pinned onto the session at creation time so the first /runs
        # skips the classifier and the dashboard paints the graph
        # immediately.
        topology = (body.get("topology") or "").strip() or None
        if topology:
            from orb.topologies import get_loader, normalize_topology_id
            normalized = normalize_topology_id(topology)
            valid = {"auto", *get_loader().list_ids()}
            if normalized not in valid:
                return err(
                    "INVALID_TOPOLOGY",
                    f"topology must be one of: {', '.join(sorted(valid))}",
                    status=400,
                )
            topology = normalized
        raw_agent_models = body.get("agent_models") or {}
        if raw_agent_models and not isinstance(raw_agent_models, dict):
            return err("INVALID_AGENT_MODELS", "agent_models must be an object", status=400)
        agent_models: dict[str, str] | None = {
            str(r).strip(): str(m).strip()
            for r, m in (raw_agent_models or {}).items()
            if str(r).strip() and str(m).strip()
        } or None
        if agent_models and (not topology or topology == "auto"):
            return err(
                "INVALID_AGENT_MODELS",
                "agent_models requires an explicit topology (not 'auto')",
                status=400,
            )
        model_pin = (body.get("model") or body.get("model_pin") or "").strip() or None

        runtime = manager.create_session(
            workdir=workdir,
            topology=topology,
            agent_models=agent_models,
            model_pin=model_pin,
        )
        return ok("SESSION_CREATED", _session_summary(runtime), status=201)

    async def list_sessions(request: web.Request) -> web.Response:
        include = (request.rel_url.query.get("include") or "").strip().lower()
        if include == "known":
            # Union of active + registry-only sessions for the resume picker.
            sessions = manager.list_known_sessions()
            return ok("SESSIONS_LISTED", {
                "sessions": sessions,
                "total": len(sessions),
            })
        sessions = [_session_summary(rt) for rt in manager.list_sessions()]
        return ok("SESSIONS_LISTED", {
            "sessions": sessions,
            "total": len(sessions),
        })

    async def get_session_info(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        return ok("SESSION_FETCHED", _session_summary(runtime))

    async def delete_session(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        deleted = manager.delete_session(session_id)
        if not deleted:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        return ok("SESSION_DELETED", {"session_id": session_id})

    # ---- Runs within a session ----

    async def start_run(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return err("INVALID_BODY", "Request body must be a JSON object", status=400)
        query = (body.get("query") or "").strip()
        if not query:
            return err("QUERY_EMPTY", "query must not be empty", status=400)
        topology = (body.get("topology") or "auto").strip()
        # Reject unknown topology IDs up-front so we never fire the FSM
        # into PLANNING just to error out once _start_run_planning runs.
        from orb.topologies import get_loader, normalize_topology_id
        normalized = normalize_topology_id(topology)
        valid = {"auto", *get_loader().list_ids()}
        if normalized not in valid:
            return err(
                "INVALID_TOPOLOGY",
                f"topology must be one of: {', '.join(sorted(valid))}",
                status=400,
            )
        topology = normalized
        model_pin = (body.get("model") or body.get("model_pin") or "auto").strip()
        raw_agent_models = body.get("agent_models") or {}
        if not isinstance(raw_agent_models, dict):
            return err("INVALID_AGENT_MODELS", "agent_models must be an object", status=400)
        agent_models: dict[str, str] = {
            str(r).strip(): str(m).strip()
            for r, m in raw_agent_models.items()
            if str(r).strip() and str(m).strip()
        } or None
        workdir = (body.get("workdir") or "").strip() or None

        status_code, payload = await runtime.start_run(
            query,
            topology,
            model_pin=model_pin,
            agent_models=agent_models,
            workdir=workdir,
        )
        if not payload.get("ok"):
            return err("RUN_START_FAILED", payload.get("error") or "Failed to start run", status=status_code or 500)
        return ok("RUN_STARTED", {
            "session_id": session_id,
            "run_state": runtime.run_state.value,
            "init": payload.get("init"),
            "session_turn": payload.get("session_turn"),
        }, status=202)

    async def stop_run(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        payload = await runtime.stop_run()
        if not payload.get("ok"):
            return err("NO_RUN_IN_FLIGHT", payload.get("error") or "No run in progress", status=409)
        return ok("RUN_STOP_REQUESTED", {
            "session_id": session_id,
            "run_state": runtime.run_state.value,
        })

    async def inject_message(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return err("INVALID_BODY", "Request body must be a JSON object", status=400)
        target = (body.get("to") or "").strip()
        message = (body.get("message") or "").strip()
        if not message:
            return err("MESSAGE_EMPTY", "message must not be empty", status=400)
        if not target:
            return err("TARGET_MISSING", "target agent id must be provided in 'to'", status=400)
        status_code, payload = await runtime.inject_message(target, message)
        if not payload.get("ok"):
            code = "INJECT_FAILED"
            if "no run" in str(payload.get("error", "")).lower():
                code = "NO_RUN_IN_FLIGHT"
            return err(code, payload.get("error") or "Inject failed", status=status_code or 400)
        return ok("MESSAGE_INJECTED", {"session_id": session_id, "target": target})

    # ---- State ----

    async def session_state(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        runtime = _get_session(manager, session_id)
        if runtime is None:
            return err("SESSION_NOT_FOUND", f"No session with id {session_id!r}", status=404)
        init = runtime.current_init_event(session_id=session_id)
        return ok("STATE_FETCHED", init)

    # ---- WebSocket ----

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        """Single multiplexed socket.

        ``?session_id=X`` optionally filters to one session's events;
        without the param the client receives every session's broadcasts
        and must filter client-side.
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        filter_session = request.rel_url.query.get("session_id") or None
        # Try to resurrect stale session_ids from the on-disk registry so a
        # page refresh after a daemon restart doesn't blank the dashboard.
        # Only send SESSION_NOT_FOUND when the id really has no backing
        # snapshot — that's the "your session was permanently deleted" case.
        if filter_session and manager.get_session(filter_session) is None:
            if manager.try_restore(filter_session) is None:
                import json as _json
                try:
                    await ws.send_str(_json.dumps({
                        "type": "error",
                        "code": "SESSION_NOT_FOUND",
                        "session_id": filter_session,
                    }))
                except (ConnectionResetError, RuntimeError):
                    pass
                await ws.close(code=4404, message=b"session not found")
                return ws
        server._clients[ws] = filter_session  # noqa: SLF001
        try:
            # Snapshot: either the filtered session, or (when no filter was
            # given) the most recent session for the legacy no-filter caller.
            target_session: "GraphRuntime | None" = None
            if filter_session:
                target_session = manager.get_session(filter_session)
            else:
                sessions = manager.list_sessions()
                target_session = sessions[-1] if sessions else None
            if target_session is not None:
                init = target_session.current_init_event(
                    session_id=target_session._conversation_session.session_id  # noqa: SLF001
                )
                init["session_id"] = target_session._conversation_session.session_id  # noqa: SLF001
                import json as _json
                await ws.send_str(_json.dumps(init))
            async for _msg in ws:  # client sends are ignored in v1
                pass
        finally:
            server._clients.pop(ws, None)  # noqa: SLF001
        return ws

    # ---- Introspection (daemon-scoped; session-agnostic) ----------

    async def models(request: web.Request) -> web.Response:
        return ok("MODELS_FETCHED", manager.models_payload())

    async def settings(request: web.Request) -> web.Response:
        return ok("SETTINGS_FETCHED", manager.settings_payload())

    async def topologies(request: web.Request) -> web.Response:
        from orb.topologies import get_loader
        loader = get_loader()
        topologies_payload = []
        for tid in loader.list_ids():
            topo = loader.get(tid)
            edges = [{"source": a, "target": b} for a, b in (topo.edges or [])]
            topologies_payload.append({
                "id": tid,
                "label": topo.label,
                "description": topo.description,
                "agents": list(topo.agents.keys()),
                "edges": edges,
            })
        return ok("TOPOLOGIES_FETCHED", {"topologies": topologies_payload})

    async def predict_topology(request: web.Request) -> web.Response:
        q = request.rel_url.query.get("q", "").strip()
        model_pin = request.rel_url.query.get("model", "auto").strip()
        # Any live session can answer (they all share the manager's providers).
        # When no sessions exist yet, synthesize a throwaway GraphRuntime
        # instead of calling manager.create_session() — otherwise every
        # /predict-topology call would leak a session into the registry.
        # Mirrors the bootstrap pattern used by manager.models_payload() and
        # manager.settings_payload().
        session = next(iter(manager.list_sessions()), None)
        if session is None:
            from orb.runtime.graph_runtime import GraphRuntime
            session = GraphRuntime(session_path=None)
            if manager._providers:  # noqa: SLF001
                session.configure(
                    manager._all_providers,  # noqa: SLF001
                    manager._config,  # noqa: SLF001
                    manager._model_overrides,  # noqa: SLF001
                    manager._tier_override,  # noqa: SLF001
                )
            if manager._topology_classifier is not None:  # noqa: SLF001
                session.set_topology_classifier(manager._topology_classifier)  # noqa: SLF001
        prediction = await session.predict_topology(q, model_pin=model_pin)
        return ok("TOPOLOGY_PREDICTED", prediction)

    # ---- Trace browser (session-agnostic read queries) -------------

    async def traces_sessions(request: web.Request) -> web.Response:
        return ok("TRACE_SESSIONS_FETCHED", manager.list_trace_sessions())

    async def traces_session_runs(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        return ok("TRACE_SESSION_RUNS_FETCHED", manager.list_session_traces(session_id))

    async def traces_run(request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        payload = manager.get_trace_payload(run_id)
        if payload is None:
            return err("TRACE_NOT_FOUND", f"No trace with id {run_id!r}", status=404)
        return ok("TRACE_FETCHED", payload)

    # ---- Filesystem picker (daemon-scoped) ------------------------

    async def fs_list(request: web.Request) -> web.Response:
        raw = (request.rel_url.query.get("path") or "").strip()
        show_hidden = request.rel_url.query.get("hidden", "").lower() in ("1", "true", "yes")
        try:
            root = Path(raw).expanduser() if raw else Path.home()
            root = root.resolve(strict=False)
            if not root.exists() or not root.is_dir():
                return err("INVALID_PATH", f"Not a directory: {root}", status=400)
            entries: list[dict] = []
            for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                if not show_hidden and child.name.startswith("."):
                    continue
                entries.append({"name": child.name, "path": str(child), "is_dir": True})
            parent = str(root.parent) if root.parent != root else ""
            return ok("FS_LISTED", {
                "path": str(root),
                "parent": parent,
                "home": str(Path.home()),
                "entries": entries,
            })
        except PermissionError as exc:
            return err("FS_PERMISSION_DENIED", f"Permission denied: {exc}", status=403)
        except Exception as exc:  # noqa: BLE001
            return err("FS_ERROR", f"Failed to list path: {exc}", status=500)

    async def fs_files(request: web.Request) -> web.Response:
        """Flat workdir listing.

        Legacy callers used this for the initial repo tree; new code uses
        ``/api/v1/fs/dir`` for lazy per-folder expansion. Kept around
        because some callers (and tests) still hit this endpoint.

        Filesystem-only (no git). Respects the workdir's ``.gitignore``
        via pathspec and a built-in deny list for VCS metadata / caches.
        """
        raw = (request.rel_url.query.get("path") or "").strip()
        if not raw:
            return err("INVALID_PATH", "path is required", status=400)
        path = Path(raw).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            return err("INVALID_PATH", f"Not a directory: {path}", status=400)
        # Scope: only folders under the user's home OR a path already
        # registered as a session workdir. Prevents callers from walking
        # /etc, ~/.ssh (unless explicitly opted in as a session workdir).
        allowed_roots: list[Path] = [Path.home().resolve()]
        for rt in manager.list_sessions():
            wd = getattr(rt._conversation_session, "workdir", None)  # noqa: SLF001
            if wd:
                try:
                    allowed_roots.append(Path(wd).expanduser().resolve())
                except (OSError, ValueError):
                    continue

        def _within(p: Path, roots: list[Path]) -> bool:
            for root in roots:
                try:
                    p.relative_to(root)
                    return True
                except ValueError:
                    continue
            return False

        if not _within(path, allowed_roots):
            return err(
                "PATH_OUT_OF_SCOPE",
                "path must be under $HOME or an existing session workdir",
                status=400,
            )
        import os as _os
        limit = 800
        files: list[str] = []
        spec = _load_gitignore_spec(path)
        ignore_dirs = set(_FS_DIR_DENY_NAMES)
        for root, dirs, entries in _os.walk(str(path)):
            # Prune deny-listed dirs in-place so os.walk doesn't descend.
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            # Also respect .gitignore for dirs (trailing slash for spec).
            if spec is not None:
                pruned: list[str] = []
                for d in dirs:
                    rel_dir = (Path(root) / d).relative_to(path).as_posix() + "/"
                    if not spec.match_file(rel_dir):
                        pruned.append(d)
                dirs[:] = pruned
            root_path = Path(root)
            for entry in entries:
                try:
                    rel = (root_path / entry).relative_to(path).as_posix()
                except ValueError:
                    continue
                if spec is not None and spec.match_file(rel):
                    continue
                files.append(rel)
                if len(files) >= limit:
                    break
            if len(files) >= limit:
                break
        files.sort()
        return ok("FS_FILES_FETCHED", {
            "path": str(path),
            "source": "walk",
            "files": files,
            "truncated": len(files) >= limit,
        })

    # Built-in deny list — VCS metadata and common cache/build dirs that
    # are never interesting to surface in the repo tree. Matched against
    # entry *name*, not a full pattern. .gitignore adds to this.
    _FS_DIR_DENY_NAMES = frozenset({
        ".git", ".hg", ".svn",
        "node_modules", ".venv", "venv", ".tox",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "dist", "build", ".next", ".turbo", ".DS_Store",
    })

    def _load_gitignore_spec(workdir: Path):
        """Load ``{workdir}/.gitignore`` into a pathspec PathSpec, or None.

        pathspec's GitWildMatchPattern handles the full .gitignore syntax
        (globs, ``!negation``, trailing slash = directory-only, etc.)
        without invoking the ``git`` CLI.
        """
        try:
            import pathspec  # type: ignore
        except ImportError:
            return None
        gitignore = workdir / ".gitignore"
        if not gitignore.exists() or not gitignore.is_file():
            return None
        try:
            lines = gitignore.read_text().splitlines()
        except OSError:
            return None
        # pathspec 1.x ships a modern `gitignore` factory; fall back to
        # the older GitWildMatchPattern for 0.x installations.
        try:
            return pathspec.PathSpec.from_lines("gitignore", lines)
        except (LookupError, ValueError):
            return pathspec.PathSpec.from_lines(
                pathspec.patterns.GitWildMatchPattern,
                lines,
            )

    async def fs_dir(request: web.Request) -> web.Response:
        """Return one level of a session workdir — lazy-tree backend.

        Query: ``workdir`` (absolute path of the session root, required)
        and optional ``path`` (relative folder inside workdir; empty =
        list the workdir root itself).

        Response data: ``{path, dirs: [{name,path}], files: [{name,path}]}``
        sorted alphabetically. Entries matching the workdir's .gitignore
        or the built-in deny list are filtered out.
        """
        workdir_raw = (request.rel_url.query.get("workdir") or "").strip()
        rel_raw = (request.rel_url.query.get("path") or "").strip()
        if not workdir_raw:
            return err("INVALID_WORKDIR", "workdir is required", status=400)
        workdir = Path(workdir_raw).expanduser().resolve()
        if not workdir.exists() or not workdir.is_dir():
            return err("INVALID_WORKDIR", f"Not a directory: {workdir}", status=400)
        # Resolve the target folder under workdir and guard against escapes.
        try:
            target = (workdir / rel_raw).resolve(strict=True) if rel_raw else workdir
            target.relative_to(workdir)
        except (OSError, ValueError, FileNotFoundError):
            return err("INVALID_PATH", "path escapes workdir", status=400)
        if not target.is_dir():
            return err("INVALID_PATH", "path must be a directory", status=400)

        spec = _load_gitignore_spec(workdir)
        dirs: list[dict] = []
        files: list[dict] = []
        try:
            for entry in target.iterdir():
                name = entry.name
                if name in _FS_DIR_DENY_NAMES:
                    continue
                try:
                    rel = entry.relative_to(workdir).as_posix()
                except ValueError:
                    continue
                # pathspec: directories must be matched with a trailing
                # slash for patterns like "build/" to hit the directory
                # (and not a file named "build").
                is_dir = entry.is_dir()
                check_path = rel + "/" if is_dir else rel
                if spec is not None and spec.match_file(check_path):
                    continue
                (dirs if is_dir else files).append({"name": name, "path": rel})
        except OSError as exc:
            return err("FS_READ_FAILED", f"Could not list directory: {exc}", status=500)

        dirs.sort(key=lambda d: d["name"].casefold())
        files.sort(key=lambda f: f["name"].casefold())
        rel_path = target.relative_to(workdir).as_posix()
        return ok("FS_DIR_FETCHED", {
            "path": "" if rel_path == "." else rel_path,
            "dirs": dirs,
            "files": files,
        })

    async def fs_read(request: web.Request) -> web.Response:
        workdir_raw = (request.rel_url.query.get("workdir") or "").strip()
        rel_raw = (request.rel_url.query.get("path") or "").strip()
        if not rel_raw or not workdir_raw:
            return err("INVALID_PATH", "workdir and path are required", status=400)
        workdir = Path(workdir_raw).expanduser().resolve()
        if not workdir.exists() or not workdir.is_dir():
            return err("INVALID_WORKDIR", f"Not a directory: {workdir}", status=400)
        try:
            # strict=True forces .resolve() to error on missing components,
            # which means broken or escaping symlinks are caught here rather
            # than slipping past .relative_to(). See fs_read_handler in
            # server.py for the matching rationale.
            target = (workdir / rel_raw).resolve(strict=True)
            target.relative_to(workdir)
        except (OSError, ValueError, FileNotFoundError):
            return err("INVALID_PATH", "path escapes workdir", status=400)
        # Re-check against os.path.realpath so any late symlink resolution
        # (TOCTOU or path-component games) still gets rejected.
        import os as _os
        real = Path(_os.path.realpath(str(target)))
        try:
            real.relative_to(workdir)
        except ValueError:
            return err("INVALID_PATH", "path escapes workdir", status=400)
        if not target.exists() or not target.is_file():
            return err("FILE_NOT_FOUND", "not a file", status=404)
        size = target.stat().st_size
        if size > 512 * 1024:
            return err("FILE_TOO_LARGE", f"file is too large ({size} bytes) to preview", status=413)
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return err("FILE_BINARY", "binary file", status=415)
        return ok("FS_FILE_READ", {"path": str(target), "relative": rel_raw, "content": content, "size": size})

    # ---- Git helpers (daemon-scoped read/write against a path) -----

    async def git_status(request: web.Request) -> web.Response:
        raw = (request.rel_url.query.get("path") or "").strip()
        path = Path(raw).expanduser().resolve() if raw else Path.cwd()
        if not path.exists() or not path.is_dir():
            return err("INVALID_PATH", f"Not a directory: {path}", status=400)
        info = await server._git_status_async(path)  # noqa: SLF001
        return ok("GIT_STATUS_FETCHED", info)

    async def git_init(request: web.Request) -> web.Response:
        import subprocess
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return err("INVALID_BODY", "Invalid JSON body", status=400)
        raw = (body.get("path") or "").strip()
        if not raw:
            return err("INVALID_PATH", "path is required", status=400)
        path = Path(raw).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            return err("INVALID_PATH", f"Not a directory: {path}", status=400)
        try:
            result = await asyncio.to_thread(
                subprocess.run, ["git", "init"],
                cwd=str(path), capture_output=True, text=True, timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return err("GIT_UNAVAILABLE", f"git CLI failed: {exc}", status=500)
        if result.returncode != 0:
            return err("GIT_INIT_FAILED", result.stderr.strip() or "git init failed", status=500)
        info = await server._git_status_async(path)  # noqa: SLF001
        return ok("GIT_INITIALIZED", {"status": info})

    async def git_pr_url(request: web.Request) -> web.Response:
        import subprocess
        try:
            body = await request.json()
        except (JSONDecodeError, UnicodeDecodeError, ValueError):
            return err("INVALID_BODY", "Invalid JSON body", status=400)
        raw = (body.get("path") or "").strip()
        if not raw:
            return err("INVALID_PATH", "path is required", status=400)
        path = Path(raw).expanduser().resolve()
        info = await server._git_status_async(path)  # noqa: SLF001
        if not info.get("is_git_repo"):
            return err("NOT_A_GIT_REPO", "Not a git repo", status=400)
        slug = info.get("github_slug")
        if not slug:
            return err("NOT_GITHUB_REMOTE", f"Origin is not a GitHub remote: {info.get('remote_url') or '(none)'}", status=400)
        branch = info.get("branch") or "HEAD"
        default_branch = "main"
        try:
            rev = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=str(path), capture_output=True, text=True, timeout=15)
            if rev.returncode == 0 and rev.stdout.strip():
                default_branch = rev.stdout.strip().split("/")[-1]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        compare_url = f"https://github.com/{slug}/compare/{default_branch}...{branch}?expand=1"
        return ok("GIT_PR_URL_GENERATED", {
            "compare_url": compare_url,
            "branch": branch,
            "default_branch": default_branch,
            "github_slug": slug,
            "has_uncommitted": info.get("has_uncommitted", False),
        })

    # ---- Route registration ----

    app.router.add_get("/api/v1/health", health)
    app.router.add_post("/api/v1/sessions", create_session)
    app.router.add_get("/api/v1/sessions", list_sessions)
    app.router.add_get("/api/v1/sessions/{session_id}", get_session_info)
    app.router.add_delete("/api/v1/sessions/{session_id}", delete_session)
    app.router.add_post("/api/v1/sessions/{session_id}/runs", start_run)
    app.router.add_post("/api/v1/sessions/{session_id}/runs/stop", stop_run)
    app.router.add_post("/api/v1/sessions/{session_id}/runs/inject", inject_message)
    app.router.add_get("/api/v1/sessions/{session_id}/state", session_state)
    app.router.add_get("/api/v1/ws", ws_handler)

    # Introspection
    app.router.add_get("/api/v1/models", models)
    app.router.add_get("/api/v1/settings", settings)
    app.router.add_get("/api/v1/topologies", topologies)
    app.router.add_get("/api/v1/predict-topology", predict_topology)

    # Trace browser
    app.router.add_get("/api/v1/traces/sessions", traces_sessions)
    app.router.add_get("/api/v1/traces/sessions/{session_id}", traces_session_runs)
    app.router.add_get("/api/v1/traces/runs/{run_id}", traces_run)

    # Filesystem picker
    app.router.add_get("/api/v1/fs/list", fs_list)
    app.router.add_get("/api/v1/fs/files", fs_files)
    app.router.add_get("/api/v1/fs/dir", fs_dir)
    app.router.add_get("/api/v1/fs/read", fs_read)

    # Git helpers
    app.router.add_get("/api/v1/git/status", git_status)
    app.router.add_post("/api/v1/git/init", git_init)
    app.router.add_post("/api/v1/git/pr-url", git_pr_url)
