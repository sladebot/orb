# Orb Demo Flow Plan

## Scope

This plan covers Orb-side improvements only.

Out of scope:

- screen recording
- recorder scripts
- video post-processing
- browser capture tooling

## Phase 1: Fix Unattended Run Behavior

Goal: make the existing TUI and dashboard flows runnable without manual intervention when explicitly requested.

- Add a CLI path to run a query automatically inside the TUI instead of opening an idle TUI.
- Add a CLI path for dashboard runs to exit automatically after completion instead of blocking on `Prompt.ask`.
- Preserve current interactive behavior as the default.

Primary files:

- `orb/cli/main.py`
- `orb/cli/tui.py`

Exit criterion:

- TUI and dashboard flows can complete unattended from the terminal.

## Phase 2: Make Dashboard Automation-Friendly

Goal: make the dashboard flow usable by external automation without changing the product experience for normal users.

- Ensure the dashboard server can be started in a mode that is stable for automation clients.
- Ensure a task can be submitted and tracked cleanly through the existing HTTP and WebSocket surfaces.
- Ensure shutdown behavior is deterministic after run completion.
- Remove any prompt-driven or terminal-only assumptions from automation-oriented paths.

Primary files:

- `orb/cli/main.py`
- `web/server.py`
- `web/bridge.py`

Exit criterion:

- An external client can start a dashboard run, observe progress, and detect completion reliably.

## Phase 3: Improve Demo Reliability

Goal: reduce nondeterminism so demo flows are repeatable and less likely to fail mid-run.

- Add a preflight check for provider availability before entering TUI or dashboard demo paths.
- Allow explicit model and topology pinning for demo-oriented commands.
- Add clearer failure messages for missing providers or unsupported configurations.
- Ensure timeouts and exit conditions are consistent across TUI and dashboard paths.

Primary files:

- `orb/cli/main.py`
- provider/config resolution code

Exit criterion:

- Demo-oriented runs fail early and clearly when prerequisites are missing.

## Phase 4: Product Polish

Goal: make the demo flows easier to use and easier to document.

- Add documented demo-oriented CLI examples to `README.md`.
- Clarify which modes are interactive and which are automation-friendly.
- Add a small set of recommended prompts and settings for reproducible demos.
- Verify that both `triangle` and `dual-review` topologies behave sensibly in unattended flows.

Primary files:

- `README.md`
- relevant CLI help text

Exit criterion:

- The product ships with a clear, documented path for running Orb demos without manual guesswork.

## Recommended Order

1. Phase 1
2. Phase 2
3. Phase 3
4. Phase 4

Rationale:

- Phase 1 removes the main blockers in current TUI and dashboard behavior.
- Phase 2 makes those paths usable by any external automation layer later.
- Phase 3 improves operational reliability once the control flow is correct.
- Phase 4 documents and polishes the finished workflow.
