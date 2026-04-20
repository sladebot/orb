"""Async client + session wrapper for the Orb multi-tenant v1 API.

``OrbClient`` owns an ``httpx.AsyncClient`` and issues REST calls against
a running daemon. ``OrbSession`` is a thin, stateful handle for a single
session in the daemon's registry — harnesses do their work through it.

Design notes:
  - Every REST helper raises :class:`OrbAPIError` on envelope ``ok: false``
    so callers can ``try/except`` instead of branching on flags.
  - Event streams are exposed as ``async for event in session.stream_events()``
    so harnesses can await the next ``run_state_changed`` to reach a
    terminal state without managing the WebSocket manually.
  - Both ``OrbClient`` and ``OrbSession`` support ``async with`` so
    resources get cleaned up even on exception paths.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
import websockets

from .types import Event, RunSummary, SessionSummary


class OrbAPIError(RuntimeError):
    """Raised when the daemon returns a non-ok envelope or unreachable."""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.message = message
        self.status = status


class OrbClient:
    """Top-level client — create sessions, manage the daemon, drive runs."""

    def __init__(
        self,
        base_url: str = "http://localhost:1337",
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        # ws URL mirrors base but swaps http→ws. Support https→wss for completeness.
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self._ws_url = f"{ws_scheme}://{parsed.netloc}"
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OrbClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ── Low-level helpers ──────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        """Issue a request and return the envelope's ``data`` payload.

        Returns ``None`` iff the envelope explicitly had ``data: null``
        (or the key was missing). Returns the dict otherwise. Callers
        that contractually require a payload should check for ``None``
        and raise :class:`OrbAPIError("EMPTY_DATA", ...)`; callers that
        legitimately return nothing (delete, stop) can ignore ``None``.
        """
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OrbAPIError("NETWORK_ERROR", str(exc), 0) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise OrbAPIError("INVALID_JSON", f"{method} {path} → {resp.status_code}: {resp.text[:200]}", resp.status_code) from exc
        if not body.get("ok"):
            raise OrbAPIError(
                str(body.get("code") or f"HTTP_{resp.status_code}"),
                str(body.get("error") or resp.text[:200]),
                resp.status_code,
            )
        data = body.get("data")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise OrbAPIError(
                "INVALID_DATA",
                f"{method} {path}: expected object in `data`, got {type(data).__name__}",
                resp.status_code,
            )
        return data

    @staticmethod
    def _require_data(data: dict[str, Any] | None, endpoint: str) -> dict[str, Any]:
        """Guard helper for endpoints that must return a payload."""
        if data is None:
            raise OrbAPIError(
                "EMPTY_DATA",
                f"{endpoint} returned an empty envelope; expected payload.",
            )
        return data

    # ── Daemon introspection ──────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        return self._require_data(
            await self._request("GET", "/api/v1/health"),
            "GET /api/v1/health",
        )

    # ── Session management ────────────────────────────────────────────

    async def create_session(
        self,
        *,
        workdir: str | None = None,
    ) -> "OrbSession":
        body: dict[str, Any] = {}
        if workdir is not None:
            body["workdir"] = workdir
        data = self._require_data(
            await self._request("POST", "/api/v1/sessions", json=body),
            "POST /api/v1/sessions",
        )
        summary = SessionSummary.from_dict(data)
        return OrbSession(self, summary)

    async def get_session(self, session_id: str) -> "OrbSession":
        data = self._require_data(
            await self._request("GET", f"/api/v1/sessions/{session_id}"),
            f"GET /api/v1/sessions/{session_id}",
        )
        return OrbSession(self, SessionSummary.from_dict(data))

    async def list_sessions(self) -> list[SessionSummary]:
        data = self._require_data(
            await self._request("GET", "/api/v1/sessions"),
            "GET /api/v1/sessions",
        )
        return [SessionSummary.from_dict(s) for s in data.get("sessions") or []]

    async def delete_session(self, session_id: str) -> None:
        # delete returns no payload; tolerate null data in the envelope.
        await self._request("DELETE", f"/api/v1/sessions/{session_id}")

    # ── WebSocket ──────────────────────────────────────────────────────

    async def stream_events(
        self,
        session_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """Stream WebSocket events, optionally filtered to one session.

        Without ``session_id`` the daemon emits every session's events;
        the client yields them all so harnesses can multiplex.
        """
        url = f"{self._ws_url}/api/v1/ws"
        if session_id:
            url = f"{url}?session_id={session_id}"
        async with websockets.connect(url) as ws:
            async for msg in ws:
                if not isinstance(msg, (str, bytes)):
                    continue
                text = msg.decode() if isinstance(msg, (bytes, bytearray)) else msg
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    yield Event.from_payload(payload)


class OrbSession:
    """Handle for a single session in the daemon's registry.

    The ``OrbSession`` object is a value object — calls against it re-issue
    HTTP requests rather than caching state. Use ``refresh()`` to fetch
    the latest summary from the server.
    """

    def __init__(self, client: OrbClient, summary: SessionSummary) -> None:
        self._client = client
        self._summary = summary

    @property
    def session_id(self) -> str:
        return self._summary.session_id

    @property
    def workdir(self) -> str:
        return self._summary.workdir

    @property
    def run_state(self) -> str:
        return self._summary.run_state

    @property
    def summary(self) -> SessionSummary:
        return self._summary

    # ── REST helpers ──────────────────────────────────────────────────

    async def refresh(self) -> SessionSummary:
        data = OrbClient._require_data(  # noqa: SLF001
            await self._client._request("GET", f"/api/v1/sessions/{self.session_id}"),  # noqa: SLF001
            f"GET /api/v1/sessions/{self.session_id}",
        )
        self._summary = SessionSummary.from_dict(data)
        return self._summary

    async def delete(self) -> None:
        await self._client.delete_session(self.session_id)

    async def start_run(
        self,
        query: str,
        *,
        topology: str = "auto",
        model: str = "auto",
        agent_models: dict[str, str] | None = None,
        workdir: str | None = None,
    ) -> RunSummary:
        body: dict[str, Any] = {
            "query": query,
            "topology": topology,
            "model": model,
        }
        if agent_models:
            body["agent_models"] = agent_models
        if workdir:
            body["workdir"] = workdir
        data = OrbClient._require_data(  # noqa: SLF001
            await self._client._request(  # noqa: SLF001
                "POST",
                f"/api/v1/sessions/{self.session_id}/runs",
                json=body,
            ),
            f"POST /api/v1/sessions/{self.session_id}/runs",
        )
        return RunSummary.from_dict(data)

    async def stop_run(self) -> dict[str, Any]:
        # stop may legitimately return null data; surface it as an empty
        # dict for back-compat with callers that expect a dict.
        data = await self._client._request(  # noqa: SLF001
            "POST",
            f"/api/v1/sessions/{self.session_id}/runs/stop",
        )
        return data or {}

    async def inject(self, target: str, message: str) -> None:
        await self._client._request(  # noqa: SLF001
            "POST",
            f"/api/v1/sessions/{self.session_id}/runs/inject",
            json={"to": target, "message": message},
        )

    async def state(self) -> dict[str, Any]:
        """Return the full init payload for the session (synchronous snapshot)."""
        return OrbClient._require_data(  # noqa: SLF001
            await self._client._request(  # noqa: SLF001
                "GET",
                f"/api/v1/sessions/{self.session_id}/state",
            ),
            f"GET /api/v1/sessions/{self.session_id}/state",
        )

    # ── Event streaming ───────────────────────────────────────────────

    def stream_events(self) -> AsyncIterator[Event]:
        """Tail the WebSocket for this session only.

        Harness pattern::

            async for event in session.stream_events():
                if event.type == "run_state_changed" and event.is_terminal:
                    break

        The iterator closes when the WebSocket disconnects.
        """
        return self._client.stream_events(self.session_id)

    async def wait_for_terminal(self) -> Event:
        """Block until the session's FSM lands in a terminal state.

        Returns the ``run_state_changed`` event that marked the transition.
        Useful for batch harness code that wants "submit and wait".

        Semantics:
          - If the session is already at rest (``idle`` / ``completed`` /
            ``errored``) when the caller waits, return a synthetic
            terminal event immediately — there is no run to wait on.
          - Otherwise, only ``completed`` and ``errored`` transitions
            unblock the waiter. An ``idle`` event emitted *during*
            streaming is taken as terminal only if it was the tail of a
            stop request (``from == "stopping"``) — this avoids the
            stale-idle race where a pre-run idle event would falsely
            unblock a fresh run.
        """
        # Already at rest — no run to wait on.
        if self._summary.run_state in {"idle", "completed", "errored"}:
            return Event.from_payload({
                "type": "run_state_changed",
                "session_id": self.session_id,
                "to": self._summary.run_state,
                "from": self._summary.run_state,
                "event": "already_terminal",
            })

        async for event in self.stream_events():
            if event.type == "run_state_changed" and event.is_terminal:
                return event
        raise OrbAPIError("WS_CLOSED_EARLY", "Stream closed before reaching a terminal state")

    # ── Context manager ──────────────────────────────────────────────

    async def __aenter__(self) -> "OrbSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # The session lives on in the daemon by default; harnesses that
        # want to tear down explicitly use `await session.delete()`.
        return
