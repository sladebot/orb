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
import threading
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
    lock = threading.Lock()
    active_iterdir = 0
    max_active_iterdir = 0

    def slow_iterdir(self):
        nonlocal active_iterdir, max_active_iterdir
        # Only slow the target directory to keep the test deterministic.
        if str(self) == str(workdir):
            with lock:
                active_iterdir += 1
                max_active_iterdir = max(max_active_iterdir, active_iterdir)
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    active_iterdir -= 1
        return real_iterdir(self)

    with patch.object(Path, "iterdir", slow_iterdir):
        start = time.monotonic()
        results = await asyncio.gather(
            client.get("/api/v1/fs/list", params={"path": str(workdir)}),
            client.get("/api/v1/fs/list", params={"path": str(workdir)}),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results)
    assert max_active_iterdir >= 2, (
        f"fs_list did not overlap directory iteration; elapsed={elapsed:.3f}s"
    )


async def test_fs_dir_does_not_block_event_loop(client, tmp_path):
    """/api/v1/fs/dir also iterates a directory — same regression."""
    workdir = tmp_path / "dirroot"
    workdir.mkdir()
    (workdir / "a.py").write_text("")
    (workdir / "b.py").write_text("")

    real_iterdir = Path.iterdir
    lock = threading.Lock()
    active_iterdir = 0
    max_active_iterdir = 0

    def slow_iterdir(self):
        nonlocal active_iterdir, max_active_iterdir
        if str(self) == str(workdir):
            with lock:
                active_iterdir += 1
                max_active_iterdir = max(max_active_iterdir, active_iterdir)
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    active_iterdir -= 1
        return real_iterdir(self)

    with patch.object(Path, "iterdir", slow_iterdir):
        start = time.monotonic()
        results = await asyncio.gather(
            client.get("/api/v1/fs/dir", params={"workdir": str(workdir)}),
            client.get("/api/v1/fs/dir", params={"workdir": str(workdir)}),
        )
        elapsed = time.monotonic() - start

    assert all(r.status == 200 for r in results)
    assert max_active_iterdir >= 2, (
        f"fs_dir did not overlap directory iteration; elapsed={elapsed:.3f}s"
    )


async def test_fs_read_does_not_block_event_loop(client, tmp_path):
    """/api/v1/fs/read opens a file; the read must run off the loop."""
    workdir = (tmp_path / "readroot").resolve()
    workdir.mkdir()
    target = workdir / "note.txt"
    target.write_text("hello world")

    real_read_text = Path.read_text
    lock = threading.Lock()
    active_reads = 0
    max_active_reads = 0

    def slow_read_text(self, *a, **kw):
        nonlocal active_reads, max_active_reads
        if self.name == "note.txt":
            with lock:
                active_reads += 1
                max_active_reads = max(max_active_reads, active_reads)
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    active_reads -= 1
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
    assert max_active_reads >= 2, (
        f"fs_read did not overlap file reads; elapsed={elapsed:.3f}s"
    )


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
    lock = threading.Lock()
    active_walks = 0
    max_active_walks = 0

    def slow_walk(p, *a, **kw):
        nonlocal active_walks, max_active_walks
        if str(p) == str(workdir):
            with lock:
                active_walks += 1
                max_active_walks = max(max_active_walks, active_walks)
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    active_walks -= 1
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
    assert max_active_walks >= 2, (
        f"fs_files did not overlap os.walk fallback; elapsed={elapsed:.3f}s"
    )


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

    lock = threading.Lock()
    active_by_cmd: dict[tuple[str, ...], int] = {}
    max_active_by_cmd: dict[tuple[str, ...], int] = {}

    def slow_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            cmd = list(args[1:])
            key = tuple(cmd)
            with lock:
                active_by_cmd[key] = active_by_cmd.get(key, 0) + 1
                max_active_by_cmd[key] = max(
                    max_active_by_cmd.get(key, 0),
                    active_by_cmd[key],
                )
            try:
                time.sleep(0.05)
                if cmd == ["rev-parse", "--is-inside-work-tree"]:
                    return subprocess.CompletedProcess(args, 0, stdout="true\n", stderr="")
                if cmd == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, stdout="feature/pr-url\n", stderr="")
                if cmd == ["remote", "get-url", "origin"]:
                    return subprocess.CompletedProcess(args, 0, stdout="git@github.com:acme/demo.git\n", stderr="")
                if cmd == ["status", "--porcelain"]:
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if cmd == ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]:
                    return subprocess.CompletedProcess(args, 0, stdout="0 0\n", stderr="")
                if cmd == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                    return subprocess.CompletedProcess(args, 0, stdout="refs/remotes/origin/master\n", stderr="")
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="unexpected git command")
            finally:
                with lock:
                    active_by_cmd[key] -= 1
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

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
    # The git calls are fully faked so the assertion is not coupled to local
    # git startup cost on slower CI/macOS runners. In particular, both PR URL
    # requests should run their symbolic-ref lookup in worker threads; if that
    # call regresses to inline event-loop subprocess.run, max concurrency for
    # this command stays at 1.
    symbolic_ref_key = ("symbolic-ref", "refs/remotes/origin/HEAD")
    assert max_active_by_cmd.get(symbolic_ref_key, 0) >= 2, (
        f"git_pr_url symbolic-ref did not overlap; elapsed={elapsed:.3f}s"
    )
