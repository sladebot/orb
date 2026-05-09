"""Unified memory-tools API.

High-level interface that agents call to read the vault.  Wraps a
``Store`` instance and exposes a clean, discoverable set of methods.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import MemoryConfig
from .store import Store


class MemoryTools:
    """Agent-facing read interface to the Orb memory vault.

    Parameters
        config  : optional ``MemoryConfig`` (defaults to disabled,
                  ``~/.orb/vault``).  When ``config.enabled`` is ``True``,
                  the vault directory is created automatically.

    Usage
        >>> mt = MemoryTools(MemoryConfig(enabled=True))
        >>> mt.read("quantum")
        >>> mt.read_entity("llm")
        >>> mt.read_tag("machine-learning")
    """

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config: MemoryConfig = config or MemoryConfig()
        self.store: Store | None = None
        self._ensure_initialized()

    # ── public read API ─────────────────────────────────────────────────────

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

    # ── vault initialization ─────────────────────────────────────────────────

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
