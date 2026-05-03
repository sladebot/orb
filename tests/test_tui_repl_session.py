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

import asyncio
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
    t._restart_ws_client = MagicMock()
    t._handle_server_event = MagicMock()
    t._http_session = None
    t._ws_task = None
    t.available_topology_labels = {"solo": "solo", "triad": "triad", "dual-review": "dual-review", "hierarchy": "hierarchy"}
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


class _FakePost:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeResponse:
        return self._resp

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakePostHttpSession(_FakeHttpSession):
    def __init__(self, post_resp: _FakeResponse) -> None:
        super().__init__(_FakeResponse(200, {"ok": True, "data": {}}))
        self._post_resp = post_resp
        self.post_calls: list[tuple[str, dict]] = []

    def post(self, url: str, json=None):
        self.post_calls.append((url, json or {}))
        return _FakePost(self._post_resp)


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
async def test_ws_client_scopes_connection_to_current_session():
    from orb.cli.tui_repl import OrbReplTUI

    class _FakeSession:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def ws_connect(self, url: str, **_kwargs):
            self.urls.append(url)

            class _CM:
                async def __aenter__(self):
                    raise asyncio.CancelledError()

                async def __aexit__(self, *_args):
                    return None

            return _CM()

    tui = OrbReplTUI.__new__(OrbReplTUI)
    tui._server_scheme = "http"
    tui._server_host = "127.0.0.1"
    tui._server_port = 1337
    tui.session_id = "sid-123"
    tui._http_session = _FakeSession()

    with pytest.raises(asyncio.CancelledError):
        await tui._start_ws_client()

    assert tui._http_session.urls == [
        "ws://127.0.0.1:1337/api/v1/ws?session_id=sid-123"
    ]


@pytest.mark.asyncio
async def test_ws_client_defers_when_session_id_missing():
    from orb.cli.tui_repl import OrbReplTUI

    class _FakeSession:
        def ws_connect(self, *_args, **_kwargs):
            raise AssertionError("must not connect without a session_id")

    tui = OrbReplTUI.__new__(OrbReplTUI)
    tui._server_scheme = "http"
    tui._server_host = "127.0.0.1"
    tui._server_port = 1337
    tui.session_id = ""
    tui._http_session = _FakeSession()
    tui._emit_whisper = MagicMock()

    task = asyncio.create_task(tui._start_ws_client())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tui._emit_whisper.assert_called()


@pytest.mark.asyncio
async def test_attach_to_session_cancels_old_ws_before_init_dispatch():
    tui = _make_tui()
    order: list[str] = []
    old_task = MagicMock()
    old_task.cancel = MagicMock(side_effect=lambda: order.append("cancel"))
    tui._ws_task = old_task
    tui._restart_ws_client = MagicMock(side_effect=lambda: order.append("restart"))
    tui._handle_init = MagicMock(side_effect=lambda _payload: order.append("init"))
    tui._http_session = _FakeHttpSession(_FakeResponse(200, {
        "ok": True,
        "data": {"session_id": "new-sid", "agents": []},
    }))

    await tui._attach_to_session("new-sid")

    assert order == ["cancel", "init", "restart"]


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
    tui._handle_init.side_effect = lambda payload: setattr(tui, "session_id", payload["session_id"])

    await tui._attach_to_session("new-sid")

    assert tui.session_id == "new-sid"
    tui._handle_init.assert_called_once()
    tui._restart_ws_client.assert_called_once()
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
    tui._restart_ws_client.assert_not_called()


@pytest.mark.asyncio
async def test_new_slash_command_creates_fresh_session_and_attaches():
    tui = _make_tui()
    tui.workdir = "/tmp/project"
    tui._topology = "triad"
    tui.topology = "triad"
    tui.locked_topology = "solo"
    tui.locked_agent_models = {"coder": "gpt-4.1"}
    tui.approval_required = True
    tui.streaming_enabled = True
    tui._emit_turn = MagicMock()
    tui._attach_to_session = AsyncMock(return_value=True)
    tui._http_session = _FakePostHttpSession(_FakeResponse(201, {
        "ok": True,
        "code": "SESSION_CREATED",
        "data": {"session_id": "fresh-sid"},
    }))

    await tui._run_slash_command("/new dual-review")

    assert tui._http_session.post_calls == [
        (
            "http://127.0.0.1:1337/api/v1/sessions",
            {
                "workdir": "/tmp/project",
                "topology": "dual-review",
                "agent_models": {"coder": "gpt-4.1"},
                "approval_required": True,
            },
        )
    ]
    tui._attach_to_session.assert_awaited_once_with("fresh-sid")
    assert "new session attached" in tui._emit_turn.call_args[0][1]


@pytest.mark.asyncio
async def test_new_slash_command_defaults_to_active_concrete_topology_for_agent_models():
    tui = _make_tui()
    tui.workdir = "/tmp/project"
    tui._topology = "auto"
    tui.topology = "triad"
    tui.locked_topology = "triad"
    tui.locked_agent_models = {"coder": "gpt-4.1"}
    tui.approval_required = False
    tui.streaming_enabled = True
    tui._emit_turn = MagicMock()
    tui._attach_to_session = AsyncMock(return_value=True)
    tui._http_session = _FakePostHttpSession(_FakeResponse(201, {
        "ok": True,
        "code": "SESSION_CREATED",
        "data": {"session_id": "fresh-sid"},
    }))

    await tui._run_slash_command("/new")

    assert tui._http_session.post_calls == [
        (
            "http://127.0.0.1:1337/api/v1/sessions",
            {
                "workdir": "/tmp/project",
                "topology": "triad",
                "agent_models": {"coder": "gpt-4.1"},
            },
        )
    ]
    tui._attach_to_session.assert_awaited_once_with("fresh-sid")


@pytest.mark.asyncio
async def test_new_slash_command_reports_success_only_when_attach_succeeds():
    tui = _make_tui()
    tui.workdir = "/tmp/project"
    tui._topology = "triad"
    tui.topology = "triad"
    tui.locked_topology = None
    tui.locked_agent_models = {}
    tui.approval_required = False
    tui.streaming_enabled = True
    tui._emit_turn = MagicMock()
    tui._attach_to_session = AsyncMock(return_value=False)
    tui._http_session = _FakePostHttpSession(_FakeResponse(201, {
        "ok": True,
        "code": "SESSION_CREATED",
        "data": {"session_id": "fresh-sid"},
    }))

    await tui._run_slash_command("/new")

    tui._attach_to_session.assert_awaited_once_with("fresh-sid")
    emitted = [call.args[1] for call in tui._emit_turn.call_args_list]
    assert not any("new session attached" in message for message in emitted)
    assert any("/new failed" in message and "could not attach" in message for message in emitted)


@pytest.mark.asyncio
async def test_locked_topology_guidance_points_to_new_command_with_requested_topology():
    tui = _make_tui()
    tui._topology = "solo"
    tui.locked_topology = "solo"
    tui._emit_turn = MagicMock()

    await tui._run_slash_command("/topology triad")

    message = tui._emit_turn.call_args[0][1]
    assert "/new triad" in message
