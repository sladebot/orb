"""End-to-end pre-write approval pipeline integration test.

Exercises the full TUI↔daemon path for ``approval_required`` sessions:
the agent attempts a file write, the daemon stages it as
``file_write_pending``, the test client (mirroring the TUI) calls the
approval endpoint, and the daemon either commits to disk + emits
``file_write`` or skips disk + emits ``file_write_rejected``.

Companion to ``tests/test_tui_daemon_e2e.py`` — this file is scoped to
the approval pipeline introduced by tasks #9 (daemon-stager) and
#10 (tui-approver). The shared contract is documented in
``CLAUDE.md`` parity rule territory; this test is the regression guard
for it.

Shared contract under test
--------------------------
- ``POST /api/v1/sessions`` accepts ``"approval_required": true``.
- WS init for an approval-required session carries
  ``"approval_required": true`` in its payload so the TUI can render
  the approval rail on attach.
- When approval is enabled, agent writes broadcast
  ``file_write_pending {agent, request_id, path, content, old_content}``
  instead of ``file_write``.
- ``POST /api/v1/sessions/{sid}/approvals/{request_id}`` body
  ``{action: "approve"|"reject", edited_content?: str, reason?: str}``
  resolves the pending write:
    * 200 ``APPROVAL_RESOLVED`` on success
    * 404 ``APPROVAL_UNKNOWN`` if the request_id is unknown
    * 400 ``INVALID_BODY`` if the action is missing/unparseable
- ``file_write`` only fires after an approve; disk holds the
  (possibly edited) content.
- ``file_write_rejected`` fires on reject or on teardown of an
  in-flight pending write; disk is unchanged.
- Sessions created without ``approval_required`` keep emitting
  ``file_write`` directly with no pending stage (regression guard).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from web.server import DashboardServer
from web.state import DashboardState


# ── Fake agent / orchestrator stub ──────────────────────────────────────
#
# The scaffold installs a ``runtime.start_run`` stub that drives the
# real FSM and then calls ``_drive_one_write`` against whatever entry
# point the daemon-stager (#9) exposes for staged writes. The exact
# call-site is filled in once #9 lands; the structure below isolates
# that single seam so the rest of the test scaffolding stays stable.


def _install_fake_run_with_one_write(
    runtime,
    *,
    agent_id: str = "coder",
    path: str = "hello.txt",
    content: str = "HELLO",
    old_content: str = "",
) -> dict:
    """Replace ``runtime.start_run`` with a stub that drives the real
    FSM through PLANNING→RUNNING, performs exactly one staged file
    write via the agent's approval-aware write callback, and lands the
    FSM in COMPLETED only after the write is resolved (approve or
    reject).

    Returns a handle dict with:
      * ``write_completed`` — asyncio.Event set after the write
        callback returns (i.e. after approve/reject is observed).
      * ``write_request_id`` — populated by the test reader from the
        ``file_write_pending`` event.

    The fake agent does not exist as a real ``LLMAgent`` instance — we
    register the bare minimum the runtime's approval gate expects, then
    invoke the gate directly. Once #9 lands the canonical entry point,
    this helper updates accordingly; no test body should need to
    change.
    """
    handle: dict[str, Any] = {
        "write_completed": asyncio.Event(),
        "write_request_id": None,
        "agent_id": agent_id,
        "path": path,
        "content": content,
        "old_content": old_content,
    }

    async def fake_start_run(
        query,
        topology="auto",
        model_pin="auto",
        agent_models=None,
        workdir=None,
    ):
        # IDLE → PLANNING
        runtime._fsm.fire("start_run_begin")  # noqa: SLF001
        await asyncio.sleep(0.02)

        init = runtime.current_init_event(
            session_id=runtime._conversation_session.session_id  # noqa: SLF001
        )
        await runtime._broadcast(json.dumps(init))  # noqa: SLF001

        async def _fake_orchestrator():
            await asyncio.sleep(0.02)
            # ── Stage one write through the daemon-stager seam ──
            # When approval_required is on, this should broadcast
            # ``file_write_pending`` and block (await) until the user
            # resolves via POST /approvals/{request_id}. When off, it
            # broadcasts ``file_write`` and returns immediately.
            try:
                await _drive_one_write(
                    runtime,
                    agent_id=agent_id,
                    path=path,
                    content=content,
                    old_content=old_content,
                )
            finally:
                handle["write_completed"].set()

            await runtime._broadcast(json.dumps({  # noqa: SLF001
                "type": "run_complete",
                "agent": agent_id,
                "result": "wrote one file",
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


async def _drive_one_write(
    runtime,
    *,
    agent_id: str,
    path: str,
    content: str,
    old_content: str,
) -> None:
    """Drive one file write the way an LLMAgent would.

    Mirrors the contract in ``LLMAgent._handle_write_file``:

      * ``approval_required=False`` — write the sandbox file and
        broadcast ``file_write`` directly. No staging.
      * ``approval_required=True`` — call the runtime's
        ``request_write_approval`` hook, which broadcasts
        ``file_write_pending`` and awaits the user's resolution. On
        approve, the agent (i.e. this stub) writes the *effective*
        content and broadcasts ``file_write``. On reject, the daemon
        already broadcast ``file_write_rejected`` from
        ``resolve_approval`` / ``_reject_all_pending_approvals`` and
        the agent simply skips the write.

    Keeping these side effects here — rather than letting
    ``request_write_approval`` write disk or fire ``file_write`` — is
    deliberate: the production path leaves disk + commit-broadcast in
    the agent so that an edited approval can land different bytes
    without the runtime knowing how the agent's sandbox is laid out.
    """
    cs = runtime._conversation_session  # noqa: SLF001
    workdir = getattr(cs, "workdir", None)

    def _commit(effective: str) -> None:
        if workdir:
            (Path(workdir) / path).write_text(effective)

    if not getattr(cs, "approval_required", False):
        _commit(content)
        await runtime._broadcast(json.dumps({  # noqa: SLF001
            "type": "file_write",
            "agent": agent_id,
            "path": path,
            "content": content,
            "old_content": old_content,
        }))
        return

    approved, effective = await runtime.request_write_approval(
        agent_id=agent_id,
        path=path,
        content=content,
        old_content=old_content,
    )
    if not approved:
        # Daemon already broadcast file_write_rejected; nothing for the
        # agent stub to do.
        return

    _commit(effective)
    await runtime._broadcast(json.dumps({  # noqa: SLF001
        "type": "file_write",
        "agent": agent_id,
        "path": path,
        "content": effective,
        "old_content": old_content,
    }))


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def e2e_server(tmp_path: Path):
    """Same shape as the fixture in ``test_tui_daemon_e2e.py`` — a real
    DashboardServer behind aiohttp's TestServer with the broadcast
    fan-out wired manually (TestServer owns the socket so we cannot
    call ``server.start()``).
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


