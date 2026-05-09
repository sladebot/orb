# Orb Memory Tools — Full Implementation Plan

**Branch:** `feature/llm-wiki-memory`
**Status:** Planning (Phases 1–4)

---

## Problem

Orb agents have one memory path: session-scoped GraphRAG (ChromaDB). Users want a persistent, cross-session knowledge layer that agents can **choose** to read/write via a unified `memory_tools` interface — **opt-in, not forced**.

## Architecture

```
Orb Agent (during session)
    │
    ▼
┌─────────────────────────┐
│  memory_tools           │  ← Agent-facing tool (optional)
│  read() / write()       │     Agent decides when to use it
│  list() / search()      │
└─────┬───────────────────┘
      │
      ▼  (persistent vault)
~/.orb/vault/
├── SCHEMA.md       # Conventions (llm-wiki style)
├── index.md        # Page catalog
├── log.md          # Append-only action log
├── wiki/           # entity/, concept/, analysis/, queries/
├── raw/            # articles/, transcripts/
└── memories/       # Session exports
```

The vault follows `llm-wiki` conventions (frontmatter, `[[wikilinks]]`, tag taxonomy). No pre-seeded content — empty until first `orb memory init`.

---

## Phase 0: Vault Initialization (Pre-condition)

**Goal:** Create `~/.orb/vault/` with proper structure via CLI.

### Tasks

- [ ] `orb/memory_tools/__init__.py` — module init, exports
- [ ] `orb/memory_tools/cli.py` — CLI entry points:
  - `orb memory init` — validates path, creates directory structure, writes SCHEMA.md + index.md + log.md
  - `orb memory status` — reports vault health (page count, orphans, tag count, last write)
  - `orb memory prune` — removes marked/archived pages
- [ ] `orb/memory_tools/config.py` — dataclass:
  ```python
  @dataclass
  class MemoryConfig:
      vault_path: str = "~/.orb/vault"
      enabled: bool = False          # Default: disabled, opt-in
      auto_write: bool = False       # Default: no auto-save
      tag_taxonomy: list[str] = field(default_factory=list)
  ```
- [ ] Wire `memory` commands into `orb/cli/__init__.py` entry point
- [ ] Test: `orb memory init` creates expected structure
- [ ] Test: `orb memory status` reports correctly on empty/full vault
- [ ] Test: `orb memory init` is idempotent (safe to run twice)

### Acceptance

- `orb memory init` → `~/.orb/vault/` exists with SCHEMA.md, index.md, log.md, wiki/, raw/, memories/
- `orb memory status` → reports vault stats
- Default: `enabled=False` — memory tools are off until user opts in

---

## Phase 1: Read Engine (Core)

**Goal:** Agents can read the vault by title, tag, keyword search, and resolve wikilinks.

### Tasks

- [ ] `orb/memory_tools/store.py` — backend abstraction:
  - `read_page(title: str) → MarkdownPage | None`
  - `search_pages(query: str) → list[MarkdownPage]` (full-text across vault)
  - `pages_by_tag(tag: str) → list[MarkdownPage]`
  - `all_pages() → list[MarkdownPage]`
  - `resolve_wikilink(link: str) → MarkdownPage | None`
- [ ] `orb/memory_tools/parsers.py` — markdown parsing:
  - YAML frontmatter extraction (title, type, tags, sources, created, updated, confidence, contested)
  - `[[wikilinks]]` extraction and resolution
  - Tag taxonomy validation (only allowed tags from SCHEMA.md)
  - File normalization (handle Obsidian-style display text in wikilinks: `[[page|Display]]`)
- [ ] `orb/memory_tools/memory_tools.py` — unified API:
  ```python
  class MemoryTools:
      def __init__(self, config: MemoryConfig | None = None):
          self.config = config or MemoryConfig()
          self.store = None
          self._ensure_initialized()  # only if enabled

      def read(self, term: str) → list[MarkdownPage]:
          """Search by keyword across all wiki pages."""

      def read_entity(self, name: str) → MarkdownPage | None:
          """Read by entity name (exact match)."""

      def read_tag(self, tag: str) → list[MarkdownPage]:
          """Read pages by tag."""

      def list_pages(self) → list[dict]:
          """Return index.md contents (structured)."""

      def list_tags(self) → list[str]:
          """Return active tag taxonomy."""

      def get_page(self, title: str) → MarkdownPage | None:
          """Low-level: get raw page by title."""
  ```
