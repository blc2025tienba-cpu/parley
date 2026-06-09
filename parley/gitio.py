"""Parley git ops (ADR-02/ADR-11). Harness owns git; agents never run git directly.

Guards: refuse on protected branches (main/master/develop); never force/reset/amend.
"""
from __future__ import annotations

import subprocess

_PROTECTED = ("main", "master", "develop")


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def current_branch(cwd) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def diff(project_dir: str, max_chars: int = 4000) -> str:
    """Captured working-tree diff (staged+unstaged) for evidence/review. Empty if not a repo."""
    _git(["add", "-A"], project_dir)
    r = _git(["diff", "--cached", "--stat"], project_dir)
    s = _git(["diff", "--cached"], project_dir)
    out = (r.stdout or "") + "\n" + (s.stdout or "") if r.returncode == 0 else ""
    return out[:max_chars]


def commit_slice(project_dir: str, slice: str, message: str | None = None,
                 protected=_PROTECTED) -> str | None:
    """Commit current changes for a slice. Refuses on protected branch. Returns sha or None."""
    if current_branch(project_dir) in protected:
        return None
    _git(["add", "-A"], project_dir)
    _git(["commit", "-m", message or f"parley: slice {slice}"], project_dir)  # may be empty -> ignore
    h = _git(["rev-parse", "HEAD"], project_dir)
    return h.stdout.strip() if h.returncode == 0 else None


def push(project_dir: str, branch: str, remote: str = "origin", protected=_PROTECTED) -> bool:
    """Push work branch (no force, never protected). Returns True on success."""
    if not branch or branch in protected:
        return False
    r = _git(["push", "-u", remote, branch], project_dir)
    return r.returncode == 0