# ── Helpers ─────────────────────────────────────────────────────────────


class _WSCollector:
    """Drain a WS into a list and surface per-type events as Events.

    Tests assert on ordering with ``asyncio.Event`` rather than sleeps:
    register the type you want to wait for, then ``await
    collector.wait("file_write_pending")``. Subsequent waits return
    the next event of that type to keep the API stateless across
    multi-write sequences.
    """

    def __init__(self) -> None:
        self.received: list[dict] = []
        self._waiters: dict[str, list[asyncio.Future[dict]]] = {}
        self._buffered: dict[str, list[dict]] = {}

    def feed(self, ev: dict) -> None:
        self.received.append(ev)
        t = ev.get("type")
        if not t:
            return
        waiters = self._waiters.get(t)
        if waiters:
            fut = waiters.pop(0)
            if not fut.done():
                fut.set_result(ev)
            return
        self._buffered.setdefault(t, []).append(ev)

    async def wait(self, type_: str, *, timeout: float = 3.0) -> dict:
        buf = self._buffered.get(type_)
        if buf:
            return buf.pop(0)
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._waiters.setdefault(type_, []).append(fut)
        return await asyncio.wait_for(fut, timeout=timeout)

    def saw(self, type_: str) -> bool:
        return any(e.get("type") == type_ for e in self.received)


async def _create_session(
    http: aiohttp.ClientSession,
    base: str,
    *,
    workdir: Path,
    approval_required: bool,
    topology: str = "triad",
) -> str:
    body = {
        "topology": topology,
        "workdir": str(workdir),
    }
    if approval_required:
        body["approval_required"] = True
    async with http.post(f"{base}/api/v1/sessions", json=body) as resp:
        assert resp.status == 201, await resp.text()
        return (await resp.json())["data"]["session_id"]


