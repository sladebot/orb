"""Multi-tenant runtime manager.

`RuntimeManager` owns the shared configuration of an Orb daemon (providers,
topology classifier, global subscriber pool) and a registry of per-session
`GraphRuntime` instances. Sessions are keyed by `session_id`; each one has
its own conversation, FSM, workdir, and dashboard state. The manager
dispatches per-session API calls to the right runtime.

This is the server-side control plane for Option B (true multi-tenant
daemon). The old single-session model becomes "a manager with one session
in its registry".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orb.agent.compaction import DEFAULT_COMPACTOR, CompactionStrategy

from .graph_runtime import BroadcastFn, GraphRuntime
from .topology_classifier import TopologyClassifier

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _registry_path() -> Path:
    # Daemon-scoped index so sessions created in one daemon process can be
    # resurrected after a restart. Lives under the daemon's fixed anchor
    # (~/.orb/daemon/) so the registry survives --workdir changes and
    # process CWD moves.
    from orb.cli.paths import daemon_registry_file
    return daemon_registry_file()


class RuntimeManager:
    """Shared daemon config + a registry of active sessions.

    Each `create_session` call returns a fresh `GraphRuntime` initialised
    with the manager's provider pool and config. Multiple sessions can be
    active concurrently, each with its own workdir.
    """

    def __init__(self, compactor: CompactionStrategy | None = None) -> None:
        # Shared configuration; mirrors the fields that used to live on a
        # singleton GraphRuntime. Sessions read these via `self.manager`.
        self._all_providers: dict = {}
        self._providers: dict = {}
        self._enabled_providers: list[str] = []
        self._config = None
        self._model_overrides = None
        self._tier_override = None
        self._compactor = compactor or DEFAULT_COMPACTOR
        self._topology_classifier: TopologyClassifier | None = None

        # Broadcast pool — session.broadcast fans out through here, tagging
        # every payload with the originating session_id so clients can
        # multiplex multiple sessions on one WebSocket.
        self._subscribers: set[BroadcastFn] = set()

        # Session registry — keyed by session_id. Order-of-insertion is
        # preserved so "most recently created" queries stay cheap.
        self._sessions: dict[str, GraphRuntime] = {}

    # ── Shared configuration ────────────────────────────────────────────

    def configure(
        self,
        providers: dict,
        config: Any,
        model_overrides: Any,
        tier_override: Any,
    ) -> None:
        """Set the daemon-wide provider pool + orchestrator config.

        Propagates to every currently-registered session so an in-flight
        run sees the same provider dict as a run started after configure().
        """
        self._all_providers = dict(providers)
        self._providers = dict(providers)
        self._enabled_providers = list(providers.keys())
        self._config = config
        self._model_overrides = model_overrides
        self._tier_override = tier_override
        # Fan the config out to existing sessions so they don't go stale.
        for session in self._sessions.values():
            session.configure(providers, config, model_overrides, tier_override)

    def set_topology_classifier(self, classifier: TopologyClassifier) -> None:
        self._topology_classifier = classifier
        for session in self._sessions.values():
            session.set_topology_classifier(classifier)

    # ── Session lifecycle ──────────────────────────────────────────────

    def create_session(
        self,
        *,
        workdir: str | None = None,
        session_path: "Path | None" = None,
        topology: str | None = None,
        agent_models: dict[str, str] | None = None,
        model_pin: str | None = None,
    ) -> GraphRuntime:
        """Spin up a new per-session runtime.

        If `workdir` is provided, the session's conversation is scoped to
        that folder — file writes and tool calls land there regardless of
        what CWD the daemon was launched from.

        If `topology` is an explicit id (not "auto"), it's pinned onto
        the conversation session immediately along with any supplied
        `agent_models` and `model_pin`. The first `/runs` call then
        skips the classifier via the lock-reuse path and spawns the
        orchestrator directly into the pre-declared graph. The dashboard
        paints the topology/agents immediately because the init event
        synthesizes them from the topology spec even before the first
        run.

        When ``session_path`` is omitted we allocate a fresh path under
        ``.orb/sessions/`` so concurrent sessions don't collide on a
        shared ``session.json`` on disk. The path uses the brand-new
        session's uuid so there's no chance of two sessions picking the
        same file.
        """
        if session_path is None:
            # Seed a throwaway runtime just to get a fresh uuid, then
            # discard it — we'll construct the real runtime with the
            # pre-allocated path so _load_session reads from disk exactly
            # once against the right file.
            from .transcript import ConversationSession as _CS
            fresh_id = _CS().session_id
            from pathlib import Path as _P
            base = _P(workdir) if workdir else _P.cwd()
            session_path = base / ".orb" / "sessions" / f"{fresh_id}.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
        runtime = GraphRuntime(session_path=session_path, compactor=self._compactor)
        if self._providers:
            runtime.configure(
                self._all_providers,
                self._config,
                self._model_overrides,
                self._tier_override,
            )
        if self._topology_classifier is not None:
            runtime.set_topology_classifier(self._topology_classifier)
        # Forward this session's broadcasts through the manager's pool so
        # any subscriber attached to the daemon sees events from any session.
        runtime.subscribe(self._forward_broadcast)
        # Scope to workdir. Also sync DashboardState so the prewarm
        # snapshot (persisted below) carries the workdir — otherwise the
        # first /state fetch reads an empty-workdir snapshot off disk and
        # the UI paints "—" instead of the folder the user picked.
        if workdir:
            runtime._conversation_session.workdir = workdir  # noqa: SLF001
            runtime._sync_session_state()  # noqa: SLF001
        # Pin topology + per-agent models onto the session so the first
        # run goes straight through the graph without re-classifying.
        if topology and topology != "auto":
            from orb.topologies import normalize_topology_id, get_loader
            normalized = normalize_topology_id(topology)
            if normalized in get_loader().list_ids():
                runtime._conversation_session.locked_topology = normalized  # noqa: SLF001
                if agent_models:
                    runtime._conversation_session.locked_agent_models = {  # noqa: SLF001
                        str(r): str(m) for r, m in agent_models.items() if r and m
                    }
                if model_pin and model_pin != "auto":
                    runtime._conversation_session.locked_model_pin = model_pin  # noqa: SLF001
                # Paint the topology up-front on the dashboard state so the
                # graph panel renders immediately, without waiting for the
                # first run to populate `state.topology_*` / `state.agents`.
                runtime._prewarm_topology_view(normalized)  # noqa: SLF001
        session_id = runtime._conversation_session.session_id  # noqa: SLF001
        self._sessions[session_id] = runtime
        self._persist_registry_entry(session_id, workdir, str(session_path))
        logger.info(
            "session created id=%s workdir=%s topology=%s",
            session_id, workdir or "<cwd>", topology or "auto",
        )
        return runtime

    def _persist_registry_entry(
        self, session_id: str, workdir: str | None, session_path: str
    ) -> None:
        """Write {session_id -> {workdir, session_path}} to a daemon-scoped
        index so a restarted daemon can resurrect sessions from disk when
        the dashboard refreshes with a stale URL.
        """
        path = _registry_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            registry: dict = {}
            if path.exists():
                try:
                    registry = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    registry = {}
            registry[session_id] = {
                "workdir": workdir or "",
                "session_path": session_path,
            }
            path.write_text(json.dumps(registry, indent=2))
        except OSError as exc:
            logger.warning("registry write failed for %s: %s", session_id, exc)

    def _lookup_registry(self, session_id: str) -> dict | None:
        path = _registry_path()
        if not path.exists():
            return None
        try:
            registry = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return registry.get(session_id)

    def _drop_registry_entry(self, session_id: str) -> None:
        path = _registry_path()
        if not path.exists():
            return
        try:
            registry = json.loads(path.read_text())
            if session_id in registry:
                registry.pop(session_id, None)
                path.write_text(json.dumps(registry, indent=2))
        except (json.JSONDecodeError, OSError):
            pass

    def try_restore(self, session_id: str) -> GraphRuntime | None:
        """Resurrect a session from disk if its id is in the registry.

        Returns the live runtime (also inserted into ``_sessions``) or
        ``None`` if we have no record of that id. The resurrected session
        can be browsed (read-only view of persisted state) and, because the
        provider pool is re-applied, can also accept new runs.
        """
        if session_id in self._sessions:
            return self._sessions[session_id]
        entry = self._lookup_registry(session_id)
        if not entry:
            return None
        session_path = Path(entry.get("session_path", ""))
        workdir = entry.get("workdir", "") or None
        if not session_path:
            return None
        # If BOTH the session file AND the dashboard snapshot are gone,
        # the session is truly dead — drop the stale index entry. Both
        # live under the session's state dir now; session.workdir is the
        # user's repo, which Orb no longer writes into.
        from orb.cli.paths import session_state_dir
        state_dir = session_state_dir(session_id)
        snapshot_file = state_dir / "snapshot.json"
        dashboard_file = state_dir / "dashboard.json"
        if not session_path.exists() and not snapshot_file.exists() and not dashboard_file.exists():
            self._drop_registry_entry(session_id)
            return None
        try:
            runtime = GraphRuntime(session_path=session_path, compactor=self._compactor)
            if workdir:
                runtime._conversation_session.workdir = workdir  # noqa: SLF001
                runtime._sync_session_state()  # noqa: SLF001
            if self._providers:
                runtime.configure(
                    self._all_providers,
                    self._config,
                    self._model_overrides,
                    self._tier_override,
                )
            if self._topology_classifier is not None:
                runtime.set_topology_classifier(self._topology_classifier)
            runtime.subscribe(self._forward_broadcast)
            # Scrub any stale in-flight markers left by the daemon that
            # originally wrote this snapshot; otherwise the UI shows a
            # phantom running state the fresh runtime can't back up.
            recovered = runtime.recover_stale_run_state()
            self._sessions[session_id] = runtime
            logger.info(
                "session restored from disk id=%s workdir=%s recovered_stale=%s",
                session_id, workdir or "<cwd>", recovered,
            )
            return runtime
        except Exception:  # noqa: BLE001
            logger.exception("failed to restore session %s", session_id)
            return None

    def get_session(self, session_id: str) -> GraphRuntime | None:
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Tear down a session.

        Cancels any in-flight run and removes the session from the registry.
        Returns True if the session existed, False otherwise.
        """
        runtime = self._sessions.pop(session_id, None)
        if runtime is None:
            return False
        # Unhook the forwarder so a future broadcast from a stopping task
        # doesn't try to fan out through a dead session.
        try:
            runtime.unsubscribe(self._forward_broadcast)
        except Exception:  # noqa: BLE001
            pass
        # Fire a stop through the runtime's normal path so the FSM
        # cleans up. We don't await here — the task will drain on its own.
        try:
            runtime._fsm.maybe_fire("stop_requested")  # noqa: SLF001
            if runtime._run_task is not None:  # noqa: SLF001
                runtime._run_task.cancel()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            logger.exception("failed to cancel session %s on delete", session_id)
        self._drop_registry_entry(session_id)
        logger.info("session deleted id=%s", session_id)
        return True

    def list_sessions(self) -> list[GraphRuntime]:
        return list(self._sessions.values())

    def list_known_sessions(self) -> list[dict]:
        """Merged view of in-memory + on-disk sessions for the resume UI.

        Each entry:
        ``{session_id, workdir, snapshot_path, active, updated_at, run_count}``.
        Active sessions are those currently loaded in this daemon; the rest
        come from the registry and can be restored via ``try_restore()``.
        Sorted most-recent-updated-first so the picker shows useful options.
        """
        from orb.cli.paths import session_state_dir
        import time as _time

        summaries: dict[str, dict] = {}

        for sid, runtime in self._sessions.items():
            sess = runtime._conversation_session  # noqa: SLF001
            summaries[sid] = {
                "session_id": sid,
                "workdir": sess.workdir or "",
                "active": True,
                "updated_at": float(sess.updated_at or 0.0),
                "user_turns": sess.user_turn_count(),
                "locked_topology": sess.locked_topology or "",
            }

        registry_path = _registry_path()
        if registry_path.exists():
            try:
                registry = json.loads(registry_path.read_text())
            except (json.JSONDecodeError, OSError):
                registry = {}
            if isinstance(registry, dict):
                for sid, entry in registry.items():
                    if sid in summaries:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    workdir = str(entry.get("workdir") or "")
                    state_dir = session_state_dir(sid)
                    snapshot_path = state_dir / "snapshot.json"
                    if not snapshot_path.exists():
                        continue
                    try:
                        updated_at = float(snapshot_path.stat().st_mtime)
                    except OSError:
                        updated_at = 0.0
                    summaries[sid] = {
                        "session_id": sid,
                        "workdir": workdir,
                        "active": False,
                        "updated_at": updated_at,
                        "user_turns": 0,  # not loaded; don't lie about it
                        "locked_topology": "",
                    }

        items = list(summaries.values())
        items.sort(key=lambda s: float(s.get("updated_at") or 0.0), reverse=True)
        return items

    def active_session_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.is_run_in_flight)

    # ── Shared introspection helpers ────────────────────────────────────

    def models_payload(self) -> dict:
        """Daemon-wide model catalog.

        Takes the first session's payload shape (all sessions share the
        same provider config on this manager) or returns an empty list
        when no sessions exist yet.
        """
        for session in self._sessions.values():
            return session.models_payload()
        # Bootstrap: synthesize via a throwaway session so the API works
        # even before create_session() has been called.
        bootstrap = GraphRuntime(session_path=None)
        if self._providers:
            bootstrap.configure(self._all_providers, self._config, self._model_overrides, self._tier_override)
        return bootstrap.models_payload()

    def settings_payload(self) -> dict:
        for session in self._sessions.values():
            return session.settings_payload()
        bootstrap = GraphRuntime(session_path=None)
        if self._providers:
            bootstrap.configure(self._all_providers, self._config, self._model_overrides, self._tier_override)
        return bootstrap.settings_payload()

    async def refresh_provider_catalogs(self) -> dict[str, str]:
        """Refresh all provider catalogs from upstream.

        Uses any available session (or a bootstrap) as the execution
        context; the resulting updates are pushed back into the manager's
        shared config.
        """
        session = next(iter(self._sessions.values()), None)
        if session is None:
            session = GraphRuntime(session_path=None)
            if self._providers:
                session.configure(self._all_providers, self._config, self._model_overrides, self._tier_override)
        return await session.refresh_provider_catalogs()

    def list_trace_sessions(self) -> dict:
        """Aggregate trace index across all sessions in the registry.

        Trace files live per-session on disk at
        `<workdir>/.orb/traces/`. This union view lets the trace browser
        show runs from every session without requiring the caller to
        enumerate session_ids first.
        """
        if not self._sessions:
            bootstrap = GraphRuntime(session_path=None)
            return bootstrap.list_trace_sessions()

        # Merge every registered session's workspace-scoped view. Each
        # session scans its own .orb/sessions/ dir, so the union covers
        # multiple workdirs; de-dupe by session_id to avoid double-listing
        # when two runtimes share a workdir.
        merged: dict[str, dict] = {}
        current_session_id = ""
        for session in self._sessions.values():
            view = session.list_trace_sessions()
            if not current_session_id:
                current_session_id = str(view.get("current_session_id") or "")
            for summary in view.get("sessions") or []:
                if not isinstance(summary, dict):
                    continue
                sid = str(summary.get("session_id") or "")
                if not sid:
                    continue
                existing = merged.get(sid)
                if existing is None or float(summary.get("updated_at") or 0.0) > float(existing.get("updated_at") or 0.0):
                    merged[sid] = summary
        summaries = list(merged.values())
        summaries.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
        return {"sessions": summaries, "current_session_id": current_session_id}

    def list_session_traces(self, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            # Even when the session isn't in the active registry, trace
            # files still live on disk — delegate to a bootstrap so the
            # caller can browse historical runs.
            session = GraphRuntime(session_path=None)
        return session.list_session_traces(session_id)

    def get_trace_payload(self, run_id: str) -> dict | None:
        for session in self._sessions.values():
            payload = session.get_trace_payload(run_id)
            if payload is not None:
                return payload
        bootstrap = GraphRuntime(session_path=None)
        return bootstrap.get_trace_payload(run_id)

    # ── Broadcast plumbing ──────────────────────────────────────────────

    def subscribe(self, callback: BroadcastFn) -> None:
        self._subscribers.add(callback)

    def unsubscribe(self, callback: BroadcastFn) -> None:
        self._subscribers.discard(callback)

    async def _forward_broadcast(self, data: str) -> None:
        """Session → Manager broadcast bridge.

        Every per-session broadcast calls this; the manager then fans out
        to every subscriber it knows about. Subscribers that want to
        filter by session_id should parse the JSON payload themselves.
        """
        stale: list[BroadcastFn] = []
        # Snapshot: a subscriber callback may (un)subscribe mid-broadcast,
        # which would otherwise raise "Set changed size during iteration".
        for callback in list(self._subscribers):
            try:
                await callback(data)
            except Exception:  # noqa: BLE001
                stale.append(callback)
        for callback in stale:
            self._subscribers.discard(callback)
