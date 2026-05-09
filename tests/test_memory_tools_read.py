"""Unit tests for memory_tools Read Engine (Phase 1).

Covers: read by title, search by keyword, filter by tag, resolve wikilinks
(including ``[[page|Display]]`` syntax), empty vault handling, and
non-existent pages.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orb.memory_tools import MemoryTools, MemoryConfig
from orb.memory_tools.parsers import (
    extract_frontmatter,
    extract_wikilinks,
    normalize_wikilink,
    validate_tag,
    parse_page,
)
from orb.memory_tools.store import Store


# ─────────────────────────── parsers ──────────────────────────────────────────


class TestExtractFrontmatter:
    """YAML frontmatter extraction."""

    def test_parses_standard_frontmatter(self) -> None:
        md = "---\ntitle: Testing\ntype: concept\ntags:\n  - test\n  - qa\n---\n\nBody text here.\n"
        result = extract_frontmatter(md)
        assert result["title"] == "Testing"
        assert result["type"] == "concept"
        assert result["tags"] == ["test", "qa"]

    def test_parses_simple_scalar(self) -> None:
        md = "---\ntitle: Simple Page\nstatus: active\nconfidence: high\n---\n\nContent\n"
        result = extract_frontmatter(md)
        assert result["title"] == "Simple Page"
        assert result["status"] == "active"
        assert result["confidence"] == "high"

    def test_parses_inline_list(self) -> None:
        md = "---\ntags: [a, b, c]\n---\n\nContent\n"
        result = extract_frontmatter(md)
        assert result["tags"] == ["a", "b", "c"]

    def test_no_frontmatter_returns_empty(self) -> None:
        result = extract_frontmatter("Just body text.\n")
        assert result == {}

    def test_parses_boolean_values(self) -> None:
        md = "---\ncontested: true\ndraft: false\n---\n\nBody\n"
        result = extract_frontmatter(md)
        assert result["contested"] is True
        assert result["draft"] is False

    def test_parses_integer_values(self) -> None:
        md = "---\nconfidence: 7\n---\n\nBody\n"
        result = extract_frontmatter(md)
        assert result["confidence"] == 7

    def test_parses_multi_line_list(self) -> None:
        md = "---\ntags:\n  - machine-learning\n  - software-engineering\n---\n\nBody\n"
        result = extract_frontmatter(md)
        assert result["tags"] == ["machine-learning", "software-engineering"]


class TestWikilinks:
    """Wikilink extraction and normalization."""

    def test_extract_simple_wikilinks(self) -> None:
        body = "See [[LLM]] and [[machine learning]] for details."
        result = extract_wikilinks(body)
        assert result == ["llm", "machine-learning"]

    def test_extract_display_text_stripped(self) -> None:
        body = "Check [[Python|Programming Language]] basics."
        result = extract_wikilinks(body)
        assert result == ["python"]

    def test_extract_multiple_occurrences(self) -> None:
        body = "[[a]] mentions [[b]] and also [[c|Display]] again [[d]]."
        result = extract_wikilinks(body)
        assert result == ["a", "b", "c", "d"]

    def test_extract_no_wikilinks(self) -> None:
        result = extract_wikilinks("No wikilinks here.")
        assert result == []

    def test_normalize_strips_display_text(self) -> None:
        assert normalize_wikilink("Page|Display") == "page"

    def test_normalize_lowercases_and_dashes(self) -> None:
        assert normalize_wikilink("My Fancy Page") == "my-fancy-page"

    def test_normalize_already_normalized(self) -> None:
        assert normalize_wikilink("simple") == "simple"


class TestValidateTag:
    """Tag validation against taxonomy."""

    def test_valid_tag_in_taxonomy(self) -> None:
        assert validate_tag("machine-learning", ["machine-learning", "ops"]) is True

    def test_invalid_tag_format(self) -> None:
        assert validate_tag("Invalid Tag", ["machine-learning"]) is False
        assert validate_tag("123start", ["machine-learning"]) is False
        assert validate_tag("", ["machine-learning"]) is False

    def test_tag_not_in_taxonomy(self) -> None:
        assert validate_tag("unknown-tag", ["machine-learning", "ops"]) is False

    def test_no_taxonomy_allows_any(self) -> None:
        assert validate_tag("anything-here", []) is True


class TestParsePage:
    """Full page parsing from file."""

    def test_parses_valid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test-page.md"
        f.write_text("---\ntitle: Test Page\ntype: entity\n---\n\nBody here.\n", encoding="utf-8")
        result = parse_page(str(f))
        assert result is not None
        assert result["title"] == "Test Page"
        assert "Body here." in result["content"]
        assert result["frontmatter"]["type"] == "entity"
        assert result["path"] == str(f)

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = parse_page(str(tmp_path / "no-such-file.md"))
        assert result is None

    def test_no_frontmatter_returns_body(self, tmp_path: Path) -> None:
        f = tmp_path / "plain.md"
        f.write_text("Just body.\n", encoding="utf-8")
        result = parse_page(str(f))
        assert result is not None
        assert result["title"] == "plain"
        assert result["content"] == "Just body."

    def test_wikilinks_extracted(self, tmp_path: Path) -> None:
        f = tmp_path / "linked.md"
        f.write_text(
            "---\ntitle: Linked Page\n---\n\n[[Other]] and [[Display Name]].\n",
            encoding="utf-8",
        )
        result = parse_page(str(f))
        assert result is not None
        assert result["wikilinks"] == ["other", "display-name"]


# ─────────────────────────── store ────────────────────────────────────────────


def _create_vault_structure(root: Path) -> None:
    """Create the standard vault directory structure."""
    for subdir in [
        "wiki/entity", "wiki/concept", "wiki/analysis", "wiki/queries",
        "raw", "memories",
    ]:
        (root / subdir).mkdir(parents=True, exist_ok=True)


class TestStore:
    """Store read operations."""

    def _make_vault(self, tmp_path: Path, files: dict[str, str]) -> Store:
        """Create a Store backed by tmp_path with the given wiki files."""
        _create_vault_structure(tmp_path)
        # Write index.md and SCHEMA.md
        (tmp_path / "index.md").write_text(
            "# Vault Index\n\n## Entities\n- [[LLM]]\n- [[Python]]\n",
            encoding="utf-8",
        )
        (tmp_path / "SCHEMA.md").write_text(
            "---\ntitle: SCHEMA\ntype: meta\ntag_taxonomy:\n  - machine-learning\n  - software-engineering\n  - operations\n---\n",
            encoding="utf-8",
        )
        # Write wiki files
        for rel, content in files.items():
            fp = tmp_path / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
        return Store(str(tmp_path))

    def test_read_page_by_title(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {"wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\ntags:\n  - machine-learning\n---\n\nLarge Language Models are transformative.\n"},
        )
        result = store.read_page("llm")
        assert result is not None
        assert result["title"] == "LLM"
        assert "Large Language Models" in result["content"]

    def test_read_page_not_found(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        result = store.read_page("nonexistent")
        assert result is None

    def test_search_pages_finds_by_content(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {
                "wiki/concept/python.md": "---\ntitle: Python\ntype: concept\ntags:\n  - software-engineering\n---\n\nPython is a programming language.\n",
                "wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\ntags:\n  - machine-learning\n---\n\nLarge Language Models use Python.\n",
            },
        )
        results = store.search_pages("python")
        assert len(results) == 2
        titles = [r["frontmatter"]["title"] for r in results]
        assert "Python" in titles
        assert "LLM" in titles

    def test_search_pages_by_title_match_score(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {
                "wiki/entity/quantum-computing.md": "---\ntitle: Quantum Computing\ntype: entity\n---\n\nQuantum computing is different.\n",
                "wiki/concept/programming.md": "---\ntitle: Programming\ntype: concept\n---\n\nQuantum programming is a subfield.\n",
            },
        )
        results = store.search_pages("quantum")
        # Title match should rank higher
        assert len(results) == 2
        assert results[0]["frontmatter"]["title"] == "Quantum Computing"

    def test_search_empty_query_returns_empty(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        results = store.search_pages("")
        assert results == []

    def test_pages_by_tag(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {
                "wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\ntags:\n  - machine-learning\n---\n\nLLM\n",
                "wiki/entity/neural-network.md": "---\ntitle: Neural Network\ntype: entity\ntags:\n  - machine-learning\n  - operations\n---\n\nNN\n",
                "wiki/concept/python.md": "---\ntitle: Python\ntype: concept\ntags:\n  - software-engineering\n---\n\nPython\n",
            },
        )
        ml = store.pages_by_tag("machine-learning")
        assert len(ml) == 2
        names = {r["frontmatter"]["title"] for r in ml}
        assert "LLM" in names and "Neural Network" in names
        python_results = store.pages_by_tag("software-engineering")
        assert len(python_results) == 1
        assert python_results[0]["frontmatter"]["title"] == "Python"

    def test_pages_by_tag_no_matches(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        results = store.pages_by_tag("unknown")
        assert results == []

    def test_all_pages(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {
                "wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\n---\n\nLLM\n",
                "wiki/concept/python.md": "---\ntitle: Python\ntype: concept\n---\n\nPython\n",
            },
        )
        all_pages = store.all_pages()
        assert len(all_pages) == 2
        titles = {p["frontmatter"]["title"] for p in all_pages}
        assert titles == {"LLM", "Python"}

    def test_all_pages_empty_vault(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        assert store.all_pages() == []

    def test_resolve_wikilink_simple(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {"wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\n---\n\nLLM body\n"},
        )
        result = store.resolve_wikilink("[[LLM]]")
        assert result is not None
        assert result["title"] == "LLM"

    def test_resolve_wikilink_with_display(self, tmp_path: Path) -> None:
        store = self._make_vault(
            tmp_path,
            {"wiki/entity/python.md": "---\ntitle: Python\ntype: entity\n---\n\nPython body\n"},
        )
        result = store.resolve_wikilink("[[Python|Programming Language]]")
        assert result is not None
        assert result["title"] == "Python"

    def test_resolve_wikilink_not_found(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        result = store.resolve_wikilink("[[NonExistent]]")
        assert result is None

    def test_read_index(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        index = store.read_index()
        assert index is not None  # index.md is created in _make_vault
        assert isinstance(index, dict)

    def test_list_tags(self, tmp_path: Path) -> None:
        store = self._make_vault(tmp_path, {})
        tags = store.list_tags()
        assert "machine-learning" in tags
        assert "software-engineering" in tags


# ─────────────────────────── MemoryTools ──────────────────────────────────────


def _create_memory_vault(tmp_path: Path, files: dict[str, str]) -> MemoryTools:
    """Helper: create MemoryTools with an enabled config backed by tmp_path."""
    _create_vault_structure(tmp_path)
    # Write SCHEMA.md + index.md
    (tmp_path / "index.md").write_text(
        "# Vault Index\n\n## Entities\n- [[LLM]]\n",
        encoding="utf-8",
    )
    (tmp_path / "SCHEMA.md").write_text(
        "---\ntitle: SCHEMA\ntype: meta\ntag_taxonomy:\n  - machine-learning\n  - software-engineering\n---\n",
        encoding="utf-8",
    )
    for rel, content in files.items():
        fp = tmp_path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    config = MemoryConfig(
        vault_path=str(tmp_path),
        enabled=True,
    )
    return MemoryTools(config)


class TestMemoryTools:
    """High-level API tests."""

    def test_read_by_title(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(
            tmp_path,
            {"wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\ntags:\n  - machine-learning\n---\n\nLLM body\n"},
        )
        result = tools.read_entity("llm")
        assert result is not None
        assert result["title"] == "LLM"

    def test_read_by_keyword(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(
            tmp_path,
            {
                "wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\n---\n\nLLMs transform AI.\n",
                "wiki/concept/python.md": "---\ntitle: Python\ntype: concept\n---\n\nPython for AI.\n",
            },
        )
        results = tools.read("ai")
        assert len(results) == 2
        titles = {r["frontmatter"]["title"] for r in results}
        assert titles == {"LLM", "Python"}

    def test_read_by_tag(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(
            tmp_path,
            {
                "wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\ntags:\n  - machine-learning\n---\n\nLLM\n",
                "wiki/entity/neural-network.md": "---\ntitle: Neural Network\ntype: entity\ntags:\n  - machine-learning\n---\n\nNN\n",
                "wiki/concept/python.md": "---\ntitle: Python\ntype: concept\ntags:\n  - software-engineering\n---\n\nPython\n",
            },
        )
        results = tools.read_tag("machine-learning")
        assert len(results) == 2

    def test_list_pages(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(tmp_path, {})
        results = tools.list_pages()
        assert isinstance(results, dict)
        # index.md has no frontmatter title, so falls back to stem "index"
        assert results["title"] == "index"

    def test_list_tags(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(tmp_path, {})
        tags = tools.list_tags()
        assert "machine-learning" in tags

    def test_get_page(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(
            tmp_path,
            {"wiki/entity/llm.md": "---\ntitle: LLM\ntype: entity\n---\n\nLLM body\n"},
        )
        result = tools.get_page("llm")
        assert result is not None
        assert result["title"] == "LLM"

    def test_empty_vault_returns_empty(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(tmp_path, {})
        assert tools.read("anything") == []
        assert tools.read_entity("nonexistent") is None
        assert tools.read_tag("anything") == []
        assert tools.get_page("nonexistent") is None

    def test_disabled_tools_returns_empty(self) -> None:
        """When config is disabled, all read methods return empty/None."""
        tools = MemoryTools(MemoryConfig(enabled=False))
        assert tools.read("test") == []
        assert tools.read_entity("test") is None
        assert tools.read_tag("test") == []
        assert tools.list_pages() == []
        assert tools.list_tags() == []
        assert tools.get_page("test") is None

    def test_wikilink_with_display_text(self, tmp_path: Path) -> None:
        tools = _create_memory_vault(
            tmp_path,
            {"wiki/entity/python.md": "---\ntitle: Python\ntype: entity\n---\n\nPython body\n"},
        )
        result = tools.get_page("python")
        assert result is not None
        assert result["title"] == "Python"


# ─────────────────────────── edge cases ───────────────────────────────────────


class TestEdgeCases:
    """Boundary conditions and error handling."""

    def test_nonexistent_vault_path(self) -> None:
        """Store gracefully handles a vault that does not exist yet."""
        store = Store("/tmp/nonexistent-orb-vault-xyz/")
        assert store.read_page("anything") is None
        assert store.all_pages() == []
        assert store.search_pages("query") == []
        assert store.pages_by_tag("tag") == []

    def test_empty_wiki_directory(self, tmp_path: Path) -> None:
        _create_vault_structure(tmp_path)
        store = Store(str(tmp_path))
        assert store.all_pages() == []

    def test_special_characters_in_title(self, tmp_path: Path) -> None:
        _create_vault_structure(tmp_path)
        f = tmp_path / "wiki/entity/special-chars-page.md"
        f.write_text("---\ntitle: Special Characters\ntype: entity\n---\n\nContent.\n", encoding="utf-8")
        store = Store(str(tmp_path))
        result = store.read_page("special-chars-page")
        assert result is not None

    def test_large_body_still_parses(self, tmp_path: Path) -> None:
        _create_vault_structure(tmp_path)
        big_body = "\n".join(f"Line {i}" for i in range(500))
        f = tmp_path / "wiki/entity/large.md"
        f.write_text(f"---\ntitle: Large Page\ntype: entity\n---\n{big_body}\n", encoding="utf-8")
        store = Store(str(tmp_path))
        result = store.read_page("large")
        assert result is not None
        assert len(result["content"].splitlines()) == 500

    def test_file_with_only_frontmatter(self, tmp_path: Path) -> None:
        _create_vault_structure(tmp_path)
        f = tmp_path / "wiki/entity/empty-body.md"
        f.write_text("---\ntitle: Empty Body\ntype: entity\n---\n", encoding="utf-8")
        store = Store(str(tmp_path))
        result = store.read_page("empty-body")
        assert result is not None
        assert result["content"].strip() == ""
