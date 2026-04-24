"""Regression tests: FS + git handlers must not block the event loop.

These handlers do synchronous I/O (directory walks, reads, subprocess
calls). Running them inline on the asyncio loop stalls every connected
WebSocket client while the handler runs. The fix is to wrap the sync
body in ``asyncio.to_thread`` so the loop stays free.

Each test uses the same "two parallel requests + slowed-down I/O"
pattern as ``test_git_status_does_not_block_event_loop`` in
tests/test_server_api.py. If the handler is still sync, the second
request is serialized behind the first and the elapsed time is ~2x
the single-request time; if the handler is properly offloaded, the
requests overlap in the thread pool and elapsed time is ~1x.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from web.server import DashboardServer
from web.state import DashboardState


@pytest.fixture
async def client():
    server = DashboardServer(DashboardState(), host="127.0.0.1", port=18444)
    ts = TestServer(server._app)  # noqa: SLF001
    await ts.start_server()
    try:
        async with TestClient(ts) as c:
            yield c
    finally:
        await ts.close()


async def test_fs_list_does_not_block_event_loop(client, tmp_path):
    """/api/v1/fs/list iterates a directory; the iteration must run off
    the event loop so a slow scandir doesn't stall concurrent requests.
    """
    workdir = tmp_path / "listroot"
    workdir.mkdir()
    for i in range(3):
        (workdir / f"sub{i}").mkdir()

    real_iterdir = Path.iterdir

    def slow_iterdir(self):
        # Only slow the target directory to keep the test deterministic.
        if str(self) == str(workdir):
            time.sleep(0.2)
        return real_iterdir(self)

    with patch.object(Path, "iterdir", slow_iterdir):
        start = time.monotonic()
        results = await asyncio.gather(
            client.get("/api/v1/fs/list", params={"path": str(workdir)}),
            client.get("/api/v1/fs/list", params={"path": str(workdir)}),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results)
    # Serial ~0.4s, parallel ~0.2s. Generous threshold to avoid flakes.
    assert elapsed < 0.35, f"fs_list blocked the event loop: elapsed={elapsed:.3f}s"


async def test_fs_dir_does_not_block_event_loop(client, tmp_path):
    """/api/v1/fs/dir also iterates a directory — same regression."""
    workdir = tmp_path / "dirroot"
    workdir.mkdir()
    (workdir / "a.py").write_text("")
    (workdir / "b.py").write_text("")

    real_iterdir = Path.iterdir

    def slow_iterdir(self):
        if str(self) == str(workdir):
            time.sleep(0.2)
        return real_iterdir(self)

    with patch.object(Path, "iterdir", slow_iterdir):
        start = time.monotonic()
        results = await asyncio.gather(
            client.get("/api/v1/fs/dir", params={"workdir": str(workdir)}),
            client.get("/api/v1/fs/dir", params={"workdir": str(workdir)}),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results)
    assert elapsed < 0.35, f"fs_dir blocked the event loop: elapsed={elapsed:.3f}s"


async def test_fs_read_does_not_block_event_loop(client, tmp_path):
    """/api/v1/fs/read opens a file; the read must run off the loop."""
    workdir = (tmp_path / "readroot").resolve()
    workdir.mkdir()
    target = workdir / "note.txt"
    target.write_text("hello world")

    real_read_text = Path.read_text

    def slow_read_text(self, *a, **kw):
        if self.name == "note.txt":
            time.sleep(0.2)
        return real_read_text(self, *a, **kw)

    with patch.object(Path, "read_text", slow_read_text):
        start = time.monotonic()
        results = await asyncio.gather(
            client.get(
                "/api/v1/fs/read",
                params={"workdir": str(workdir), "path": "note.txt"},
            ),
            client.get(
                "/api/v1/fs/read",
                params={"workdir": str(workdir), "path": "note.txt"},
            ),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results)
    assert elapsed < 0.35, f"fs_read blocked the event loop: elapsed={elapsed:.3f}s"


async def test_fs_files_walk_fallback_does_not_block_event_loop(client, tmp_path):
    """fs_files' os.walk fallback (no git repo, or git unavailable)
    must not block the loop. We pick a non-git directory so the walk
    branch is exercised directly.
    """
    workdir = (tmp_path / "walkroot").resolve()
    workdir.mkdir()
    for i in range(3):
        (workdir / f"f{i}.py").write_text("")

    # fs_files rejects paths outside $HOME unless they're a registered
    # session workdir — create one so this tmp_path is in scope.
    reg = await client.post("/api/v1/sessions", json={"workdir": str(workdir)})
    assert reg.status == 201, await reg.text()

    real_walk = os.walk

    def slow_walk(p, *a, **kw):
        if str(p) == str(workdir):
            time.sleep(0.2)
        return real_walk(p, *a, **kw)

    with patch.object(os, "walk", slow_walk):
        start = time.monotonic()
        results = await asyncio.gather(
            client.get("/api/v1/fs/files", params={"path": str(workdir)}),
            client.get("/api/v1/fs/files", params={"path": str(workdir)}),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results), [
        (r.status, await r.text()) for r in results
    ]
    assert elapsed < 0.35, f"fs_files blocked the event loop: elapsed={elapsed:.3f}s"


async def test_git_pr_url_does_not_block_event_loop(client, tmp_path):
    """/api/v1/git/pr-url's raw subprocess.run for symbolic-ref must be
    offloaded to a thread. With a slow git binary the two parallel
    requests would serialize on the sync path.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(workdir), check=True, timeout=10)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "remote", "add", "origin", "git@github.com:acme/demo.git"],
        cwd=str(workdir), check=True, timeout=10,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "x", "-q"],
        cwd=str(workdir), check=True, timeout=10,
    )

    real_run = subprocess.run

    def slow_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            time.sleep(0.15)
        return real_run(args, *a, **kw)

    with patch.object(subprocess, "run", slow_run):
        start = time.monotonic()
        results = await asyncio.gather(
            client.post("/api/v1/git/pr-url", json={"path": str(workdir)}),
            client.post("/api/v1/git/pr-url", json={"path": str(workdir)}),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results), [
        (r.status, await r.text()) for r in results
    ]
    # _git_status_async (5 git calls) is already offloaded → ~750ms overlap.
    # The symbolic-ref call at line 697 is the raw sync one — unwrapped it
    # serializes across the two requests (2×150ms extra on the loop) so
    # the post-_git_status tail adds ~300ms sequentially. When wrapped in
    # to_thread the two symbolic-ref calls overlap instead, adding only
    # ~150ms. Measured: ≈1.04s wrapped, ≈1.21s raw. 1.15 cleanly separates.
    assert elapsed < 1.15, f"git_pr_url blocked the event loop: elapsed={elapsed:.3f}s"
