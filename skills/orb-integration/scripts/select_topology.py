#!/usr/bin/env python3
"""Select the smallest sufficient portable Orb topology."""

from __future__ import annotations

import argparse
import re


TOPOLOGIES = {
    "solo",
    "implement-review",
    "research-build",
    "parallel-review",
    "council",
    "specialist-network",
}


def select_topology(task: str, *, explicit: str | None = None) -> str:
    if explicit:
        if explicit not in TOPOLOGIES:
            choices = ", ".join(sorted(TOPOLOGIES))
            raise ValueError(f"unknown topology {explicit!r}; choose one of: {choices}")
        return explicit

    text = task.casefold()
    # This is an advisory lexical baseline. The host can override with --topology.
    # A narrow copy edit should not recruit a council merely for mentioning one.
    if re.search(r"\b(typo|spelling|punctuation)\b", text) and not re.search(
        r"\b(implement|build|refactor|security|migration)\b", text
    ):
        return "solo"
    if any(signal in text for signal in ("peer network", "specialist network", "specialists", "separable components")):
        return "specialist-network"
    if any(signal in text for signal in ("council", "debate", "reconcile", "compare alternative", "competing approach")):
        return "council"
    research_signals = (
        "research",
        "investigate",
        "architecture",
        "design",
        "unfamiliar",
        "requirements",
    )
    critical_signals = (
        "critical",
        "security",
        "migration",
        "authentication",
        "authorization",
        "production",
        "independent",
    )
    implementation_signals = (
        "implement",
        "build",
        "fix",
        "refactor",
        "change",
        "add ",
    )

    if any(signal in text for signal in research_signals):
        return "research-build"
    if sum(signal in text for signal in critical_signals) >= 2:
        return "parallel-review"
    if any(signal in text for signal in implementation_signals):
        return "implement-review"
    return "solo"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--topology", choices=sorted(TOPOLOGIES))
    args = parser.parse_args()
    print(select_topology(args.task, explicit=args.topology))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
