from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import MemoryConfig


# ── Vault layout constants ──────────────────────────────────────────────────

WIKI_SUBDIRS = (
    "wiki",
    "wiki/entities",
    "wiki/concepts",
    "wiki/analysis",
    "wiki/queries",
    "raw",
    "raw/articles",
    "raw/transcripts",
    "memories",
)


# ── SCHEMA.md template ──────────────────────────────────────────────────────

def _schema_content() -> str:
    return """# SCHEMA.md — llm-wiki conventions

This vault follows **llm-wiki style** conventions for all pages.

## File conventions

- All pages are Markdown (`.md`).
- Every page has YAML frontmatter (````---` block at top).
- Frontmatter fields: `title`, `type`, `tags`, `sources`, `created`, `updated`, `confidence` (optional), `contested` (optional).
- Wikilinks use double-bracket syntax: `[[page-title]]`.
- Display-text wikilinks: `[[actual-page|Display Text]]`.

## Page types (defined by `type` field)

| type       | Description                                      |
|------------|--------------------------------------------------|
| entity     | Concrete thing: person, project, tool, API       |
| concept    | Abstract idea: architecture pattern, design choice |
| analysis   | Critical review, evaluation, comparison          |
| query      | Question or investigation with a specific answer |

## Tags

- Tags appear in frontmatter: `tags: [tag1, tag2]`.
- Only tags defined in the taxonomy below are allowed.
- Tags are lowercase with hyphens (kebab-case).

## Tag taxonomy

- `source/:<provider>` — provenance markers (e.g. `source/openai`, `source/anthropic`)
- `status/:<state>` — lifecycle (draft, active, deprecated)
- `category/:<domain>` — topical area (architecture, workflow, tooling)
- `persona/:<role>` — stakeholder or user role

## Linking

- Every page SHOULD link to 2+ other pages (no orphans).
- Use `[[page-title]]` for internal links.
- Add provenance markers for multi-source pages: `^[raw/articles/source.md]`.

## Confidence marking

- `confidence: high` — corroborated by 2+ independent sources.
- `confidence: medium` — supported by 1 source but cross-linked.
- `confidence: low` — single source, uncorroborated (flag for review).
- `contested: true` — conflicting claims exist; mark for discussion.
"""


# ── index.md template ──────────────────────────────────────────────────────

def _index_content() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""\n# Vault Index\n\n> Auto-generated page catalog.  Updated: {now}\n\n## Pages by Type\n\n### Entities\n\n_No entities yet._\n\n### Concepts\n\n_No concepts yet._\n\n### Analysis\n\n_No analysis yet._\n\n### Queries\n\n_No queries yet._\n\n---\n\n*Last updated: {now}*\n"""


# ── log.md template ────────────────────────────────────────────────────────

