from __future__ import annotations

from .message import Message


class HopLimitExceeded(Exception):
    pass


class BudgetExhausted(Exception):
    pass


class HopCounter:
    """Rejects messages that exceed max hop depth."""

    def __init__(self, max_depth: int = 10) -> None:
        self.max_depth = max_depth

    def check(self, msg: Message) -> None:
        if msg.depth > self.max_depth:
            raise HopLimitExceeded(
                f"Message {msg.id} exceeded max depth {self.max_depth} (depth={msg.depth})"
            )


class BudgetTracker:
    """Tracks global message count and rejects when budget exhausted."""

    def __init__(self, budget: int = 200) -> None:
        self.budget = budget
        self.count = 0

    def check(self, msg: Message) -> None:
        if self.count >= self.budget:
            raise BudgetExhausted(
                f"Global message budget exhausted ({self.budget})"
            )

    def increment(self) -> None:
        self.count += 1

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.count)


class CooldownTracker:
    """Prevents an agent from sending to the same target too many times per chain."""

    def __init__(self, max_per_chain: int = 5) -> None:
        self.max_per_chain = max_per_chain
        # {(from, to, chain_id): count}
        self._counts: dict[tuple[str, str, str], int] = {}

    def check(self, msg: Message) -> None:
        key = (msg.from_, msg.to, msg.chain_id)
        count = self._counts.get(key, 0)
        if count >= self.max_per_chain:
            raise HopLimitExceeded(
                f"Agent {msg.from_!r} exceeded cooldown limit to {msg.to!r} "
                f"in chain {msg.chain_id!r} ({count}/{self.max_per_chain})"
            )

    def increment(self, msg: Message) -> None:
        key = (msg.from_, msg.to, msg.chain_id)
        self._counts[key] = self._counts.get(key, 0) + 1
