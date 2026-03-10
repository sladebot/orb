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
    provider: str  # "anthropic", "openai", "ollama"
    max_tokens: int = 8192
    temperature: float = 0.7


# Default model configs per tier (Anthropic-first; OpenAI alternatives listed separately)
DEFAULT_MODELS: dict[ModelTier, ModelConfig] = {
    ModelTier.LOCAL_SMALL:  ModelConfig(ModelTier.LOCAL_SMALL,  "qwen3.5:9b",   "ollama"),
    ModelTier.LOCAL_MEDIUM: ModelConfig(ModelTier.LOCAL_MEDIUM, "qwen3.5:27b",  "ollama"),
    ModelTier.LOCAL_LARGE:  ModelConfig(ModelTier.LOCAL_LARGE,  "qwen3.5:27b",  "ollama"),
    ModelTier.CLOUD_LITE:   ModelConfig(ModelTier.CLOUD_LITE,   "claude-haiku-4-5-20251001",  "anthropic"),
    ModelTier.CLOUD_FAST:   ModelConfig(ModelTier.CLOUD_FAST,   "claude-sonnet-4-5-20251001",   "anthropic"),
    ModelTier.CLOUD_STRONG: ModelConfig(ModelTier.CLOUD_STRONG, "claude-opus-4-20250514",     "anthropic"),
}

# OpenAI API key models per tier
OPENAI_MODELS: dict[ModelTier, ModelConfig] = {
    ModelTier.CLOUD_LITE:   ModelConfig(ModelTier.CLOUD_LITE,   "gpt-4o-mini",  "openai"),
    ModelTier.CLOUD_FAST:   ModelConfig(ModelTier.CLOUD_FAST,   "gpt-4o",       "openai"),
    ModelTier.CLOUD_STRONG: ModelConfig(ModelTier.CLOUD_STRONG, "o3",           "openai"),
}

# OpenAI Codex (ChatGPT subscription) — all tiers use gpt-5.4
CODEX_MODELS: dict[ModelTier, ModelConfig] = {
    ModelTier.CLOUD_LITE:   ModelConfig(ModelTier.CLOUD_LITE,   "gpt-5.4",  "openai-codex"),
    ModelTier.CLOUD_FAST:   ModelConfig(ModelTier.CLOUD_FAST,   "gpt-5.4",  "openai-codex"),
    ModelTier.CLOUD_STRONG: ModelConfig(ModelTier.CLOUD_STRONG, "gpt-5.4",  "openai-codex"),
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
