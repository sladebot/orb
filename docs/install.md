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

Out of the box, Orb defaults to:

- `vmlx`: enabled
- `openai-codex`: enabled
- `ollama`: disabled
- `omlx`: disabled
- `anthropic`: disabled

This gives one local path and one cloud path without requiring all providers configured.

Provider settings live in `~/.orb/config.json`. Provider/model selection comes from config and the provider catalog — runtime paths don't hardcode model IDs or inline fallback defaults. If no valid configured model exists for a selected provider/tier, Orb fails explicitly instead of silently using a hardcoded fallback.

## Typical first runs

Defaults (local `vmlx` + cloud `openai-codex`):

```bash
orb onboard
orb daemon start
orb tui
```

Local-only:

```bash
orb daemon start
orb tui --topology auto
```

Cloud-only:

```bash
orb auth openai
orb daemon start
orb tui --connect http://127.0.0.1:1337
```

→ Next: [Getting started](getting-started.md)
