from __future__ import annotations

from abc import ABC, abstractmethod

from .types import CompletionRequest, CompletionResponse


class LLMClient(ABC):
    """Abstract LLM client protocol."""

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
