from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .loader import USER_TOPOLOGIES_PATH, get_loader

logger = logging.getLogger(__name__)


class TopologyWatcher:
    """Watches ~/.orb/topologies.yaml for changes and fires reload callbacks."""

    def __init__(self, poll_interval: float = 1.0) -> None:
        self._poll_interval = poll_interval
        self._callbacks: list[Callable[[], Awaitable[None]]] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def on_reload(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _watch_loop(self) -> None:
        loader = get_loader()
        while self._running:
            try:
                if loader.reload_if_changed():
                    logger.info(
                        "Topologies reloaded from %s", USER_TOPOLOGIES_PATH
                    )
                    for cb in self._callbacks:
                        try:
                            await cb()
                        except Exception as exc:
                            logger.warning("Reload callback failed: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Watcher loop error: %s", exc)
            await asyncio.sleep(self._poll_interval)


# ---- global singleton ----

_watcher: TopologyWatcher | None = None


def get_watcher() -> TopologyWatcher:
    global _watcher
    if _watcher is None:
        _watcher = TopologyWatcher()
    return _watcher
