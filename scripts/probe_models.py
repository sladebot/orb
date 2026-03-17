from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from orb.llm.registry import build_providers
from orb.llm.types import (
    ANTHROPIC_MODELS,
    CODEX_MODELS,
    CompletionRequest,
    ModelConfig,
    ModelTier,
)


MODEL_MATRIX: dict[str, list[ModelConfig]] = {
    "anthropic": [
        ANTHROPIC_MODELS[ModelTier.CLOUD_LITE],
        ANTHROPIC_MODELS[ModelTier.CLOUD_FAST],
        ANTHROPIC_MODELS[ModelTier.CLOUD_STRONG],
    ],
    "openai": [
        ModelConfig(ModelTier.CLOUD_FAST, "gpt-4o", "openai"),
    ],
    "openai-codex": [
        CODEX_MODELS[ModelTier.CLOUD_FAST],
    ],
    "ollama": [
        ModelConfig(ModelTier.LOCAL_SMALL, "qwen3.5:9b", "ollama"),
        ModelConfig(ModelTier.LOCAL_LARGE, "qwen3.5:27b", "ollama"),
    ],
}

TOOL_SPEC = [{
    "name": "echo",
    "description": "Echoes a short string back to the caller.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    },
}]


@dataclass
class ProbeResult:
    provider: str
    model: str
    mode: str
    ok: bool
    detail: str


async def _probe_text(provider_name: str, provider, model: ModelConfig) -> ProbeResult:
    req = CompletionRequest(
        system="You are a diagnostic probe. Reply with exactly OK.",
        messages=[{"role": "user", "content": "Reply with exactly OK."}],
        model_config=ModelConfig(
            tier=model.tier,
            model_id=model.model_id,
            provider=provider_name,
            max_tokens=32,
            temperature=0,
        ),
    )
    try:
        resp = await provider.complete(req)
        return ProbeResult(
            provider=provider_name,
            model=model.model_id,
            mode="text",
            ok=True,
            detail=(resp.content or "").strip()[:120] or f"stop={resp.stop_reason}",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(provider_name, model.model_id, "text", False, str(exc))


async def _probe_tool(provider_name: str, provider, model: ModelConfig) -> ProbeResult:
    req = CompletionRequest(
        system="You are a diagnostic probe. You must call the echo tool.",
        messages=[{"role": "user", "content": "Call the echo tool with text set to OK."}],
        tools=TOOL_SPEC,
        model_config=ModelConfig(
            tier=model.tier,
            model_id=model.model_id,
            provider=provider_name,
            max_tokens=64,
            temperature=0,
        ),
    )
    try:
        resp = await provider.complete(req)
        detail = f"tool_calls={len(resp.tool_calls)}"
        if resp.tool_calls:
            detail += f" first={resp.tool_calls[0].name}"
        elif resp.content:
            detail += f" text={resp.content.strip()[:80]}"
        return ProbeResult(provider_name, model.model_id, "tool", bool(resp.tool_calls), detail)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(provider_name, model.model_id, "tool", False, str(exc))


async def main() -> int:
    providers = build_providers(local_only=False, cloud_only=False)
    if not providers:
        print("No providers available.")
        return 1

    results: list[ProbeResult] = []
    for provider_name, provider in providers.items():
        models = MODEL_MATRIX.get(provider_name, [])
        if not models:
            continue
        for model in models:
            results.append(await _probe_text(provider_name, provider, model))
            results.append(await _probe_tool(provider_name, provider, model))

    for provider in providers.values():
        await provider.close()

    grouped: dict[str, list[ProbeResult]] = {}
    for item in results:
        grouped.setdefault(item.provider, []).append(item)

    for provider_name, items in grouped.items():
        print(f"\n[{provider_name}]")
        for item in items:
            status = "PASS" if item.ok else "FAIL"
            print(f"  {status:4} {item.model:28} {item.mode:4} {item.detail}")

    failures = [r for r in results if not r.ok]
    summary = {
        "providers": sorted(grouped),
        "total_checks": len(results),
        "failures": len(failures),
        "failure_matrix": [
            {
                "provider": f.provider,
                "model": f.model,
                "mode": f.mode,
                "detail": f.detail,
            }
            for f in failures
        ],
    }
    print("\nJSON_SUMMARY=" + json.dumps(summary))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
