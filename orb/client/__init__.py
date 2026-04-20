"""Async Python client for the Orb multi-tenant v1 API.

External harnesses (hermes, openclaw, evaluation pipelines) use this
package to drive a running Orb daemon: create sessions, submit runs,
stream live events. Example::

    from orb.client import OrbClient

    async with OrbClient("http://localhost:1337") as client:
        session = await client.create_session(workdir="/path/to/repo")
        run = await session.start_run(query="fix the bug", topology="triad")
        async for event in session.stream_events():
            if event.type == "run_state_changed" and event.to == "completed":
                break
        result = await session.state()

The client targets the v1 surface only; legacy `/api/*` routes are not
supported.
"""

from __future__ import annotations

from .client import OrbClient, OrbSession
from .types import Event, RunSummary, SessionSummary

__all__ = [
    "OrbClient",
    "OrbSession",
    "Event",
    "RunSummary",
    "SessionSummary",
]
