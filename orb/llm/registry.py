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


def _ollama_base_url() -> str:
    """Resolve Ollama base URL.

    Priority:
    1. OLLAMA_HOST            — explicit Ollama host
    2. OPENAI_BASE_URL        — OpenAI-compat shim pointing at Ollama (non-openai.com URL)
    3. http://localhost:11434  — default
    """
    if os.environ.get("OLLAMA_HOST"):
        return os.environ["OLLAMA_HOST"].rstrip("/")
    openai_base = os.environ.get("OPENAI_BASE_URL", "")
    if openai_base and "openai.com" not in openai_base:
        # Strip /v1 suffix — Ollama's native API lives at the root
        return openai_base.rstrip("/").removesuffix("/v1")
    return "http://localhost:11434"


def _ollama_reachable() -> bool:
    import httpx
    try:
        httpx.get(f"{_ollama_base_url()}/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def _real_openai_api_key() -> str | None:
    """Return a genuine OpenAI API key, ignoring Ollama-compat env vars."""
    key = os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("OPENAI_API_BASE", "")
    # If OPENAI_BASE_URL points somewhere other than api.openai.com, the env
    # vars belong to an Ollama-compat setup — don't use them for the real
    # OpenAI provider.
    if base and "openai.com" not in base:
        key = ""
    # Placeholder keys used by Ollama shims
    if key.lower() in ("ollama", "none", "na", ""):
        key = ""
    return key or None


def _openai_available() -> bool:
    """True if a real OpenAI API key exists (not Ollama shim)."""
    return bool(_real_openai_api_key())


def _openai_factory() -> "LLMClient":
    """Build OpenAIProvider with real API key (never Ollama shim vars)."""
    from .providers import OpenAIProvider
    return OpenAIProvider(api_key=_real_openai_api_key(), base_url="https://api.openai.com/v1")


def _codex_token() -> str | None:
    """Return the stored OAuth access token if available and not expired."""
    try:
        from ..cli.auth import get_openai_token
        return get_openai_token()
    except Exception:
        return None


def _codex_available() -> bool:
    return bool(_codex_token())


def _codex_factory() -> "LLMClient":
    from .providers import OpenAICodexProvider
    return OpenAICodexProvider(access_token=_codex_token())


def _build_registry() -> list[ProviderSpec]:
    from .providers import AnthropicProvider, OllamaProvider
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
            env_vars=[],
            check=_openai_available,
            factory=_openai_factory,
        ),
        ProviderSpec(
            name="openai-codex",   # ChatGPT subscription — free with Plus/Pro
            is_cloud=True,
            env_vars=[],
            check=_codex_available,
            factory=_codex_factory,
        ),
        ProviderSpec(
            name="ollama",
            is_cloud=False,
            env_vars=[],               # detected by liveness check, no env var required
            check=_ollama_reachable,
            factory=lambda: OllamaProvider(base_url=_ollama_base_url()),
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
