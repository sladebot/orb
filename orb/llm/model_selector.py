from __future__ import annotations

from ..messaging.message import Message, MessageType
from .types import ModelTier


class ModelSelector:
    """Scores task complexity and selects appropriate model tier."""

    def __init__(self, base_complexity: int = 50) -> None:
        self.base_complexity = base_complexity
        self._retry_count = 0

    def select(self, msg: Message) -> ModelTier:
        score = self._score(msg)
        return self._tier_from_score(score)

    def escalate(self) -> None:
        """Called when a response seems low-quality; bumps retry count."""
        self._retry_count += 1

    def reset_retries(self) -> None:
        self._retry_count = 0

    def _score(self, msg: Message) -> int:
        score = self.base_complexity

        # Deeper chains = harder reasoning
        if msg.depth > 3:
            score += 15

        # Feedback messages tend to need nuanced reasoning
        if msg.type == MessageType.FEEDBACK:
            score += 20

        # Large payloads suggest complex content
        if len(msg.payload) > 2000:
            score += 10

        # Explicit complexity hint
        hint = msg.metadata.get("complexity", "")
        if hint == "high":
            score += 25
        elif hint == "low":
            score -= 15

        # Self-escalation on retries
        score += 10 * self._retry_count

        return min(100, max(0, score))

    @staticmethod
    def _tier_from_score(score: int) -> ModelTier:
        if score <= 30:
            return ModelTier.LOCAL_SMALL
        elif score <= 60:
            return ModelTier.LOCAL_MEDIUM
        elif score <= 80:
            return ModelTier.LOCAL_LARGE
        elif score <= 95:
            return ModelTier.CLOUD_FAST
        else:
            return ModelTier.CLOUD_STRONG
