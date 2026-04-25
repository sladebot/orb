# Models and providers

## Supported provider families

- `vmlx` (local, OpenAI-compatible)
- `omlx` (local, OpenAI-compatible)
- `openai-codex` (cloud)
- `ollama` (local)
- `anthropic` (cloud)

## Per-node model allocation

After topology selection, Orb assigns models per node — not one model for the whole graph.

Allocation considers:

- provider availability
- enabled/disabled models from config
- task and node complexity
- node role/category
- explicit model pins

The dashboard surfaces planned assignments before the run, and the active model IDs as the run progresses.

## Provider selection at runtime

Provider/model choices resolve from:

- configured `default_models`
- enabled catalog entries refreshed for each provider
- enabled configured models

If no valid configured model exists for a selected provider/tier, Orb fails explicitly instead of silently choosing a hardcoded fallback.

## Bounded timeouts

All three local providers use a bounded, split httpx timeout (`connect=10s`, `read=180s`) so a model that the server advertises but hasn't actually loaded surfaces as a `ReadTimeout` in minutes, not the 10-minute flat hang it used to be. Failed LLM calls report the specific exception in agent retry activity (e.g. `Retrying gemma-4-e4b-it-8bit (2/3) — ReadTimeout: …`) so stalls are attributable instead of opaque.

## Streaming

LLM responses stream token-by-token via the `message_delta` broadcast event:

```json
{
  "type": "message_delta",
  "from": "<agent_id>",
  "chain_id": "<chain_id>",
  "delta": "<text chunk>",
  "index": 0
}
```

- One stream per `(chain_id, from)` — two agents on the same chain have independent 0..N sequences.
- The terminal `message` event still fires; its `content` is the `send_message` tool argument, NOT the streamed assistant text. They differ. Clients keep the streamed body.
- Session opt-out via `streaming_enabled: bool = True` (settable on `POST /sessions`; strict literal-False check).
- Non-streaming providers (`ollama`, `omlx`, `vmlx`) accept the streaming hook as a no-op; only `anthropic`, `openai-codex` actually stream.

## Inspect catalogs

```bash
orb models
```

## Examples

```bash
orb --cloud-only "plan a refactor"
orb --local-only "summarize this module"
orb --model gpt-5.4-mini "build a CLI with tests"
```

Provider settings live in `~/.orb/config.json`. See [install.md](install.md) for the default mix and provider auth.

## GraphRAG memory

Orb persists structured memory into Chroma-backed stores organized by topology and cluster.

```yaml
persist_base: "~/.orb/chroma"

clusters:
  implementation:
    agents: [coordinator, coder]
  review:
    agents: [reviewer, tester]
```

Recent work optimized ephemeral Chroma stores so short-lived runs use a lighter embedding path, reducing write/query latency in tests and local iteration.

To inspect local Chroma data:

```bash
chroma run --path ~/.orb/chroma --port 8001
npx chromadb-admin
```

→ See: [Topologies](topologies.md) · [SDK](sdk.md)