def _log_content(vault_path: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""\n# Vault Log\n\n> Append-only action log.  Vault: `{vault_path}`\n\n## Entries\n\n| Time | Action | Subject | Notes |\n|------|--------|---------|-------|\n| {now} | init | vault scaffolded | Created initial directory structure with wiki/, raw/, memories/, SCHEMA.md, index.md, log.md |\n\n---\n*Auto-generated on vault initialization.*\n"""


# ── Core helpers ─────────────────────────────────────────────────────────────


def _resolve_vault_path(vault_path: str) -> Path:
    """Resolve a possibly-tilde path to an absolute Path."""
    return Path(vault_path).expanduser().resolve()


def _ensure_vault(config: MemoryConfig) -> Path:
    """Ensure the vault directory and all subdirs exist.

    Returns the absolute vault path.  Safe to call multiple times (idempotent).
    """
    vault = _resolve_vault_path(config.vault_path)
    (vault).mkdir(parents=True, exist_ok=True)
    for sub in WIKI_SUBDIRS:
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


# ── CLI: orb memory init ────────────────────────────────────────────────────


def _cmd_memory_init(args: argparse.Namespace) -> None:
    """Handle `orb memory init`: create vault structure + default files."""
    config = MemoryConfig(
        vault_path=getattr(args, "vault_path", None) or "~/.orb/vault",
        enabled=False,
    )
    vault = _ensure_vault(config)

    # Write SCHEMA.md (idempotent: skip if exists)
    schema_path = vault / "SCHEMA.md"
    if not schema_path.exists():
        schema_path.write_text(_schema_content())

    # Write index.md (idempotent: skip if exists)
    index_path = vault / "index.md"
    if not index_path.exists():
        index_path.write_text(_index_content())

    # Write log.md (idempotent: skip if exists)
    log_path = vault / "log.md"
    if not log_path.exists():
        log_path.write_text(_log_content(str(vault)))

    print(f"Vault initialized at {vault}")


# ── CLI: orb memory status ─────────────────────────────────────────────────


def _cmd_memory_status(args: argparse.Namespace) -> None:
    """Handle `orb memory status`: report vault health."""
    config = MemoryConfig(
        vault_path=getattr(args, "vault_path", None) or "~/.orb/vault",
    )
    vault = _resolve_vault_path(config.vault_path)

    if not vault.exists():
        print(f"Vault not found at {vault}. Run `orb memory init` first.")
        return

    # Count pages in wiki/ (recursively .md files, excluding directory headers)
    wiki_dir = vault / "wiki"
    page_files: list[Path] = []
    if wiki_dir.exists():
        page_files = sorted(p for p in wiki_dir.rglob("*.md") if p.is_file())

    page_count = len(page_files)

    # Count unique tags across all wiki pages (from frontmatter)
    tags: set[str] = set()
    for pf in page_files:
        text = pf.read_text(encoding="utf-8", errors="replace")
        # Extract tags from YAML frontmatter if present
        m = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
        if m:
            fm_text = m.group(1)
            tag_match = re.search(r"^tags:\s*\[([^\]]*)\]", fm_text, re.MULTILINE)
            if tag_match:
                raw_tags = tag_match.group(1)
                for t in re.split(r"[,\s]+", raw_tags):
                    t = t.strip().strip("'\"")
                    if t:
                        tags.add(t)

    # Last write date (across all vault files)
    last_write: str = "never"
    files_to_check: list[Path] = []
    for root_dir in [vault]:
        files_to_check.extend(
            p for p in root_dir.rglob("*") if p.is_file()
        )
    if files_to_check:
        last_file = max(files_to_check, key=lambda p: p.stat().st_mtime)
        last_write = datetime.fromtimestamp(
            last_file.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

    # Report
    print(f"Vault:   {vault}")
    print(f"Pages:   {page_count}")
    print(f"Tags:    {len(tags)}")
    if tags:
        print("  " + "  ".join(sorted(tags)))
    print(f"Written:  {last_write}")


# ── CLI: orb memory prune ───────────────────────────────────────────────────


def _cmd_memory_prune(args: argparse.Namespace) -> None:
    """Handle `orb memory prune`: archive page(s)."""
    config = MemoryConfig(
        vault_path=getattr(args, "vault_path", None) or "~/.orb/vault",
    )
    vault = _resolve_vault_path(config.vault_path)

    if not vault.exists():
        print(f"Vault not found at {vault}. Run `orb memory init` first.")
        return

    title = getattr(args, "page_title", None)
    if not title:
        print("Error: specify a page title to prune: `orb memory prune <page-title>`")
        return

    # Search for matching page in wiki/
    wiki_dir = vault / "wiki"
    matching: list[Path] = []
    if wiki_dir.exists():
        for pf in wiki_dir.rglob("*.md"):
            if pf.stem.lower() == title.lower():
                matching.append(pf)

    if not matching:
        print(f"Page not found: {title}")
        return

    # Archive (move to memories/)
    memories_dir = vault / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for old_path in matching:
        new_name = f"{old_path.stem}_archived_{old_path.suffix}"
        archive_path = memories_dir / f"{now}_{old_path.stem}{old_path.suffix}"
        # Handle duplicates
        counter = 1
        while archive_path.exists():
            archive_path = memories_dir / f"{now}_{old_path.stem}_{counter}{old_path.suffix}"
            counter += 1
        old_path.rename(archive_path)
        print(f"  Archived: {old_path} -> {archive_path}")

    print(f"Pruned {len(matching)} page(s).")


# ── argparse wiring ─────────────────────────────────────────────────────────


def add_memory_subparser(
    parser: argparse.ArgumentParser,
    subparsers: argparse._SubParsersAction,
) -> None:
    """Wire `orb memory` subcommands into the root argparse parser.

    Called by ``parse_args()`` in ``orb/cli/main.py``.
    """
    memory_parser = subparsers.add_parser("memory", help="Manage the persistent memory vault")
    memory_sub = memory_parser.add_subparsers(dest="memory_action")

    # orb memory init
    init_p = memory_sub.add_parser("init", help="Initialize the memory vault structure")
    init_p.add_argument(
        "--vault-path",
        type=str,
        default="~/.orb/vault",
        help="Path to the vault (default: ~/.orb/vault)",
    )

    # orb memory status
    memory_sub.add_parser("status", help="Report vault health (page count, tags, last write)")

    # orb memory prune <title>
    prune_p = memory_sub.add_parser("prune", help="Archive page(s) to memories/")
    prune_p.add_argument("page_title", help="Page title to archive (matches wiki/*.md stem)")


def _handle_memory_command(args: argparse.Namespace) -> None:
    """Dispatch `orb memory <action>` to the appropriate handler.

    Called from ``async_main()`` in ``orb/cli/main.py``.
    """
    action = getattr(args, "memory_action", None) or "init"
    if action == "init":
        _cmd_memory_init(args)
    elif action == "status":
        _cmd_memory_status(args)
    elif action == "prune":
        _cmd_memory_prune(args)
    else:
        print("Unknown memory action: use init | status | prune")
        import sys
        sys.exit(1)
