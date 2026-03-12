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


# ── Filesystem tools ──────────────────────────────────────────────────────────

def write_file_tool() -> dict:
    return {
        "name": "write_file",
        "description": (
            "Write content to a file on disk. Creates parent directories as needed. "
            "Use relative paths (resolved from the current working directory)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write to (e.g. 'src/foo.py')",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write",
                },
            },
            "required": ["path", "content"],
        },
    }


def read_file_tool() -> dict:
    return {
        "name": "read_file",
        "description": "Read the contents of a file on disk.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to read",
                },
            },
            "required": ["path"],
        },
    }


def list_directory_tool() -> dict:
    return {
        "name": "list_directory",
        "description": "List files and directories at a given path (non-recursive).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (default: '.')",
                },
            },
            "required": [],
        },
    }


def run_command_tool() -> dict:
    return {
        "name": "run_command",
        "description": (
            "Run a shell command and return its stdout/stderr output. "
            "Useful for executing tests, linting, building, or checking the environment. "
            "Commands time out after 30 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run (e.g. 'python -m pytest tests/')",
                },
            },
            "required": ["command"],
        },
    }


def filesystem_tools() -> list[dict]:
    """Return all filesystem tool definitions."""
    return [write_file_tool(), read_file_tool(), list_directory_tool(), run_command_tool()]
