from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ModelTier(Enum):
    LOCAL_SMALL = "local_small"    # 9B params
    LOCAL_MEDIUM = "local_medium"  # 14B params
    LOCAL_LARGE = "local_large"    # 30B params
    CLOUD_FAST = "cloud_fast"      # Sonnet / GPT-4o-mini
    CLOUD_STRONG = "cloud_strong"  # Opus / GPT-4o


@dataclass
class ModelConfig:
    tier: ModelTier
    model_id: str
    provider: str  # "anthropic", "openai", "ollama"
    max_tokens: int = 4096
    temperature: float = 0.7


# Default model configs per tier
DEFAULT_MODELS: dict[ModelTier, ModelConfig] = {
    ModelTier.LOCAL_SMALL: ModelConfig(ModelTier.LOCAL_SMALL, "llama3.2:latest", "ollama"),
    ModelTier.LOCAL_MEDIUM: ModelConfig(ModelTier.LOCAL_MEDIUM, "qwen2.5:14b", "ollama"),
    ModelTier.LOCAL_LARGE: ModelConfig(ModelTier.LOCAL_LARGE, "qwen2.5:32b", "ollama"),
    ModelTier.CLOUD_FAST: ModelConfig(ModelTier.CLOUD_FAST, "claude-sonnet-4-20250514", "anthropic"),
    ModelTier.CLOUD_STRONG: ModelConfig(ModelTier.CLOUD_STRONG, "claude-opus-4-20250514", "anthropic"),
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
