"""Unified memory-tools API.

High-level interface that agents call to read and write the vault.
Wraps a ``Store`` instance and exposes a clean, discoverable set of methods.

Phase 1: read operations only.  Phase 2: adds write operations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MemoryConfig
from .models import MarkdownPage, confidence_from_sources
from .store import Store


class MemoryTools:
    """Agent-facing read + write interface to the Orb memory vault.

    Parameters
        config  : optional ``MemoryConfig`` (defaults to disabled,
                  ``~/.orb/vault``).  When ``config.enabled`` is ``True``,
                  the vault directory is created automatically.

    Usage
        >>> mt = MemoryTools(MemoryConfig(enabled=True))
        >>> mt.read("quantum")
        >>> mt.write_entity("llm", "Large language model...")
    """

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config: MemoryConfig = config or MemoryConfig()
        self.store: Store | None = None
        self._ensure_initialized()

    # ── public read API (Phase 1 — unchanged) ────────────────────────────────

    def read(self, term: str) -> list[dict[str, Any]]:
        """Search by keyword across all wiki pages.

        Parameters
            term  : keyword / phrase to search for.

        Returns
            List of matching page dicts, sorted by relevance.
        """
        if self.store is None:
            return []
        return self.store.search_pages(term)

    def read_entity(self, name: str) -> dict[str, Any] | None:
        """Read a page by exact entity name (title match).

        Parameters
            name  : title / entity name.

        Returns
            Page dict, or ``None`` when not found.
        """
        if self.store is None:
            return None
        return self.store.read_page(name)

    def read_tag(self, tag: str) -> list[dict[str, Any]]:
        """Read pages by tag.

        Parameters
            tag  : tag name.

        Returns
            List of page dicts that carry the given tag.
        """
        if self.store is None:
            return []
        return self.store.pages_by_tag(tag)

    def list_pages(self) -> list[dict[str, Any]]:
        """Return vault index (index.md entries).

        Returns
            List of index entries (``[{"title": "...", "path": "..."}]``).
            Returns ``[]`` when index.md is missing.
        """
        if self.store is None:
            return []
        return self.store.read_index()

    def list_tags(self) -> list[str]:
        """Return the active tag taxonomy.

        Returns
            List of tag strings from SCHEMA.md.  May be empty for new vaults.
        """
        if self.store is None:
            return []
        return self.store.list_tags()

    def get_page(self, title: str) -> dict[str, Any] | None:
        """Low-level: get a raw page by title.

        Parameters
            title  : page title.

        Returns
            Page dict (with ``title``, ``path``, ``content``,
            ``frontmatter``, ``wikilinks``), or ``None``.
        """
        if self.store is None:
            return None
        return self.store.read_page(title)

    # ── vault initialization (Phase 1 — unchanged) ────────────────────────────

    def _ensure_initialized(self) -> None:
        """Create the vault directory structure if the vault is enabled.

        This is the equivalent of ``orb memory init`` but called implicitly
        from ``__init__``.  Safe to call multiple times (idempotent).
        """
        if not self.config.enabled:
            return

        vault = Path(self.config.vault_path).expanduser().resolve()

        # Create directory structure
        subdirs = ["wiki/entity", "wiki/concept", "wiki/analysis", "wiki/queries", "raw", "memories"]
        for subdir in subdirs:
            (vault / subdir).mkdir(parents=True, exist_ok=True)

        # Write SCHEMA.md if missing
        schema_path = vault / "SCHEMA.md"
        if not schema_path.is_file():
            schema_path.write_text(self._default_schema(), encoding="utf-8")

        # Write index.md if missing
        index_path = vault / "index.md"
        if not index_path.is_file():
            index_path.write_text("# Vault Index\n\nPages are listed by category.\n", encoding="utf-8")

        # Write log.md if missing
        log_path = vault / "log.md"
        if not log_path.is_file():
            log_path.write_text("# Action Log\n\n<!-- append-only log -->\n", encoding="utf-8")

        # Now load the store (which reads SCHEMA.md tags)
        self.store = Store(str(vault))

    @staticmethod
    def _default_schema() -> str:
        """Return default SCHEMA.md content (Phase 0 template)."""
        return (
            "---\ntitle: SCHEMA\ntype: meta\ntag_taxonomy:\n  - machine-learning\n  - software-engineering\n  - operations\n  - research\n  - design\n  - security\n  - data\n  - infrastructure\n---\n\n# Vault Schema\n\nTag taxonomy and conventions for the Orb memory vault.\n"
        )

    # ── public write API (Phase 2 — new) ──────────────────────────────────────

    def write(self, entity: str, content: str, page_type: str = "concept") -> MarkdownPage:
        """Write a new or updated wiki page.

        This is the primary write entry point.  It constructs a
        ``MarkdownPage`` from the provided parameters, writes it to disk
        via ``Store.write_page``, updates the index, logs the action,
        checks for page-length warnings, and syncs inbound links.

        Parameters
            entity    : page title (canonical name, lowercase-kebab).
            content   : page body text (without frontmatter).
            page_type : one of ``entity``, ``concept``, ``analysis``, ``query``.

        Returns
            The written ``MarkdownPage``.
        """
        page = MarkdownPage(
            title=entity,
            content=content,
            type=page_type,
            sources=[],
        )

        # Determine confidence based on content length and link targets
        outbound_links = []
        import re
        wikilink_re = re.compile(r"\[\[([^\]]+)\]\]")
        outbound_links = wikilink_re.findall(content)
        page.confidence = confidence_from_sources(
            source_count=len(page.sources),
            has_cross_links=len(outbound_links) >= 2,
        )

        if self.store is None:
            raise RuntimeError("Store not initialized — ensure vault is enabled.")

        self.store.write_page(page)

        # Check page length / split threshold
        self.store.log_split_warning(page)

        return page

    def write_entity(self, name: str, content: str) -> MarkdownPage:
        """Convenience method to write an entity-type page.

        Parameters
            name    : entity name (canonical, lowercase-kebab).
            content : entity description.

        Returns
            The written ``MarkdownPage``.
        """
        return self.write(entity=name, content=content, page_type="entity")

    def write_analysis(self, title: str, content: str, tags: list[str] | None = None) -> MarkdownPage:
        """Convenience method to write an analysis-type page.

        Parameters
            title   : analysis title (canonical, lowercase-kebab).
            content : analysis body text.
            tags    : optional list of tags from SCHEMA.md taxonomy.

        Returns
            The written ``MarkdownPage``.
        """
        page = MarkdownPage(
            title=title,
            content=content,
            type="analysis",
            tags=tags or [],
            sources=[],
        )

        if self.store is None:
            raise RuntimeError("Store not initialized — ensure vault is enabled.")

        self.store.write_page(page)
        self.store.log_split_warning(page)

        return page

    # ── convenience: write + link helpers ─────────────────────────────────────

    def write_with_sources(self, entity: str, content: str, source_files: list[str],
                           page_type: str = "concept") -> MarkdownPage:
        """Write a page with explicit source provenance.

        Parameters
            entity       : page title.
            content      : page body.
            source_files : list of source file names (for provenance markers).
            page_type    : page type (default ``concept``).

        Returns
            The written ``MarkdownPage``.
        """
        page = MarkdownPage(
            title=entity,
            content=content,
            type=page_type,
            sources=source_files,
        )
        # Re-evaluate confidence with actual sources (cross_links counts wikilinks in content, not sources)
        import re
        wikilink_re = re.compile(r"\[\[([^\]]+)\]\]")
        cross_links = len(wikilink_re.findall(content))
        page.confidence = confidence_from_sources(
            source_count=len(source_files),
            has_cross_links=cross_links >= 2,
        )
        if self.store is None:
            raise RuntimeError("Store not initialized.")
        self.store.write_page(page)
        self.store.log_split_warning(page)
        return page

    def update_page(self, title: str, new_content: str) -> MarkdownPage:
        """Update an existing page's body (bumps ``updated`` date).

        Parameters
            title       : page title to update.
            new_content : new body text.

        Returns
            The updated ``MarkdownPage``.
        """
        if self.store is None:
            raise RuntimeError("Store not initialized.")

        existing = self.store.get_page_by_title(title)
        if existing is None:
            # Fall through to create new page
            return self.write(entity=title, content=new_content)

        existing.content = new_content
        existing.updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.store.write_page(existing)
        self.store.log_split_warning(existing)
        return existing

    def sync_links(self, title: str) -> None:
        """Sync inbound links for a page (run after major edits).

        Parameters
            title  : page title whose inbound links should be fixed.
        """
        if self.store is None:
            return
        page = self.store.get_page_by_title(title)
        if page is not None:
            self.store.sync_inbound_links(page)
