"""Deep end-to-end integration tests for ``OrbReplTUI``.

Unlike ``test_tui_repl_event_handlers.py`` (which exercises the event
handlers against a ``__new__``-constructed app with stub widgets), this
file mounts the **real** Textual app via ``run_test()`` and drives a
realistic server-event sequence through it. The goal is to catch
regressions in actual widget rendering — Rich/Textual markup errors,
missing widgets, re-entrancy issues on ``refresh_content``, etc.

Every event call is followed by ``await pilot.pause()`` so Textual gets
to run its render cycle; any rendering exception (e.g. ``MarkupError``)
will propagate out of the pause/exit and fail the test.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orb.cli.tui_repl import ContextRail, OrbReplTUI, ReplStream, StatusStrip


# ── Fake aiohttp session for composer-send tests ──────────────────────


class _FakeResp:
    def __init__(self) -> None:
        self._data = b""

    async def read(self) -> bytes:
        return self._data

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSession:
    """Records every POST for later assertion; quacks like aiohttp.ClientSession."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> _FakeResp:  # sync call returning async CM
        self.posts.append({"url": url, **kwargs})
        return _FakeResp()

    async def close(self) -> None:
        self.closed = True


# ── Helpers ──────────────────────────────────────────────────────────


def _init_payload(
    *,
    session_id: str = "sid-1",
    workdir: str = "/tmp/project",
) -> dict:
    return {
        "type": "init",
        "session_id": session_id,
        "workdir": workdir,
        "plan": {"topology": {"id": "triad"}},
        "agents": [
            {"id": "coordinator", "role": "coordinator", "status": "idle", "model": "opus"},
            {"id": "coder", "role": "coder", "status": "idle", "model": "sonnet"},
            {"id": "reviewer", "role": "reviewer", "status": "idle", "model": "haiku"},
            {"id": "tester", "role": "tester", "status": "idle", "model": "haiku"},
        ],
        "stats": {"message_count": 0, "elapsed": 0.0},
        "messages": [],
    }


def _render_plain(widget: Any) -> str:
    """Return a widget's rendered plain text (markup stripped)."""
    rendered = widget.render()
    # Textual ``Content`` and Rich ``Text`` both expose ``.plain``.
    plain = getattr(rendered, "plain", None)
    if plain is not None:
        return plain
    return str(rendered)


# ── Test: full event flow ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_event_flow_no_render_errors():
    """Drive every server-event type through a real mounted app."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        # Replace the live aiohttp session (created in on_mount) with a
        # fake so teardown and any accidental sends don't touch the net.
        real_session = app._http_session
        app._http_session = _FakeSession()
        if real_session is not None:
            await real_session.close()
        await pilot.pause()

        # 1. init ──────────────────────────────────────────────────
        app._handle_server_event(_init_payload())
        await pilot.pause()
        assert app.session_id == "sid-1"
        assert app.topology == "triad"
        assert set(app.agent_order) == {"coordinator", "coder", "reviewer", "tester"}

        # 2. agent_status → running on coder ───────────────────────
        app._handle_server_event({
            "type": "agent_status", "agent": "coder", "status": "running",
        })
        await pilot.pause()
        assert app.agents["coder"]["status"] == "running"

        # 3. agent_activity → ⏳ Waiting for user (regression for
        #    rendering ``⏳ Waiting for user: …`` with full_content).
        app._handle_server_event({
            "type": "agent_activity",
            "agent": "coordinator",
            "activity": "⏳ Waiting for user: quick summary?",
            "details": {
                "full_content": "Please clarify requirement X\nand Y — need an answer.",
            },
        })
        await pilot.pause()
        assert "waiting" in app.live_text.lower()

        # 4. file_write ────────────────────────────────────────────
        # This is *the* regression path — the original markup chose
        # ``[[ok]applied[/]]`` which Rich's parser chokes on (nested
        # brackets inside a tag). Render must succeed.
        app._handle_server_event({
            "type": "file_write",
            "agent": "coder",
            "path": "src/orb/example.py",
            "old_content": "a\nb\n",
            "content": "a\nb\nc\nd\ne\n",
        })
        await pilot.pause()
        assert "src/orb/example.py" in app.file_changes

        # 5. plan_step ─────────────────────────────────────────────
        app._handle_server_event({
            "type": "plan_step",
            "title": "design schema",
            "detail": "sketch tables, choose indexes",
        })
        await pilot.pause()
        assert any(t == "design schema" for _, t in app.plan_items)

        # 6. message (user + coder) ────────────────────────────────
        app._handle_server_event({
            "type": "message", "from": "user", "content": "please add a test",
        })
        await pilot.pause()
        app._handle_server_event({
            "type": "message", "from": "coder",
            "content": "on it — writing tests now", "model": "sonnet",
        })
        await pilot.pause()

        # 7. run_complete ─────────────────────────────────────────
        app._handle_server_event({
            "type": "run_complete",
            "agent": "coordinator",
            "elapsed": 12.5,
            "result": "All checks green.",
        })
        await pilot.pause()
        assert app.live_text == "done"

        # Sanity: every core widget is still mounted and renderable.
        strip = app.query_one(StatusStrip)
        rail = app.query_one(ContextRail)
        stream = app.query_one(ReplStream)
        # These calls must not raise a MarkupError or similar.
        _render_plain(strip)
        _render_plain(rail)
        # Stream has child turns.
        assert len(list(stream.children)) >= 1


# ── Test: composer send path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_composer_send_posts_to_inject_endpoint():
    """Ctrl+Enter in the composer must POST to ``/runs/inject``."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        # Swap out the real aiohttp session for a recorder.
        real = app._http_session
        fake = _FakeSession()
        app._http_session = fake
        if real is not None:
            await real.close()

        app.session_id = "sid-1"
        await pilot.pause()

        ta = app.query_one("#query-input")
        ta.text = "please implement foo"
        await pilot.pause()

        await pilot.press("ctrl+enter")
        await pilot.pause()

        # Exactly one POST to the inject endpoint with the expected body.
        inject_posts = [
            p for p in fake.posts if p["url"].endswith("/sessions/sid-1/runs/inject")
        ]
        assert len(inject_posts) == 1, f"expected 1 inject POST, got: {fake.posts}"
        post = inject_posts[0]
        assert post["url"] == "http://127.0.0.1:1337/api/v1/sessions/sid-1/runs/inject"
        assert post["json"] == {"to": "coordinator", "message": "please implement foo"}

        # Composer should have been cleared.
        assert (ta.text or "").strip() == ""


