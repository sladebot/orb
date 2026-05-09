"""Backend abstraction for the Orb vault — read and write operations.

Phase 1 added read-only traversal.  Phase 2 extends ``Store`` with write
methods: ``write_page``, ``update_index``, ``append_log``,
``resolve_write_links``, and ``sync_inbound_links``.

All public methods follow the same conventions as Phase 1:
- Paths are resolved via ``_resolve_vault_path``.
- Frontmatter is produced by ``models.page_to_frontmatter_text``.
- Wikilinks use double-bracket syntax: ``[[page-title]]``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MarkdownPage, page_to_file_content
from .parsers import (
    extract_frontmatter,
    extract_wikilinks,
    normalize_wikilink,
    parse_page,
)


# ── Constants ──────────────────────────────────────────────────────────────────

# Subdirectories (plural "queries" for the directory name).
WIKI_SUBDIRS = (
    "wiki/entity",
    "wiki/concept",
    "wiki/analysis",
    "wiki/queries",
)

# ── Helpers ─────────────────────────────────────────────────────────────────────


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


# Mapping of page type to wiki subdirectory name.
_TYPE_TO_SUBDIR: dict[str, str] = {
    "entity": "entity",
    "concept": "concept",
    "analysis": "analysis",
    "query": "queries",
}


def _vault_type_dir(vault_root: Path, page_type: str) -> Path:
    """Return the wiki subdirectory for a given page type."""
    subdir_name = _TYPE_TO_SUBDIR.get(page_type, f"{page_type}s")
    sub = f"wiki/{subdir_name}"
    target = vault_root / sub
    target.mkdir(parents=True, exist_ok=True)
    return target


def _ensure_vault_subdirs(vault_root: Path) -> None:
    """Create all wiki subdirectories (idempotent)."""
    for sub in WIKI_SUBDIRS:
        (vault_root / sub).mkdir(parents=True, exist_ok=True)


def _log_entry(timestamp: str, action: str, subject: str, notes: str = "") -> str:
    """Return a single CSV-style log line for log.md.

    The log uses a CSV header row followed by pipe-delimited entries.
    """
    return f"| {timestamp} | {action} | {subject} | {notes} |\n"


def _insert_into_log(log_path: Path, entry: str) -> None:
    """Append an entry after the header table, before the closing ``---``.

    Strategy: find the ``---`` closing line; insert the new entry before it,
    then re-add the closing ``---``.
    """
    text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""

    # Find the last '---' (closing fence)
    close_idx = text.rfind("---")
    if close_idx == -1:
        # Append at end
        text = text.rstrip("\n") + "\n" + entry + "---\n"
    else:
        # Insert before the closing ---
        text = text[:close_idx].rstrip("\n") + "\n" + entry + text[close_idx:]

    log_path.write_text(text, encoding="utf-8")


def _update_index_file(vault_root: Path, all_pages: list[MarkdownPage]) -> None:
    """Rewrite index.md as alphabetical sections by type.

    Sections: Entities, Concepts, Analysis, Queries.
    Each section lists pages alphabetically by title.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    sections: dict[str, list[MarkdownPage]] = {}
    type_labels = {
        "entity": "Entities",
        "concept": "Concepts",
        "analysis": "Analysis",
        "query": "Queries",
    }
    for page in all_pages:
        label = type_labels.get(page.type, f"{page.type}s")
        sections.setdefault(label, []).append(page)

    parts: list[str] = [
        "# Vault Index",
        "",
        f"> Auto-generated page catalog.  Updated: {now}",
        "",
        "## Pages by Type",
        "",
    ]

    for label, pages in sections.items():
        pages.sort(key=lambda p: p.title.lower())
        parts.append(f"### {label}")
        parts.append("")
        for page in pages:
            wiki_path = f"wiki/{page.type}/{page.title}.md"
            parts.append(f"- [[{wiki_path}]] {page.title}")
        parts.append("")

    parts.append("---")
    parts.append(f"*Last updated: {now}*")

    (vault_root / "index.md").write_text("\n".join(parts) + "\n", encoding="utf-8")


# ── Read operations (Phase 1 — preserved) ──────────────────────────────────────


