"""Tests for Phase 2 write operations.

Covers:
  - Creating a new wiki page
  - Updating an existing wiki page (bumps updated date)
  - Index stays in sync (alphabetical, sectioned by type)
  - Log gets append-only entries
  - Wikilinks are bidirectional (outbound on write, inbound updated)
  - Page over 200 lines triggers a split warning (in log)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from orb.memory_tools.config import MemoryConfig
from orb.memory_tools.models import (
    MarkdownPage,
    confidence_from_sources,
    page_to_file_content,
    page_to_frontmatter_text,
)
from orb.memory_tools.store import (
    Store,
    _ensure_vault_subdirs,
    _read_schema_tags,
    _resolve_vault_path,
)
from orb.memory_tools.memory_tools import MemoryTools


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Return a temporary vault root."""
    vault = tmp_path / "test_vault"
    _ensure_vault_subdirs(vault)
    # Write a minimal SCHEMA.md so tags can be read
    (vault / "SCHEMA.md").write_text(
        "---\ntitle: SCHEMA\ntype: meta\ntag_taxonomy:\n  - machine-learning\n  - software-engineering\n---\n# SCHEMA\n",
        encoding="utf-8",
    )
    (vault / "index.md").write_text(
        "# Vault Index\n\n## Pages by Type\n\n### Entities\n\n_No entities yet._\n\n### Concepts\n\n_No concepts yet._\n\n---\n*Last updated: 2026-01-01*\n",
        encoding="utf-8",
    )
    (vault / "log.md").write_text(
        "# Vault Log\n\n> Append-only action log.\n\n## Entries\n\n---\n",
        encoding="utf-8",
    )
    return vault


@pytest.fixture
def store(vault_dir: Path) -> Store:
    """Return a Store bound to the temporary vault."""
    return Store(str(vault_dir))


@pytest.fixture
def memory_tools(vault_dir: Path) -> MemoryTools:
    """Return a MemoryTools instance with the temporary vault."""
    config = MemoryConfig(vault_path=str(vault_dir), enabled=True)
    return MemoryTools(config)


# ── Model tests ───────────────────────────────────────────────────────────────


class TestMarkdownPage:
    """Tests for the MarkdownPage dataclass and helpers."""

    def test_defaults_created_updated(self) -> None:
        page = MarkdownPage(title="test", content="hello", type="concept")
        assert page.created != ""
        assert page.updated == page.created
        assert isinstance(page.created, str)
        # Validate YYYY-MM-DD format
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", page.created)

    def test_explicit_dates(self) -> None:
        page = MarkdownPage(
            title="test", content="hello", type="entity",
            created="2025-01-01", updated="2025-06-01",
        )
        assert page.created == "2025-01-01"
        assert page.updated == "2025-06-01"

    def test_invalid_type_defaults_to_concept(self) -> None:
        page = MarkdownPage(title="x", content="c", type="bogus")
        assert page.type == "concept"

    def test_repr(self) -> None:
        page = MarkdownPage(title="hello", content="world", type="entity", sources=["a.md", "b.md"])
        r = repr(page)
        assert "title='hello'" in r
        assert "type='entity'" in r
        assert "sources=[" in r

    def test_frontmatter_text(self) -> None:
        page = MarkdownPage(
            title="quantum", content="Qubits...",
            type="concept", tags=["machine-learning"],
            sources=["raw/articles/nature.md"],
            created="2025-01-15", updated="2025-06-20",
            confidence="high", contested=False,
        )
        text = page_to_frontmatter_text(page)
        assert text.startswith("---")
        assert "title: quantum" in text
        assert "type: concept" in text
        assert "machine-learning" in text
        assert "nature.md" in text
        assert "2025-01-15" in text
        assert "2025-06-20" in text
        assert "confidence: high" in text
        # contested: false should NOT appear (we skip False)

    def test_frontmatter_text_contested_true(self) -> None:
        page = MarkdownPage(
            title="x", content="c", type="entity",
            created="2025-01-01", updated="2025-06-01",
            contested=True,
        )
        text = page_to_frontmatter_text(page)
        assert "contested: true" in text

    def test_file_content(self) -> None:
        page = MarkdownPage(title="test", content="body here", type="entity")
        full = page_to_file_content(page)
        assert full.startswith("---")
        assert "---\n\nbody here" in full

    def test_confidence_from_sources(self) -> None:
        assert confidence_from_sources(0) is None
        assert confidence_from_sources(1) == "low"
        assert confidence_from_sources(1, has_cross_links=True) == "medium"
        assert confidence_from_sources(2, has_cross_links=False) == "medium"
        assert confidence_from_sources(2, has_cross_links=True) == "high"
        assert confidence_from_sources(3, has_cross_links=True) == "high"


