from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

from .types import CompletionRequest, CompletionResponse


# ``on_chunk`` is the token-streaming hook. Providers that support
# streaming MUST invoke it with each non-empty text delta as soon as the
# chunk arrives from the upstream SDK (before accumulating the final
# response). Providers that don't stream (ollama/omlx/vmlx) accept the
# kwarg for a uniform call site but never invoke it — the agent's single
# ``message`` broadcast preserves back-compat for those providers.
#
# See ``tests/test_streaming.py`` + the shared contract with
# stream-tui/#13 and stream-dashboard/#14 on ``tui-improvements``.
OnChunk = Callable[[str], Awaitable[None]]


class LLMClient(ABC):
    """Abstract LLM client protocol."""

    @abstractmethod
    async def complete(
        self,
        request: CompletionRequest,
        *,
        on_chunk: Optional[OnChunk] = None,
    ) -> CompletionResponse:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