async def _open_ws(
    http: aiohttp.ClientSession,
    base: str,
    sid: str,
    collector: _WSCollector,
) -> tuple[aiohttp.ClientWebSocketResponse, asyncio.Task]:
    async def _reader(ws):
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                ev = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            collector.feed(ev)

    ws = await http.ws_connect(f"{base}/api/v1/ws?session_id={sid}", heartbeat=30)
    reader_task = asyncio.create_task(_reader(ws))
    # Consume the WS hello init before tests poke the run.
    await collector.wait("init", timeout=2.0)
    return ws, reader_task


async def _start_run(http: aiohttp.ClientSession, base: str, sid: str) -> dict:
    async with http.post(
        f"{base}/api/v1/sessions/{sid}/runs",
        json={"query": "do the thing", "topology": "triad"},
    ) as resp:
        assert resp.status == 202, await resp.text()
        return await resp.json()


async def _post_approval(
    http: aiohttp.ClientSession,
    base: str,
    sid: str,
    request_id: str,
    *,
    action: str,
    edited_content: str | None = None,
    reason: str | None = None,
) -> tuple[int, dict]:
    body: dict[str, Any] = {"action": action}
    if edited_content is not None:
        body["edited_content"] = edited_content
    if reason is not None:
        body["reason"] = reason
    async with http.post(
        f"{base}/api/v1/sessions/{sid}/approvals/{request_id}",
        json=body,
    ) as resp:
        return resp.status, await resp.json()


# ── Tests ───────────────────────────────────────────────────────────────


