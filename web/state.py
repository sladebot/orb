from __future__ import annotations

import time
from dataclasses import dataclass, field


# Maximum number of distinct files tracked in ``DashboardState.file_changes``.
# Rewrites of the same path replace in place (path-keyed dedup); distinct paths
# past this cap evict the oldest entry (LRU by last-touched). The counter
# ``file_changes_truncated_count`` surfaces the lifetime eviction count so
# clients can show "N older changes hidden". Exposed as a module constant so
# it can be tuned or monkeypatched in tests.
MAX_FILE_CHANGES: int = 200


@dataclass
class AgentState:
    node_id: str
    role: str
    status: str = "idle"
    model: str = ""
    completed_result: str = ""
    msg_count: int = 0
    complexity: int = 0
    last_heartbeat: float = 0.0


@dataclass
class EdgeState:
    source: str
    target: str


@dataclass
class MessageRecord:
    id: str
    from_: str
    to: str
    content: str
    model: str
    depth: int
    elapsed: float
    chain_id: str
    msg_type: str
    context_slice: list[str] = field(default_factory=list)


@dataclass
class ActivityRecord:
    agent: str
    activity: str
    elapsed: float
    details: dict = field(default_factory=dict)


@dataclass
class PlanStepRecord:
    stage: str
    title: str
    detail: str
    elapsed: float


@dataclass
class FileChangeRecord:
    path: str
    agent: str
    content: str
    old_content: str = ""


