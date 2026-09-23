from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SKILL = Path(__file__).parents[1] / "skills" / "orb-integration"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("task", "expected"), [
    ("Compare alternative architectures and reconcile the tradeoffs", "council"),
    ("Use a council to debate two approaches", "council"),
    ("Coordinate backend and frontend specialists in a peer network", "specialist-network"),
    ("Fix a typo in the council documentation", "solo"),
    ("Implement pagination", "implement-review"),
])
def test_selection_matches_coordination_problem(task, expected):
    assert load_script("select_topology").select_topology(task) == expected


def test_explicit_topology_overrides_adaptive_selection():
    assert load_script("select_topology").select_topology(
        "Compare alternatives", explicit="specialist-network"
    ) == "specialist-network"


def test_all_topology_steps_and_edges_reference_real_roles():
    for path in (SKILL / "references" / "topologies").glob("*.json"):
        topology = json.loads(path.read_text())
        roles = set(topology["roles"])
        for source, target in topology["visual"]["edges"]:
            assert source in roles and target in roles
        for step in topology["steps"]:
            for field in ("role", "to"):
                if field in step:
                    assert step[field] in roles
            for field in ("for", "from"):
                assert set(step.get(field, [])) <= roles


def completed_record():
    return {
        "status": "completed", "round": 2, "max_rounds": 3,
        "participants": {"a": {"role": "proposer", "model": "host-default"},
                         "b": {"role": "critic", "model": "host-default"}},
        "claims": [
            {"id": "c1", "node": "a", "statement": "Cache reduces repeated work", "evidence": []},
            {"id": "c2", "node": "b", "statement": "Invalidation must be verified", "evidence": ["tests/cache.txt"]},
        ],
        "disagreements": [{"id": "d1", "claims": ["c1", "c2"], "blocking": True,
                           "status": "resolved", "resolution": "Use versioned keys",
                           "evidence": ["tests/cache.txt"]}],
        "decision": {"summary": "Use versioned caching", "evidence": ["tests/cache.txt"],
                     "remaining_disagreements": []},
    }


def test_completed_record_and_same_model_fallback_are_valid():
    assert load_script("validate_council").validate(completed_record()) == []


@pytest.mark.parametrize("mutation", ["blocking", "no_evidence", "unknown_claim", "duplicate_claim", "unknown_node", "round_limit", "bool_round", "false_resolution"])
def test_cannot_claim_unsupported_completion(mutation):
    record = completed_record()
    if mutation == "blocking":
        record["disagreements"][0]["status"] = "open"
    elif mutation == "no_evidence":
        record["decision"]["evidence"] = []
    elif mutation == "unknown_claim":
        record["disagreements"][0]["claims"] = ["missing"]
    elif mutation == "duplicate_claim":
        record["claims"].append(copy.deepcopy(record["claims"][0]))
    elif mutation == "unknown_node":
        record["claims"][0]["node"] = "missing"
    elif mutation == "round_limit":
        record["round"] = 4
    elif mutation == "bool_round":
        record["round"] = True
    elif mutation == "false_resolution":
        record["disagreements"][0]["evidence"] = []
    assert load_script("validate_council").validate(record)


def test_nonblocking_dissent_must_be_disclosed_but_need_not_prevent_completion():
    record = completed_record()
    record["disagreements"][0].update(status="open", blocking=False)
    assert load_script("validate_council").validate(record)
    record["decision"]["remaining_disagreements"] = ["d1"]
    assert load_script("validate_council").validate(record) == []


def test_budget_exhaustion_can_stop_honestly_without_resolution():
    record = completed_record()
    record.update(status="stopped", round=3, stop_reason="Round budget exhausted")
    record["disagreements"][0]["status"] = "open"
    record["decision"] = {}
    assert load_script("validate_council").validate(record) == []
    record["status"] = "active"
    assert load_script("validate_council").validate(record)


@pytest.mark.parametrize("record", [None, [], {}, {"participants": [], "claims": 1}])
def test_malformed_input_returns_errors(record):
    assert load_script("validate_council").validate(record)


def test_cli_rejects_invalid_record(tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps({"status": "completed"}))
    result = subprocess.run([sys.executable, str(SKILL / "scripts" / "validate_council.py"), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["valid"] is False
