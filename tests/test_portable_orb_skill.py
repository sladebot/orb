from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "orb-integration"


def _load_selector():
    path = SKILL / "scripts" / "select_topology.py"
    spec = importlib.util.spec_from_file_location("orb_select_topology", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_is_host_owned_not_daemon_wrapper() -> None:
    body = (SKILL / "SKILL.md").read_text()

    assert "host harness owns" in body.lower()
    assert "orb daemon start" not in body
    assert "references/codex.md" in body
    assert "references/claude-code.md" in body
    assert "references/hermes.md" in body


def test_supported_host_adapters_share_portable_operations() -> None:
    required_operations = {"spawn", "send", "wait", "cancel", "collect"}

    for host in ("codex", "claude-code", "hermes"):
        body = (SKILL / "references" / f"{host}.md").read_text().lower()
        assert required_operations <= {
            operation for operation in required_operations if f"`{operation}`" in body
        }


def test_topologies_follow_the_portable_contract() -> None:
    topology_dir = SKILL / "references" / "topologies"
    expected = {"solo", "implement-review", "research-build", "parallel-review", "council", "specialist-network"}
    found = set()

    for path in topology_dir.glob("*.json"):
        topology = json.loads(path.read_text())
        found.add(topology["id"])
        assert topology["version"] == "1.0"
        assert topology["roles"]
        assert topology["steps"]
        assert topology["completion"]["require"]
        assert set(topology["visual"]["nodes"]) == set(topology["roles"])
        assert isinstance(topology["visual"]["edges"], list)
        for step in topology["steps"]:
            assert step["operation"] in {
                "work",
                "spawn",
                "send",
                "wait",
                "collect",
                "cancel",
            }

    assert found == expected


def test_selector_chooses_smallest_sufficient_topology() -> None:
    selector = _load_selector()

    assert selector.select_topology("Fix typo in README") == "solo"
    assert (
        selector.select_topology("Implement a cache and review the changes")
        == "implement-review"
    )
    assert (
        selector.select_topology(
            "Research the authentication design, implement it, and review security risks"
        )
        == "research-build"
    )
    assert (
        selector.select_topology(
            "Implement this critical migration with independent security and correctness reviews"
        )
        == "parallel-review"
    )


def test_selector_honors_explicit_topology() -> None:
    selector = _load_selector()

    assert selector.select_topology("anything", explicit="solo") == "solo"


def test_renderer_shows_topology_state_as_mermaid() -> None:
    path = SKILL / "scripts" / "render_topology.py"
    spec = importlib.util.spec_from_file_location("orb_render_topology", path)
    assert spec and spec.loader
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)

    topology = json.loads(
        (SKILL / "references" / "topologies" / "implement-review.json").read_text()
    )
    output = renderer.render_mermaid(
        topology,
        {
            "implementer": "completed",
            "reviewer": "running",
        },
    )

    assert output.startswith("flowchart LR")
    assert 'implementer["Implementer · completed"]' in output
    assert 'reviewer["Reviewer · running"]' in output
    assert "implementer --> reviewer" in output
    assert "class reviewer running" in output


def test_visual_protocol_is_required_by_skill_and_adapters() -> None:
    skill = (SKILL / "SKILL.md").read_text().lower()
    assert "render_topology.py" in skill
    assert "visual status" in skill

    for host in ("codex", "claude-code", "hermes"):
        adapter = (SKILL / "references" / f"{host}.md").read_text().lower()
        assert "mermaid" in adapter
        assert "visual" in adapter
