from __future__ import annotations

from time import time

from rich.console import Console
from rich.text import Text

from ..messaging.message import Message, MessageType
from .run_trace import RunTrace

AGENT_COLORS = {
    "coder": "cyan",
    "reviewer": "yellow",
    "tester": "green",
}


class EventLogger:
    """Real-time event logger for message routing."""

    def __init__(self, enabled: bool = True, trace: RunTrace | None = None) -> None:
        self.enabled = enabled
        self._start_time = time()
        self._console = Console()
        self._events: list[dict] = []
        self.trace = trace

    def reset(self) -> None:
        self._start_time = time()
        self._events.clear()
        if self.trace is not None:
            self.trace.reset()

    def __call__(self, event: str, msg: Message) -> None:
        if self.trace is not None:
            if event == "injected":
                self.trace.record_initial_injection(
                    target=msg.to,
                    message=msg.payload,
                    data={
                        "message_id": msg.id,
                        "msg_type": msg.type.value,
                        "depth": msg.depth,
                        "chain_id": msg.chain_id,
                        "model": msg.metadata.get("model", ""),
                        "context_slice": list(msg.context_slice),
                    },
                )
            else:
                self.trace.record_message_routed(
                    event,
                    actor=msg.from_,
                    target=msg.to,
                    message=msg.payload,
                    data={
                        "message_id": msg.id,
                        "msg_type": msg.type.value,
                        "depth": msg.depth,
                        "chain_id": msg.chain_id,
                        "model": msg.metadata.get("model", ""),
                        "context_slice": list(msg.context_slice),
                    },
                )

        if not self.enabled:
            return

        elapsed = time() - self._start_time
        model = msg.metadata.get("model", "")
        model_str = f" ({model})" if model else ""

        record = {
            "elapsed": elapsed,
            "event": event,
            "from": msg.from_,
            "to": msg.to,
            "model": model,
            "type": msg.type.value,
            "depth": msg.depth,
            "preview": msg.payload[:80],
        }
        self._events.append(record)

        if msg.type == MessageType.COMPLETE:
            to_str = "[COMPLETE]"
        else:
            to_str = msg.to

        from_color = AGENT_COLORS.get(msg.from_.lower(), "white")
        to_color = AGENT_COLORS.get(to_str.lower(), "white") if to_str != "[COMPLETE]" else "bold green"

        text = Text()
        text.append(f"[{elapsed:5.1f}s] ", style="dim")
        text.append(msg.from_, style=from_color)
        text.append(model_str, style="dim")
        text.append(" -> ", style="dim")
        text.append(to_str, style=to_color)
        text.append(": ", style="dim")

        preview = msg.payload[:100].replace("\n", " ")
        text.append(f'"{preview}"', style="italic")

        self._console.print(text)

    @property
    def events(self) -> list[dict]:
        return list(self._events)
