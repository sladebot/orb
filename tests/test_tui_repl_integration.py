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

from orb.cli.tui_repl import (
    ContextRail,
    LiveStatusBar,
    Milestone,
    OrbReplTUI,
    ReplStream,
    SlashPalette,
    StatusStrip,
    ToolBlock,
    Turn,
)


# ── Fake aiohttp session for composer-send tests ──────────────────────


class _FakeResp:
    def __init__(self, status: int = 200, text: str = "") -> None:
        self.status = status
        self._text = text
        self._data = text.encode() if text else b""

    async def read(self) -> bytes:
        return self._data

    async def text(self) -> str:
        return self._text

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
async def test_composer_send_injects_when_run_is_in_flight():
    """Ctrl+Enter while a run is running must POST to ``/runs/inject``."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        fake = _FakeSession()
        app._http_session = fake
        if real is not None:
            await real.close()

        app.session_id = "sid-1"
        # Active run — submit should inject, not start.
        app.run_state = "running"
        await pilot.pause()

        ta = app.query_one("#query-input")
        ta.text = "please implement foo"
        await pilot.pause()

        await pilot.press("ctrl+enter")
        await pilot.pause()

        inject_posts = [
            p for p in fake.posts if p["url"].endswith("/sessions/sid-1/runs/inject")
        ]
        assert len(inject_posts) == 1, f"expected 1 inject POST, got: {fake.posts}"
        post = inject_posts[0]
        assert post["url"] == "http://127.0.0.1:1337/api/v1/sessions/sid-1/runs/inject"
        assert post["json"] == {"to": "coordinator", "message": "please implement foo"}
        assert (ta.text or "").strip() == ""


@pytest.mark.asyncio
async def test_composer_send_starts_run_when_session_is_idle():
    """Ctrl+Enter on an idle session must POST to ``/runs`` to start one.

    Inject only works while a run is in flight. Firing inject on an
    idle session 409s and the TUI freezes with nothing happening — which
    is exactly the bug the user caught after typing their first query.
    """
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337, topology="solo")
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        fake = _FakeSession()
        app._http_session = fake
        if real is not None:
            await real.close()

        app.session_id = "sid-2"
        app.run_state = "idle"
        await pilot.pause()

        ta = app.query_one("#query-input")
        ta.text = "Explain this repo"
        await pilot.press("ctrl+enter")
        await pilot.pause()

        # Exactly one POST to /runs, none to /runs/inject.
        start_posts = [p for p in fake.posts if p["url"].endswith("/sessions/sid-2/runs")]
        inject_posts = [p for p in fake.posts if p["url"].endswith("/sessions/sid-2/runs/inject")]
        assert len(start_posts) == 1, f"expected 1 start POST, got: {fake.posts}"
        assert inject_posts == [], f"inject must not fire on idle, got: {inject_posts}"
        post = start_posts[0]
        assert post["json"]["query"] == "Explain this repo"
        assert post["json"].get("topology") == "solo"
        # Optimistic local state flip so a rapid second Enter injects.
        assert app.run_state in {"planning", "running"}


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


# ── v2 Turn widget: two-column lane layout ───────────────────────────


@pytest.mark.asyncio
async def test_turn_widget_renders_vertical_lane_with_separator():
    """v2 Turn: speaker/ts/model stacked in a left lane, body to the right
    with an agent-colored ``│`` separator on every line."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        turn = Turn("coder", elapsed=845.0, body="Reading /shorten handler…\nplan: land patch", model="haiku-4-5")
        app.query_one(ReplStream).mount(turn)
        await pilot.pause()

        plain = _render_plain(turn)
        # Lane cells appear on their own row prefixed with the label.
        assert "Coder" in plain
        assert "0:14:05" in plain  # 845s == 0:14:05
        assert "haiku-4-5" in plain
        # Separator character is present on multiple rows.
        assert plain.count("│") >= 2
        # Body text appears.
        assert "Reading /shorten handler" in plain


# ── v2 Slash palette: show/hide as user types ────────────────────────


