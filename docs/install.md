# Install

## Prerequisites

- Python `3.11+`
- `git`
- one or more reachable model providers
- optional: Conda for an isolated env (the repo examples use it)

## Clone and install

```bash
git clone <your-orb-repo-url>
cd orb
```

Create an environment and install the package:

```bash
conda create -n orb python=3.12 -y
conda activate orb
pip install -e .
orb onboard
```

For local development, install the test extras too:

```bash
pip install -e ".[dev]"
```

`orb onboard` walks through initial auth and common setup.

## Configure providers

Depending on the providers you want to use:

- `vmlx` expects a local OpenAI-compatible endpoint, defaulting to `http://localhost:1234/v1`
- `omlx` expects a local OpenAI-compatible endpoint, defaulting to `http://localhost:8000/v1`
- `openai-codex` uses your OpenAI/Codex credentials
- `anthropic` uses your Anthropic credentials
- `ollama` expects a reachable Ollama server

You can also configure auth directly:

```bash
orb auth openai
orb auth anthropic
```

## Default provider mix

Out of the box, every provider ships **disabled**. `orb onboard` walks you
through enabling the ones you actually plan to use — it folds in auth (or a
reachability probe for local providers), refreshes the model catalog, and
lets you pick per-tier defaults before flipping `enabled: true` in
`~/.orb/config.json`.

Provider settings live in `~/.orb/config.json`. Provider/model selection
comes from config and the provider catalog — runtime paths don't hardcode
model IDs or inline fallback defaults. If no valid configured model exists
for a selected provider/tier, Orb fails explicitly instead of silently
using a hardcoded fallback.

## Typical first runs

First run (any provider mix — onboarding is the entry point):

```bash
orb onboard
orb daemon start
orb tui
```

Local-only — pick `vmlx`, `omlx`, or `ollama` when prompted by onboard:

```bash
orb onboard
orb daemon start
orb tui --topology auto
```

Cloud-only — onboard collects auth in the same flow:

```bash
orb onboard
orb daemon start
orb tui --connect http://127.0.0.1:1337
```

→ Next: [Getting started](getting-started.md)