class Store:
    """Read + write backend for the Orb vault.

    Parameters
        vault_path  : path to the vault root (``~/.orb/vault`` by default).

    Attributes
        vault_root  : resolved, absolute Path to the vault.
        tag_taxonomy: active tag list from SCHEMA.md.
    """

    def __init__(self, vault_path: str = "~/.orb/vault") -> None:
        self.vault_root: Path = _resolve_vault_path(vault_path)
        self.tag_taxonomy: list[str] = _read_schema_tags(self.vault_root)

    # ── page reads (Phase 1 — unchanged) ──────────────────────────────────────

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
        raw = link.strip()
        if raw.startswith("[["):
            raw = raw[2:]
        if raw.endswith("]]"):
            raw = raw[:-2]
        name = normalize_wikilink(raw)
        return self.read_page(name)

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

    def _find_candidates(self, title: str) -> list[Path]:
        """Find .md files matching the title (case-insensitive stem)."""
        stem = title.lower()
        wiki_dir = self.vault_root / "wiki"
        candidates: list[Path] = []
        if wiki_dir.is_dir():
            candidates.extend(wiki_dir.rglob(f"{stem}.md"))
            candidates.extend(wiki_dir.rglob(f"{stem}-*.md"))
        for f in self.vault_root.glob("*.md"):
            if f.stem.lower() == stem:
                candidates.append(f)
        return candidates

    # ── Page-to-MarkdownPage conversion helpers ───────────────────────────────

    def _page_dict_to_markdown_page(self, page_dict: dict[str, Any]) -> MarkdownPage:
        """Convert a ``parse_page`` result (dict) to a ``MarkdownPage``."""
        fm = page_dict.get("frontmatter") or {}
        return MarkdownPage(
            title=page_dict.get("title", page_dict.get("path", "").replace(".md", "").split("/")[-1]),
            content=page_dict.get("content", ""),
            type=fm.get("type", "concept") or "concept",
            tags=fm.get("tags", []),
            sources=fm.get("sources", []),
            created=fm.get("created", ""),
            updated=fm.get("updated", ""),
            confidence=fm.get("confidence"),
            contested=fm.get("contested", False),
        )

    def _page_to_path(self, page: MarkdownPage) -> Path:
        """Return the filesystem path for a page."""
        return _vault_type_dir(self.vault_root, page.type) / f"{page.title}.md"

    # ── Write operations (Phase 2 — new) ──────────────────────────────────────

    def write_page(self, page: MarkdownPage) -> bool:
        """Create or update a page in ``wiki/<type>/``.

        Parameters
            page  : the ``MarkdownPage`` to write.

        Returns
            ``True`` when the file was successfully written (new or updated).
        """
        _ensure_vault_subdirs(self.vault_root)

        path = self._page_to_path(page)
        # Check existence BEFORE writing (write_text creates the file)
        is_new = not path.exists() or self._is_page_older(page, path)
        full_content = page_to_file_content(page)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(full_content, encoding="utf-8")

        # Update index and log
        self.update_index(page)
        self.append_log(
            action="write" if is_new else "update",
            subject=page.title,
            files=[str(path)],
        )

        # Resolve outbound links (create empty placeholder pages for missing links)
        self.resolve_write_links(page)

        # Check page length / split threshold
        self.log_split_warning(page)

        return True

    def _is_page_older(self, page: MarkdownPage, path: Path) -> bool:
        """Heuristic: the page is 'new' if no file exists or it's very old."""
        if not path.exists():
            return True
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return mtime.year < datetime.now(timezone.utc).year - 1

    def update_index(self, page: MarkdownPage) -> None:
        """Add or update *page* in ``index.md`` (alphabetical, sectioned by type).

        Parameters
            page  : the ``MarkdownPage`` to add to the index.
        """
        _ensure_vault_subdirs(self.vault_root)
        all_pages = self.list_wiki_pages()
        _update_index_file(self.vault_root, all_pages)

    def append_log(self, action: str, subject: str, files: list[str]) -> None:
        """Append an entry to the vault ``log.md`` (append-only).

        Parameters
            action  : action type (e.g. ``write``, ``update``, ``prune``, ``split_warning``).
            subject : page or entity name.
            files   : list of affected file paths.
        """
        _ensure_vault_subdirs(self.vault_root)
        log_path = self.vault_root / "log.md"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = _log_entry(now, action, subject, ", ".join(files))

        # Create log.md if it doesn't exist (minimal bootstrap)
        if not log_path.is_file():
            log_path.write_text(
                "# Vault Log\n\n> Append-only action log.\n\n## Entries\n\n" + entry + "---\n",
                encoding="utf-8",
            )
        else:
            _insert_into_log(log_path, entry)

    def resolve_write_links(self, page: MarkdownPage) -> list[str]:
        """Find outbound wikilinks that need target pages to exist.

        For every outbound link that does not yet point to an existing page,
        this method creates a minimal placeholder page (empty body) so that
        the link is valid.

        Parameters
            page  : the page being written (contains outbound wikilinks in content).

        Returns
            List of target page names that needed to be created (or already existed).
        """
        _ensure_vault_subdirs(self.vault_root)
        outbound = extract_wikilinks(page.content)
        created: list[str] = []

        for link_name in outbound:
            target = self.read_page(link_name)
            if target is None:
                # Create a placeholder page
                placeholder = MarkdownPage(
                    title=link_name,
                    content="<!-- Placeholder — no content yet -->\n",
                    type="entity",
                    tags=["status:draft"],
                    sources=[f"{page.title}.md"],
                    created=page.created,
                    updated=page.created,
                )
                # We write to a different subdirectory (entity) since we don't know the type
                _vault_type_dir(self.vault_root, "entity")
                path = _vault_type_dir(self.vault_root, "entity") / f"{link_name}.md"
                path.write_text(page_to_file_content(placeholder), encoding="utf-8")
                created.append(link_name)

        return created

    def sync_inbound_links(self, page: MarkdownPage) -> None:
        """Ensure pages that link to *this* page have correct wikilinks.

        Walks all wiki pages and checks that any references to this page
        use the canonical title.  Updates any stale links (e.g.
        ``[[old-name]]`` → ``[[canonical-title]]``).

        Parameters
            page  : the page whose inbound links should be fixed.
        """
        _ensure_vault_subdirs(self.vault_root)
        wiki_dir = self.vault_root / "wiki"
        canonical = page.title

        for md_file in sorted(wiki_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            # Pattern for wikilinks pointing to this page (direct or with display text)
            pattern = re.compile(r"\[\[(" + re.escape(canonical) + r"|" + re.escape(canonical) + r"\|[^\]]+)\]\]")
            if not pattern.search(text):
                continue

            # Replace old link form with canonical form
            new_text = pattern.sub(rf"[[{canonical}]]", text)
            if new_text != text:
                md_file.write_text(new_text, encoding="utf-8")
                self.append_log(
                    action="link_sync",
                    subject=canonical,
                    files=[str(md_file)],
                )

    def list_wiki_pages(self) -> list[MarkdownPage]:
        """Return all wiki pages as ``MarkdownPage`` objects.

        Returns
            List of ``MarkdownPage`` instances sorted by title (case-insensitive).
            May be empty (new vault).
        """
        wiki_dir = self.vault_root / "wiki"
        if not wiki_dir.is_dir():
            return []
        pages: list[MarkdownPage] = []
        for md_file in sorted(wiki_dir.rglob("*.md")):
            result = parse_page(str(md_file))
            if result is not None:
                pages.append(self._page_dict_to_markdown_page(result))
        pages.sort(key=lambda p: p.title.lower())
        return pages

    def get_page_by_title(self, title: str) -> MarkdownPage | None:
        """Read a single wiki page by title and return as ``MarkdownPage``.

        Parameters
            title  : page title (case-insensitive match on filename stem).

        Returns
            ``MarkdownPage`` or ``None`` when not found.
        """
        candidates = self._find_candidates(title)
        for c in candidates:
            result = parse_page(str(c))
            if result is not None:
                return self._page_dict_to_markdown_page(result)
        return None

    # ── Split / page-threshold helpers ────────────────────────────────────────

    def check_page_length(self, page: MarkdownPage) -> tuple[bool, int]:
        """Check if a page exceeds the line threshold (200 lines).

        Returns
            Tuple of (exceeds_threshold, line_count).
        """
        line_count = len(page.content.splitlines())
        return line_count > 200, line_count

    def log_split_warning(self, page: MarkdownPage) -> None:
        """If a page exceeds 200 lines, log a split warning."""
        exceeds, count = self.check_page_length(page)
        if exceeds:
            self.append_log(
                action="split_warning",
                subject=page.title,
                files=[str(self._page_to_path(page))],
            )
