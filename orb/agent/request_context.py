from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.subgraph_store import SubgraphStore


def compose_system_prompt(base_system_prompt: str, shared_transcript: str = "") -> str:
    """Retained helper for light prompt augmentation."""
    if not shared_transcript:
        return base_system_prompt
    return f"{base_system_prompt}\n\n## Shared Conversation Context\n{shared_transcript}"


def build_request_messages(local_messages: list[dict], shared_transcript: str = "") -> list[dict]:
    """Compose provider-facing messages from shared transcript and local agent history.

    The shared transcript is prepended as a normal user turn so the model sees
    it as part of the conversational context rather than only as system prose.
    To avoid leading consecutive user turns, merge it into the first user
    message when possible.
    """
    messages = [dict(m) for m in local_messages]
    if not shared_transcript:
        return messages

    if messages and messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str):
        messages[0] = {
            **messages[0],
            "content": (
                f"{shared_transcript}\n\n"
                "--- Current local context ---\n"
                f"{messages[0]['content']}"
            ),
        }
        return messages

    return [{"role": "user", "content": shared_transcript}] + messages


async def build_graph_context_block(
    store: "SubgraphStore",
    agent_id: str,
    query: str,
    *,
    limit: int = 10,
) -> str:
    """Query SubgraphStore and format results as a context block string.

    Calls store.query() for semantic search and store.get_facts() for recent
    agent-specific facts, deduplicates by fact id, and formats as a readable
    context block.
    """
    from ..memory.subgraph_store import Fact  # noqa: F401 — type-only but needed for runtime isinstance

    query_facts = await store.query(query, agent_id=agent_id, limit=limit)
    recent_facts = await store.get_facts(agent_id, limit=limit)

    # Deduplicate by fact id, preserving query_facts ordering first
    seen: set[str] = set()
    combined: list[Fact] = []
    for fact in list(query_facts) + list(recent_facts):
        if fact.id not in seen:
            seen.add(fact.id)
            combined.append(fact)

    if not combined:
        return ""

    lines = [f"Graph-retrieved context (agent: {agent_id}):"]
    for fact in combined:
        lines.append(
            f"- [{fact.predicate}] {fact.subject} \u2192 {fact.object}"
            f"  (confidence: {fact.confidence:.2f})"
        )
    return "\n".join(lines)


def build_request_messages_with_graph(
    local_messages: list[dict],
    graph_context: str,
    shared_transcript: str = "",
) -> list[dict]:
    """Like build_request_messages but uses graph context instead of (or in addition to) transcript.

    - Both provided: combines them with a separator, then injects into first user message.
    - Only graph_context: uses it alone as the injected prefix.
    - Only shared_transcript: behaves exactly like the existing build_request_messages.
    - Neither: returns local_messages unchanged (deep-copied dicts).
    """
    if not graph_context and not shared_transcript:
        return [dict(m) for m in local_messages]

    if not graph_context:
        # Delegate to the original behaviour
        return build_request_messages(local_messages, shared_transcript)

    # Build the combined context prefix
    if shared_transcript:
        context_prefix = f"{graph_context}\n\n--- Session context ---\n{shared_transcript}"
    else:
        context_prefix = graph_context

    # Inject into first user message, mirroring build_request_messages strategy
    messages = [dict(m) for m in local_messages]
    if messages and messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str):
        messages[0] = {
            **messages[0],
            "content": (
                f"{context_prefix}\n\n"
                "--- Current local context ---\n"
                f"{messages[0]['content']}"
            ),
        }
        return messages

    return [{"role": "user", "content": context_prefix}] + messages
