from __future__ import annotations

import subprocess
from pathlib import Path


def capture_diff(cwd: str | Path | None = None) -> str:
    """Return the current `git diff` (unstaged changes) in the working directory."""
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True,
            cwd=str(cwd) if cwd else None,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def diff_stat(cwd: str | Path | None = None) -> str:
    """Return `git diff --stat` summary."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True,
            cwd=str(cwd) if cwd else None,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def parse_diff_files(diff: str) -> list[dict]:
    """
    Parse a unified diff string into a list of per-file diffs.
    Returns [{"path": str, "stat": str, "body": str}, ...]
    """
    if not diff:
        return []

    files: list[dict] = []
    current: dict | None = None

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                files.append(current)
            # extract b/path
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) > 1 else line.strip()
            current = {"path": path, "stat": "", "body": line}
        elif current is not None:
            current["body"] += line
            if line.startswith("@@"):
                # count +/- lines already collected
                pass

    if current:
        files.append(current)

    # Build a short stat per file (added/removed lines)
    for f in files:
        added   = f["body"].count("\n+")
        removed = f["body"].count("\n-")
        f["stat"] = f"+{added} -{removed}"

    return files
