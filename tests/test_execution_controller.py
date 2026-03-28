import pytest

from orb.runtime.execution_controller import (
    ControllerAction,
    ControllerContext,
    DefaultExecutionController,
)


@pytest.mark.asyncio
async def test_planning_fallback_when_fanout_exceeds_limit():
    controller = DefaultExecutionController()

    decision = await controller.evaluate(ControllerContext(
        topology_id="hierarchy",
        stage="planning",
        fanout=4,
        max_fanout=3,
        metadata={"compact_topology": "triad"},
    ))

    assert decision.action == ControllerAction.FALLBACK_TOPOLOGY
    assert decision.topology_id == "triad"


@pytest.mark.asyncio
async def test_planning_escalates_pinned_topology_to_recommended_one():
    controller = DefaultExecutionController()

    decision = await controller.evaluate(ControllerContext(
        topology_id="triad",
        stage="planning",
        escalation_allowed=True,
        metadata={
            "requested_topology": "triad",
            "recommended_topology": "hierarchy",
            "escalation_reason": "Task requires broader coordination.",
        },
    ))

    assert decision.action == ControllerAction.FALLBACK_TOPOLOGY
    assert decision.topology_id == "hierarchy"
    assert decision.reason == "Task requires broader coordination."


@pytest.mark.asyncio
async def test_completion_stop_early_after_first_completion():
    controller = DefaultExecutionController()

    decision = await controller.evaluate(ControllerContext(
        topology_id="triad",
        stage="completion",
        stop_early_allowed=True,
        metadata={
            "completion_count": 1,
            "completed_by": "coder",
            "stop_early_reason": "One strong completion is enough.",
        },
    ))

    assert decision.action == ControllerAction.STOP_EARLY
    assert decision.target == "coder"
    assert decision.reason == "One strong completion is enough."
