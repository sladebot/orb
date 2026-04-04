from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ModelTier(Enum):
    LOCAL_SMALL = "local_small"    # 9B params
    LOCAL_MEDIUM = "local_medium"  # 14B params
    LOCAL_LARGE = "local_large"    # 30B params
    CLOUD_LITE = "cloud_lite"      # Haiku / GPT-4o-mini
    CLOUD_FAST = "cloud_fast"      # Sonnet / GPT-4o
    CLOUD_STRONG = "cloud_strong"  # Opus / GPT-4


@dataclass
class ModelConfig:
    tier: ModelTier
    model_id: str
    provider: str  # "anthropic", "openai-codex", "ollama", "vmlx", "omlx"
    max_tokens: int = 8192
    temperature: float = 0.7


ANTHROPIC_PROVIDER = "anthropic"
OPENAI_CODEX_PROVIDER = "openai-codex"
OLLAMA_PROVIDER = "ollama"
VMLX_PROVIDER = "vmlx"
OMLX_PROVIDER = "omlx"
OPENAI_MINI_MODEL = "gpt-5.4-mini"
OPENAI_STRONG_MODEL = "gpt-5.4"

ANTHROPIC_HAIKU_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_SONNET_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_OPUS_MODEL = "claude-opus-4-20250514"

ANTHROPIC_MODEL_LABELS: dict[str, str] = {
    ANTHROPIC_HAIKU_MODEL: "Claude Haiku 4.5",
    ANTHROPIC_SONNET_MODEL: "Claude Sonnet 4",
    ANTHROPIC_OPUS_MODEL: "Claude Opus 4",
}

ANTHROPIC_MODEL_DESCRIPTIONS: dict[str, str] = {
    ANTHROPIC_HAIKU_MODEL: "fastest / lowest cost",
    ANTHROPIC_SONNET_MODEL: "strong balanced reasoning",
    ANTHROPIC_OPUS_MODEL: "strongest reasoning",
}

ANTHROPIC_MODEL_TIERS: dict[ModelTier, str] = {
    ModelTier.CLOUD_LITE: ANTHROPIC_HAIKU_MODEL,
    ModelTier.CLOUD_FAST: ANTHROPIC_SONNET_MODEL,
    ModelTier.CLOUD_STRONG: ANTHROPIC_OPUS_MODEL,
}


# Default model configs per tier (Codex-first cloud defaults; local tiers remain Ollama)
DEFAULT_MODELS: dict[ModelTier, ModelConfig] = {
    ModelTier.LOCAL_SMALL:  ModelConfig(ModelTier.LOCAL_SMALL,  "qwen3.5:9b", OLLAMA_PROVIDER),
    ModelTier.LOCAL_MEDIUM: ModelConfig(ModelTier.LOCAL_MEDIUM, "qwen3.5:27b", OLLAMA_PROVIDER),
    ModelTier.LOCAL_LARGE:  ModelConfig(ModelTier.LOCAL_LARGE,  "qwen3.5:27b", OLLAMA_PROVIDER),
    ModelTier.CLOUD_LITE:   ModelConfig(ModelTier.CLOUD_LITE,   OPENAI_MINI_MODEL, OPENAI_CODEX_PROVIDER),
    ModelTier.CLOUD_FAST:   ModelConfig(ModelTier.CLOUD_FAST,   OPENAI_MINI_MODEL, OPENAI_CODEX_PROVIDER),
    ModelTier.CLOUD_STRONG: ModelConfig(ModelTier.CLOUD_STRONG, OPENAI_STRONG_MODEL, OPENAI_CODEX_PROVIDER),
}

# Anthropic cloud alternatives per tier
ANTHROPIC_MODELS: dict[ModelTier, ModelConfig] = {
    tier: ModelConfig(tier, model_id, ANTHROPIC_PROVIDER)
    for tier, model_id in ANTHROPIC_MODEL_TIERS.items()
}

# OpenAI Codex (ChatGPT subscription) — all tiers use gpt-5.4
CODEX_MODELS: dict[ModelTier, ModelConfig] = {
    ModelTier.CLOUD_LITE:   ModelConfig(ModelTier.CLOUD_LITE,   OPENAI_MINI_MODEL, OPENAI_CODEX_PROVIDER),
    ModelTier.CLOUD_FAST:   ModelConfig(ModelTier.CLOUD_FAST,   OPENAI_MINI_MODEL, OPENAI_CODEX_PROVIDER),
    ModelTier.CLOUD_STRONG: ModelConfig(ModelTier.CLOUD_STRONG, OPENAI_STRONG_MODEL, OPENAI_CODEX_PROVIDER),
}


@dataclass
class CompletionRequest:
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    system: str = ""
    model_config: ModelConfig | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class CompletionResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)