@pytest.mark.asyncio
async def test_slash_palette_shows_when_user_types_slash():
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        palette = app.query_one(SlashPalette)
        # Hidden by default.
        assert "hidden" in palette.classes

        ta = app.query_one("#query-input")
        ta.text = "/"
        await pilot.pause()
        assert "hidden" not in palette.classes
        rendered = _render_plain(palette)
        # All catalog commands are visible when just "/" is typed.
        assert "/help" in rendered
        assert "/clear" in rendered
        assert "/stop" in rendered

        # Typing a concrete command filters to just that row.
        ta.text = "/help"
        await pilot.pause()
        assert "hidden" not in palette.classes
        rendered2 = _render_plain(palette)
        assert "/help" in rendered2
        # Other commands should NOT be visible under exact-match filter.
        assert "/clear" not in rendered2
        assert "/topology" not in rendered2

        # Clearing the composer hides the palette again.
        ta.text = ""
        await pilot.pause()
        assert "hidden" in palette.classes


# ── v2 File-write → ToolBlock with agent border + accept bar ─────────


@pytest.mark.asyncio
async def test_file_write_emits_block_with_border_and_accept_bar():
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        app._handle_server_event(_init_payload())
        await pilot.pause()

        app._handle_server_event({
            "type": "file_write",
            "agent": "coder",
            "path": "src/app.py",
            "old_content": "a\n",
            "content": "a\nb\nc\n",
        })
        await pilot.pause()

        blocks = list(app.query(ToolBlock))
        assert blocks, "expected at least one ToolBlock mounted"
        block = blocks[-1]
        rendered = _render_plain(block)

        # Agent-colored thick vertical bar prefixes each rendered line.
        assert "┃" in rendered
        # Accept bar footer — all four options must be present.
        assert "accept" in rendered
        assert "accept all" in rendered
        assert "edit" in rendered
        assert "reject" in rendered
        # Block header is preserved.
        assert "edit_file" in rendered
        assert "src/app.py" in rendered


# ── v2 Milestone before plan step ────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_step_emits_milestone_before_turn():
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        app._handle_server_event(_init_payload())
        await pilot.pause()

        stream = app.query_one(ReplStream)
        # Snapshot children before firing plan_step.
        before = list(stream.children)

        app._handle_server_event({
            "type": "plan_step",
            "title": "apply rate-limit",
            "detail": "wrap /shorten with slowapi limiter",
        })
        await pilot.pause()

        # A Milestone widget was appended as part of this plan_step.
        milestones = list(app.query(Milestone))
        assert milestones, "expected a Milestone widget from plan_step"
        latest = milestones[-1]
        text = _render_plain(latest)
        assert "step 1" in text
        # Label from detail preferred over title.
        assert "wrap /shorten" in text

        # Milestone appeared in the stream (ordering sanity — it shows
        # up after the pre-existing widgets).
        after = list(stream.children)
        assert len(after) > len(before)


# ── v2 Live status bar ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_status_bar_updates_on_agent_activity():
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        app._handle_server_event(_init_payload())
        await pilot.pause()

        app._handle_server_event({
            "type": "agent_activity",
            "agent": "coder",
            "activity": "editing src/app.py",
        })
        await pilot.pause()

        bar = app.query_one(LiveStatusBar)
        text = _render_plain(bar)
        # Pill shows agent label and activity.
        assert "Coder" in text
        assert "editing src/app.py" in text
        # Elapsed ``Ns`` pill is present.
        assert "s" in text


# ── v2 Topology lock: /topology aware of session pin ──────────────────
#
# After the first run the server pins the session's topology (see
# ``GraphRuntime._start_run_planning``). Follow-up ``/topology`` requests
# from the TUI against a locked session silently no-op on the server side
# — the user has no idea. The TUI now surfaces the lock: the init event
# carries ``session.locked_topology`` which the TUI tracks, and the
# ``/topology <id>`` slash command refuses + whispers when the argument
# doesn't match the lock.


