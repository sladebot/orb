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
            "Paths resolve against the session workdir (see the Working directory "
            "section of your system prompt). Pass a relative path like 'src/foo.py'."
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
        "description": (
            "Read the contents of a file on disk. Paths resolve against the "
            "session workdir. Call list_directory('.') first if you don't know "
            "which files exist."
        ),
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
        "description": (
            "List files and directories at a given path (non-recursive). "
            "Paths resolve against the session workdir. Default '.' lists the "
            "workdir root — use this to discover the repo layout."
        ),
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
            "Useful for executing tests, linting, building, grep, git, or inspecting the filesystem. "
            "Commands execute with the session workdir as cwd and time out after 30 seconds."
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


# ── Persistent memory tools ───────────────────────────────────────────────────

def memory_read_tool() -> dict:
    return {
        "name": "memory_read",
        "description": (
            "Search Orb's persistent wiki memory vault by keyword or phrase. "
            "Returns compact snippets and metadata, not full vault contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5, max 20)",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    }


def memory_read_entity_tool() -> dict:
    return {
        "name": "memory_read_entity",
        "description": "Read one persistent memory wiki page by exact entity/page title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity or page title to read"},
            },
            "required": ["entity"],
        },
    }


def memory_read_tag_tool() -> dict:
    return {
        "name": "memory_read_tag",
        "description": "List compact memory page summaries that carry a specific tag.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Tag to filter by"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of pages to return (default 10, max 50)",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["tag"],
        },
    }


def memory_list_pages_tool() -> dict:
    return {
        "name": "memory_list_pages",
        "description": "List known persistent memory pages without reading full page content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of pages to return (default 50, max 100)",
                    "minimum": 1,
                    "maximum": 100,
                },
                "page_type": {
                    "type": "string",
                    "description": "Optional page type filter: entity, concept, analysis, or query",
                    "enum": ["entity", "concept", "analysis", "query"],
                },
            },
            "required": [],
        },
    }


def memory_write_tool() -> dict:
    return {
        "name": "memory_write",
        "description": (
            "Write or update a durable persistent memory wiki page. "
            "Use only for stable facts, decisions, architecture notes, or reusable project knowledge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Canonical page title"},
                "content": {"type": "string", "description": "Markdown page body"},
                "page_type": {
                    "type": "string",
                    "description": "Page type (default concept)",
                    "enum": ["entity", "concept", "analysis", "query"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags from the vault taxonomy",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional source/provenance identifiers",
                },
            },
            "required": ["title", "content"],
        },
    }


def memory_write_entity_tool() -> dict:
    return {
        "name": "memory_write_entity",
        "description": "Convenience tool for writing or updating an entity-type memory page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity title"},
                "content": {"type": "string", "description": "Durable entity facts as Markdown"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags from the vault taxonomy",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional source/provenance identifiers",
                },
            },
            "required": ["entity", "content"],
        },
    }


def memory_read_tools() -> list[dict]:
    """Return read-only persistent memory tool definitions."""
    return [
        memory_read_tool(),
        memory_read_entity_tool(),
        memory_read_tag_tool(),
        memory_list_pages_tool(),
    ]


def memory_write_tools() -> list[dict]:
    """Return write-capable persistent memory tool definitions."""
    return [memory_write_tool(), memory_write_entity_tool()]
