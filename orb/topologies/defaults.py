"""Bundled default topology definitions shipped with Orb."""

BUILTIN_TOPOLOGIES_YAML = """\
version: "1.0"

topologies:
  solo:
    id: "solo"
    label: "Solo"
    description: "A single agent handles the task end-to-end. No coordination, no review — pick for trivial or throwaway tasks."
    entry_agent: "solo"

    agents:
      solo:
        role: "Solo Agent"
        category: "implementation"
        position_label: "sole worker"
        description: >
          You are the only agent on this task. There are no neighbors to
          delegate to and no reviewer to check your work. Read any
          relevant files, make the change directly in the shared sandbox,
          and complete the task when done. Be terse — you own every step.
        base_complexity: 30
        enable_filesystem: true

    edges: []

    workflow_steps:
      - "Read the task."
      - "Inspect any relevant files in the sandbox."
      - "Make the change (or answer the question) directly."
      - "Call complete_task with the result."

    completion_rules:
      solo:
        - "You have no neighbors — do not attempt to send_message to anyone."
        - "Do the whole task yourself, then complete."
        - "If the task is larger than expected, note it in the result rather than trying to recruit help."

    graph_view:
      order: ["solo"]
      rows:
        - [{text: "        "}, {node: "solo"}]

    selection_hints:
      ideal_for:
        - "trivial one-off changes (rename, typo, one-line edit)"
        - "short explanatory questions about existing code"
        - "tasks a single engineer would finish in under five minutes"
      keywords: ["quick", "simple", "one-off", "rename", "typo", "trivial", "explain"]
      min_complexity: 0
      max_complexity: 20

  triad:
    id: "triad"
    label: "Triad"
    description: "Coder, reviewer, and tester collaborate through a compact graph."
    entry_agent: "coordinator"

    agents:
      coordinator:
        role: "Coordinator"
        category: "entry"
        position_label: "entry router"
        description: >
          You are the routing coordinator for this team.
          When you receive the user's task, send it to the correct worker
          immediately with the task text unchanged. Do NOT answer the task,
          do NOT write code, and do NOT analyze the task yourself. You only
          route inputs across the graph and surface user clarifications to
          the correct node.
        base_complexity: 20
        suppress_context_guidelines: true

      coder:
        role: "Coder"
        category: "implementation"
        position_label: "implementation hub"
        description: >
          You write and update the implementation for programming tasks.
          Use the shared sandbox to inspect, write, and verify code.
          Coordinate with your graph neighbors for review, testing, and
          follow-up changes.
        base_complexity: 50
        enable_filesystem: true

      reviewer:
        role: "Reviewer"
        category: "review"
        position_label: "quality edge"
        description: >
          You review implementation quality, correctness, and edge cases.
          Inspect files directly in the shared sandbox and send concrete
          feedback to the relevant neighbors.
        base_complexity: 65
        enable_filesystem: true

      tester:
        role: "Tester"
        category: "validation"
        position_label: "validation edge"
        description: >
          You validate implementations through execution, test cases, and
          bug discovery. Use the shared sandbox to read files, write tests,
          run commands, and report concrete findings.
        base_complexity: 25
        enable_filesystem: true

    edges:
      - [coordinator, coder]
      - [coder, reviewer]
      - [coder, tester]
      - [reviewer, tester]

    workflow_steps:
      - "Coordinator receives the user task and routes it into the graph."
      - "Coder implements or updates the solution in the shared sandbox."
      - "Coder hands work to Reviewer and Tester for feedback and validation."
      - "Reviewer and Tester send concrete findings back through their graph neighbors."
      - "Each node completes only after finishing its own role in the workflow."

    completion_rules:
      coordinator:
        - "Route the incoming task to the correct worker without solving it yourself."
        - "Do not ask the user questions unless a worker explicitly bubbles one up."
        - "Complete after routing or forwarding the necessary input."
      coder:
        - "Do not complete immediately after first-pass implementation."
        - "Hand off the implementation to both Reviewer and Tester before final completion."
        - "If feedback or bugs arrive, update the implementation before completing."
      reviewer:
        - "Review concrete files or outputs before completing."
        - "Send actionable feedback to the Coder when changes are needed."
      tester:
        - "Run validation against the current implementation before completing."
        - "Send failing cases or validation results to the relevant neighbors."
    selection_hints:
      ideal_for:
        - "compact implementation tasks with one coding path"
        - "small to medium coding requests"
      keywords: ["bugfix", "feature", "script", "tool", "prototype"]
      min_complexity: 15
      max_complexity: 60

  dual-review:
    id: "dual-review"
    label: "Dual Review"
    description: "Coder works with two reviewer branches and tester validation."
    entry_agent: "coordinator"

    agents:
      coordinator:
        role: "Coordinator"
        category: "entry"
        position_label: "entry router"
        description: >
          You are the routing coordinator for this team.
          When you receive the user's task, send it to the correct worker
          immediately with the task text unchanged. Do NOT answer the task,
          do NOT write code, and do NOT analyze the task yourself. You only
          route work and surface direct user clarifications to the correct node.
        base_complexity: 20
        suppress_context_guidelines: true

      coder:
        role: "Coder"
        category: "implementation"
        position_label: "implementation hub"
        description: >
          You write and update the implementation for programming tasks.
          Use the shared sandbox to inspect, write, and verify code.
          Coordinate with your graph neighbors for review, testing, and
          follow-up changes.
        base_complexity: 50
        enable_filesystem: true

      reviewer_a:
        role: "Reviewer A"
        category: "review"
        position_label: "review branch A"
        description: >
          You are one of two independent reviewers. Inspect files in the
          shared sandbox, review them from your perspective, and communicate
          with the other reviewer and relevant neighbors.
        base_complexity: 65
        enable_filesystem: true
        model_selection:
          prefer_provider: "anthropic"
          fallback_providers: ["openai-codex", "ollama"]

      reviewer_b:
        role: "Reviewer B"
        category: "review"
        position_label: "review branch B"
        description: >
          You are one of two independent reviewers. Inspect files in the
          shared sandbox, review them from your perspective, and communicate
          with the other reviewer and relevant neighbors.
        base_complexity: 65
        enable_filesystem: true
        model_selection:
          prefer_different_provider_than: "reviewer_a"
          fallback_providers: ["openai-codex", "ollama"]

      tester:
        role: "Tester"
        category: "validation"
        position_label: "validation edge"
        description: >
          You validate implementations through execution, test cases, and
          bug discovery. Use the shared sandbox to read files, write tests,
          run commands, and report concrete findings.
        base_complexity: 25
        enable_filesystem: true

    edges:
      - [coordinator, coder]
      - [coder, reviewer_a]
      - [reviewer_a, coder]
      - [coder, reviewer_b]
      - [reviewer_b, coder]
      - [reviewer_a, reviewer_b]
      - [reviewer_b, reviewer_a]
      - [coder, tester]
      - [tester, coder]
      - [tester, reviewer_a]
      - [reviewer_a, tester]
      - [tester, reviewer_b]
      - [reviewer_b, tester]

    workflow_steps:
      - "Coordinator routes the incoming task to the Coder."
      - "Coder implements or updates the solution in the shared sandbox."
      - "Coder hands the work to Reviewer A, Reviewer B, and Tester."
      - "The two reviewers compare findings with each other before approval."
      - "Tester validates the implementation and sends concrete failures or pass signals."
      - "Each node completes only after finishing its own role in the workflow."

    completion_rules:
      coordinator:
        - "Route the incoming task to the correct worker without solving it yourself."
        - "Forward user clarifications to the right node when needed."
        - "Complete after routing or forwarding the necessary input."
      coder:
        - "Do not complete immediately after first-pass implementation."
        - "Hand off the implementation to both reviewers and the tester before final completion."
        - "Incorporate reviewer and tester feedback before completing."
      reviewer_a:
        - "Review the implementation independently before completing."
        - "Discuss disagreements directly with Reviewer B before signaling approval."
      reviewer_b:
        - "Review the implementation independently before completing."
        - "Discuss disagreements directly with Reviewer A before signaling approval."
      tester:
        - "Run validation against the current implementation before completing."
        - "Send failing cases or validation results to the coder and reviewers."
    selection_hints:
      ideal_for:
        - "high-risk or high-complexity implementation tasks"
        - "tasks that benefit from multiple independent review passes"
      keywords: ["security", "critical", "production", "architecture", "review", "refactor"]
      min_complexity: 55
      max_complexity: 100

  hierarchy:
    id: "hierarchy"
    label: "Hierarchy"
    description: "Coordinator routes through a research/planning layer before implementation, review, and testing."
    entry_agent: "coordinator"

    agents:
      coordinator:
        role: "Coordinator"
        category: "entry"
        position_label: "entry router"
        description: >
          You are the routing coordinator for this team.
          When you receive the user's task, route it to the correct next node
          with the task text unchanged. Do NOT answer the task, do NOT write
          code, and do NOT analyze the task yourself. You only route work and
          forward user clarifications to the correct node.
        base_complexity: 20
        suppress_context_guidelines: true

      researcher:
        role: "Researcher"
        category: "discovery"
        position_label: "discovery layer"
        description: >
          You gather context, clarify requirements, inspect the repository,
          and produce a concrete implementation brief for the coder. Use the
          shared sandbox to inspect files and report constraints, interfaces,
          and risks before implementation proceeds.
        base_complexity: 45
        enable_filesystem: true

      coder:
        role: "Coder"
        category: "implementation"
        position_label: "implementation hub"
        description: >
          You write and update the implementation for programming tasks.
          Use the shared sandbox to inspect, write, and verify code.
          Coordinate with your graph neighbors for review, testing, and
          follow-up changes.
        base_complexity: 55
        enable_filesystem: true

      reviewer:
        role: "Reviewer"
        category: "review"
        position_label: "quality edge"
        description: >
          You review implementation quality, correctness, and edge cases.
          Inspect files directly in the shared sandbox and send concrete
          feedback to the relevant neighbors.
        base_complexity: 65
        enable_filesystem: true

      tester:
        role: "Tester"
        category: "validation"
        position_label: "validation edge"
        description: >
          You validate implementations through execution, test cases, and
          bug discovery. Use the shared sandbox to read files, write tests,
          run commands, and report concrete findings.
        base_complexity: 30
        enable_filesystem: true

    edges:
      - [coordinator, researcher]
      - [researcher, coder]
      - [coder, reviewer]
      - [reviewer, coder]
      - [coder, tester]
      - [tester, coder]

    workflow_steps:
      - "Coordinator routes the incoming task to the Researcher."
      - "Researcher inspects context and prepares an implementation brief for the Coder."
      - "Coder implements or updates the solution in the shared sandbox."
      - "Reviewer and Tester send concrete findings back to the Coder."
      - "Each node completes only after finishing its own role in the workflow."

    completion_rules:
      coordinator:
        - "Route the incoming task to the correct worker without solving it yourself."
        - "Forward user clarifications to the right node when needed."
        - "Complete after routing or forwarding the necessary input."
      researcher:
        - "Inspect relevant files and constraints before completing."
        - "Send the coder a concise implementation brief with assumptions and risks."
      coder:
        - "Do not complete immediately after the first implementation pass."
        - "Hand off the implementation to the reviewer and tester before final completion."
        - "Incorporate feedback from reviewer and tester before completing."
      reviewer:
        - "Review concrete files or outputs before completing."
        - "Send actionable feedback to the Coder when changes are needed."
      tester:
        - "Run validation against the current implementation before completing."
        - "Send failing cases or validation results to the Coder."

    graph_view:
      order: ["coordinator", "researcher", "coder", "reviewer", "tester"]
      rows:
        - [{text: "        "}, {node: "coordinator"}]
        - [{text: "             "}, {edge: ["coordinator", "researcher"], text: "│"}]
        - [{text: "        "}, {node: "researcher"}]
        - [{text: "             "}, {edge: ["researcher", "coder"], text: "│"}]
        - [{text: "        "}, {node: "coder"}]
        - [{text: "       "}, {edge: ["coder", "reviewer"], text: "╱"}, {text: "   "}, {edge: ["coder", "tester"], text: "╲"}]
        - [{text: "   "}, {node: "reviewer"}, {text: "     "}, {node: "tester"}]
    selection_hints:
      ideal_for:
        - "tasks that benefit from repository discovery or explicit planning before coding"
        - "larger feature work where requirements need to be clarified up front"
      keywords: ["research", "plan", "explore", "understand", "repository", "investigate", "migration"]
      min_complexity: 35
      max_complexity: 85
"""
