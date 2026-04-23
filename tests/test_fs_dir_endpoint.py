"""Tests for /api/v1/fs/dir — lazy, per-folder tree listing.

The endpoint returns one level of a session workdir at a time, filtered
by the workdir's own .gitignore (via pathspec, no git CLI) plus a small
hard-coded deny list for VCS metadata and build caches. The frontend
expands folders on click instead of pre-walking the whole tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from web.server import DashboardServer
from web.state import DashboardState


@pytest.fixture
async def client():
    server = DashboardServer(DashboardState(), host="127.0.0.1", port=18322)
    ts = TestServer(server._app)  # noqa: SLF001
    await ts.start_server()
    try:
        async with TestClient(ts) as c:
            yield c
    finally:
        await ts.close()


async def test_fs_dir_lists_top_level(client, tmp_path):
    """Plain directory, no .gitignore — top-level files and folders
    must surface, including untracked ones.
    """
    wd = tmp_path / "repo"
    wd.mkdir()
    (wd / "README.md").write_text("hi")
    (wd / "main.py").write_text("print('hi')")
    (wd / "src").mkdir()
    (wd / "src" / "app.py").write_text("")

    resp = await client.get("/api/v1/fs/dir", params={"workdir": str(wd)})
    assert resp.status == 200, await resp.text()
    env = await resp.json()
    assert env["ok"] is True
    data = env["data"]
    file_names = {f["name"] for f in data.get("files", [])}
    dir_names = {d["name"] for d in data.get("dirs", [])}
    assert file_names == {"README.md", "main.py"}
    assert dir_names == {"src"}


async def test_fs_dir_lists_subfolder(client, tmp_path):
    """?path=<rel> returns that subfolder's direct children."""
    wd = tmp_path / "repo"
    (wd / "src").mkdir(parents=True)
    (wd / "src" / "a.py").write_text("")
    (wd / "src" / "b.py").write_text("")
    (wd / "src" / "nested").mkdir()

    resp = await client.get("/api/v1/fs/dir", params={"workdir": str(wd), "path": "src"})
    data = (await resp.json())["data"]
    assert {f["name"] for f in data["files"]} == {"a.py", "b.py"}
    assert {d["name"] for d in data["dirs"]} == {"nested"}
    # Returned paths are relative to workdir, not to the subfolder.
    assert {f["path"] for f in data["files"]} == {"src/a.py", "src/b.py"}
    assert {d["path"] for d in data["dirs"]} == {"src/nested"}


async def test_fs_dir_respects_gitignore(client, tmp_path):
    """Entries matched by the workdir's .gitignore must be filtered out,
    regardless of whether the repo is actually a git repo.
    """
    wd = tmp_path / "repo"
    wd.mkdir()
    (wd / ".gitignore").write_text("secret.env\n*.log\nbuild/\n")
    (wd / "secret.env").write_text("API_KEY=xxx")
    (wd / "run.log").write_text("")
    (wd / "app.py").write_text("")
    (wd / "build").mkdir()
    (wd / "build" / "out.js").write_text("")
    (wd / "src").mkdir()

    resp = await client.get("/api/v1/fs/dir", params={"workdir": str(wd)})
    data = (await resp.json())["data"]
    file_names = {f["name"] for f in data["files"]}
    dir_names = {d["name"] for d in data["dirs"]}
    assert "secret.env" not in file_names
    assert "run.log" not in file_names
    assert "app.py" in file_names
    assert "build" not in dir_names
    assert "src" in dir_names


async def test_fs_dir_hides_builtin_deny_list(client, tmp_path):
    """Even without .gitignore, .git and common build dirs are hidden."""
    wd = tmp_path / "repo"
    wd.mkdir()
    for d in [".git", "node_modules", "__pycache__", ".mypy_cache"]:
        (wd / d).mkdir()
    (wd / "real.py").write_text("")
    resp = await client.get("/api/v1/fs/dir", params={"workdir": str(wd)})
    data = (await resp.json())["data"]
    dir_names = {d["name"] for d in data["dirs"]}
    assert dir_names == set()  # all were deny-listed
    assert {f["name"] for f in data["files"]} == {"real.py"}


async def test_fs_dir_rejects_path_escape(client, tmp_path):
    """A relative path that resolves outside the workdir must 400."""
    wd = tmp_path / "repo"
    wd.mkdir()
    (tmp_path / "outside.txt").write_text("")
    resp = await client.get(
        "/api/v1/fs/dir",
        params={"workdir": str(wd), "path": "../outside.txt"},
    )
    assert resp.status == 400, await resp.text()


async def test_fs_dir_rejects_nonexistent_workdir(client, tmp_path):
    resp = await client.get(
        "/api/v1/fs/dir",
        params={"workdir": str(tmp_path / "missing")},
    )
    assert resp.status == 400


async def test_fs_dir_sorts_alphabetically(client, tmp_path):
    wd = tmp_path / "repo"
    wd.mkdir()
    for name in ["zed.py", "alpha.py", "Mid.py"]:
        (wd / name).write_text("")
    resp = await client.get("/api/v1/fs/dir", params={"workdir": str(wd)})
    data = (await resp.json())["data"]
    file_names = [f["name"] for f in data["files"]]
    assert file_names == sorted(file_names, key=str.casefold)