- [ ] Register `memory_tools` as an optional agent tool (not in default toolset)
- [ ] Tests: read by title, search by keyword, resolve wikilinks, read by tag, handle empty vault gracefully

### Acceptance

- Agent can read any page in the vault
- Wikilinks resolve correctly (including `[[page|Display]]` display text)
- Search works across all files
- `memory_tools` is available as an optional agent tool (disabled by default)
- Tests pass

---

## Phase 2: Write Engine

**Goal:** Agents can create/update pages with proper frontmatter, maintain index and log.

### Tasks

- [ ] Extend `store.py` with write operations:
  - `write_page(page: MarkdownPage) → bool` (create or update)
  - `update_index(page: MarkdownPage) → None` (add/update in index.md, alphabetical)
  - `append_log(action: str, subject: str, files: list[str]) → None`
  - `resolve_write_links(page: MarkdownPage) → list[str]` (ensure outbound wikilinks exist)
  - `sync_inbound_links(page: MarkdownPage) → None` (ensure pages linking back have correct refs)
- [ ] Page object model in `orb/memory_tools/models.py`:
  ```python
  @dataclass
  class MarkdownPage:
      title: str                    # filename without .md
      content: str                  # body (without frontmatter)
      type: str                     # entity | concept | analysis | query
      tags: list[str]               # from SCHEMA.md taxonomy
      sources: list[str]            # raw source filenames
      created: str                  # YYYY-MM-DD
      updated: str                  # YYYY-MM-DD
      confidence: str | None        # high | medium | low (optional)
      contested: bool = False
  ```
- [ ] Page creation rules (from llm-wiki skill):
  - New pages: meet Page Thresholds (2+ source mentions, or central to one source)
  - Existing pages: append new info, update `updated` date
  - Every page must link to 2+ other pages (no orphans)
  - Tags from SCHEMA.md taxonomy only
  - Provenance markers: `^[raw/articles/source.md]` for multi-source pages
  - Confidence marking for single-source/contested claims
- [ ] Tests: write new page, update existing page, index sync, log sync, wikilink maintenance
- [ ] Edge cases: concurrent writes (file locks or atomic writes), page over 200 lines (split warning), invalid tags (reject + warn)

### Acceptance

- Agent can create and update wiki pages
- `index.md` stays in sync (alphabetical, sectioned)
- `log.md` gets append-only entries
- Wikilinks are bidirectional (outbound on write, inbound updated)
- Tests pass

---

## Phase 3: Agent Integration

**Goal:** `memory_tools` is registered as an optional tool in the agent toolset. Agents can invoke it.

### Tasks

- [ ] `orb/agent/tool_registry.py` — register `memory_tools` as an optional tool group:
  - Group name: `memory_tools`
  - Tools: `memory_read`, `memory_write`, `memory_list`, `memory_search`
  - Enabled only if `config.memory_tools.enabled = True`
- [ ] `orb/cli/onboard.py` — add `memory` step to onboarding:
  - Question: "Enable persistent memory vault? (recommended for long-running projects)"
  - If yes: run `orb memory init` + set `config.memory_tools.enabled = True`
  - If no: leave disabled (default behavior)
- [ ] `orb/runtime/session_manager.py` (or equivalent):
  - Optionally expose `memory_tools` to active sessions (if enabled in config)
- [ ] Agent tool documentation (help text for each tool):
  - `memory_read(query)` — "Search the persistent knowledge vault for information about X"
  - `memory_write(concept, content)` — "Store knowledge in the persistent vault for future sessions"
  - `memory_list()` — "List all pages in the vault"
  - `memory_search(tag)` — "List pages by tag"
