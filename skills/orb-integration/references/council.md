# Evidence-based council protocol

Use for council and specialist-network topologies. The JSON steps describe the
initial exchange; this protocol governs subsequent evidence rounds and completion.
The host schedules work. Peers can exchange findings directly where supported;
otherwise the host relays attributed messages without rewriting their substance.

## Start and preserve independence

Define the decision question, acceptance criteria, constraints, write ownership,
and max_rounds (default 3). A round is a batch of targeted challenges, evidence work,
and collected responses after the initial independent proposals. Round 0 includes
initial proposals, analysis-only peer rebuttals, and the first critic/integration
assessment using existing evidence. Any newly requested experiment or validation
after the proposals starts an evidence round, even if requested before the critic
finishes. Do not hide evidence rounds inside the initial exchange. Check elapsed time,
user budgets, and remaining slots before each dispatch; reduce work when necessary.
The record checker enforces the recorded round count, not live spending or time.

Bind abstract specialist roles to concrete subproblems. Give first proposers the
same task and relevant source material, but not one another's proposals. In a
specialist network, shared interface constraints are available from the start.
Use isolated contexts when available. Record any loss of model diversity or context
independence. A sequential execution with separate contexts can remain independent;
several role labels in one conversation cannot.

## Exchange and reconcile

Each participant returns a concise proposal or finding, assumptions, supporting
artifact references, uncertainty, and a test or observation that could disprove it.
Keep private chain-of-thought out of handoffs. Label untested assertions as hypotheses.

Assign claim ids and disagreement ids in a decision record. For each disagreement:
identify the competing claims, consequence, whether it blocks acceptance, and what
observation would distinguish them. Send only relevant claims and artifacts to the
peer best suited to investigate. Use the host's direct messaging or faithful relay;
do not broadcast the entire transcript to every node.

Each evidence round:
1. Select unresolved questions whose answers could change the decision.
2. Assign bounded experiments, source checks, or implementation revisions with
   disjoint write ownership. Agreement or a majority vote is not evidence.
3. Wait, collect observable results, and update claims and disagreements.
4. Resolve disputes only with cited evidence and a reason. A test log or authoritative
   source may qualify; preference tradeoffs require the user's stated priorities or
   an explicitly documented assumption, not a fabricated empirical proof.

A smaller model can overturn a larger model's claim with better evidence. The
reconciler documents what follows from the evidence; it does not erase dissent.
Do not lower a blocking disagreement to nonblocking merely to finish. Record why
its consequence no longer blocks the acceptance criteria if classification changes.

For implementation tasks, preserve required reviews and revalidate changed artifacts.
Send material revisions back to their reviewer before treating findings as resolved.
The same rule applies to implement-review, research-build, and parallel-review.

## Stop and record

Stop early once acceptance criteria have evidence and no blocking disagreement
remains. Disclose nonblocking dissent and rejected alternatives. If a round brings
no new evidence, stop or escalate the specific missing fact rather than repeat the
debate. At the round/time/cost limit, return the supported partial result with open
questions and status `stopped`. Do not claim completion or reset the budget by
changing topologies. Resume only within an existing authorized budget or after a
new user instruction extending it.

Write a record only in a task-approved output location (temporary storage is fine).
Do not commit council transcripts automatically. Keep sensitive source content in
its original location and use references where practical.

## Decision record format

All lists below are required. `claims[].evidence` may be empty for a hypothesis.
Participant `model` records the actual model or `host-default`, not a requested but
unavailable model. Additional fields such as topology, transitions, limitations,
source claim versions, and measured cost can be retained by the host.

```json
{
  "status": "completed",
  "round": 1,
  "max_rounds": 3,
  "participants": {
    "proposer_a": {"role": "proposer", "model": "host-default"},
    "critic": {"role": "critic", "model": "host-default"}
  },
  "claims": [
    {"id": "c1", "node": "proposer_a", "statement": "Versioned keys avoid stale cache entries", "evidence": ["artifacts/cache-test.txt"]},
    {"id": "c2", "node": "critic", "statement": "Writes might leave stale entries", "evidence": []}
  ],
  "disagreements": [
    {"id": "d1", "claims": ["c1", "c2"], "blocking": true, "status": "resolved", "resolution": "Write-path invalidation passed the specified tests", "evidence": ["artifacts/cache-test.txt"]}
  ],
  "decision": {"summary": "Use versioned keys", "evidence": ["artifacts/cache-test.txt"], "remaining_disagreements": []}
}
```

This is an illustrative record, not evidence of an executed test. Use real artifacts
for actual decisions. Include all participating nodes, not just the two in the example.
For `stopped`, include `stop_reason`; a final `decision` is optional. For `active`,
round must be below max_rounds. A completed decision may retain open nonblocking
questions only when their ids appear in `remaining_disagreements`.

Run `python <skill-dir>/scripts/validate_council.py <record.json>`.
Exit 0 means internally consistent bookkeeping, including an honest stopped run;
it does not mean the user's task succeeded. The host must inspect the record status,
verify evidence exists and supports the claims, verify all required role outputs
were collected, and judge the actual acceptance criteria. The checker cannot detect
fabricated evidence, omitted disagreements, or unexecuted reviews.
