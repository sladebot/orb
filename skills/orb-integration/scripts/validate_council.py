#!/usr/bin/env python3
"""Check council evidence bookkeeping; does not execute agents or prove claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, list) and all(_text(item) for item in value)


def validate(record):
    errors = []
    if not isinstance(record, dict):
        return ["record must be an object"]
    status = record.get("status")
    if status not in ("active", "completed", "stopped"):
        errors.append("status must be active, completed, or stopped")
    round_no, limit = record.get("round"), record.get("max_rounds")
    if type(round_no) is not int or type(limit) is not int or limit < 1 or round_no < 0:
        errors.append("round and max_rounds must be integers with round >= 0 and max_rounds >= 1")
    elif round_no > limit or (status == "active" and round_no == limit):
        errors.append("round budget exhausted; complete with evidence or stop")
    if status == "stopped" and not _text(record.get("stop_reason")):
        errors.append("stopped records need stop_reason")

    participants = record.get("participants")
    if not isinstance(participants, dict) or not participants:
        errors.append("participants must be a nonempty object")
        participants = {}
    for node, participant in participants.items():
        if not _text(node) or not isinstance(participant, dict) or not all(
            _text(participant.get(key)) for key in ("role", "model")
        ):
            errors.append("each participant needs a node id, role, and actual model or host-default")

    claims = record.get("claims")
    if not isinstance(claims, list):
        errors.append("claims must be a list")
        claims = []
    claim_ids = set()
    for claim in claims:
        if not isinstance(claim, dict):
            errors.append("each claim must be an object")
            continue
        cid = claim.get("id")
        if not _text(cid) or cid in claim_ids:
            errors.append("claim ids must be nonempty and unique")
        else:
            claim_ids.add(cid)
        node = claim.get("node")
        if not _text(node) or node not in participants:
            errors.append("claim references an unknown node")
        if not _text(claim.get("statement")) or not _strings(claim.get("evidence")):
            errors.append("claim needs a statement and an evidence list (empty for hypotheses)")

    disagreements = record.get("disagreements")
    if not isinstance(disagreements, list):
        errors.append("disagreements must be a list")
        disagreements = []
    dispute_ids, open_ids = set(), set()
    for dispute in disagreements:
        if not isinstance(dispute, dict):
            errors.append("each disagreement must be an object")
            continue
        did = dispute.get("id")
        valid_id = _text(did)
        if not valid_id or did in dispute_ids:
            errors.append("disagreement ids must be nonempty and unique")
        else:
            dispute_ids.add(did)
        refs = dispute.get("claims")
        if not _strings(refs) or len(set(refs)) < 2 or not set(refs) <= claim_ids:
            errors.append("disagreement needs at least two distinct known claim ids")
        if type(dispute.get("blocking")) is not bool:
            errors.append("disagreement blocking must be a boolean")
        if dispute.get("status") == "open":
            if valid_id:
                open_ids.add(did)
            if status == "completed" and dispute.get("blocking") is True:
                errors.append("cannot complete with an open blocking disagreement")
        elif dispute.get("status") == "resolved":
            evidence = dispute.get("evidence")
            if not _text(dispute.get("resolution")) or not _strings(evidence) or not evidence:
                errors.append("resolved disagreements need resolution and evidence")
        else:
            errors.append("disagreement status must be open or resolved")

    if status == "completed":
        decision = record.get("decision")
        if not isinstance(decision, dict):
            decision = {}
        evidence = decision.get("evidence")
        if not _text(decision.get("summary")) or not _strings(evidence) or not evidence:
            errors.append("completed decision needs summary and evidence")
        remaining = decision.get("remaining_disagreements")
        if not _strings(remaining) or set(remaining) != open_ids:
            errors.append("decision must disclose exactly the remaining open disagreement ids")
        if not claims:
            errors.append("completed council needs claims")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    try:
        errors = validate(json.loads(args.record.read_text()))
    except (OSError, ValueError) as exc:
        errors = [str(exc)]
    print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
