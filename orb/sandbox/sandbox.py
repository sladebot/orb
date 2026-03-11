from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class Sandbox:
    """
    Isolated working directory for agent tool calls.

    All file paths are resolved within `root`, preventing path-traversal
    escapes.  `run_command` executes with cwd=root and a scrubbed environment
    so stray writes to the real filesystem are much harder to trigger
    accidentally.

    One sandbox is created per orchestrator run and shared across all agents,
    so a coder can write files that the tester can read.
    """

    COMMAND_TIMEOUT: float = float(os.environ.get("ORB_COMMAND_TIMEOUT", "30"))  # seconds

    def __init__(
        self,
        root: str | Path | None = None,
        name: str | None = None,
        projects_dir: str | Path | None = None,
    ) -> None:
        """
        Create a sandbox.

        Priority:
        1. ``root``         — use this exact directory (persistent, not auto-cleaned)
        2. ``name``         — create ``<projects_dir>/<name>/`` (persistent)
        3. no args          — temp directory (cleaned up on ``cleanup()``)

        Args:
            root:         Explicit directory to use as the sandbox root.
            name:         Project name; creates ``projects/<name>/`` under cwd.
            projects_dir: Override the parent dir for named sandboxes (default: ``./projects``).
        """
        self._tmpdir: str | None = None

        if root is not None:
            self._owned = False
            self.root = Path(root).resolve()
            self.root.mkdir(parents=True, exist_ok=True)
        elif name is not None:
            self._owned = False
            base = Path(projects_dir).resolve() if projects_dir else Path.cwd() / "projects"
            base.mkdir(parents=True, exist_ok=True)
            self.root = (base / name).resolve()
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            self._owned = True
            self._tmpdir = tempfile.mkdtemp(prefix="orb_sandbox_")
            self.root = Path(self._tmpdir).resolve()

        logger.info(f"Sandbox root: {self.root}")

    # ── Path resolution ───────────────────────────────────────────────────────

    def resolve(self, path: str) -> Path:
        """Resolve *path* relative to the sandbox root.

        Absolute paths are treated as relative to the sandbox root (the leading
        ``/`` is stripped).  Paths that would escape the root via ``..`` traversal
        raise ``PermissionError``.
        """
        p = Path(path)
        if p.is_absolute():
            # Treat absolute paths as relative to sandbox root
            p = Path(*p.parts[1:])  # strip leading /
        resolved = (self.root / p).resolve()
        # Ensure the resolved path stays within the sandbox
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            raise PermissionError(f"Path {path!r} escapes sandbox root")
        return resolved

    # ── Filesystem operations ─────────────────────────────────────────────────

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        rel = p.relative_to(self.root)
        logger.info(f"sandbox write_file: {rel} ({lines} lines)")
        return f"Written {lines} lines to {rel}"

    def read_file(self, path: str) -> str:
        p = self.resolve(path)
        if not p.exists():
            raise FileNotFoundError(f"No such file: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        logger.info(f"sandbox read_file: {p.relative_to(self.root)} ({len(content)} chars)")
        return content

    def list_directory(self, path: str = ".") -> str:
        p = self.resolve(path)
        if not p.exists():
            raise FileNotFoundError(f"No such directory: {path}")
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        if not entries:
            return "(empty)"
        lines = []
        for e in entries:
            tag = "f" if e.is_file() else "d"
            size = f"  {e.stat().st_size:>8} B" if e.is_file() else ""
            lines.append(f"{tag}  {e.name}{size}")
        return "\n".join(lines)

    # ── Command execution ─────────────────────────────────────────────────────

    async def run_command(self, command: str) -> str:
        """Run *command* inside the sandbox root with a scrubbed environment."""
        safe_env = _scrub_env(self.root)
        logger.info(f"sandbox run_command: {command!r} (cwd={self.root})")

        try:
            args = shlex.split(command)
        except ValueError as e:
            return f"Error: invalid command syntax: {e}"
        if not args:
            return "Error: empty command"

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.root),
                env=safe_env,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.COMMAND_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return f"Error: command timed out after {self.COMMAND_TIMEOUT:.0f}s"
        except Exception as exc:
            return f"Error: {exc}"

        output = stdout.decode("utf-8", errors="replace").strip()
        rc = proc.returncode
        logger.info(f"sandbox run_command exit={rc}")
        return f"exit code: {rc}\n{output}" if output else f"exit code: {rc}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Delete the sandbox directory (only if we created it)."""
        if self._owned and self._tmpdir and Path(self._tmpdir).exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            logger.info(f"Sandbox cleaned up: {self._tmpdir}")

    def __repr__(self) -> str:
        return f"Sandbox(root={self.root})"


# ── Helpers ───────────────────────────────────────────────────────────────────

_PASSTHROUGH_ENV = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE",
    "PYTHONPATH", "VIRTUAL_ENV",
    "NODE_PATH", "GOPATH", "GOROOT",
    "JAVA_HOME", "CARGO_HOME", "RUSTUP_HOME",
}


def _scrub_env(sandbox_root: Path) -> dict[str, str]:
    """Return a minimal environment for sandboxed command execution."""
    env: dict[str, str] = {}
    for key in _PASSTHROUGH_ENV:
        val = os.environ.get(key)
        if val:
            env[key] = val
    # Override home/tmp so rogue scripts can't write to real home dir
    env["HOME"]   = str(sandbox_root)
    env["TMPDIR"] = str(sandbox_root / "tmp")
    env["PWD"]    = str(sandbox_root)
    (sandbox_root / "tmp").mkdir(exist_ok=True)
    return env