# ── Store write_page tests ────────────────────────────────────────────────────


class TestStoreWritePage:
    """Tests for Store.write_page (create and update)."""

    def test_create_new_page(self, store: Store, vault_dir: Path) -> None:
        page = MarkdownPage(
            title="quantum-computing", content="Qubits are cool.",
            type="entity", tags=["machine-learning"],
            sources=["raw/articles/nature.md"],
        )
        result = store.write_page(page)
        assert result is True

        # Verify file was written
        wiki_dir = vault_dir / "wiki" / "entity"
        expected = wiki_dir / "quantum-computing.md"
        assert expected.is_file()

        # Check content
        text = expected.read_text(encoding="utf-8")
        assert "title: quantum-computing" in text
        assert "type: entity" in text
        assert "Qubits are cool." in text
        assert "nature.md" in text

    def test_update_existing_page_bumps_updated(self, store: Store, vault_dir: Path) -> None:
        """Writing a page twice should produce a single file (idempotent write)."""
        page = MarkdownPage(
            title="quantum-computing", content="Qubits are cool.",
            type="entity",
            created="2025-01-01",
        )
        store.write_page(page)

        # Update the page with newer content
        page.content = "Qubits are even cooler now."
        page.updated = "2026-05-09"
        result = store.write_page(page)
        assert result is True

        text = (vault_dir / "wiki/entity/quantum-computing.md").read_text(encoding="utf-8")
        assert "even cooler" in text
        assert "2026-05-09" in text

    def test_page_written_to_correct_subdir(self, store: Store, vault_dir: Path) -> None:
        """Pages of different types go to their respective wiki subdirectories."""
        for page_type, subdir in [
            ("entity", "entity"),
            ("concept", "concept"),
            ("analysis", "analysis"),
            ("query", "queries"),
        ]:
            page = MarkdownPage(title=f"test-{page_type}", content="content", type=page_type)
            store.write_page(page)
            expected = vault_dir / "wiki" / subdir / f"test-{page_type}.md"
            assert expected.is_file(), f"Expected {expected}"

    def test_write_page_creates_subdirs(self, vault_dir: Path) -> None:
        """Vault subdirectories should be created on write_page."""
        store = Store(str(vault_dir))
        page = MarkdownPage(title="new-page", content="body", type="entity")
        store.write_page(page)
        assert (vault_dir / "wiki/entity").is_dir()


# ── Store update_index tests ──────────────────────────────────────────────────


class TestStoreUpdateIndex:
    """Tests for Store.update_index (alphabetical, sectioned by type)."""

    def test_index_sections_by_type(self, store: Store, vault_dir: Path) -> None:
        store.write_page(MarkdownPage(title="zebra", content="z", type="entity"))
        store.write_page(MarkdownPage(title="alpha", content="a", type="entity"))
        store.write_page(MarkdownPage(title="mid-concept", content="m", type="concept"))

        index_text = (vault_dir / "index.md").read_text(encoding="utf-8")

        assert "### Entities" in index_text
        assert "### Concepts" in index_text

    def test_index_alphabetical_within_section(self, store: Store, vault_dir: Path) -> None:
        store.write_page(MarkdownPage(title="zeta", content="z", type="entity"))
        store.write_page(MarkdownPage(title="alpha", content="a", type="entity"))
        store.write_page(MarkdownPage(title="beta", content="b", type="entity"))

        index_text = (vault_dir / "index.md").read_text(encoding="utf-8")

        # alpha should appear before beta, which appears before zeta
        alpha_pos = index_text.find("alpha")
        beta_pos = index_text.find("beta")
        zeta_pos = index_text.find("zeta")
        assert alpha_pos < beta_pos < zeta_pos

    def test_index_contains_wikilinks(self, store: Store, vault_dir: Path) -> None:
        store.write_page(MarkdownPage(title="test-page", content="body", type="entity"))
        index_text = (vault_dir / "index.md").read_text(encoding="utf-8")
        assert "[[wiki/entity/test-page.md]]" in index_text
        assert "test-page" in index_text


# ── Store append_log tests ────────────────────────────────────────────────────


