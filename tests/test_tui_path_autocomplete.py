"""Tests for PathAutocomplete class and path scanning helpers."""
import os
import re
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from orb.cli.tui_repl import (
    PathAutocomplete,
    SCOPE_FRAGMENT_RE,
    SCOPE_MAX,
    _AUTOCOMPLETE_MAX_RESULTS,
    _extract_autocomplete_query,
    _render_scope_chips,
    _scan_workdir_paths,
    _tui_escape,
)


# ── _scan_workdir_paths tests ──

class TestScanWorkdirPaths:
    """Tests for the filesystem path scanning helper."""

    def test_empty_workdir_returns_empty(self) -> None:
        """No workdir or non-existent directory should return []."""
        result = _scan_workdir_paths("", "foo")
        assert result == []
        result = _scan_workdir_paths("/nonexistent/path", "foo")
        assert result == []

    def test_finds_matching_prefix(self, tmp_path: Path) -> None:
        """Files starting with the prefix should be returned."""
        (tmp_path / "alpha.txt").touch()
        (tmp_path / "beta.py").touch()
        (tmp_path / "alphabet.md").touch()

        result = _scan_workdir_paths(str(tmp_path), "alph")
        assert "alpha.txt" in result
        assert "alphabet.md" in result
        assert "beta.py" not in result

    def test_substring_match(self, tmp_path: Path) -> None:
        """Paths containing the prefix as substring should match."""
        (tmp_path / "my_file.txt").touch()
        (tmp_path / "other.py").touch()

        result = _scan_workdir_paths(str(tmp_path), "file")
        assert "my_file.txt" in result
        assert "other.py" not in result

    def test_capped_results(self, tmp_path: Path) -> None:
        """Results should be capped at _AUTOCOMPLETE_MAX_RESULTS."""
        for i in range(100):
            (tmp_path / f"file_{i:03d}.txt").touch()

        result = _scan_workdir_paths(str(tmp_path), "file_")
        assert len(result) <= _AUTOCOMPLETE_MAX_RESULTS

    def test_does_not_follow_symlinks(self, tmp_path: Path) -> None:
        """Symlinks should not be followed (security)."""
        (tmp_path / "real_dir").mkdir()
        (tmp_path / "real_dir" / "secret.txt").touch()

        # Create a directory outside tmp_path
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "sensitive.txt").touch()

        # Create symlink to outside
        (tmp_path / "link").symlink_to(outside)

        result = _scan_workdir_paths(str(tmp_path), "sens")
        assert "sensitive.txt" not in str(result)

    def test_case_insensitive(self, tmp_path: Path) -> None:
        """Matching should be case-insensitive."""
        (tmp_path / "Config.yaml").touch()
        (tmp_path / "config.txt").touch()

        result = _scan_workdir_paths(str(tmp_path), "CONF")
        assert "Config.yaml" in result
        assert "config.txt" in result

    def test_capped_depth(self, tmp_path: Path) -> None:
        """Should not recurse beyond 3 levels."""
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "found.txt").touch()

        result = _scan_workdir_paths(str(tmp_path), "found")
        # "found.txt" is 5 levels deep (a/b/c/d/e/found.txt)
        # Depth 3 cap means we reach level 3 (a/b/c/) so "found.txt"
        # at level 5 should NOT be found.
        assert "found.txt" not in result


# ── _extract_autocomplete_query tests ──

class TestExtractAutocompleteQuery:
    """Tests for the cursor-aware @ path fragment extraction."""

    def test_no_at_sign_returns_none(self) -> None:
        """Text without @ should return (None, None)."""
        frag, start = _extract_autocomplete_query("hello world", 5)
        assert frag is None
        assert start is None

    def test_trailing_space_returns_none(self) -> None:
        """Fragment ending in space is not in-progress."""
        frag, start = _extract_autocomplete_query("hello @config ", 18)
        assert frag is None
        assert start is None

    def test_trailing_paren_returns_none(self) -> None:
        """Fragment ending in ) is not in-progress."""
        frag, start = _extract_autocomplete_query("(@config.txt)", 11)
        assert frag is None
        assert start is None

    def test_in_progress_fragment(self) -> None:
        """Active @path fragment under cursor should be extracted."""
        text = "Use @config.yaml for setup"
        frag, start = _extract_autocomplete_query(text, text.index("@config.yaml") + len("@config.yaml"))
        assert frag is not None
        assert frag == "config.yaml"
        assert start >= 0

    def test_nested_path(self) -> None:
        """Multi-level paths should work."""
        text = "Check src/components/Button.tsx"
        cursor = text.index("@src/components/Button.tsx") + len("@src/components/Button.tsx")
        frag, start = _extract_autocomplete_query(text, cursor)
        assert frag is not None
        assert "src/components/Button.tsx" in frag
        assert start >= 0

    def test_out_of_bounds_cursor(self) -> None:
        """Out-of-range cursor positions should return (None, None)."""
        frag, start = _extract_autocomplete_query("hello", -1)
        assert frag is None
        frag, start = _extract_autocomplete_query("hello", 999)
        assert frag is None

    def test_multiple_at_mentions(self) -> None:
        """Should extract the last @ fragment under cursor."""
        text = "@alpha @beta"
        cursor = len(text) - 1  # After 'beta'
        frag, start = _extract_autocomplete_query(text, cursor)
        assert frag is not None
        assert "beta" in frag


