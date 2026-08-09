"""tmux windows and git worktrees: the sandbox an implementer session runs in.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block, the derivation of the module-level path globals and the scratch-directory
prefix (which named the consuming project in the reference) differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_worktree.py`; none of them is fixed here.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo

TMUX_SESSION  = "agents"

# The reference implementation hardcodes a /tmp scratch prefix that embeds the consuming
# project's name. Only that literal is neutralised here; the shape is identical.
WORKTREE_PREFIX = "/tmp/maestro-impl-"


# ── tmux / worktree helpers ──

def tmux_window_exists(window: str) -> bool:
    r = subprocess.run(["tmux", "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
                       capture_output=True, text=True)
    return window in r.stdout.splitlines()


def worktree_path_for(task_id: str) -> Path:
    return Path(f"{WORKTREE_PREFIX}{task_id}")


def create_worktree(task_id: str, worktree: Path, branch: str) -> None:
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=str(REPO), capture_output=True)
        shutil.rmtree(worktree, ignore_errors=True)
    # A reboot can wipe /tmp, leaving git with a "missing but already registered"
    # worktree: the dir is gone (so the .exists() cleanup above is skipped) yet
    # `git worktree add` still fails with exit 128 every time. Prune always clears
    # such stale registrations. (TUNE1 retry-storm, 2026-06-26.)
    subprocess.run(["git", "worktree", "prune"],
                   cwd=str(REPO), capture_output=True)
    subprocess.run(["git", "worktree", "add", "-b", branch, str(worktree)],
                   cwd=str(REPO), check=True, capture_output=True)


def remove_worktree(worktree: Path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                   cwd=str(REPO), capture_output=True)
    shutil.rmtree(worktree, ignore_errors=True)


def _kill_tmux_window(window: str) -> None:
    """Best-effort kill of a lingering implementer window (and its hung process)."""
    subprocess.run(["tmux", "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                   capture_output=True)


def _tail_tmux_pane(window: str, n: int = 3) -> str:
    """Last `n` non-blank lines of an implementer's tmux pane — live activity.
    Best-effort: returns '' if the window is gone or capture fails."""
    if not window or not tmux_window_exists(window):
        return ""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"{TMUX_SESSION}:{window}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return ""
        lines = [ln.rstrip() for ln in r.stdout.splitlines() if ln.strip()]
        return "\n".join(lines[-n:])
    except Exception:
        return ""
