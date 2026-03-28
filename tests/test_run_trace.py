from __future__ import annotations

from orb.tracing import RunTrace, TraceEventKind


class TestRunTrace:
    def test_record_helpers_capture_core_phase_one_events(self):
        trace = RunTrace()
        trace.metadata["topology_id"] = "triad"

        trace.record_topology_choice(
            "planner_executor_verifier",
            reason="risky task",
            task_type="coding",
            candidates=["single_agent", "planner_executor_verifier"],
            data={
                "routing_mode": "llm",
                "classifier_model": "gpt-5.4-mini",
                "classifier_provider": "openai-codex",
                "escalation_allowed": True,
                "stop_early_allowed": False,
                "escalation_reason": "Use a verifier-backed topology for risky work.",
                "stop_early_reason": "",
            },
        )
        trace.record_initial_injection(target="coordinator", message="Build a CLI tool")
        trace.record_message_routed(
            "routed",
            actor="coordinator",
            target="coder",
            message="Write code",
        )
        trace.record_agent_spawn("planner", role="planner", model="claude")
        trace.record_stage_start("planning", actor="planner")
        trace.record_tool_call("read_file", actor="planner", data={"path": "src/app.py"})
        trace.record_retry(actor="planner", stage="planning", attempt=2, message="provider timeout")
        trace.record_verifier_decision("approved", actor="verifier", stage="review")
        trace.record_human_override(actor="human", action="approve", message="override safety gate")
        trace.record_stage_finish("planning", actor="planner", status="ok")
        trace.record_final_outcome(success=True, message="completed", result="done")

        kinds = [event.kind for event in trace.events]
        assert kinds == [
            TraceEventKind.TOPOLOGY_CHOICE,
            TraceEventKind.INITIAL_INJECTION,
            TraceEventKind.MESSAGE_ROUTED,
            TraceEventKind.AGENT_SPAWN,
            TraceEventKind.STAGE_START,
            TraceEventKind.TOOL_CALL,
            TraceEventKind.RETRY,
            TraceEventKind.VERIFIER_DECISION,
            TraceEventKind.HUMAN_OVERRIDE,
            TraceEventKind.STAGE_FINISH,
            TraceEventKind.FINAL_OUTCOME,
        ]
        assert trace.events[0].target == "planner_executor_verifier"
        assert trace.events[1].target == "coordinator"
        assert trace.events[2].actor == "coordinator"
        assert trace.events[3].data["role"] == "planner"
        assert trace.events[5].data["tool_name"] == "read_file"
        assert trace.events[6].data["attempt"] == 2
        assert trace.events[7].data["decision"] == "approved"
        assert trace.events[8].data["action"] == "approve"
        assert trace.summary()["topology_id"] == "planner_executor_verifier"
        assert trace.summary()["agent_ids"] == ["planner"]
        assert trace.summary()["task_type"] == "coding"
        assert trace.summary()["routing_reason"] == "risky task"
        assert trace.summary()["routing_mode"] == "llm"
        assert trace.summary()["classifier_model"] == "gpt-5.4-mini"
        assert trace.summary()["classifier_provider"] == "openai-codex"
        assert trace.summary()["escalation_allowed"] is True
        assert trace.summary()["stop_early_allowed"] is False
        assert trace.summary()["escalation_reason"] == "Use a verifier-backed topology for risky work."
        assert trace.summary()["success"] is True

    def test_trace_json_round_trip_preserves_events(self):
        trace = RunTrace(session_id="session-123", metadata={"run_type": "integration"})
        trace.record_topology_choice("single_agent", reason="simple task")
        trace.record_final_outcome(success=False, error="timeout")

        payload = trace.to_json()
        loaded = RunTrace.from_json(payload)

        assert loaded.session_id == "session-123"
        assert loaded.run_id == trace.run_id
        assert loaded.metadata["run_type"] == "integration"
        assert loaded.events[0].kind == TraceEventKind.TOPOLOGY_CHOICE
        assert loaded.events[1].data["success"] is False

    def test_trace_save_and_load_round_trip(self, tmp_path):
        trace = RunTrace()
        trace.record_agent_spawn("coder", role="coder", model="claude")
        trace.record_final_outcome(success=True, result="ok")

        path = tmp_path / "trace.json"
        trace.save(path)
        loaded = RunTrace.load(path)

        assert loaded.run_id == trace.run_id
        assert loaded.agent_ids() == ["coder"]
        assert loaded.summary()["event_count"] == 2
        assert "outcome=success" in loaded.summary_text()
