"""Bundled default topology definitions shipped with Orb."""

BUILTIN_TOPOLOGIES_YAML = """\
version: "1.0"

topologies:
  triangle:
    id: "triangle"
    label: "Triad"
    description: "Coder, reviewer, and tester collaborate through a compact graph."
    entry_agent: "coordinator"

    agents:
      coordinator:
        role: "Coordinator"
        description: >
          You are the routing coordinator for this team.
          When you receive the user's task, send it to the correct worker
          immediately with the task text unchanged. Do NOT answer the task,
          do NOT write code, and do NOT analyze the task yourself. You only
          route inputs across the graph and surface user clarifications to
          the correct node.
        base_complexity: 20
        suppress_context_guidelines: true
        position_hint: "entry router"

      coder:
        role: "Coder"
        description: >
          You write and update the implementation for programming tasks.
          Use the shared sandbox to inspect, write, and verify code.
          Coordinate with your graph neighbors for review, testing, and
          follow-up changes.
        base_complexity: 50
        enable_filesystem: true
        position_hint: "implementation hub"

      reviewer:
        role: "Reviewer"
        description: >
          You review implementation quality, correctness, and edge cases.
          Inspect files directly in the shared sandbox and send concrete
          feedback to the relevant neighbors.
        base_complexity: 65
        enable_filesystem: true
        position_hint: "quality edge"

      tester:
        role: "Tester"
        description: >
          You validate implementations through execution, test cases, and
          bug discovery. Use the shared sandbox to read files, write tests,
          run commands, and report concrete findings.
        base_complexity: 25
        enable_filesystem: true
        position_hint: "validation edge"

    edges:
      - [coordinator, coder]
      - [coder, reviewer]
      - [coder, tester]
      - [reviewer, tester]

    graph_view:
      rows:
        -
          - {text: "       "}
          - {node: "coordinator"}
        -
          - {text: "            "}
          - {edge: ["coordinator", "coder"], text: "|"}
        -
          - {text: "  "}
          - {node: "coder"}
          - {text: "  "}
          - {edge: ["coder", "reviewer"], text: "---  "}
          - {node: "reviewer"}
        -
          - {text: "  "}
          - {edge: ["coder", "tester"], text: "|"}
          - {text: "                 "}
          - {edge: ["reviewer", "tester"], text: "|"}
        -
          - {text: "  "}
          - {node: "tester"}
          - {text: "  "}
          - {edge: ["tester", "reviewer"], text: "-------------'"}
      order: ["coordinator", "coder", "reviewer", "tester"]

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

  dual-review:
    id: "dual-review"
    label: "Dual Review"
    description: "Coder works with two reviewer branches and tester validation."
    entry_agent: "coordinator"

    agents:
      coordinator:
        role: "Coordinator"
        description: >
          You are the routing coordinator for this team.
          When you receive the user's task, send it to the correct worker
          immediately with the task text unchanged. Do NOT answer the task,
          do NOT write code, and do NOT analyze the task yourself. You only
          route work and surface direct user clarifications to the correct node.
        base_complexity: 20
        suppress_context_guidelines: true
        position_hint: "entry router"

      coder:
        role: "Coder"
        description: >
          You write and update the implementation for programming tasks.
          Use the shared sandbox to inspect, write, and verify code.
          Coordinate with your graph neighbors for review, testing, and
          follow-up changes.
        base_complexity: 50
        enable_filesystem: true
        position_hint: "implementation hub"

      reviewer_a:
        role: "Reviewer A"
        description: >
          You are one of two independent reviewers. Inspect files in the
          shared sandbox, review them from your perspective, and communicate
          with the other reviewer and relevant neighbors.
        base_complexity: 65
        enable_filesystem: true
        position_hint: "review branch A"
        model_selection:
          prefer_provider: "anthropic"
          fallback_providers: ["openai-codex", "openai", "ollama"]

      reviewer_b:
        role: "Reviewer B"
        description: >
          You are one of two independent reviewers. Inspect files in the
          shared sandbox, review them from your perspective, and communicate
          with the other reviewer and relevant neighbors.
        base_complexity: 65
        enable_filesystem: true
        position_hint: "review branch B"
        model_selection:
          prefer_different_provider_than: "reviewer_a"
          fallback_providers: ["openai-codex", "openai", "ollama"]

      tester:
        role: "Tester"
        description: >
          You validate implementations through execution, test cases, and
          bug discovery. Use the shared sandbox to read files, write tests,
          run commands, and report concrete findings.
        base_complexity: 25
        enable_filesystem: true
        position_hint: "validation sink"

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

    graph_view:
      rows:
        -
          - {text: "         "}
          - {node: "coordinator"}
        -
          - {text: "              "}
          - {edge: ["coordinator", "coder"], text: "|"}
        -
          - {text: "         "}
          - {node: "coder"}
        -
          - {text: "        "}
          - {edge: ["coder", "reviewer_a"], text: "/"}
          - {text: "     "}
          - {edge: ["coder", "reviewer_b"], text: "\\\\"}
        -
          - {text: "  "}
          - {node: "reviewer_a"}
          - {text: "  "}
          - {edge: ["reviewer_a", "reviewer_b"], text: "---  "}
          - {node: "reviewer_b"}
        -
          - {text: "       "}
          - {edge: ["reviewer_a", "tester"], text: "\\\\"}
          - {text: "     "}
          - {edge: ["reviewer_b", "tester"], text: "/"}
        -
          - {text: "          "}
          - {node: "tester"}
      order: ["coordinator", "coder", "reviewer_a", "reviewer_b", "tester"]

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
"""
