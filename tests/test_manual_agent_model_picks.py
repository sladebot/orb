"""User-picked per-agent models must be respected through manual prediction.

Bug: when the Session Config modal pushed a model id like
`claude-opus-4-7` per agent, _manual_prediction emitted
``{provider: "", model: "claude-opus-4-7"}`` with a comment saying the
provider would be resolved downstream. But ``_validate_agent_model_
assignments`` gated on ``if provider and model_id``, so the empty
provider made the check fall through to the heuristic allocator and the
user's pick was silently dropped.
"""
from __future__ import annotations

from orb.llm.types import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_MODEL,
    ANTHROPIC_SONNET_MODEL,
)
from orb.runtime import graph_runtime as runtime_mod
from orb.runtime.graph_runtime import GraphRuntime


def _all_anthropic_enabled_cfg():
    return {
        "anthropic": {
            "enabled": True,
            "models": {
                ANTHROPIC_HAIKU_MODEL: {"enabled": True},
                ANTHROPIC_SONNET_MODEL: {"enabled": True},
                ANTHROPIC_OPUS_MODEL: {"enabled": True},
            },
            "default_models": {
                "cloud_lite": ANTHROPIC_HAIKU_MODEL,
                "cloud_fast": ANTHROPIC_SONNET_MODEL,
                "cloud_strong": ANTHROPIC_OPUS_MODEL,
            },
        },
    }


def test_user_picks_are_respected_when_only_model_id_is_supplied(monkeypatch):
    """Manual-mode assignment with only model_id (no provider) must resolve
    to the user's pick, not fall back to the heuristic allocator.
    """
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: _all_anthropic_enabled_cfg() if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"anthropic": object()}  # noqa: SLF001

    predicted = runtime._manual_prediction(  # noqa: SLF001
        topology="triad",
        agent_models={
            "coder": ANTHROPIC_OPUS_MODEL,
            "reviewer": ANTHROPIC_SONNET_MODEL,
        },
        model_pin="auto",
    )

    # _manual_prediction deliberately leaves provider blank.
    for role, entry in predicted["agent_assignments"].items():
        assert entry["model"], role
        assert entry["provider"] == "", role

    # Heuristic baseline — what the validator would fall back to.
    fallback = runtime._build_agent_model_map(  # noqa: SLF001
        complexity=50,
        topology_id="triad",
        agent_complexity={},
    )

    validated, _ = runtime._validate_agent_model_assignments(  # noqa: SLF001
        "triad",
        predicted["agent_assignments"],
        fallback,
    )

    assert validated["coder"].model_id == ANTHROPIC_OPUS_MODEL, (
        f"coder pick dropped — got {validated['coder'].model_id}"
    )
    assert validated["reviewer"].model_id == ANTHROPIC_SONNET_MODEL, (
        f"reviewer pick dropped — got {validated['reviewer'].model_id}"
    )
    # Provider must be resolved from the model catalog.
    assert validated["coder"].provider == "anthropic"
    assert validated["reviewer"].provider == "anthropic"


def test_user_picks_with_unknown_model_fall_back_to_heuristic(monkeypatch):
    """If the model id is garbage (not in any enabled catalog), the
    validator must ignore it and use the heuristic baseline, not crash.
    """
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: _all_anthropic_enabled_cfg() if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"anthropic": object()}  # noqa: SLF001

    predicted = runtime._manual_prediction(  # noqa: SLF001
        topology="triad",
        agent_models={"coder": "not-a-real-model"},
        model_pin="auto",
    )
    fallback = runtime._build_agent_model_map(  # noqa: SLF001
        complexity=50,
        topology_id="triad",
        agent_complexity={},
    )
    validated, _ = runtime._validate_agent_model_assignments(  # noqa: SLF001
        "triad",
        predicted["agent_assignments"],
        fallback,
    )

    assert validated["coder"].model_id == fallback["coder"].model_id
    assert validated["coder"].provider == fallback["coder"].provider


def test_explicit_provider_still_honored(monkeypatch):
    """When provider IS supplied, the existing path must still accept it."""
    monkeypatch.setattr(
        runtime_mod,
        "get_config",
        lambda key: _all_anthropic_enabled_cfg() if key == "providers" else None,
    )

    runtime = GraphRuntime()
    runtime._providers = {"anthropic": object()}  # noqa: SLF001

    raw = {
        "coder": {"provider": "anthropic", "model": ANTHROPIC_OPUS_MODEL},
    }
    fallback = runtime._build_agent_model_map(  # noqa: SLF001
        complexity=50,
        topology_id="triad",
        agent_complexity={},
    )
    validated, _ = runtime._validate_agent_model_assignments(  # noqa: SLF001
        "triad", raw, fallback,
    )
    assert validated["coder"].model_id == ANTHROPIC_OPUS_MODEL
    assert validated["coder"].provider == "anthropic"
