from __future__ import annotations

from pathlib import Path

import pytest

from orb.messaging.message import Message, MessageType
from orb.runtime.graph_runtime import GraphRuntime
from orb.runtime.transcript import ConversationSession
from orb.tracing import RunTrace


class _DummyTask:
    def done(self) -> bool:
        return False


class _DummyChannel:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, msg: Message) -> None:
        self.messages.append(msg)


class _DummyAgent:
    def __init__(self) -> None:
        self.channel = _DummyChannel()


class TestConversationSession:
    def test_round_trip_preserves_compactions_and_carryover(self, tmp_path: Path):
        session = ConversationSession()
        session.add_message(Message(from_="user", to="coordinator", type=MessageType.TASK, payload="build it"))
        session.add_completion("coder", "done")
        session.agent_carryover = {"coder": [{"role": "assistant", "content": "cached"}]}
        session.apply_compaction("Earlier context summary", preserve_recent_turns=1)

        path = tmp_path / "session.json"
        session.save(path)
        loaded = ConversationSession.load(path)

        assert loaded.compactions[-1].summary == "Earlier context summary"
        assert loaded.agent_carryover["coder"][0]["content"] == "cached"
        assert loaded.user_turn_count() == 0

    def test_round_trip_preserves_workdir_and_topology_lock(self, tmp_path: Path):
        session = ConversationSession(
            workdir="/tmp/some-repo",
            locked_topology="triad",
            locked_agent_models={"coordinator": "claude-sonnet", "coder": "claude-opus"},
            locked_model_pin="cloud_fast",
        )
        path = tmp_path / "session.json"
        session.save(path)
        loaded = ConversationSession.load(path)

        assert loaded.workdir == "/tmp/some-repo"
        assert loaded.locked_topology == "triad"
        assert loaded.locked_agent_models == {
            "coordinator": "claude-sonnet",
            "coder": "claude-opus",
        }
        assert loaded.locked_model_pin == "cloud_fast"


