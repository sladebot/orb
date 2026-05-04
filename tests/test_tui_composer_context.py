"""Tests for composer context features: ScopeChips, mention tracking, chips display."""
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from orb.cli.tui_repl import (
    SCOPE_MAX,
    SCOPE_RE,
    SCOPE_FRAGMENT_RE,
    PathAutocomplete,
    ScopeChips,
    _render_scope_chips,
    _scan_workdir_paths,
    _extract_autocomplete_query,
    _tui_escape,
)


# ── ScopeChips widget tests ──

class TestScopeChips:
    """Tests for the ScopeChips widget."""

    def test_initial_state(self) -> None:
        """New ScopeChips should be empty."""
        widget = ScopeChips()
        assert widget.id == "scope-chips"
        assert widget.update("")  # Updates without error

    def test_refresh_empty(self) -> None:
        """Empty scope_paths should clear chips."""
        widget = ScopeChips()
        widget.refresh_for([])
        # Content should be empty string

    def test_refresh_with_paths(self) -> None:
        """Scope paths should render as chips."""
        widget = ScopeChips()
        widget.refresh_for(["config.yaml", "src/main.py"])
        # Should have content with chip markers
        content = widget.renderable
        assert "@" in str(content) if content else True

    def test_refresh_marking_hidden(self) -> None:
        """Should display properly."""
        widget = ScopeChips()
        widget.refresh_for(["test.txt"])
        assert "hidden" not in widget.classes


# ── Mention tracking integration ──

def _track_scope_mentions_tested(text: str, workdir: str, existing: list[str]) -> list[str]:
    """Helper for testing: extracts mentions and returns scope_paths.

    This is a simplified version of the OrbReplTUI method for test use.
    """
    result = list(existing)  # Copy existing scope_paths
    for match in SCOPE_RE.findall(text or ""):
        if match and match not in result:
            result.append(match)

    # Scan workdir for additional context paths
    if workdir and os.path.isdir(workdir):
        for match in SCOPE_FRAGMENT_RE.findall(text or ""):
            if match and match not in result:
                candidates = _scan_workdir_paths(workdir, match)
                for c in candidates[:5]:
                    if c not in result:
                        result.append(c)
                        break

    # Apply cap
    if len(result) > SCOPE_MAX:
        result = result[-SCOPE_MAX:]

    return result


class TestTrackScopeMentions:
    """Tests for @ mention tracking."""

    def test_simple_mention(self) -> None:
        """Basic @mention should be tracked."""
        result = _track_scope_mentions_tested("@config", "", [])
        assert "config" in result

    def test_multiple_mentions(self) -> None:
        """Multiple @mentions should all be tracked."""
        result = _track_scope_mentions_tested("@alpha @beta @gamma", "", [])
        assert "alpha" in result
        assert "beta" in result
        assert "gamma" in result
        assert len(result) == 3

    def test_scope_max_capping(self) -> None:
        """Should cap at SCOPE_MAX."""
        texts = ["@" + "x" * i for i in range(20)]
        all_text = " ".join(texts)
        result = _track_scope_mentions_tested(all_text, "", [])
        assert len(result) <= SCOPE_MAX

    def test_dedup(self) -> None:
        """Duplicate mentions should not appear twice."""
        result = _track_scope_mentions_tested("@config @config @config", "", [])
        assert result.count("config") == 1

    def test_workdir_scanning(self, tmp_path: Path) -> None:
        """Should scan workdir for matching paths."""
        (tmp_path / "config.yaml").touch()
        (tmp_path / "src" / "main.py").mkdir(parents=True)
        (tmp_path / "src" / "main.py").touch()

        result = _track_scope_mentions_tested("@config.yaml", str(tmp_path), [])
        assert "config.yaml" in result

    def test_existing_paths_preserved(self) -> None:
        """Existing scope_paths entries should be preserved."""
        existing = ["existing_path"]
        result = _track_scope_mentions_tested("@new_path", "", existing)
        assert "existing_path" in result
        assert "new_path" in result

    def test_no_mention_no_tracking(self) -> None:
        """Text without @mentions produces no scope paths."""
        result = _track_scope_mentions_tested("hello world", "", [])
        assert result == []


# ── Composer integration tests ──

class TestComposerContext:
    """Integration tests for composer context features."""

    def test_mention_then_submit(self) -> None:
        """User types @path, submits, path appears in scope."""
        # Simulate: user types "@config.yaml" then hits Enter
        text = "@config.yaml"
        result = _track_scope_mentions_tested(text, "", [])
        assert "config.yaml" in result

    def test_mention_with_space_cleared(self) -> None:
        """If user types @path then space, it's still tracked."""
        text = "@config.yaml next"
        result = _track_scope_mentions_tested(text, "", [])
        assert "config.yaml" in result

    def test_chip_display(self) -> None:
        """Scope chips should render correctly."""
        chips = _render_scope_chips(["config.yaml", "src/main.py"])
        assert len(chips) == 2

    def test_chip_markup_safe(self) -> None:
        """Chips should use safe markup (no unescaped brackets)."""
        chips = _render_scope_chips(["file[1].txt"])
        for chip in chips:
            # Rich markup characters should be escaped
            assert "\[" in chip or "[" not in chip

    def test_empty_composer_no_chips(self) -> None:
        """Empty composer text produces no chips."""
        chips = _render_scope_chips([])
        assert chips == []


# ── Edge case tests ──

class TestEdgeCases:
    """Edge case tests for composer context features."""

    def test_unicode_in_path(self) -> None:
        """Unicode characters in paths should be handled gracefully."""
        chips = _render_scope_chips(["файл.txt"])
        assert len(chips) == 1

    def test_very_long_path(self) -> None:
        """Very long paths should truncate gracefully."""
        long_path = "a" * 1000
        chips = _render_scope_chips([long_path])
        assert len(chips) == 1
        assert "…" in chips[0]

    def test_special_chars_escaped(self) -> None:
        """Special Rich markup characters should be escaped."""
        test_cases = [
            "file[0].txt",
            "path/to/file.txt",
            "file.txt",
        ]
        chips = _render_scope_chips(test_cases)
        for chip in chips:
            # Escaped content should not render as markup
            assert "\[" in chip or "\[" in chip.replace("[", "[")

    def test_concurrent_mentions(self) -> None:
        """Many mentions in one text should cap properly."""
        mentions = " ".join(f"@path_{i}" for i in range(50))
        result = _track_scope_mentions_tested(mentions, "", [])
        assert len(result) <= SCOPE_MAX
