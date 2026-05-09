"""Markdown / frontmatter / wikilink parsing utilities.

All functions are pure — they accept a string and return parsed data.
No filesystem access here; ``store.py`` handles I/O.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ── frontmatter ----------------------------------------------------------------

_YAML_BLOCK_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*", re.DOTALL
)


def _parse_value(value: str) -> Any:
    """Parse a scalar YAML value string."""
    value = value.strip()
    if not value:
        return value
    # Booleans
    if value.lower() in ("true", "yes", "on"):
        return True
    if value.lower() in ("false", "no", "off"):
        return False
    # Integers
    if value.isdigit():
        return int(value)
    # Strip optional quotes
    return value.strip('"').strip("'")


def _parse_simple_frontmatter(block: str) -> dict[str, Any]:
    """Parse frontmatter using a simple line-by-line approach.

    Supports:
      - Scalar values:  key: value
      - Inline lists:   key: [a, b, c]
      - Multi-line lists: key:\\n  - item1\\n  - item2
    """
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_items: list[str] = []

    def _flush() -> None:
        nonlocal current_key, current_items
        if current_key is not None:
            if current_items:
                result[current_key] = [_parse_value(item) for item in current_items]
        current_key = None
        current_items = []

    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if ":" in stripped:
            _flush()  # write previous key, reset
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()
            current_key = key
            if value:
                # Inline scalar or inline list [a, b, c]
                if value.startswith("[") and value.endswith("]"):
                    inner = value[1:-1]
                    result[key] = [
                        _parse_value(v)
                        for v in inner.split(",")
                        if v.strip()
                    ] if inner else []
                else:
                    result[key] = _parse_value(value)
            else:
                # Multi-line list starts (e.g., "tags:" with no value on this line)
                current_items = []
        elif stripped.startswith("- "):
            # Continuation of a multi-line list
            if current_key is not None:
                item = stripped[2:].strip().strip('"').strip("'")
                current_items.append(_parse_value(item))

    _flush()
    return result


def extract_frontmatter(md_content: str) -> dict[str, Any]:
    """Parse YAML frontmatter from a Markdown string.

    Returns
        A dict with the parsed keys.  Returns ``{}`` when no frontmatter
        block (triple-dash delimited) is found.
    """
    match = _YAML_BLOCK_RE.search(md_content)
    if match is None:
        return {}
    return _parse_simple_frontmatter(match.group(1))


# ── wikilinks ----------------------------------------------------------------

_WIKILINK_RE = re.compile(
    r"\[\[([^\]]+)\]\]"
)


def normalize_wikilink(link: str) -> str:
    """Convert ``[[page|Display]]`` → ``page``.

    Strips display text (everything after the pipe) and collapses
    whitespace / slashes to a canonical filename-friendly form.
    """
    # Remove display text after pipe
    name = link.split("|")[0].strip()
    # Normalise spaces / underscores: "My Page" → "my-page"
    name = name.lower().strip()
    name = re.sub(r"[^\w]+", "-", name).strip("-")
    return name


def extract_wikilinks(body: str) -> list[str]:
    """Find all ``[[wikilinks]]`` in the body text.

    Returns
        List of *normalised* page names (display text stripped).
    """
    raw_links = _WIKILINK_RE.findall(body)
    return [normalize_wikilink(link) for link in raw_links]


# ── tag validation -----------------------------------------------------------

_TAG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def validate_tag(tag: str, taxonomy: list[str]) -> bool:
    """Check whether *tag* is allowed by the given taxonomy.

    A tag is valid when:
      1. It matches the naming convention (lowercase, starts with letter).
      2. It appears in the taxonomy list (typically read from SCHEMA.md).

    Parameters
        tag       : tag string to validate.
        taxonomy  : allowed tags (from SCHEMA.md).

    Returns
        ``True`` if the tag is valid, ``False`` otherwise.
    """
    if not tag or not _TAG_RE.match(tag):
        return False
    if not taxonomy:
        # No taxonomy yet — allow any well-formed tag (will be caught by lint).
        return True
    return tag in taxonomy


# ── page parsing -------------------------------------------------------------

def parse_page(path: str) -> dict[str, Any]:
    """Read a Markdown file and return a parsed-page dict.

    Parameters
        path  : filesystem path to the .md file.

    Returns
        A dict with keys: title, path, content, frontmatter, wikilinks.
        Returns ``None`` when the file does not exist.
    """
    p = Path(path)
    if not p.is_file():
        return None

    raw = p.read_text(encoding="utf-8")
    fm = extract_frontmatter(raw)
    # Body is everything after the frontmatter block (if any)
    match = _YAML_BLOCK_RE.search(raw)
    body = raw[match.end():].strip() if match else raw.strip()
    wikilinks = extract_wikilinks(body)

    title = fm.get("title") or p.stem
    return {
        "title": title,
        "path": str(p),
        "content": body,
        "frontmatter": fm,
        "wikilinks": wikilinks,
    }
