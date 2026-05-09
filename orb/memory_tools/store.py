"""Backend abstraction for read operations on the vault.

Handles filesystem traversal, frontmatter + wikilink parsing, and index
management.  All public methods are read-only (Phase 1).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .parsers import extract_frontmatter, extract_wikilinks, parse_page


def _resolve_vault_path(vault_path: str) -> Path:
    """Expand ~ and return the vault root."""
    return Path(vault_path).expanduser().resolve()


def _read_schema_tags(vault_root: Path) -> list[str]:
    """Extract tag taxonomy from SCHEMA.md (Phase 0 creates it)."""
    schema = vault_root / "SCHEMA.md"
    if not schema.is_file():
        return []
    raw = schema.read_text(encoding="utf-8")
    fm = extract_frontmatter(raw)
    tags = fm.get("tag_taxonomy", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tags = [str(t).strip() for t in tags]
    return tags


class Store:
    """Read-only backend for the Orb vault.

    Parameters
        vault_path  : path to the vault root (``~/.orb/vault`` by default).

    Attributes
        vault_root  : resolved, absolute Path to the vault.
        tag_taxonomy: active tag list from SCHEMA.md.
    """

    def __init__(self, vault_path: str = "~/.orb/vault") -> None:
        self.vault_root: Path = _resolve_vault_path(vault_path)
        self.tag_taxonomy: list[str] = _read_schema_tags(self.vault_root)

    # ── page reads ──────────────────────────────────────────────────────────

    def read_page(self, title: str) -> dict[str, Any] | None:
        """Read a single wiki page by title.

        Searches ``wiki/`` subdirectories first (entity/, concept/,
        analysis/, queries/), then falls back to the vault root.

        Parameters
            title  : page title (case-insensitive match on filename stem).

        Returns
            Parsed page dict, or ``None`` when not found.
        """
        candidates = self._find_candidates(title)
        for c in candidates:
            result = parse_page(str(c))
            if result is not None:
                return result
        return None

    def search_pages(self, query: str) -> list[dict[str, Any]]:
        """Full-text search across all ``wiki/*.md`` pages.

        Searches title, frontmatter values, and body text.  Results are
        sorted by a simple score: title matches > frontmatter matches >
        body matches.

        Parameters
            query  : keyword / phrase to search for (lower-cased internally).

        Returns
            List of parsed page dicts, sorted by relevance score.
        """
        if not query:
            return []
        q = query.lower().strip()
        pages = self.all_pages()
        scored: list[tuple[int, dict[str, Any]]] = []
        for page in pages:
            score = 0
            title = (page.get("frontmatter", {}).get("title") or page.get("title", "")).lower()
            content = (page.get("content") or "").lower()
            fm_text = " ".join(
                str(v).lower()
                for v in (page.get("frontmatter") or {}).values()
            )
            if title == q:
                score += 100
            elif title and q in title:
                score += 50
            if fm_text and q in fm_text:
                score += 30
            if content and q in content:
                score += 10
            if score > 0:
                scored.append((score, page))
        scored.sort(key=lambda x: (-x[0], x[1].get("title", "")))
        return [p for _, p in scored]

    def pages_by_tag(self, tag: str) -> list[dict[str, Any]]:
        """Return all pages that carry the given tag.

        Parameters
            tag  : tag name (case-sensitive, must match frontmatter).

        Returns
            List of parsed page dicts that have the tag in frontmatter.
        """
        if not tag:
            return []
        all_pages = self.all_pages()
        return [
            p for p in all_pages
            if tag in (p.get("frontmatter") or {}).get("tags", [])
        ]

    def all_pages(self) -> list[dict[str, Any]]:
        """Scan ``wiki/`` and return every parsed page.

        Pages are returned sorted by title (case-insensitive).

        Returns
            List of parsed page dicts.  May be empty (new vault).
        """
        wiki_dir = self.vault_root / "wiki"
        if not wiki_dir.is_dir():
            return []
        pages: list[dict[str, Any]] = []
        for md_file in sorted(wiki_dir.rglob("*.md")):
            result = parse_page(str(md_file))
            if result is not None:
                pages.append(result)
        pages.sort(key=lambda p: (p.get("frontmatter") or {}).get("title", p.get("title", "")).lower())
        return pages

    def resolve_wikilink(self, link: str) -> dict[str, Any] | None:
        """Resolve a ``[[wikilink]]`` to its target page.

        Handles both ``[[page]]`` and ``[[page|Display]]`` forms by
        normalising the link name and searching the vault.

        Parameters
            link  : raw wikilink string (with or without surrounding brackets).

        Returns
            Parsed page dict, or ``None`` when target not found.
        """
        # Strip surrounding brackets if present
        raw = link.strip()
        if raw.startswith("[["):
            raw = raw[2:]
        if raw.endswith("]]"):
            raw = raw[:-2]
        from .parsers import normalize_wikilink
        name = normalize_wikilink(raw)
        return self.read_page(name)

    # ── index / taxonomy helpers ──────────────────────────────────────────────

    def read_index(self) -> list[dict[str, Any]]:
        """Read vault ``index.md`` and return entries.

        Returns
            List of dicts with ``title`` and ``path`` keys for each
            index entry.  Returns ``[]`` when index.md is missing.
        """
        idx_path = self.vault_root / "index.md"
        if not idx_path.is_file():
            return []
        result = parse_page(str(idx_path))
        if result is None:
            return []
        return result

    def list_tags(self) -> list[str]:
        """Return the active tag taxonomy from SCHEMA.md."""
        return self.tag_taxonomy

    # ── internal helpers ──────────────────────────────────────────────────────

    def _find_candidates(self, title: str) -> list[Path]:
        """Find .md files matching the title (case-insensitive stem)."""
        stem = title.lower()
        wiki_dir = self.vault_root / "wiki"
        candidates: list[Path] = []
        if wiki_dir.is_dir():
            candidates.extend(wiki_dir.rglob(f"{stem}.md"))
            candidates.extend(wiki_dir.rglob(f"{stem}-*.md"))
        # Also check vault root (e.g. SCHEMA.md, index.md)
        for f in self.vault_root.glob("*.md"):
            if f.stem.lower() == stem:
                candidates.append(f)
        return candidates