@dataclass
class DashboardState:
    """Snapshot of the full system state for dashboard rendering."""

    agents: dict[str, AgentState] = field(default_factory=dict)
    edges: list[EdgeState] = field(default_factory=list)
    messages: list[MessageRecord] = field(default_factory=list)
    activity_events: list[ActivityRecord] = field(default_factory=list)
    plan_steps: list[PlanStepRecord] = field(default_factory=list)
    file_changes: list[FileChangeRecord] = field(default_factory=list)
    # Lifetime count of file_changes entries evicted by the ``MAX_FILE_CHANGES``
    # cap. Clients render this as "N older changes hidden". Reset on ``reset()``.
    file_changes_truncated_count: int = 0
    message_count: int = 0
    budget: int = 200
    budget_remaining: int = 200
    start_time: float = field(default_factory=time.time)
    run_query: str = ""
    topology_id: str = ""
    topology_label: str = ""
    topology_description: str = ""
    agent_complexity: dict[str, int] = field(default_factory=dict)
    agent_models: dict[str, str] = field(default_factory=dict)
    agent_neighbors: dict[str, list[str]] = field(default_factory=dict)
    agent_positions: dict[str, str] = field(default_factory=dict)
    graph_view: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)
    final_result: str = ""
    final_agent: str = ""
    final_diff: str = ""
    session_turn: int = 0
    session_id: str = ""
    session_generation: int = 1
    workdir: str = ""
    # Session-level topology/model pin. After the first run completes the
    # runtime pins ``selected_topology`` + per-agent ``agent_models`` onto
    # the conversation session; follow-up runs reuse them instead of
    # re-classifying (see ``GraphRuntime._start_run_planning``). Surfacing
    # the lock here lets both surfaces (TUI + dashboard) render "pinned"
    # affordances instead of silently accepting no-op ``/topology``
    # requests. Empty string / empty dict mean "not yet locked".
    locked_topology: str = ""
    locked_agent_models: dict[str, str] = field(default_factory=dict)
    # Per-session "stage every file write for human approval" toggle.
    # Mirrored from ``ConversationSession.approval_required`` via
    # ``GraphRuntime._sync_session_state`` so both the init event and the
    # live state stay in sync. See ``GraphRuntime.request_write_approval``
    # for the runtime side of the staging pipeline.
    approval_required: bool = False

    def reset(self) -> None:
        """Reset all state back to defaults (called before starting a new run)."""
        self.agents = {}
        self.edges = []
        self.messages = []
        self.activity_events = []
        self.plan_steps = []
        self.file_changes = []
        self.file_changes_truncated_count = 0
        self.message_count = 0
        self.budget_remaining = self.budget
        self.start_time = time.time()
        self.run_query = ""
        self.topology_id = ""
        self.topology_label = ""
        self.topology_description = ""
        self.agent_complexity = {}
        self.agent_models = {}
        self.agent_neighbors = {}
        self.agent_positions = {}
        self.graph_view = {}
        self.routing = {}
        self.final_result = ""
        self.final_agent = ""
        self.final_diff = ""
        self.session_turn = 0
        self.session_id = ""
        self.session_generation = 1
        self.workdir = ""
        self.locked_topology = ""
        self.locked_agent_models = {}
        self.approval_required = False

    def record_file_change(
        self,
        *,
        path: str,
        agent: str,
        content: str,
        old_content: str = "",
    ) -> None:
        """Record a file write, path-keyed with LRU eviction at ``MAX_FILE_CHANGES``.

        Rewrites of the same path replace the existing entry in-place (and
        refresh its LRU position to "most recently touched"). When the cap is
        exceeded by a brand-new path, the oldest entry is evicted and
        ``file_changes_truncated_count`` is incremented so clients can surface
        the fact that older changes were dropped.

        Prefer this helper over appending to ``self.file_changes`` directly so
        the cap isn't silently bypassed.
        """
        if not path:
            return
        record = FileChangeRecord(
            path=path, agent=agent, content=content, old_content=old_content
        )
        # Drop any existing entry for this path so we can re-append at the end
        # (newest position). This gives us path-dedup + LRU-by-last-touched.
        for idx, existing in enumerate(self.file_changes):
            if existing.path == path:
                del self.file_changes[idx]
                break
        self.file_changes.append(record)
        # Evict oldest entries once we exceed the cap.
        while len(self.file_changes) > MAX_FILE_CHANGES:
            self.file_changes.pop(0)
            self.file_changes_truncated_count += 1

    def to_init_event(self) -> dict:
        return {
            "type": "init",
            "plan": {
                "query": self.run_query,
                "topology": {
                    "id": self.topology_id,
                    "label": self.topology_label,
                    "description": self.topology_description,
                },
                "agent_complexity": self.agent_complexity,
                "agent_models": self.agent_models,
                "neighbors": self.agent_neighbors,
                "positions": self.agent_positions,
                "graph_view": self.graph_view,
                "routing": self.routing,
                "workdir": self.workdir,
            },
            "agents": [
                {
                    "id": a.node_id,
                    "role": a.role,
                    "status": a.status,
                    "model": a.model,
                    "msg_count": a.msg_count,
                    "completed_result": a.completed_result,
                    "complexity": a.complexity,
                    "last_heartbeat": a.last_heartbeat,
                }
                for a in self.agents.values()
            ],
            "edges": [{"source": e.source, "target": e.target} for e in self.edges],
            "messages": [
                {
                    "id": m.id,
                    "from": m.from_,
                    "to": m.to,
                    "content": m.content,
                    "model": m.model,
                    "depth": m.depth,
                    "elapsed": m.elapsed,
                    "chain_id": m.chain_id,
                    "msg_type": m.msg_type,
                    "context_slice": m.context_slice,
                }
                for m in self.messages
            ],
            "activity_events": [
                {
                    "agent": a.agent,
                    "activity": a.activity,
                    "elapsed": a.elapsed,
                    "details": a.details,
                }
                for a in self.activity_events
            ],
            "plan_steps": [
                {
                    "stage": s.stage,
                    "title": s.title,
                    "detail": s.detail,
                    "elapsed": s.elapsed,
                }
                for s in self.plan_steps
            ],
            "file_changes": [
                {
                    "path": change.path,
                    "agent": change.agent,
                    "content": change.content,
                    "old_content": change.old_content,
                }
                for change in self.file_changes
            ],
            "file_changes_truncated_count": self.file_changes_truncated_count,
            "stats": {
                "message_count": self.message_count,
                "budget_remaining": self.budget_remaining,
                "elapsed": time.time() - self.start_time,
            },
            "final_result": self.final_result,
            "final_agent": self.final_agent,
            "final_diff": self.final_diff,
            "session_turn": self.session_turn,
            "session_id": self.session_id,
            "session_generation": self.session_generation,
            "workdir": self.workdir,
            # Session-level lock block. ``GraphRuntime._dashboard_snapshot_payload``
            # may extend this with runtime-only fields (``id``, ``workdir``,
            # ``locked_model_pin``) before the payload goes on the wire, but
            # the ``DashboardState`` contract is the stable source of truth
            # for ``locked_topology`` + ``locked_agent_models`` so tests and
            # consumers that read ``to_init_event()`` directly get a
            # consistent shape.
            "session": {
                "locked_topology": self.locked_topology,
                "locked_agent_models": dict(self.locked_agent_models),
            },
            # Top-level so clients (TUI keypress handler, dashboard) can
            # check ``init.approval_required`` without diving into a
            # nested block. The TUI flips its "(staged)" affordance off
            # this single bool.
            "approval_required": bool(self.approval_required),
        }
