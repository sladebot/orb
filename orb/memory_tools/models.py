"""Data models for the Orb memory vault.

These dataclasses represent parsed or constructed wiki pages — the
in-memory representation used by ``Store.write_page`` and
``MemoryTools.write``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MarkdownPage:
    """Structured representation of a wiki page (no frontmatter, no file path).

    Parameters
        title     : page filename without ``.md`` (canonical key).
        content   : body text (without frontmatter).
        type      : page type — one of ``entity | concept | analysis | query``.
        tags      : list of tags from SCHEMA.md taxonomy.
        sources   : raw source filenames that contributed to this page.
        created   : ISO date (YYYY-MM-DD) of first write.
        updated   : ISO date (YYYY-MM-DD) of last write — bumped on every update.
        confidence: ``high | medium | low`` (``None`` when not set).
        contested : ``True`` when conflicting claims exist (default ``False``).
    """

    title: str
    content: str
    type: str
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    confidence: str | None = None
    contested: bool = False

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not self.updated:
            self.updated = self.created
        # Normalise type to a lower-case set value
        valid_types = {"entity", "concept", "analysis", "query"}
        if self.type not in valid_types:
            self.type = "concept"  # default

    def __repr__(self) -> str:
        return f"MarkdownPage(title={self.title!r}, type={self.type!r}, sources={self.sources!r})"


# ── frontmatter / backmatter helpers ────────────────────────────────────────────


def _build_frontmatter(page: MarkdownPage) -> str:
    """Build a YAML frontmatter block for *page*.

    Follows the SCHEMA.md convention: title, type, tags (inline list),
    sources (inline list), created, updated, confidence (optional),
    contested (optional).
    """
    lines: list[str] = [
        "---",
        f"title: {page.title}",
        f"type: {page.type}",
    ]
    # Tags as inline list
    if page.tags:
        lines.append(f"tags: [{', '.join(page.tags)}]")
    else:
        lines.append("tags: []")
    # Sources as inline list
    if page.sources:
        lines.append(f"sources: [{', '.join(page.sources)}]")
    else:
        lines.append("sources: []")
    lines.append(f"created: {page.created}")
    lines.append(f"updated: {page.updated}")

    if page.confidence:
        lines.append(f"confidence: {page.confidence}")

    if page.contested:
        lines.append("contested: true")

    lines.append("---")
    return "\n".join(lines) + "\n"


def page_to_frontmatter_text(page: MarkdownPage) -> str:
    """Return the frontmatter block for a page (used when writing files).

    Parameters
        page  : the ``MarkdownPage`` to serialize.

    Returns
        A YAML frontmatter string terminated by a newline.
    """
    return _build_frontmatter(page)


def page_to_file_content(page: MarkdownPage) -> str:
    """Return the full file content (frontmatter + body) for a wiki page.

    Parameters
        page  : the ``MarkdownPage`` to write.

    Returns
        A complete Markdown string ready to be written to disk.
    """
    return _build_frontmatter(page) + "\n" + page.content + "\n"


def confidence_from_sources(
    source_count: int,
    has_cross_links: bool = False,
) -> str:
    """Determine confidence based on source count and cross-linking.

    Parameters
        source_count  : number of distinct source files.
        has_cross_links : whether the page links to 2+ other pages.

    Returns
        ``high``, ``medium``, ``low``, or ``None``.
    """
    if source_count >= 2 and has_cross_links:
        return "high"
    if source_count >= 2:
        return "medium"
    if source_count == 1:
        if has_cross_links:
            return "medium"
        return "low"
    return None