- [ ] Tests: agent can call memory_tools.read/write/list when enabled, nothing breaks when disabled
- [ ] Manual test: run Orb session with memory_tools enabled, verify agent can read/write

### Acceptance

- `orb onboard` offers memory vault during setup
- When enabled, agents receive memory_tools in their toolset
- When disabled, agents work normally (no memory_tools in toolset)
- Existing GraphRAG sessions continue to work unchanged
- Tests pass

---

## Phase 4: Advanced Features

**Goal:** Full vault lifecycle management — lint, sync, pruning, and optional Obsidian integration.

### Tasks

- [ ] `orb/memory_tools/linter.py`:
  - Orphan pages (no inbound wikilinks) — flag for review
  - Broken wikilinks (pointing to non-existent pages) — flag for fix
  - Index completeness (every page in index.md) — flag missing entries
  - Stale pages (updated >90 days, newer sources exist) — flag for update
  - Contradictions (pages sharing tags but conflicting claims) — surface for review
  - Source drift (sha256 mismatches in raw/) — flag for re-ingest
  - Page size (>200 lines) — flag for splitting
  - Log rotation (log.md >500 entries) — rotate to log-YYYY.md
- [ ] `orb/memory_tools/sync.py`:
  - `orb memory sync` — re-ingest all raw-sources, update wiki pages
  - Force re-ingest of specific source
- [ ] `orb/memory_tools/prune.py`:
  - `orb memory prune <page-title>` — archive page, update index, update back-links
- [ ] Optional: Obsidian vault sync — if user also wants to edit the vault in Obsidian:
  - Setting: `config.memory_tools.obsidian_vault_path` (optional)
  - When set, sync written vault files to the Obsidian vault (symlink or copy)
  - When the vault IS the Obsidian vault, no sync needed — just point both at the same path
- [ ] Tests: lint reports correctly, sync updates pages, prune archives properly

### Acceptance

- `orb memory lint` reports vault health with specific file paths
- `orb memory sync` re-ingests sources and updates wiki pages
- `orb memory prune` archives pages with proper back-link updates
- (Optional) Obsidian vault sync works when configured
- Tests pass

---

## File Map

| File | Phase | Purpose |
|---|---|---|
| `orb/memory_tools/__init__.py` | 0 | Module init, exports |
| `orb/memory_tools/config.py` | 0 | MemoryConfig dataclass |
| `orb/memory_tools/cli.py` | 0 | `orb memory init/status/prune` CLI |
| `orb/memory_tools/store.py` | 1, 2 | Backend abstraction (read + write) |
| `orb/memory_tools/parsers.py` | 1 | Frontmatter, wikilinks, tag parsing |
| `orb/memory_tools/memory_tools.py` | 1 | Unified API (read/list/search) |
| `orb/memory_tools/models.py` | 2 | MarkdownPage dataclass |
| `orb/memory_tools/linter.py` | 4 | Vault health checks |
| `orb/memory_tools/sync.py` | 4 | Source re-ingest |
| `orb/agent/tool_registry.py` | 3 | Register memory_tools as optional tool |
| `orb/cli/onboard.py` | 3 | Memory vault step in onboarding |
| `tests/test_memory_tools_*.py` | 1–4 | Unit tests |

---

## Acceptance Criteria (All Phases)

- [ ] `orb memory init` creates `~/.orb/vault/` with full structure
- [ ] `orb memory status` reports vault health
- [ ] Agents can read pages by title, tag, keyword search
- [ ] Wikilinks resolve correctly (inbound and outbound, including display text)
- [ ] Agents can write pages with proper frontmatter (title, type, tags, sources, confidence)
- [ ] `index.md` stays in sync after writes (alphabetical, sectioned)
- [ ] `log.md` gets append-only entries after writes
- [ ] Page creation follows llm-wiki thresholds (no orphan pages)
- [ ] `memory_tools` is registered as an **optional** agent tool
- [ ] Onboarding offers memory vault — user can opt in or out
- [ ] When disabled, agents work normally (no memory_tools in toolset)
- [ ] Existing GraphRAG sessions continue to work unchanged (parallel, not replacement)
- [ ] All tests pass
