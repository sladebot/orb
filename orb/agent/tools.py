from __future__ import annotations


def send_message_tool(neighbors: list[str]) -> dict:
    """Build the send_message tool schema with dynamic neighbor enum."""
    return {
        "name": "send_message",
        "description": (
            "Send a message to a neighboring agent in the graph. "
            "Include only the context your neighbor needs to do their job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "enum": neighbors,
                    "description": "The agent to send the message to",
                },
                "content": {
                    "type": "string",
                    "description": "The message content",
                },
                "context": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of relevant context snippets to share "
                        "(code, decisions, constraints). Share only what the recipient needs."
                    ),
                },
            },
            "required": ["to", "content"],
        },
    }


def complete_task_tool() -> dict:
    """Build the complete_task tool schema."""
    return {
        "name": "complete_task",
        "description": (
            "Signal that you have completed your part of the task. "
            "Include a summary of what you accomplished."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "Summary of what was accomplished",
                },
            },
            "required": ["result"],
        },
    }