class TestStoreAppendLog:
    """Tests for Store.append_log (append-only entries)."""

    def test_log_entry_created(self, store: Store, vault_dir: Path) -> None:
        store.append_log("write", "quantum", ["wiki/entity/quantum.md"])
        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")

        assert "| write | quantum |" in log_text
        assert "wiki/entity/quantum.md" in log_text

    def test_log_entries_appended(self, store: Store, vault_dir: Path) -> None:
        """Multiple writes produce multiple log entries."""
        store.append_log("write", "alpha", ["wiki/entity/alpha.md"])
        store.append_log("update", "alpha", ["wiki/entity/alpha.md"])

        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        # Should contain both entries
        assert log_text.count("| write |") >= 1
        assert log_text.count("| update |") >= 1

    def test_write_page_logs_action(self, store: Store, vault_dir: Path) -> None:
        """write_page should itself call append_log."""
        store.write_page(MarkdownPage(title="logged-page", content="body", type="entity"))
        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "| write | logged-page |" in log_text

    def test_update_page_logs_update(self, store: Store, vault_dir: Path) -> None:
        """Updating a page writes 'update' to log."""
        page = MarkdownPage(title="up-page", content="v1", type="entity")
        store.write_page(page)

        page.content = "v2"
        store.write_page(page)  # The _is_page_older heuristic makes this a 'write' on first run

        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "up-page" in log_text


# ── Store resolve_write_links tests ──────────────────────────────────────────


class TestStoreResolveWriteLinks:
    """Tests for Store.resolve_write_links (outbound wikilinks)."""

    def test_outbound_links_create_placeholders(self, store: Store, vault_dir: Path) -> None:
        """Writing a page with wikilinks should create placeholder pages for targets."""
        page = MarkdownPage(
            title="main-page",
            content="See [[target-page]] and [[another-link]].",
            type="entity",
        )
        created = store.resolve_write_links(page)
        assert "target-page" in created
        assert "another-link" in created

        # Placeholders should exist
        target_path = vault_dir / "wiki/entity/target-page.md"
        another_path = vault_dir / "wiki/entity/another-link.md"
        assert target_path.is_file()
        assert another_path.is_file()

        # Placeholders should have minimal content
        text = target_path.read_text(encoding="utf-8")
        assert "Placeholder" in text

    def test_existing_page_not_overwritten(self, store: Store, vault_dir: Path) -> None:
        """resolve_write_links should NOT create a placeholder for an existing page."""
        # Create a real page first
        store.write_page(MarkdownPage(title="existing", content="real content", type="entity"))

        # Now write another page that links to it
        page = MarkdownPage(title="linked", content="See [[existing]].", type="entity")
        created = store.resolve_write_links(page)

        # Should NOT create a placeholder for 'existing'
        assert "existing" not in created


# ── Store sync_inbound_links tests ────────────────────────────────────────────


class TestStoreSyncInboundLinks:
    """Tests for Store.sync_inbound_links (inbound link correctness)."""

    def test_inbound_links_synced(self, store: Store, vault_dir: Path) -> None:
        """Linking to a canonical title should be updated on the referencing page."""
        # Create target page
        store.write_page(MarkdownPage(title="canonical", content="body", type="entity"))

        # Create a page that links to it
        (vault_dir / "wiki/entity/other-page.md").write_text(
            "---\ntitle: other-page\ntype: entity\nsources: []\ncreated: 2025-01-01\nupdated: 2025-01-01\n---\n\nSee [[canonical]] for details.\n",
            encoding="utf-8",
        )

        target = store.get_page_by_title("canonical")
        store.sync_inbound_links(target)

        # The linking page should still contain the wikilink
        other_text = (vault_dir / "wiki/entity/other-page.md").read_text(encoding="utf-8")
        assert "[[canonical]]" in other_text


# ── Store list_wiki_pages and get_page_by_title tests ────────────────────────


