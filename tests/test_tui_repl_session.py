"""Session bootstrap + resume-picker tests for ``OrbReplTUI``.

These tests cover:

1. ``attach_tui_repl`` POSTs ``/api/v1/sessions`` on startup and passes
   the freshly minted session id into ``OrbReplTUI`` so the TUI does
   not have to wait for the WS ``init`` event to route inject/run
   requests to the right runtime.
2. ``OrbReplTUI._attach_to_session`` refuses error envelopes (non-200
   or ``ok: false`` bodies) and does not mutate ``session_id``.
3. On a valid ``200 OK`` envelope the method swaps ``session_id`` and
   dispatches the payload through ``_handle_init`` so every widget
   rebuilds against the new session.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_tui():
    """Build a bare ``OrbReplTUI`` with only the attributes
    ``_attach_to_session`` touches.  No Textual mount required."""
    from orb.cli.tui_repl import OrbReplTUI

    t = OrbReplTUI.__new__(OrbReplTUI)
    t.session_id = ""
    t._server_scheme = "http"
    t._server_host = "127.0.0.1"
    t._server_port = 1337
    t._handle_init = MagicMock()
    t._handle_server_event = MagicMock()
    return t


class _FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def json(self) -> dict:
        return self._body

    async def read(self) -> bytes:
        return b""


class _FakeGet:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeResponse:
        return self._resp

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeHttpSession:
    def __init__(self, get_resp: _FakeResponse) -> None:
        self._get_resp = get_resp
        self.get_calls: list[str] = []

    def get(self, url: str) -> _FakeGet:
        self.get_calls.append(url)
        return _FakeGet(self._get_resp)


# ── attach_tui_repl: POSTs a new session on startup ─────────────────────


@pytest.mark.asyncio
async def test_attach_tui_repl_posts_new_session_on_startup():
    """``attach_tui_repl`` must hit ``POST /api/v1/sessions`` on startup
    (when no explicit ``session_id`` is passed) and forward the
    returned id into ``OrbReplTUI``."""
    from orb.cli import tui_repl

    # Fake aiohttp.ClientSession → one POST that returns a v1 envelope.
    post_calls: list[tuple[str, dict]] = []

    class _FakePostResp:
        status = 201

        async def json(self) -> dict:
            return {
                "ok": True,
                "code": "SESSION_CREATED",
                "data": {"session_id": "created-sid-123"},
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def post(self, url, json=None):
            post_calls.append((url, json))
            return _FakePostResp()

    # Stub OrbReplTUI.run_async so we don't actually mount Textual.
    run_async = AsyncMock(return_value=None)
    with patch.object(tui_repl, "OrbReplTUI") as MockApp, \
         patch("aiohttp.ClientSession", return_value=_FakeClient()):
        instance = MagicMock()
        instance.run_async = run_async
        MockApp.return_value = instance

        await tui_repl.attach_tui_repl(
            "http://127.0.0.1:1337",
            topology="triad",
            budget=200,
            show_logs=False,
            initial_query=None,
            exit_after_run=False,
            workdir="/tmp",
        )

    # Exactly one POST to /api/v1/sessions with the expected shape.
    assert len(post_calls) == 1
    url, body = post_calls[0]
    assert url == "http://127.0.0.1:1337/api/v1/sessions"
    assert body["workdir"] == "/tmp"
    assert body["topology"] == "triad"

    # Session id threaded into OrbReplTUI constructor.
    MockApp.assert_called_once()
    _, kwargs = MockApp.call_args
    assert kwargs["session_id"] == "created-sid-123"


# ── _attach_to_session: envelope safety ─────────────────────────────────


@pytest.mark.asyncio
async def test_attach_to_session_refuses_404():
    tui = _make_tui()
    tui._http_session = _FakeHttpSession(_FakeResponse(404, {"ok": False, "error": "gone"}))

    await tui._attach_to_session("ghost-sid")

    assert tui.session_id == ""
    tui._handle_init.assert_not_called()


@pytest.mark.asyncio
async def test_attach_to_session_refuses_ok_false_envelope():
    tui = _make_tui()
    # 200 but ``ok: false`` — still an error envelope.
    tui._http_session = _FakeHttpSession(_FakeResponse(200, {"ok": False, "error": "nope"}))

    await tui._attach_to_session("bad-sid")

    assert tui.session_id == ""
    tui._handle_init.assert_not_called()


@pytest.mark.asyncio
async def test_attach_to_session_accepts_valid_envelope_and_dispatches_init():
    tui = _make_tui()
    init_payload = {
        "session_id": "new-sid",
        "workdir": "/tmp/project",
        "agents": [],
        "plan": {"topology": {"id": "triad"}},
        "stats": {"message_count": 0, "elapsed": 0.0},
    }
    tui._http_session = _FakeHttpSession(_FakeResponse(200, {
        "ok": True,
        "code": "SESSION_STATE",
        "data": init_payload,
    }))

    await tui._attach_to_session("new-sid")

    assert tui.session_id == "new-sid"
    tui._handle_init.assert_called_once()
    dispatched = tui._handle_init.call_args[0][0]
    assert dispatched["session_id"] == "new-sid"
    assert dispatched["workdir"] == "/tmp/project"
    # URL assembled from server scheme/host/port.
    assert tui._http_session.get_calls == [
        "http://127.0.0.1:1337/api/v1/sessions/new-sid/state"
    ]


@pytest.mark.asyncio
async def test_attach_to_session_ignores_empty_or_same_session():
    tui = _make_tui()
    tui.session_id = "current"
    tui._http_session = _FakeHttpSession(_FakeResponse(200, {"ok": True, "data": {}}))

    await tui._attach_to_session("")
    await tui._attach_to_session("current")

    # No HTTP call, no init dispatch.
    assert tui._http_session.get_calls == []
    tui._handle_init.assert_not_called()