# 1. Happy path — approve.
async def test_approve_flow_writes_disk(e2e_server, tmp_path: Path):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_session(
            http, base, workdir=workdir, approval_required=True,
        )
        runtime = server.manager.get_session(sid)
        assert runtime is not None
        handle = _install_fake_run_with_one_write(
            runtime, path="hello.txt", content="HELLO",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            # init payload should advertise approval_required=True
            init = next(e for e in collector.received if e.get("type") == "init")
            assert init.get("approval_required") is True, (
                f"WS init missing approval_required flag: {init}"
            )

            await _start_run(http, base, sid)

            pending = await collector.wait("file_write_pending", timeout=3.0)
            assert pending["agent"] == "coder"
            assert pending["path"] == "hello.txt"
            assert pending["content"] == "HELLO"
            assert "request_id" in pending
            request_id = pending["request_id"]

            status, env = await _post_approval(
                http, base, sid, request_id, action="approve",
            )
            assert status == 200, env
            assert env["ok"] is True
            assert env["code"] == "APPROVAL_RESOLVED"

            committed = await collector.wait("file_write", timeout=3.0)
            assert committed["path"] == "hello.txt"
            assert committed["content"] == "HELLO"

            # Disk reflects the approved content.
            disk_path = workdir / "hello.txt"
            assert disk_path.exists(), f"expected disk file at {disk_path}"
            assert disk_path.read_text() == "HELLO"

            await collector.wait("run_complete", timeout=3.0)
            await asyncio.wait_for(handle["write_completed"].wait(), timeout=3.0)
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 2. Edited approve.
async def test_approve_edited_content_overrides_disk(e2e_server, tmp_path: Path):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_session(
            http, base, workdir=workdir, approval_required=True,
        )
        runtime = server.manager.get_session(sid)
        _install_fake_run_with_one_write(
            runtime, path="hello.txt", content="HELLO",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)
            pending = await collector.wait("file_write_pending", timeout=3.0)
            request_id = pending["request_id"]

            status, env = await _post_approval(
                http, base, sid, request_id,
                action="approve", edited_content="EDITED",
            )
            assert status == 200, env
            assert env["code"] == "APPROVAL_RESOLVED"

            committed = await collector.wait("file_write", timeout=3.0)
            assert committed["content"] == "EDITED", (
                f"edited_content not propagated: {committed}"
            )
            assert committed["path"] == "hello.txt"

            disk_path = workdir / "hello.txt"
            assert disk_path.read_text() == "EDITED", (
                "disk should reflect edited_content, not original content"
            )

            await collector.wait("run_complete", timeout=3.0)
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 3. Reject.
async def test_reject_flow_skips_disk(e2e_server, tmp_path: Path):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_session(
            http, base, workdir=workdir, approval_required=True,
        )
        runtime = server.manager.get_session(sid)
        _install_fake_run_with_one_write(
            runtime, path="hello.txt", content="HELLO",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)
            pending = await collector.wait("file_write_pending", timeout=3.0)
            request_id = pending["request_id"]

            status, env = await _post_approval(
                http, base, sid, request_id,
                action="reject", reason="not yet",
            )
            assert status == 200, env
            assert env["code"] == "APPROVAL_RESOLVED"

            rejected = await collector.wait("file_write_rejected", timeout=3.0)
            assert rejected["request_id"] == request_id
            assert rejected["path"] == "hello.txt"
            assert rejected.get("reason"), (
                f"reject envelope missing reason: {rejected}"
            )

            await collector.wait("run_complete", timeout=3.0)

            # No file_write for this request_id.
            assert not any(
                e.get("type") == "file_write"
                and e.get("path") == "hello.txt"
                for e in collector.received
            ), "file_write should not fire for a rejected request"

            # Disk untouched.
            assert not (workdir / "hello.txt").exists(), (
                "rejected write must not commit to disk"
            )
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 4. Teardown auto-reject — pending writes resolve as rejected when the
#    run is stopped without approval.
async def test_stop_run_auto_rejects_pending_write(e2e_server, tmp_path: Path):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_session(
            http, base, workdir=workdir, approval_required=True,
        )
        runtime = server.manager.get_session(sid)
        _install_fake_run_with_one_write(
            runtime, path="hello.txt", content="HELLO",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            await _start_run(http, base, sid)
            pending = await collector.wait("file_write_pending", timeout=3.0)
            request_id = pending["request_id"]

            async with http.post(
                f"{base}/api/v1/sessions/{sid}/runs/stop", json={},
            ) as resp:
                assert resp.status == 200, await resp.text()

            rejected = await collector.wait("file_write_rejected", timeout=3.0)
            assert rejected["request_id"] == request_id
            reason = (rejected.get("reason") or "").lower()
            assert "stop" in reason or "run" in reason or "cancel" in reason, (
                f"teardown reject reason should mention stop/run/cancel: {rejected}"
            )

            assert not (workdir / "hello.txt").exists(), (
                "teardown-auto-rejected write must not commit to disk"
            )
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 5. Default (approval_required omitted) keeps the legacy direct-write
#    path. Regression guard for sessions that opt out of staging.
async def test_default_session_writes_through_without_pending(
    e2e_server, tmp_path: Path,
):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_session(
            http, base, workdir=workdir, approval_required=False,
        )
        runtime = server.manager.get_session(sid)
        _install_fake_run_with_one_write(
            runtime, path="hello.txt", content="HELLO",
        )

        collector = _WSCollector()
        ws, reader_task = await _open_ws(http, base, sid, collector)
        try:
            init = next(e for e in collector.received if e.get("type") == "init")
            assert init.get("approval_required") in (False, None), (
                f"default session leaked approval_required=True: {init}"
            )

            await _start_run(http, base, sid)

            committed = await collector.wait("file_write", timeout=3.0)
            assert committed["path"] == "hello.txt"
            assert committed["content"] == "HELLO"

            await collector.wait("run_complete", timeout=3.0)

            # Pending stage must not appear when approval is off.
            assert not collector.saw("file_write_pending"), (
                "non-approval session must not emit file_write_pending"
            )
            assert not collector.saw("file_write_rejected"), (
                "non-approval session must not emit file_write_rejected"
            )

            disk_path = workdir / "hello.txt"
            assert disk_path.exists()
            assert disk_path.read_text() == "HELLO"
        finally:
            reader_task.cancel()
            await ws.close()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass


# 6. Unknown approval_id → 404 APPROVAL_UNKNOWN envelope.
async def test_unknown_approval_id_returns_404(e2e_server, tmp_path: Path):
    ts, server = e2e_server
    base = f"http://{ts.host}:{ts.port}"

    workdir = tmp_path / "wd"
    workdir.mkdir()

    async with aiohttp.ClientSession() as http:
        sid = await _create_session(
            http, base, workdir=workdir, approval_required=True,
        )
        bogus_id = uuid.uuid4().hex
        status, env = await _post_approval(
            http, base, sid, bogus_id, action="approve",
        )
        assert status == 404, env
        assert env["ok"] is False
        assert env["code"] == "APPROVAL_UNKNOWN"