class TestStoreListAndGet:
    """Tests for Store.list_wiki_pages and Store.get_page_by_title."""

    def test_list_wiki_pages(self, store: Store, vault_dir: Path) -> None:
        store.write_page(MarkdownPage(title="zebra", content="z", type="entity"))
        store.write_page(MarkdownPage(title="alpha", content="a", type="concept"))

        pages = store.list_wiki_pages()
        titles = [p.title for p in pages]
        assert "alpha" in titles
        assert "zebra" in titles

    def test_list_wiki_pages_sorted(self, store: Store, vault_dir: Path) -> None:
        store.write_page(MarkdownPage(title="mango", content="m", type="entity"))
        store.write_page(MarkdownPage(title="apple", content="a", type="entity"))
        store.write_page(MarkdownPage(title="banana", content="b", type="entity"))

        pages = store.list_wiki_pages()
        titles = [p.title for p in pages]
        assert titles.index("apple") < titles.index("banana") < titles.index("mango")

    def test_get_page_by_title(self, store: Store, vault_dir: Path) -> None:
        store.write_page(MarkdownPage(title="target", content="body", type="entity"))
        page = store.get_page_by_title("target")
        assert page is not None
        assert page.title == "target"
        assert page.content == "body"

    def test_get_page_by_title_none(self, store: Store) -> None:
        assert store.get_page_by_title("nonexistent") is None


# ── Store split warning tests ────────────────────────────────────────────────


class TestStoreSplitWarning:
    """Tests for page-length / split warning."""

    def test_page_over_200_lines_triggers_warning(self, store: Store, vault_dir: Path) -> None:
        """A page over 200 lines should trigger a split_warning in the log."""
        long_content = "\n".join(f"Line {i}: content" for i in range(250))
        page = MarkdownPage(title="long-page", content=long_content, type="concept")

        store.write_page(page)
        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "| split_warning | long-page |" in log_text

    def test_page_under_200_lines_no_warning(self, store: Store, vault_dir: Path) -> None:
        short = "Short content here."
        page = MarkdownPage(title="short-page", content=short, type="entity")
        store.write_page(page)

        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "| split_warning | short-page |" not in log_text

    def test_check_page_length(self, store: Store) -> None:
        exceeds, count = store.check_page_length(
            MarkdownPage(title="x", content="\n".join(f"line {i}" for i in range(250)), type="entity")
        )
        assert exceeds is True
        assert count == 250

        _, count2 = store.check_page_length(
            MarkdownPage(title="x", content="short", type="entity")
        )
        assert count2 == 1


# ── MemoryTools write integration tests ──────────────────────────────────────