# ── Test: malformed / empty payloads must not crash ──────────────────


@pytest.mark.asyncio
async def test_missing_and_unexpected_fields_do_not_crash():
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        # Totally empty envelope.
        app._handle_server_event({})
        await pilot.pause()

        # Unknown type.
        app._handle_server_event({"type": "not_a_real_type"})
        await pilot.pause()

        # init with no agents / no plan / no stats.
        app._handle_server_event({"type": "init"})
        await pilot.pause()

        # init with agents that have blank ids — must be filtered.
        app._handle_server_event({
            "type": "init",
            "agents": [
                {"id": "", "role": "coder"},
                {"id": None, "role": "reviewer"},
                {"id": "coder", "role": "coder"},
            ],
        })
        await pilot.pause()
        assert app.agent_order == ["coder"]

        # file_write with no path — must not record a change and not
        # raise even though some fields are missing.
        app._handle_server_event({"type": "file_write", "agent": "coder"})
        await pilot.pause()
        assert app.file_changes == {}

        # agent_activity with nothing.
        app._handle_server_event({"type": "agent_activity"})
        await pilot.pause()

        # message with only a raw string in content — markup braces
        # in user content must not leak into Textual's parser and
        # crash the render (Static.update with markup=True is the
        # risk; current code uses plain strings in turn bodies, but
        # if that ever changes this test will catch it).
        app._handle_server_event({
            "type": "message",
            "from": "user",
            "content": "[not a tag] <<brackets>> & ampersand",
        })
        await pilot.pause()

        # plan_step with no title should still append a default entry.
        app._handle_server_event({"type": "plan_step"})
        await pilot.pause()
        assert ("now", "Planning update") in app.plan_items

        # stats-only update.
        app._handle_server_event({
            "type": "stats", "message_count": 7, "elapsed": 3.2,
        })
        await pilot.pause()
        assert app.message_count == 7


# ── Test: widget render contents ─────────────────────────────────────


@pytest.mark.asyncio
async def test_context_rail_renders_agent_labels():
    """After init, the ContextRail must show agent labels and headings."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        app._handle_server_event(_init_payload())
        await pilot.pause()

        # Also fire a file_write so CHANGES section has content.
        app._handle_server_event({
            "type": "file_write",
            "agent": "coder",
            "path": "src/orb/rail_probe.py",
            "old_content": "",
            "content": "print('x')\n",
        })
        await pilot.pause()

        rail = app.query_one(ContextRail)
        rail_text = _render_plain(rail)

        # Section headings.
        assert "AGENTS" in rail_text
        assert "PLAN" in rail_text
        assert "CHANGES" in rail_text

        # Agent labels (human-readable forms).
        assert "Coordinator" in rail_text
        assert "Coder" in rail_text
        assert "Reviewer" in rail_text
        assert "Tester" in rail_text

        # File path (possibly truncated with a leading ellipsis).
        assert "rail_probe.py" in rail_text

        # Status strip must render too and mention ORB + topology.
        strip_text = _render_plain(app.query_one(StatusStrip))
        assert "ORB" in strip_text
        assert "triad" in strip_text
