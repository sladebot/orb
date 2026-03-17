from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass

import anthropic

from orb.cli.auth import load_credentials
from orb.llm.anthropic import OAUTH_BETAS, is_oauth_token


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
class ModelCheck:
    model: str
    mode: str
    ok: bool
    detail: str


def _resolve_token() -> str | None:
    creds = load_credentials("anthropic") or {}
    return (
        creds.get("oauth_token")
        or creds.get("api_key")
        or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


def _build_client(token: str) -> anthropic.AsyncAnthropic:
    if is_oauth_token(token):
        return anthropic.AsyncAnthropic(
            auth_token=token,
            default_headers={"anthropic-beta": OAUTH_BETAS},
        )
    return anthropic.AsyncAnthropic(api_key=token)


async def _list_models(client: anthropic.AsyncAnthropic, limit: int | None) -> list[tuple[str, str]]:
    page = await client.models.list(limit=limit)
    return [(item.id, item.display_name) for item in page.data]


async def _probe_text(client: anthropic.AsyncAnthropic, model_id: str) -> ModelCheck:
    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        return ModelCheck(model=model_id, mode="text", ok=True, detail=text or f"stop={response.stop_reason}")
    except Exception as exc:  # noqa: BLE001
        return ModelCheck(model=model_id, mode="text", ok=False, detail=str(exc))


async def _probe_tool(client: anthropic.AsyncAnthropic, model_id: str) -> ModelCheck:
    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=64,
            messages=[{"role": "user", "content": "Call the echo tool with text set to OK."}],
            tools=TOOL_SPEC,
        )
        tool_calls = [block for block in response.content if getattr(block, "type", None) == "tool_use"]
        detail = f"tool_calls={len(tool_calls)}"
        if tool_calls:
            detail += f" first={tool_calls[0].name}"
        return ModelCheck(model=model_id, mode="tool", ok=bool(tool_calls), detail=detail)
    except Exception as exc:  # noqa: BLE001
        return ModelCheck(model=model_id, mode="tool", ok=False, detail=str(exc))


async def _run(args: argparse.Namespace) -> int:
    token = _resolve_token()
    if not token:
        print("No Anthropic credentials found.")
        return 1

    client = _build_client(token)
    try:
        visible_models = await _list_models(client, args.limit)
        selected_models = [model_id for model_id, _ in visible_models]
        if args.model:
            requested = set(args.model)
            selected_models = [model_id for model_id in selected_models if model_id in requested]

        if args.list_only:
            payload = [{"id": model_id, "display_name": display_name} for model_id, display_name in visible_models]
            print(json.dumps(payload, indent=2) if args.json else "\n".join(f"{item['id']} | {item['display_name']}" for item in payload))
            return 0

        checks: list[ModelCheck] = []
        for model_id in selected_models:
            checks.append(await _probe_text(client, model_id))
            if not args.text_only:
                checks.append(await _probe_tool(client, model_id))

        if args.json:
            print(json.dumps({
                "visible_models": [{"id": model_id, "display_name": display_name} for model_id, display_name in visible_models],
                "checks": [asdict(check) for check in checks],
                "failures": [asdict(check) for check in checks if not check.ok],
            }, indent=2))
        else:
            print("Visible Anthropic models:")
            for model_id, display_name in visible_models:
                print(f"  {model_id} | {display_name}")
            print("\nChecks:")
            for check in checks:
                status = "PASS" if check.ok else "FAIL"
                print(f"  {status:4} {check.model:28} {check.mode:4} {check.detail}")

        return 1 if any(not check.ok for check in checks) else 0
    finally:
        await client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Admin probe for Anthropic model availability and message execution.")
    parser.add_argument("--limit", type=int, default=20, help="Limit the number of visible Anthropic models to inspect.")
    parser.add_argument("--model", action="append", help="Probe only this model ID. Repeat for multiple models.")
    parser.add_argument("--list-only", action="store_true", help="Only list models visible to the current Anthropic credentials.")
    parser.add_argument("--text-only", action="store_true", help="Skip tool-use checks and run only minimal text probes.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of plain text.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