def _render_stream_plain(app: OrbReplTUI) -> str:
    """Flatten every ``Turn`` body in the stream into a searchable string."""
    stream = app.query_one(ReplStream)
    parts: list[str] = []
    for turn in stream.query(Turn):
        parts.append(_render_plain(turn))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_init_hydrates_locked_topology_from_session_block():
    """The TUI stores ``locked_topology`` from the init's session block."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        payload = _init_payload()
        payload["session"] = {
            "id": "sid-1",
            "locked_topology": "triad",
            "locked_agent_models": {"coordinator": "opus", "coder": "sonnet"},
            "locked_model_pin": "auto",
            "workdir": "/tmp/project",
        }
        app._handle_server_event(payload)
        await pilot.pause()

        assert app.locked_topology == "triad"
        assert app.locked_agent_models == {"coordinator": "opus", "coder": "sonnet"}


@pytest.mark.asyncio
async def test_slash_topology_whispers_when_session_is_pinned():
    """/topology <mismatch> must refuse + whisper when the session is locked."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337, topology="auto")
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        # Lock the session to triad via the init payload.
        payload = _init_payload()
        payload["session"] = {
            "id": "sid-1",
            "locked_topology": "triad",
            "locked_agent_models": {},
            "locked_model_pin": "auto",
            "workdir": "/tmp/project",
        }
        app._handle_server_event(payload)
        await pilot.pause()

        # Try to switch — must refuse.
        topology_before = app._topology
        await app._run_slash_command("/topology solo")
        await pilot.pause()

        # Internal setting must not change.
        assert app._topology == topology_before
        # Whisper/error must surface the lock + suggest /new.
        text = _render_stream_plain(app)
        assert "pinned" in text.lower() or "locked" in text.lower()
        assert "triad" in text


@pytest.mark.asyncio
async def test_slash_topology_matching_lock_is_accepted():
    """/topology <same-as-lock> is a no-op on the server but must not error."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337, topology="auto")
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        payload = _init_payload()
        payload["session"] = {
            "id": "sid-1",
            "locked_topology": "triad",
            "locked_agent_models": {},
            "locked_model_pin": "auto",
            "workdir": "/tmp/project",
        }
        app._handle_server_event(payload)
        await pilot.pause()

        await app._run_slash_command("/topology triad")
        await pilot.pause()

        # Setting accepted (value unchanged since it matched the lock).
        assert app._topology == "triad"
        text = _render_stream_plain(app)
        # No "pinned" refusal — but a confirmation is fine.
        assert "pinned" not in text.lower() or "already" in text.lower() or "matches" in text.lower()


@pytest.mark.asyncio
async def test_run_complete_refreshes_locked_topology():
    """run_complete carries ``locked_topology`` so late-joining clients can
    refresh their lock view without waiting for the next run's init."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337)
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        # Start unlocked.
        app._handle_server_event(_init_payload())
        await pilot.pause()
        assert app.locked_topology == ""

        # run_complete carries the lock; TUI should pick it up.
        app._handle_server_event({
            "type": "run_complete",
            "agent": "coordinator",
            "elapsed": 2.0,
            "result": "done",
            "locked_topology": "triad",
            "locked_agent_models": {"coordinator": "opus"},
        })
        await pilot.pause()
        assert app.locked_topology == "triad"


@pytest.mark.asyncio
async def test_slash_topology_works_when_unlocked():
    """Without a lock, /topology continues to mutate ``_topology`` as before."""
    app = OrbReplTUI(server_host="127.0.0.1", server_port=1337, topology="auto")
    async with app.run_test(size=(140, 44)) as pilot:
        real = app._http_session
        app._http_session = _FakeSession()
        if real is not None:
            await real.close()
        await pilot.pause()

        # Unlocked init — no ``session`` block with locked_topology.
        app._handle_server_event(_init_payload())
        await pilot.pause()
        assert app.locked_topology == ""

        await app._run_slash_command("/topology solo")
        await pilot.pause()

        assert app._topology == "solo"
