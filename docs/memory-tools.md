# Memory Tools (Memory Vault)

Orb's memory vault is a persistent, wikia-style knowledge store for agents.
It provides both read and write operations through a clean, discoverable API.

## Quick Start

Enable the vault in your Orb config:

```yaml
memory:
  enabled: true
  vault_path: "~/.orb/vault"
```

Once enabled, agents can read and write wiki pages automatically.

## Architecture

```
~/.orb/vault/
├── SCHEMA.md          # Tag taxonomy and conventions
├── index.md           # Auto-generated page catalog
├── log.md             # Action log (append-only)
└── wiki/
    ├── entity/        # Entity pages (tools, frameworks, people)
    ├── concept/       # Concept pages (algorithms, patterns, ideas)
    ├── analysis/      # Analysis pages (reviews, assessments)
    └── queries/       # Query pages (search results, investigations)
```

## API Reference

### Read Operations

```python
from orb.memory_tools import MemoryTools, MemoryConfig

mt = MemoryTools(MemoryConfig(enabled=True))

# Search by keyword across all pages
results = mt.read("machine learning")

# Read a page by exact entity name
page = mt.read_entity("docker")

# Read pages by tag
pages = mt.read_tag("python")

# List all vault pages (index)
pages = mt.list_pages()

# List active tags
tags = mt.list_tags()

# Get raw page data
page = mt.get_page("kubernetes")
```

### Write Operations

```python
# Write/update a wiki page
page = mt.write(
    entity="kubernetes",
    content="Kubernetes is an open-source container orchestration system...",
    page_type="entity"  # or "concept", "analysis", "query"
)

# Convenience: write an entity page
mt.write_entity("docker", "Container runtime for applications")

# Convenience: write an analysis page with tags
mt.write_analysis(
    title="microservices-review",
    content="Analysis of microservices adoption...",
    tags=["software-engineering", "operations"]
)

# Write with explicit source provenance
mt.write_with_sources(
    entity="docker",
    content="Container runtime...",
    source_files=["docker-docs.md", "readme.md"]
)
```

## Page Structure

Pages use Markdown with YAML frontmatter and wikilinks:

```markdown
---
title: docker
type: entity
tags: [infrastructure, containers]
---

## Overview

Docker is a platform for developing, shipping, and running applications in containers.

See also: [[kubernetes]], [[containerd]]
```

### Page Types

| Type       | Directory        | Purpose                           |
|------------|------------------|-----------------------------------|
| `entity`   | `wiki/entity/`   | Tools, frameworks, people, APIs   |
| `concept`  | `wiki/concept/`  | Algorithms, patterns, ideas       |
| `analysis` | `wiki/analysis/` | Reviews, assessments, comparisons |
| `query`    | `wiki/queries/`  | Search results, investigations      |

### Wikilinks

Double-bracket syntax links pages: `[[page-title]]` or `[[page-title|display text]]`.

On write, outbound wikilinks are resolved automatically — missing target pages
are created as placeholder entities with `status:draft` tag.

### Confidence Scoring

Pages receive a confidence score based on:
- Number of source references
- Presence of cross-links (2+ outbound wikilinks)

### Page Splitting

Pages exceeding 200 lines trigger a split warning logged to `log.md`.

## Index Management

The vault index (`index.md`) is auto-regenerated on every write operation:
- Grouped by page type (Entities, Concepts, Analysis, Queries)
- Alphabetized within each section
- Date-stamped with last update

## Schema

`SCHEMA.md` defines the allowed tag taxonomy. Pages with unrecognized tags
are flagged during linting. Default tags:

```yaml
tag_taxonomy:
  - machine-learning
  - software-engineering
  - operations
  - research
  - design
  - security
  - data
  - infrastructure
```

## Logging

All write operations are recorded in `log.md` with timestamps, actions,
and subjects. Supported log actions:

- `write` — page written/updated
- `split_warning` — page exceeds 200 lines
- `link_sync` — inbound wikilinks corrected
- `entity_created` — placeholder entity auto-created