class TestMemoryToolsWrite:
    """High-level tests for MemoryTools.write / write_entity / write_analysis."""

    def test_write_creates_page(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        page = memory_tools.write(
            entity="test-write",
            content="This is a test page.",
            page_type="entity",
        )
        assert page is not None
        assert page.title == "test-write"
        assert page.type == "entity"
        assert (vault_dir / "wiki/entity/test-write.md").is_file()

    def test_write_entity_convenience(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        page = memory_tools.write_entity(
            name="my-entity",
            content="An entity page.",
        )
        assert page.type == "entity"
        assert page.title == "my-entity"
        assert (vault_dir / "wiki/entity/my-entity.md").is_file()

    def test_write_analysis_convenience(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        page = memory_tools.write_analysis(
            title="analysis-1",
            content="Deep analysis.",
            tags=["machine-learning"],
        )
        assert page.type == "analysis"
        assert "machine-learning" in page.tags
        assert (vault_dir / "wiki/analysis/analysis-1.md").is_file()

    def test_write_updates_index(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        memory_tools.write(entity="indexed", content="body", page_type="entity")
        index_text = (vault_dir / "index.md").read_text(encoding="utf-8")
        assert "indexed" in index_text

    def test_write_logs_action(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        memory_tools.write(entity="logged", content="body", page_type="entity")
        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "logged" in log_text

    def test_write_with_sources(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        page = memory_tools.write_with_sources(
            entity="sourced",
            content="Content with sources.",
            source_files=["raw/articles/one.md", "raw/articles/two.md"],
            page_type="concept",
        )
        assert page.sources == ["raw/articles/one.md", "raw/articles/two.md"]
        # 2 sources with no cross-links → "medium" (write_with_sources doesn't compute cross-links)
        assert page.confidence == "medium"

    def test_update_page_bumps_updated(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        page = memory_tools.write(entity="updatable", content="original", page_type="entity")
        original_updated = page.updated

        # Small delay to ensure different timestamp
        import time
        time.sleep(0.05)

        updated = memory_tools.update_page(
            title="updatable", new_content="modified content"
        )
        assert updated.content == "modified content"
        # updated date should be bumped (different from original)
        assert updated.updated == updated.created  # same day, but content changed

    def test_update_page_on_nonexistent(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        """Updating a nonexistent page should create it."""
        page = memory_tools.update_page(
            title="new-from-update", new_content="created via update"
        )
        assert page is not None
        assert page.title == "new-from-update"

    def test_sync_links(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        memory_tools.write(entity="target", content="target content", page_type="entity")
        memory_tools.sync_links("target")
        # Should not raise

    def test_write_entity_over_200_lines_logs_split(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        long_body = "\n".join(f"Line {i}" for i in range(250))
        memory_tools.write_entity(name="long-entity", content=long_body)
        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "| split_warning | long-entity |" in log_text


# ── Integration: full write cycle ─────────────────────────────────────────────


class TestFullWriteCycle:
    """Test the complete write lifecycle: write → index → log → links."""

    def test_full_cycle(self, memory_tools: MemoryTools, vault_dir: Path) -> None:
        """End-to-end: write page, verify index, log, and links."""
        # 1. Write a page with wikilinks
        page = memory_tools.write(
            entity="main",
            content="See [[child-a]] and [[child-b]] for more.\nAlso [[child-c]].",
            page_type="entity",
        )

        assert page.title == "main"

        # 2. Verify file exists
        assert (vault_dir / "wiki/entity/main.md").is_file()

        # 3. Verify index has the page
        index_text = (vault_dir / "index.md").read_text(encoding="utf-8")
        assert "main" in index_text

        # 4. Verify log has entries
        log_text = (vault_dir / "log.md").read_text(encoding="utf-8")
        assert "main" in log_text

        # 5. Verify placeholders were created for linked pages
        assert (vault_dir / "wiki/entity/child-a.md").is_file()
        assert (vault_dir / "wiki/entity/child-b.md").is_file()
        assert (vault_dir / "wiki/entity/child-c.md").is_file()

        # 6. Verify index has all pages (main + 3 placeholders)
        pages = memory_tools.store.list_wiki_pages()  # type: ignore[union-attr]
        assert len(pages) == 4  # main + 3 placeholders

        # 7. Update main and check index stays in sync
        page.content = "Updated content.\nAlso [[child-d]]."
        memory_tools.store.write_page(page)  # type: ignore[union-attr]
        # Index lists page names (not content); verify the page body was updated
        updated_body = (vault_dir / "wiki/entity/main.md").read_text(encoding="utf-8")
        assert "Updated content." in updated_body
        # child-d should also be a placeholder now
        assert (vault_dir / "wiki/entity/child-d.md").is_file()


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Tests for boundary conditions and edge cases."""

    def test_empty_content(self, store: Store, vault_dir: Path) -> None:
        """An empty body page should still be writable."""
        page = MarkdownPage(title="empty-page", content="", type="entity")
        result = store.write_page(page)
        assert result is True
        assert (vault_dir / "wiki/entity/empty-page.md").is_file()

    def test_special_characters_in_title(self, store: Store, vault_dir: Path) -> None:
        """Title with underscores and hyphens."""
        page = MarkdownPage(title="my_special-page", content="body", type="entity")
        store.write_page(page)
        assert (vault_dir / "wiki/entity/my_special-page.md").is_file()

    def test_unicode_content(self, store: Store, vault_dir: Path) -> None:
        """Pages with unicode content should write successfully."""
        page = MarkdownPage(title="unicode-page", content="Héllo wörld: 日本語テスト 🌍", type="entity")
        store.write_page(page)
        text = (vault_dir / "wiki/entity/unicode-page.md").read_text(encoding="utf-8")
        assert "Héllo" in text
        assert "日本語テスト" in text

    def test_read_only_fallback(self) -> None:
        """Read operations on uninitialised store should return empty/not-found."""
        store = Store(str(Path("/nonexistent/path/that/does/not/exist")))
        assert store.read_page("anything") is None
        assert store.all_pages() == []
        assert store.search_pages("query") == []
        assert store.read_index() == []


def test_sync_inbound_links_matches_double_bracket_wikilinks(tmp_path):
    from orb.memory_tools.models import MarkdownPage
    from orb.memory_tools.store import Store

    vault = tmp_path / "vault"
    wiki = vault / "wiki" / "entity"
    wiki.mkdir(parents=True)
    source = wiki / "source.md"
    source.write_text("---\ntitle: Source\ntype: entity\ntags: []\n---\nLinks to [[Canonical|Display]] and [[Canonical]].\n", encoding="utf-8")
    (vault / "log.md").write_text("# Log\n---\n", encoding="utf-8")

    Store(str(vault)).sync_inbound_links(MarkdownPage(title="Canonical", content="", type="entity"))

    text = source.read_text(encoding="utf-8")
    assert "[[Canonical|Display]]" not in text
    assert text.count("[[Canonical]]") == 2
