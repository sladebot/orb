"""End-to-end TUI↔daemon integration test.

Spins up a real ``DashboardServer`` on an ephemeral 127.0.0.1 port,
attaches a stripped-down WebSocket + HTTP client mirroring what
``orb/cli/tui_repl.py::attach_tui_repl`` does, POSTs a run via
``/api/v1/sessions/{sid}/runs``, and asserts the client sees the full
event sequence in the expected order:

    init (WS hello)
    → run_state_changed[to=planning]
    → init (re-broadcast so the TUI paints the topology)
    → message
    → agent_status
    → complete
    → run_complete

Why this test exists
--------------------
CLAUDE.md's top-level invariant is TUI↔dashboard parity. Unit tests
elsewhere cover each layer in isolation (FSM transitions, HTTP
envelope, bridge event shapes, TUI handlers against a
``__new__``-constructed app). None of them prove that a ``POST /runs``
actually *reaches* the WS client with the right payloads in the right
order — silent drift between any pair of those layers would go
undetected until a user runs the TUI.

To stay deterministic and provider-free, ``runtime.start_run`` is
replaced on the per-session instance with a stub that drives the real
FSM and broadcasts real events; the only thing mocked is the
orchestrator's LLM work.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from web.server import DashboardServer
from web.state import DashboardState


# ── Fake orchestrator ───────────────────────────────────────────────────


def _install_fake_start_run(runtime) -> None:
    """Replace ``runtime.start_run`` with a stub that drives the real FSM
    and broadcasts a realistic run lifecycle without calling providers.

    The FSM listener (``_on_fsm_state_changed``) is what actually emits
    ``run_state_changed`` — we only fire transitions. Every broadcast
    goes through the real ``_broadcast`` + manager fan-out, so session
    tagging, JSON serialization, and WebSocket delivery are exercised
    just like the production path.
    """

    async def fake_start_run(
        query,
        topology="auto",
        model_pin="auto",
        agent_models=None,
        workdir=None,
    ):
        # IDLE → PLANNING. Emits run_state_changed via the FSM listener,
        # which uses loop.create_task to broadcast — so we yield after
        # to let that task's whole chain (manager → server → ws.send_str)
        # drain before emitting the next event. A bare ``asyncio.sleep(0)``
        # only yields once; the broadcast chain involves several awaits,
        # hence the tiny quantum here. This is the same ordering
        # guarantee ``_start_run_planning`` gets implicitly because the
        # real orchestrator doesn't race its own FSM transitions.
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0.02)

        # Re-broadcast the init snapshot so the TUI repaints the
        # topology — mirrors _start_run_planning (graph_runtime.py:1658).
        init = runtime.current_init_event(
            session_id=runtime._conversation_session.session_id  # noqa: SLF001
        )
        await runtime._broadcast(json.dumps(init))  # noqa: SLF001

        async def _fake_orchestrator():
            # Small yield so the POST reply and the preceding broadcasts
            # (init + PLANNING→RUNNING) drain before the run-event storm.
            await asyncio.sleep(0.02)
            await runtime._broadcast(json.dumps({  # noqa: SLF001
                "type": "message",
                "from": "coordinator",
                "to": "coder",
                "content": "please implement widget",
                "msg_type": "task",
                "depth": 0,
                "chain_id": "chain-e2e-1",
            }))
            await runtime._broadcast(json.dumps({  # noqa: SLF001
                "type": "agent_status",
                "agent": "coder",
                "status": "running",
                "model": "claude-haiku-4-5-20251001",
            }))
            await runtime._broadcast(json.dumps({  # noqa: SLF001
                "type": "complete",
                "agent": "coder",
                "result": "all done",
                "is_consensus": False,
            }))
            await runtime._broadcast(json.dumps({  # noqa: SLF001
                "type": "run_complete",
                "agent": "coder",
                "result": "all done",
                "elapsed": 0.01,
                "routed": 1,
                "session_turn": 1,
                "diff": "",
            }))
            # RUNNING → COMPLETED
            runtime._fsm.maybe_fire("orchestrator_succeeded")  # noqa: SLF001

        runtime._run_task = asyncio.create_task(_fake_orchestrator())  # noqa: SLF001
        # PLANNING → RUNNING. Emits another run_state_changed.
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001

        return 200, {
            "ok": True,
            "session_id": runtime._conversation_session.session_id,  # noqa: SLF001
            "init": init,
            "session_turn": 1,
        }

    runtime.start_run = fake_start_run


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def e2e_server(tmp_path: Path):
    """Bring up a real DashboardServer on 127.0.0.1 at an ephemeral port.

    The server's broadcast fan-out is wired up manually (normally done
    by ``DashboardServer.start()``) because TestServer binds the port
    for us — calling ``start()`` would double-bind.
    """
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=0)
    # Keep the default session's snapshot inside tmp so parallel runs
    # don't collide on ~/.orb/daemon/sessions/.../snapshot.json.
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001

    # Wire the WS fan-out: manager → server.broadcast → each connected
    # ws client. DashboardServer.start() does this, but TestServer owns
    # the socket lifecycle so we can't call start().
    server.manager.subscribe(server.broadcast)

    ts = TestServer(server._app, host="127.0.0.1")  # noqa: SLF001
    await ts.start_server()
    try:
        yield ts, server
    finally:
        server.manager.unsubscribe(server.broadcast)
        await ts.close()


# ── Test ────────────────────────────────────────────────────────────────


async def test_full_tui_client_run_flow(e2e_server):
    """Drive a full run through the real server and assert the WS client
    sees the expected event sequence in order with the right payloads.

    Asserts:
      * WS hello delivers an `init` tagged with the session_id.
      * `run_state_changed → planning` fires before any agent activity.
      * An `init` re-broadcast follows the planning transition.
      * `message`, `agent_status`, and `complete` all arrive during the
        running phase, strictly after the planning transition.
      * A PLANNING → RUNNING `run_state_changed` is observed.
      * The terminal `run_complete` carries agent/result/elapsed/session_id.
      * Every per-session event is tagged with the originating session_id
        (regression guard for the multi-tenant broadcast fan-out).
      * The runtime's FSM ends in the COMPLETED state.
    """
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    async with aiohttp.ClientSession() as http:
        # ── Create a prewarmed session (triad so the init lists agents) ──
        async with http.post(
            f"{base}/api/v1/sessions",
            json={"topology": "triad"},
        ) as resp:
            assert resp.status == 201, await resp.text()
            sid = (await resp.json())["data"]["session_id"]

        runtime = server.manager.get_session(sid)
        assert runtime is not None
        _install_fake_start_run(runtime)

        # ── Subscribe via WS (mirrors attach_tui_repl → _start_ws_client) ──
        received: list[dict] = []
        run_complete_evt = asyncio.Event()

        async def _reader(ws):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    ev = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                received.append(ev)
                if ev.get("type") == "run_complete":
                    run_complete_evt.set()

        ws_url = f"{base}/api/v1/ws?session_id={sid}"
        async with http.ws_connect(ws_url, heartbeat=30) as ws:
            reader_task = asyncio.create_task(_reader(ws))

            # Wait for the WS hello `init` so we don't race the POST.
            hello_seen = asyncio.Event()

            async def _wait_for_init():
                while not any(e.get("type") == "init" for e in received):
                    await asyncio.sleep(0.01)
                hello_seen.set()

            await asyncio.wait_for(_wait_for_init(), timeout=2.0)

            # ── Submit the run ──
            async with http.post(
                f"{base}/api/v1/sessions/{sid}/runs",
                json={"query": "do the thing", "topology": "triad"},
            ) as resp:
                assert resp.status == 202, await resp.text()
                env = await resp.json()
                assert env["ok"] is True
                assert env["code"] == "RUN_STARTED"
                assert env["data"]["session_id"] == sid

            # ── Wait for the stub to finish the lifecycle ──
            await asyncio.wait_for(run_complete_evt.wait(), timeout=3.0)
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Assertions ──────────────────────────────────────────────────────

    types = [e.get("type") for e in received]

    # 1. WS hello init exists.
    assert "init" in types, f"no init hello; got types={types}"

    # 2. run_state_changed → planning fires.
    planning_events = [
        e for e in received
        if e.get("type") == "run_state_changed" and e.get("to") == "planning"
    ]
    assert planning_events, (
        f"no run_state_changed→planning; types={types}"
    )

    # 3. init re-broadcast after planning.
    planning_idx = received.index(planning_events[0])
    post_planning = received[planning_idx:]
    post_types = [e.get("type") for e in post_planning]
    assert "init" in post_types, (
        f"no init re-broadcast after planning; post={post_types}"
    )

    # 4. message / agent_status / complete all land during the run.
    assert "message" in post_types, f"no message event; got {post_types}"
    assert "agent_status" in post_types, f"no agent_status; got {post_types}"
    assert "complete" in post_types, f"no complete; got {post_types}"

    # 5. PLANNING → RUNNING observed.
    assert any(
        e.get("type") == "run_state_changed"
        and e.get("from") == "planning"
        and e.get("to") == "running"
        for e in received
    ), f"no PLANNING→RUNNING transition; types={types}"

    # 6. Terminal run_complete carries the expected shape.
    rc = next(e for e in received if e.get("type") == "run_complete")
    assert rc.get("agent") == "coder"
    assert rc.get("result") == "all done"
    assert rc.get("session_id") == sid
    assert "elapsed" in rc
    assert "session_turn" in rc

    # 7. Every per-session payload is tagged — regression for
    # multi-tenant fan-out where untagged events would leak across
    # sessions. RunTime._broadcast() is expected to inject session_id.
    tagged_types = {
        "message", "agent_status", "complete",
        "run_complete", "run_state_changed", "init",
    }
    for ev in received:
        if ev.get("type") in tagged_types:
            assert ev.get("session_id") == sid, (
                f"untagged {ev.get('type')} payload reached the WS client: {ev}"
            )

    # 8. FSM lands in COMPLETED.
    assert runtime.run_state.value == "completed", (
        f"expected FSM=completed, got {runtime.run_state.value}"
    )

    # 9. Strict ordering: planning → init → message/agent_status/complete → run_complete.
    #    The TUI relies on this order to paint the topology before agent
    #    events arrive (otherwise the ContextRail renders placeholder rows).
    order = [e.get("type") for e in received]
    first_planning = next(
        i for i, e in enumerate(received)
        if e.get("type") == "run_state_changed" and e.get("to") == "planning"
    )
    first_init_after = next(
        (i for i, e in enumerate(received[first_planning + 1:], first_planning + 1)
         if e.get("type") == "init"),
        None,
    )
    assert first_init_after is not None, (
        f"no init after planning at idx {first_planning}; order={order}"
    )
    first_message = next(
        (i for i, e in enumerate(received) if e.get("type") == "message"),
        None,
    )
    first_run_complete = next(
        i for i, e in enumerate(received) if e.get("type") == "run_complete"
    )
    assert first_message is not None and first_message > first_init_after, (
        f"message arrived before post-planning init re-broadcast; order={order}"
    )
    assert first_run_complete > first_message, (
        f"run_complete fired before message; order={order}"
    )
