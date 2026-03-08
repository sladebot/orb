from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .client import LLMClient


@dataclass
class ProviderSpec:
    """Describes how to detect and instantiate a single LLM provider."""
    name: str
    factory: Callable[[], LLMClient]
    is_cloud: bool
    # At least one of these env vars must be set to activate this provider.
    # Empty list = always attempt (e.g. Ollama — uses liveness check instead).
    env_vars: list[str] = field(default_factory=list)
    # Optional extra liveness check (e.g. HTTP ping). Return True if available.
    check: Callable[[], bool] | None = None


def _ollama_reachable() -> bool:
    import httpx
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def _build_registry() -> list[ProviderSpec]:
    from .providers import AnthropicProvider, OpenAIProvider, OllamaProvider
    return [
        ProviderSpec(
            name="anthropic",
            is_cloud=True,
            env_vars=["ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
            factory=AnthropicProvider,
        ),
        ProviderSpec(
            name="openai",
            is_cloud=True,
            env_vars=["OPENAI_API_KEY"],
            factory=OpenAIProvider,
        ),
        ProviderSpec(
            name="ollama",
            is_cloud=False,
            env_vars=["OLLAMA_ENABLED"],  # set OLLAMA_ENABLED=1 to activate
            check=_ollama_reachable,
            factory=OllamaProvider,
        ),
    ]


# Module-level registry — extend this list to add new providers.
PROVIDER_REGISTRY: list[ProviderSpec] = _build_registry()


def register_provider(spec: ProviderSpec) -> None:
    """Add a new provider spec to the registry at runtime."""
    PROVIDER_REGISTRY.append(spec)


def build_providers(
    local_only: bool = False,
    cloud_only: bool = False,
) -> dict[str, LLMClient]:
    """Discover and instantiate all available providers from the registry."""
    providers: dict[str, LLMClient] = {}

    for spec in PROVIDER_REGISTRY:
        if local_only and spec.is_cloud:
            continue
        if cloud_only and not spec.is_cloud:
            continue

        # Env var gate
        if spec.env_vars and not any(os.environ.get(v) for v in spec.env_vars):
            continue

        # Liveness check
        if spec.check and not spec.check():
            continue

        providers[spec.name] = spec.factory()

    return providers
