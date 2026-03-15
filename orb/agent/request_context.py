from __future__ import annotations


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