class TestGraphRuntimeSession:
    def test_runtime_default_session_paths_are_per_session(self, tmp_path: Path, monkeypatch):
        """Session state lands at ``~/.orb/daemon/sessions/{sid}/snapshot.json``,
        keyed by session_id — not under the user's CWD.
        """
        home = tmp_path / "home"; home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(tmp_path)

        runtime = GraphRuntime()
        first_path = runtime._resolved_session_path()  # noqa: SLF001
        first_id = runtime._conversation_session.session_id  # noqa: SLF001
        runtime._persist_session()  # noqa: SLF001

        expected = home / ".orb" / "daemon" / "sessions" / first_id / "snapshot.json"
        assert first_path == expected
        assert first_path.exists()
        # No state written under the user's CWD any more.
        assert not (tmp_path / ".orb").exists()

    @pytest.mark.asyncio
    async def test_new_session_uses_new_session_file_by_default(self, tmp_path: Path, monkeypatch):
        home = tmp_path / "home"; home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(tmp_path)

        runtime = GraphRuntime()
        first_path = runtime._resolved_session_path()  # noqa: SLF001
        runtime._persist_session()  # noqa: SLF001

        status, payload = await runtime.new_session()

        second_path = runtime._resolved_session_path()  # noqa: SLF001
        assert status == 200
        assert payload["ok"] is True
        assert second_path != first_path
        assert second_path.exists()
        # Both snapshots live under ~/.orb/daemon/sessions/, in different sid dirs.
        daemon_sessions = home / ".orb" / "daemon" / "sessions"
        assert daemon_sessions in first_path.parents
        assert daemon_sessions in second_path.parents

    def test_runtime_init_event_uses_persisted_user_turn_count(self, tmp_path: Path):
        path = tmp_path / "session.json"
        session = ConversationSession()
        session.add_message(Message(from_="user", to="coordinator", type=MessageType.TASK, payload="task 1"))
        session.add_message(Message(from_="coordinator", to="coder", type=MessageType.TASK, payload="route"))
        session.add_message(Message(from_="user", to="reviewer", type=MessageType.RESPONSE, payload="task 2"))
        session.save(path)

        runtime = GraphRuntime(session_path=path)
        runtime.state.routing = {"task_type": "review", "reason": "qa run"}
        event = runtime.current_init_event()

        assert event["session_turn"] == 2
        # A session without an explicit workdir must NOT leak Path.cwd()
        # into the dashboard payload — that used to surface the daemon's
        # launch dir in the breadcrumb for every unscoped session.
        assert event["workdir"] == ""
        assert event["plan"]["routing"]["task_type"] == "review"

    def test_runtime_init_event_surfaces_session_topology_lock(self, tmp_path: Path):
        """The init event's ``session`` block must carry ``locked_topology``
        and ``locked_agent_models`` so both TUI + dashboard can render
        "pinned" affordances instead of silently no-opping ``/topology``.
        """
        path = tmp_path / "session.json"
        session = ConversationSession(
            locked_topology="triad",
            locked_agent_models={"coordinator": "opus", "coder": "sonnet"},
            locked_model_pin="auto",
        )
        session.save(path)

        runtime = GraphRuntime(session_path=path)
        event = runtime.current_init_event()

        session_block = event.get("session") or {}
        assert session_block.get("locked_topology") == "triad"
        assert session_block.get("locked_agent_models") == {
            "coordinator": "opus",
            "coder": "sonnet",
        }
        # Runtime layer also includes the pin mode for the dashboard picker.
        assert session_block.get("locked_model_pin") == "auto"

    def test_runtime_init_event_empty_lock_for_fresh_session(self, tmp_path: Path):
        """Fresh sessions (pre-first-run) still surface the lock field shape
        so clients can check ``locked_topology`` without a ``KeyError``."""
        path = tmp_path / "session.json"
        session = ConversationSession()
        session.save(path)

        runtime = GraphRuntime(session_path=path)
        event = runtime.current_init_event()
        session_block = event.get("session") or {}
        assert session_block.get("locked_topology") == ""
        assert session_block.get("locked_agent_models") == {}

    def test_runtime_resolves_mentions_server_side(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")

        target, text = runtime._resolve_conversation_target(  # noqa: SLF001
            "@coder fix the failing test",
            default_target="coordinator",
            known_targets={"coder", "reviewer"},
        )

        assert target == "coder"
        assert text == "fix the failing test"

    @pytest.mark.asyncio
    async def test_inject_message_routes_to_mentioned_agent_and_persists_session(self, tmp_path: Path):
        session_path = tmp_path / "session.json"
        runtime = GraphRuntime(session_path=session_path)
        runtime._run_task = _DummyTask()  # noqa: SLF001
        runtime._agents = {  # noqa: SLF001
            "coordinator": _DummyAgent(),
            "coder": _DummyAgent(),
        }
        # Drive the FSM into RUNNING so the .running property is True —
        # the refactor made the FSM authoritative instead of inferring from _run_task.
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001

        status, payload = await runtime.inject_message("coordinator", "@coder fix src/app.py")

        assert status == 200
        assert payload["ok"] is True
        coder_channel = runtime._agents["coder"].channel  # noqa: SLF001
        assert coder_channel.messages[-1].payload == "fix src/app.py"

        loaded = ConversationSession.load(session_path)
        assert loaded.user_turn_count() == 1
        assert loaded.turns[-1].audience == "coder"

    @pytest.mark.asyncio
    async def test_inject_message_uses_task_type_and_depth_zero(self, tmp_path: Path):
        """A user-injected message is a TASK (not a RESPONSE) at depth 0.

        Regression guard: CLAUDE.md parity rule calls out `MessageType.TASK →
        RESPONSE` drift explicitly. The synthetic Message + broadcast payload
        must agree: both must advertise the task as `task`.
        """
        import json as _json

        session_path = tmp_path / "session.json"
        runtime = GraphRuntime(session_path=session_path)
        runtime._run_task = _DummyTask()  # noqa: SLF001
        runtime._agents = {  # noqa: SLF001
            "coordinator": _DummyAgent(),
            "coder": _DummyAgent(),
        }
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001

        broadcasts: list[dict] = []

        async def _sink(raw: str) -> None:
            broadcasts.append(_json.loads(raw))

        runtime.subscribe(_sink)

        status, payload = await runtime.inject_message("coder", "please land the patch")
        assert status == 200
        assert payload["ok"] is True

        # The synthetic Message created for the agent channel must be a TASK.
        coder_channel = runtime._agents["coder"].channel  # noqa: SLF001
        injected = coder_channel.messages[-1]
        assert injected.type == MessageType.TASK
        assert injected.depth == 0
        assert injected.from_ == "user"
        assert injected.to == "coder"

        # The broadcast payload must agree: msg_type=task, depth=0,
        # chain_id matches the stored Message, from=user, to=coder.
        message_events = [b for b in broadcasts if b.get("type") == "message"]
        assert message_events, f"no broadcast message event found: {broadcasts}"
        ev = message_events[-1]
        assert ev["msg_type"] == "task", f"user-injected must be a task, got {ev['msg_type']}"
        assert ev["depth"] == 0
        assert ev["from"] == "user"
        assert ev["to"] == "coder"
        assert ev["chain_id"] == injected.chain_id
        assert ev["content"] == "please land the patch"

    @pytest.mark.asyncio
    async def test_classifier_plan_step_emits_before_llm_call(self, tmp_path: Path, monkeypatch):
        """The ``classifier`` plan_step must be broadcast BEFORE the
        classifier LLM call starts — otherwise the TUI shows a blank
        screen for the 5–15s the ``predict_topology`` await takes.

        Regression: prior behaviour only emitted plan_step AFTER the
        classifier returned (``routing`` / ``topology`` / ``allocator``),
        so the user saw nothing between "Starting run planning" and the
        classifier finishing.
        """
        import asyncio
        import json as _json

        monkeypatch.chdir(tmp_path)
        runtime = GraphRuntime()
        # start_run rejects if no providers are configured (test env has
        # none). We don't actually call any provider — predict_topology
        # and _run_orchestrator are both stubbed — so a sentinel suffices.
        runtime._providers = [object()]  # noqa: SLF001

        broadcasts: list[dict] = []

        async def _sink(raw: str) -> None:
            broadcasts.append(_json.loads(raw))

        runtime.subscribe(_sink)

        # Snapshot what the runtime had broadcast as of the moment
        # ``predict_topology`` is invoked. A sentinel — the classifier
        # plan_step must already be in ``broadcasts`` by then.
        seen_at_call: list[dict] = []

        async def _fake_predict(query, *, model_pin="", requested_topology="auto"):  # noqa: ARG001
            # Capture state at the start of the would-be LLM call.
            seen_at_call.extend(broadcasts)
            # Simulate a slow LLM response to make the blank-window
            # regression observable (if we broadcast before awaiting,
            # the TUI sees the plan_step during this sleep).
            await asyncio.sleep(0.05)
            return {
                "topology": "triad",
                "task_type": "coding",
                "summary": "",
                "reason": "fake",
                "complexity": 50,
                "candidates": [],
                "options": [],
                "signals": {},
            }

        monkeypatch.setattr(runtime, "predict_topology", _fake_predict)

        # Keep the orchestrator from actually running — we only care
        # about what's broadcast during planning.
        async def _noop_orch(*args, **kwargs):  # noqa: ARG001
            runtime._fsm.maybe_fire("orchestrator_succeeded")  # noqa: SLF001

        monkeypatch.setattr(runtime, "_run_orchestrator", _noop_orch)

        status, _payload = await runtime.start_run(query="fix a bug", topology="auto")
        assert status == 200

        # The classifier plan_step must have been broadcast BEFORE
        # predict_topology was entered — not just at some later point.
        classifier_steps = [
            b for b in seen_at_call
            if b.get("type") == "plan_step" and b.get("stage") == "classifier"
        ]
        assert classifier_steps, (
            "No 'classifier' plan_step was broadcast before predict_topology was called — "
            f"the TUI would see a blank gap. Broadcasts captured at call time: "
            f"{[b.get('stage') for b in seen_at_call if b.get('type') == 'plan_step']}"
        )

    @pytest.mark.asyncio
    async def test_explicit_topology_skips_classifier_llm(self, tmp_path: Path, monkeypatch):
        """When the caller passes an explicit topology — any non-'auto'
        value — ``start_run`` must skip the classifier LLM round-trip
        entirely and synthesize the prediction locally via
        ``_manual_prediction``. The user has already decided the shape
        of the graph; re-deciding via an LLM buys nothing and costs a
        3–15s blank window per submit.

        Pair this with the trivial-query short-circuit in the classifier
        for safety on the auto path — together they cover:
          - auto + trivial: heuristic synth, no LLM
          - auto + non-trivial: LLM classifier (needs to decide topology)
          - explicit + anything: manual_prediction, no LLM

        Regression guard for the earlier reverted attempt that broke
        triad on trivial queries by skipping the classifier WITHOUT a
        triviality safety net.
        """
        import asyncio  # noqa: F401 — imported in parent; re-import for clarity
        import json as _json

        monkeypatch.chdir(tmp_path)
        runtime = GraphRuntime()
        runtime._providers = [object()]  # noqa: SLF001

        predict_called = False

        async def _exploding_predict(*args, **kwargs):  # noqa: ARG001
            nonlocal predict_called
            predict_called = True
            raise AssertionError(
                "predict_topology was called with explicit topology — "
                "the LLM classifier should NOT have run."
            )

        monkeypatch.setattr(runtime, "predict_topology", _exploding_predict)

        async def _noop_orch(*args, **kwargs):  # noqa: ARG001
            runtime._fsm.maybe_fire("orchestrator_succeeded")  # noqa: SLF001

        monkeypatch.setattr(runtime, "_run_orchestrator", _noop_orch)

        status, payload = await runtime.start_run(
            query="refactor the authentication module into a factory pattern",
            topology="triad",
        )
        assert status == 200, (status, payload)
        assert payload.get("ok") is True
        assert predict_called is False

    def test_trace_indexes_are_session_aware(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        runtime = GraphRuntime()
        runtime._conversation_session = ConversationSession()
        trace = RunTrace(session_id=runtime._conversation_session.session_id)
        trace.record_topology_choice("triad", reason="test")
        trace.record_final_outcome(success=True, result="ok")
        runtime._last_trace = trace  # noqa: SLF001

        runtime._persist_run_trace()  # noqa: SLF001

        sessions = runtime.list_trace_sessions()
        assert sessions["current_session_id"] == runtime._conversation_session.session_id
        session_runs = runtime.list_session_traces(runtime._conversation_session.session_id)
        assert len(session_runs["runs"]) == 1
        assert session_runs["runs"][0]["run_id"] == trace.run_id

        payload = runtime.get_trace_payload(trace.run_id)
        assert payload is not None
        assert payload["summary"]["session_id"] == runtime._conversation_session.session_id
        assert payload["summary"]["routing_reason"] == "test"