# ── _render_scope_chips tests ──

class TestRenderScopeChips:
    """Tests for scope chip rendering."""

    def test_empty_list(self) -> None:
        """Empty scope_paths should return empty list."""
        result = _render_scope_chips([])
        assert result == []

    def test_single_path(self) -> None:
        """Single path should produce one chip."""
        result = _render_scope_chips(["config.yaml"])
        assert len(result) == 1
        assert "@config.yaml" in result[0]

    def test_multiple_paths(self) -> None:
        """Multiple paths should produce multiple chips."""
        result = _render_scope_chips(["config.yaml", "src/main.py"])
        assert len(result) == 2

    def test_truncation(self) -> None:
        """Long paths should be truncated with ellipsis."""
        long_path = "a" * 50
        result = _render_scope_chips([long_path])
        assert "…" in result[0]
        assert len(result[0]) < len(long_path)

    def test_markup_escaping(self) -> None:
        """Paths with Rich markup characters should be escaped."""
        result = _render_scope_chips(["file[1].txt"])
        # The bracket should be escaped so it doesn't break Textual rendering
        assert "[" not in result[0] or "\[" in result[0]


# ── SCOPE_FRAGMENT_RE tests ──

class TestScopeFragmentRegex:
    """Tests for the SCOPE_FRAGMENT_RE regex."""

    def test_basic_path(self) -> None:
        """Simple path fragment should match."""
        match = SCOPE_FRAGMENT_RE.search("config")
        assert match is not None
        assert match.group(1) == "config"

    def test_nested_path(self) -> None:
        """Multi-level path should match."""
        match = SCOPE_FRAGMENT_RE.search("src/components/Button.tsx")
        assert match is not None
        assert match.group(1) == "src/components/Button.tsx"

    def test_hyphenated_name(self) -> None:
        """Hyphenated path components should match."""
        match = SCOPE_FRAGMENT_RE.search("my-file_name.ts")
        assert match is not None
        assert match.group(1) == "my-file_name.ts"

    def test_not_floating(self) -> None:
        """Should only match at end of string (anchored)."""
        match = SCOPE_FRAGMENT_RE.search("path/to/thing extra")
        assert match is None  # Not anchored at end


# ── Integration tests for PathAutocomplete ──

class TestPathAutocompleteIntegration:
    """Integration tests for the PathAutocomplete widget."""

    def test_initial_state_hidden(self) -> None:
        """New PathAutocomplete should be hidden."""
        widget = PathAutocomplete()
        assert "hidden" in widget.classes

    def test_refresh_for_no_fragment_hides(self) -> None:
        """No @ fragment in text should keep hidden."""
        widget = PathAutocomplete()
        widget.state = mock.MagicMock()
        widget.state.scope_paths = []
        widget.state.workdir = ""
        widget.refresh_for("hello world", 5)
        assert "hidden" in widget.classes

    def test_refresh_for_missing_state_uses_empty(self) -> None:
        """Widget without .state should handle gracefully."""
        widget = PathAutocomplete()
        # No .state attribute — should not crash
        widget.refresh_for("@foo", 4)
        # Should hide since no matches


# ── SCOPE_RE and SCOPE_MAX constants tests ──

class TestScopeConstants:
    """Tests for SCOPE_RE and SCOPE_MAX."""

    def test_scope_re_finds_mentions(self) -> None:
        """SCOPE_RE should find all @path mentions."""
        text = "Use @config and @src/main.py for setup"
        matches = SCOPE_RE.findall(text)
        assert "config" in matches
        assert "src/main.py" in matches

    def test_scope_re_ignores_non_paths(self) -> None:
        """@mentions with special chars should not match."""
        text = "@foo!bar @baz:qux"
        matches = SCOPE_RE.findall(text)
        assert "foo" in matches  # Only word chars, dots, hyphens, slashes
        assert "bar!bar" not in str(matches)

    def test_scope_max_is_reasonable(self) -> None:
        """SCOPE_MAX should cap mentions to prevent rail overflow."""
        assert SCOPE_MAX == 8
        assert SCOPE_MAX > 0

    def test_autocomplete_max_results_reasonable(self) -> None:
        """_AUTOCOMPLETE_MAX_RESULTS should limit filesystem scans."""
        assert 0 < _AUTOCOMPLETE_MAX_RESULTS < 200

    def test_tui_escape_escapes_rich_markup(self) -> None:
        """Rich markup brackets should be escaped."""
        text = "hello [bold]world[/bold]"
        escaped = _tui_escape(text)
        assert "[" not in escaped
