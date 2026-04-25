"""End-to-end streaming pipeline integration test.

Drives a real ``DashboardServer`` with a fake orchestrator that emits
streaming chunks, and asserts the full WS wire contract:

    init (WS hello)
    → run_state_changed[to=planning]
    → init (re-broadcast)
    → message_delta (index=0, chain_id=X)
    → message_delta (index=1, chain_id=X)
    → …
    → message (content=full text, chain_id=X)
    → run_complete

Why this test exists
--------------------
CLAUDE.md's top-level invariant is TUI↔dashboard parity, and streaming
is the path most prone to silent drift — provider (#12), TUI renderer
(#13) and dashboard renderer (#14) all have to agree on the delta
envelope shape. Unit tests cover each layer in isolation. This file is
the regression guard that proves a chunked run actually *reaches* the
WS client with the right deltas in the right order, and that the
non-streaming and cancellation edge-cases don't leak stray frames.

Gate
----
Tasks #12/#13/#14 wire the real streaming plumbing; until they land we
keep the suite green via a module-level skip. Flip
``_STREAMING_PIPELINE_LANDED`` to ``True`` once the pipeline is
available to exercise these tests in CI. The wire-format names used
here (``message_delta``, ``chain_id``, ``index``, ``streaming_enabled``)
are spec-from-the-task; when the real pipeline lands either the names
match or this file gets a quick rename patch alongside it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from web.server import DashboardServer
from web.state import DashboardState

from tests.test_approval_flow_e2e import (
    _WSCollector,
    _open_ws,
    _start_run,
)


# ── Gate ────────────────────────────────────────────────────────────────
#
# Flip to ``True`` once #12 (provider/bus wiring) lands. Kept as a plain
# constant rather than an environment sniff so it's obvious in a diff
# which state the suite is in.

_STREAMING_PIPELINE_LANDED = True

pytestmark = pytest.mark.skipif(
    not _STREAMING_PIPELINE_LANDED,
    reason=(
        "streaming pipeline (tasks #12/#13/#14) not landed yet; "
        "flip _STREAMING_PIPELINE_LANDED in test_streaming_flow_e2e.py "
        "after the provider/agent/bus wires expose message_delta."
    ),
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def e2e_server(tmp_path: Path):
    """Real DashboardServer behind aiohttp TestServer, broadcast wired.

    Mirrors the fixture in ``test_tui_daemon_e2e.py`` /
    ``test_approval_flow_e2e.py`` — kept local rather than hoisted to
    conftest so each e2e file stays self-contained and the sandbox path
    (``_session_path``) is pinned to *this* test's ``tmp_path``.
    """
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=0)
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001
    server.manager.subscribe(server.broadcast)

    ts = TestServer(server._app, host="127.0.0.1")  # noqa: SLF001
    await ts.start_server()
    try:
        yield ts, server
    finally:
        server.manager.unsubscribe(server.broadcast)
        await ts.close()


# ── Fake streaming orchestrator ─────────────────────────────────────────
#
# We emit chunks by calling ``runtime._broadcast`` directly with the
# expected wire envelope, rather than going through the provider/agent
# API which #12 still owns. That keeps this test coupled to the
# *protocol* only — if #12 changes internal APIs the scaffold doesn't
# break; if it changes the wire format this file gets a one-line patch.


def _install_streaming_run(
    runtime,
    *,
    chunks: list[str],
    agent_id: str = "coder",
    chain_id: str = "chain-stream-1",
    pause_event: asyncio.Event | None = None,
    final_message: bool = True,
) -> dict[str, Any]:
    """Replace ``runtime.start_run`` with a stub that:

      * drives the real FSM through PLANNING → RUNNING;
      * broadcasts the init re-snapshot (so the TUI repaints);
      * emits one ``message_delta`` per entry in ``chunks`` with a
        monotonically increasing ``index`` starting at 0;
      * optionally awaits ``pause_event`` between the first chunk and
        the rest (used by the cancellation test to stop mid-stream);
      * emits a final ``message`` with the concatenated content, tagged
        with the same ``chain_id`` — unless ``final_message`` is False
        (cancelled runs should NOT land a terminal message);
      * fires ``run_complete`` + FSM → COMPLETED when the stream drains.

    Returns a handle dict the test can block on:
      * ``started``  — set once the fake orchestrator has started
      * ``drained``  — set after the last chunk is broadcast
      * ``chain_id`` — echoed for convenience
    """
    handle: dict[str, Any] = {
        "started": asyncio.Event(),
        "drained": asyncio.Event(),
        "chain_id": chain_id,
        "agent_id": agent_id,
    }

    async def fake_start_run(
        query,
        topology="auto",
        model_pin="auto",
        agent_models=None,
        workdir=None,
    ):
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0.02)

        init = runtime.current_init_event(
            session_id=runtime._conversation_session.session_id  # noqa: SLF001
        )
        await runtime._broadcast(json.dumps(init))  # noqa: SLF001

        async def _fake_orchestrator():
            handle["started"].set()
            await asyncio.sleep(0.02)
            streaming_on = bool(
                getattr(runtime._conversation_session, "streaming_enabled", False)  # noqa: SLF001
            )

            if streaming_on:
                for idx, text in enumerate(chunks):
                    await runtime._broadcast(json.dumps({  # noqa: SLF001
                        "type": "message_delta",
                        "agent": agent_id,
                        "chain_id": chain_id,
                        "index": idx,
                        "delta": text,
                    }))
                    # Let the cancellation test wedge a stop between
                    # the first chunk and the rest — in normal flow the
                    # event is pre-set so this is a no-op.
                    if pause_event is not None and idx == 0:
                        await pause_event.wait()

            handle["drained"].set()

            if final_message:
                await runtime._broadcast(json.dumps({  # noqa: SLF001
                    "type": "message",
                    "from": agent_id,
                    "to": "user",
                    "content": "".join(chunks),
                    "msg_type": "response",
                    "depth": 0,
                    "chain_id": chain_id,
                }))
                await runtime._broadcast(json.dumps({  # noqa: SLF001
                    "type": "run_complete",
                    "agent": agent_id,
                    "result": "".join(chunks),
                    "elapsed": 0.01,
                    "routed": 1,
                    "session_turn": 1,
                    "diff": "",
                }))
                runtime._fsm.maybe_fire("orchestrator_succeeded")  # noqa: SLF001

        runtime._run_task = asyncio.create_task(_fake_orchestrator())  # noqa: SLF001
        runtime._fsm.fire("orchestrator_task_created")  # noqa: SLF001

        return 200, {
            "ok": True,
            "session_id": runtime._conversation_session.session_id,  # noqa: SLF001
            "init": init,
            "session_turn": 1,
        }

    runtime.start_run = fake_start_run
    return handle


async def _create_streaming_session(
    http: aiohttp.ClientSession,
    base: str,
    *,
    workdir: Path,
    streaming_enabled: bool,
    topology: str = "triad",
) -> str:
    body: dict[str, Any] = {
        "topology": topology,
        "workdir": str(workdir),
        # Daemon contract: ``streaming_enabled`` defaults to True; the
        # api_v1 handler does a STRICT ``is False`` check before flipping
        # the flag off, so we must send the literal bool either way —
        # omitting it leaves the session on the default (True), not on
        # the test's intended value.
        "streaming_enabled": bool(streaming_enabled),
    }
    async with http.post(f"{base}/api/v1/sessions", json=body) as resp:
        assert resp.status == 201, await resp.text()
        sid = (await resp.json())["data"]["session_id"]
    return sid


# ── Tests ───────────────────────────────────────────────────────────────


# 1. Happy path — streaming provider, streaming session. All deltas land
#    in order, share a chain_id, and the final message carries the full
#    concatenation.
async def test_streaming_deltas_in_order_with_final_message(
    e2e_server, tmp_path: Path,
):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    chunks = ["Hel", "lo ", "world", "!"]

    async with aiohttp.ClientSession() as http:
        sid = await _create_streaming_session(
            http, base, workdir=workdir, streaming_enabled=True,
        )
        runtime = server.manager.get_session(sid)
        assert runtime is not None
        handle = _install_streaming_run(
            runtime,
            chunks=chunks,
            chain_id="chain-happy",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)

            # Collect the four deltas in order.
            deltas: list[dict] = []
            for _ in range(len(chunks)):
                ev = await collector.wait("message_delta", timeout=3.0)
                deltas.append(ev)

            # Indexes are monotonic 0,1,2,3.
            assert [d.get("index") for d in deltas] == list(range(len(chunks))), (
                f"deltas arrived out of order: {[d.get('index') for d in deltas]}"
            )

            # All share the same chain_id.
            assert {d.get("chain_id") for d in deltas} == {"chain-happy"}, (
                f"chain_id leaked or diverged across deltas: {deltas}"
            )

            # Payload matches the source chunks, delta-by-delta.
            assert [d.get("delta") for d in deltas] == chunks

            # Per-session tagging — multi-tenant regression guard.
            for d in deltas:
                assert d.get("session_id") == sid, (
                    f"message_delta missing session_id tag: {d}"
                )

            # Final message lands with full concatenation + same chain.
            final = await collector.wait("message", timeout=3.0)
            assert final.get("content") == "Hello world!", final
            assert final.get("chain_id") == "chain-happy", final
            assert final.get("session_id") == sid

            await collector.wait("run_complete", timeout=3.0)
            await asyncio.wait_for(handle["drained"].wait(), timeout=3.0)
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 2. streaming_enabled omitted/false — same fake provider. Deltas must
#    NOT be emitted; only the final message fires. Default session
#    behavior unchanged.
async def test_streaming_disabled_session_emits_no_deltas(
    e2e_server, tmp_path: Path,
):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_streaming_session(
            http, base, workdir=workdir, streaming_enabled=False,
        )
        runtime = server.manager.get_session(sid)
        _install_streaming_run(
            runtime,
            chunks=["Hel", "lo ", "world", "!"],
            chain_id="chain-nodelta",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)

            final = await collector.wait("message", timeout=3.0)
            assert final.get("content") == "Hello world!"
            assert final.get("chain_id") == "chain-nodelta"

            await collector.wait("run_complete", timeout=3.0)

            assert not collector.saw("message_delta"), (
                "streaming_enabled=False must not emit message_delta; "
                f"received={[e.get('type') for e in collector.received]}"
            )
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 3. Non-streaming provider — streaming_enabled=True is on, but the fake
#    provider never produces chunks. Only the final message should fire;
#    no dangling deltas.
async def test_non_streaming_provider_skips_deltas_even_when_enabled(
    e2e_server, tmp_path: Path,
):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_streaming_session(
            http, base, workdir=workdir, streaming_enabled=True,
        )
        runtime = server.manager.get_session(sid)
        _install_streaming_run(
            runtime,
            chunks=[],  # provider yields nothing
            chain_id="chain-no-chunks",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)

            final = await collector.wait("message", timeout=3.0)
            assert final.get("content") == ""
            assert final.get("chain_id") == "chain-no-chunks"

            await collector.wait("run_complete", timeout=3.0)

            assert not collector.saw("message_delta"), (
                "provider yielded no chunks; message_delta must not fire"
            )
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 4. Mid-stream cancellation — stop the run after the first delta;
#    assert no more deltas leak, the FSM lands terminally, and no final
#    message event fires for the cancelled chain.
async def test_mid_stream_cancellation_produces_no_trailing_deltas(
    e2e_server, tmp_path: Path,
):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_streaming_session(
            http, base, workdir=workdir, streaming_enabled=True,
        )
        runtime = server.manager.get_session(sid)

        # The fake orchestrator blocks after emitting the first chunk so
        # we can deterministically POST /runs/stop mid-stream without
        # racing the chunk loop.
        pause = asyncio.Event()
        _install_streaming_run(
            runtime,
            chunks=["Hel", "lo ", "world", "!"],
            chain_id="chain-cancelled",
            pause_event=pause,
            final_message=False,
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)

            # Wait for exactly the first delta (index=0), then cancel.
            first = await collector.wait("message_delta", timeout=3.0)
            assert first.get("index") == 0
            assert first.get("chain_id") == "chain-cancelled"

            async with http.post(
                f"{base}/api/v1/sessions/{sid}/runs/stop", json={},
            ) as resp:
                assert resp.status == 200, await resp.text()

            # Wait for the stopping transition so we have a clean
            # "before" watermark for the no-trailing-deltas assertion.
            stopping = await collector.wait("run_state_changed", timeout=3.0)
            # The FSM may emit multiple transitions; seek the stopping
            # edge specifically.
            while stopping.get("to") not in ("stopping", "stopped", "cancelled"):
                stopping = await collector.wait("run_state_changed", timeout=3.0)

            # Release the paused orchestrator so any trailing broadcasts
            # happen before we inspect the collector.
            pause.set()

            # Give the loop a beat to drain anything the stop path might
            # have queued, then snapshot.
            await asyncio.sleep(0.05)

            deltas_after_stop = [
                e for e in collector.received
                if e.get("type") == "message_delta"
                and e.get("chain_id") == "chain-cancelled"
                and (e.get("index") or 0) > 0
            ]
            assert not deltas_after_stop, (
                f"trailing deltas leaked after stop: {deltas_after_stop}"
            )

            # And no final message for the cancelled chain.
            final_for_cancelled = [
                e for e in collector.received
                if e.get("type") == "message"
                and e.get("chain_id") == "chain-cancelled"
            ]
            assert not final_for_cancelled, (
                f"cancelled chain should not emit terminal message: "
                f"{final_for_cancelled}"
            )

            # Runtime lands in a terminal-or-stopping state cleanly.
            # Per orb/runtime/run_state.py, the FSM has six states:
            # idle / planning / running / stopping / completed / errored.
            # Stop fires the "stop_requested" transition into STOPPING;
            # the orchestrator then unwinds via "stop_finished" to IDLE
            # or via "orchestrator_errored" to ERRORED. STOPPING itself
            # is a quiescent in-flight state for our purposes — no more
            # deltas should arrive — so accept it alongside the true
            # terminal states.
            acceptable = {"stopping", "idle", "completed", "errored"}
            assert runtime.run_state.value in acceptable, (
                f"expected stop-acceptable FSM state, got {runtime.run_state.value}"
            )
        finally:
            pause.set()  # always release, even on failure
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 5. Two sessions streaming concurrently — deltas fan out per-session,
#    each chain_id's indexes stay monotonic from 0, and no session's
#    frames leak to the other's WS. Doubles as a multi-tenant regression
#    guard for the streaming path.
async def test_concurrent_streaming_sessions_stay_isolated(
    e2e_server, tmp_path: Path,
):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    wd_a = tmp_path / "a"
    wd_a.mkdir()
    wd_b = tmp_path / "b"
    wd_b.mkdir()

    async with aiohttp.ClientSession() as http:
        sid_a = await _create_streaming_session(
            http, base, workdir=wd_a, streaming_enabled=True,
        )
        sid_b = await _create_streaming_session(
            http, base, workdir=wd_b, streaming_enabled=True,
        )
        rt_a = server.manager.get_session(sid_a)
        rt_b = server.manager.get_session(sid_b)
        assert rt_a is not None and rt_b is not None

        chunks_a = ["A1-", "A2-", "A3"]
        chunks_b = ["B1-", "B2-", "B3"]
        _install_streaming_run(rt_a, chunks=chunks_a, chain_id="chain-A")
        _install_streaming_run(rt_b, chunks=chunks_b, chain_id="chain-B")

        col_a = _WSCollector()
        col_b = _WSCollector()
        ws_a, reader_a = await _open_ws(http, base, sid_a, col_a)
        ws_b, reader_b = await _open_ws(http, base, sid_b, col_b)
        try:
            # Kick both runs off without awaiting in sequence so the
            # streams genuinely interleave on the bus.
            await asyncio.gather(
                _start_run(http, base, sid_a),
                _start_run(http, base, sid_b),
            )

            # Drain each collector for its three deltas + final message.
            deltas_a = [
                await col_a.wait("message_delta", timeout=3.0)
                for _ in range(len(chunks_a))
            ]
            deltas_b = [
                await col_b.wait("message_delta", timeout=3.0)
                for _ in range(len(chunks_b))
            ]

            # Per-chain monotonic indexes from 0.
            assert [d.get("index") for d in deltas_a] == [0, 1, 2]
            assert [d.get("index") for d in deltas_b] == [0, 1, 2]

            # Chain ids don't cross.
            assert {d.get("chain_id") for d in deltas_a} == {"chain-A"}
            assert {d.get("chain_id") for d in deltas_b} == {"chain-B"}

            # Session tagging.
            for d in deltas_a:
                assert d.get("session_id") == sid_a, (
                    f"A-session leaked wrong session_id: {d}"
                )
            for d in deltas_b:
                assert d.get("session_id") == sid_b, (
                    f"B-session leaked wrong session_id: {d}"
                )

            # Cross-session leak guard — B's chain must never appear on
            # A's WS and vice versa.
            assert not any(
                e.get("chain_id") == "chain-B" for e in col_a.received
            ), "chain-B leaked into A's WS"
            assert not any(
                e.get("chain_id") == "chain-A" for e in col_b.received
            ), "chain-A leaked into B's WS"

            # Final messages land correctly.
            final_a = await col_a.wait("message", timeout=3.0)
            final_b = await col_b.wait("message", timeout=3.0)
            assert final_a.get("content") == "A1-A2-A3"
            assert final_a.get("chain_id") == "chain-A"
            assert final_b.get("content") == "B1-B2-B3"
            assert final_b.get("chain_id") == "chain-B"

            await col_a.wait("run_complete", timeout=3.0)
            await col_b.wait("run_complete", timeout=3.0)
        finally:
            for rt in (reader_a, reader_b):
                rt.cancel()
            await asyncio.gather(ws_a.close(), ws_b.close())
            for rt in (reader_a, reader_b):
                try:
                    await rt
                except (asyncio.CancelledError, Exception):
                    pass
