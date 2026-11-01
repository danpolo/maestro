"""Automated lifecycle for the `meta` orphan branch (DESIGN.md's process/project git
separation, decided 2026-09-09).

Handoffs, plans, progress records, and lessons are process artifacts: useful, worth
versioning, but not part of a project's portfolio history. They live on an orphan
`meta` branch, checked out as a sibling worktree next to the main tree — one clone,
one remote, two branches. Nothing about this is a manual git operation: `ensure_meta_branch`
creates the branch and attaches the worktree (or reconnects it after a fresh clone,
where the branch already exists on the remote but no local worktree does yet),
`sync_meta` copies the current process-doc content into it and commits, and
`push_meta` pushes — checked, unlike the two `git push origin main` call sites in
merge.py this module was written to stop from failing silently.

`reconcile_meta` chains all three and never raises, so it is safe to call from
`orchestrator.py`'s startup path, from `doctor`, and from `merge.py` right after
every `main` push — the three places DESIGN.md's setup section (§10) and the
process-git-separation handoff name as the candidate attach points. All three call
it; each call after the first is a no-op once converged.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

META_BRANCH = "meta"

# Relative paths (files or directories) that belong on the meta branch, not on
# main/master. Single source of truth: cli.py's .gitignore scaffolding and the
# one-time migration both read this list, so a newly added process-doc path only
# needs to be added here.
META_PATHS: tuple[str, ...] = (
    "handoffs",
    "docs/plans",
    "docs/superpowers",
    "docs/graph-engineering",
    "docs/PROGRESS.md",
    "tasks/lessons.md",
)


def meta_worktree_path(repo_root: Path) -> Path:
    """A sibling of the repo root, never inside it — a worktree nested inside the
    main tree would put a second git checkout inside the first's working directory,
    and the main tree would then see it as untracked content to ignore or accidentally
    walk into."""
    return repo_root.parent / f"{repo_root.name}-meta"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _branch_exists_local(repo_root: Path, branch: str) -> bool:
    r = _git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return r.returncode == 0


def _branch_exists_remote(repo_root: Path, branch: str, remote: str) -> bool:
    r = _git(repo_root, "ls-remote", "--exit-code", "--heads", remote, branch)
    return r.returncode == 0


def ensure_meta_branch(repo_root: Path, remote: str = "origin") -> tuple[bool, str]:
    """Idempotent: create the orphan `meta` branch and/or attach its sibling worktree
    if either is missing. Handles three starting states — nothing exists yet, the
    branch exists on the remote but this clone has no local worktree for it (the
    fresh-clone case), or everything is already in place. Returns (changed, detail)."""
    worktree = meta_worktree_path(repo_root)
    if worktree.is_dir() and (worktree / ".git").exists():
        return False, f"meta worktree already attached at {worktree}"

    if _branch_exists_local(repo_root, META_BRANCH):
        r = _git(repo_root, "worktree", "add", str(worktree), META_BRANCH)
        if r.returncode != 0:
            return False, f"worktree add (local branch) failed: {r.stderr.strip()[:300]}"
        return True, f"attached existing local branch at {worktree}"

    if _branch_exists_remote(repo_root, META_BRANCH, remote):
        # Fresh clone: the branch is on the remote, nothing local yet. This is the
        # exact case DESIGN.md's setup section requires automatic reconciliation for.
        fetch = _git(repo_root, "fetch", remote, META_BRANCH)
        if fetch.returncode != 0:
            return False, f"fetch {remote}/{META_BRANCH} failed: {fetch.stderr.strip()[:300]}"
        r = _git(repo_root, "worktree", "add", "-b", META_BRANCH, str(worktree),
                  f"{remote}/{META_BRANCH}")
        if r.returncode != 0:
            return False, f"worktree add (tracking) failed: {r.stderr.strip()[:300]}"
        return True, f"reconnected to {remote}/{META_BRANCH} at {worktree}"

    # Nothing anywhere yet: create the orphan branch from scratch. `git worktree add`
    # has no orphan-branch flag on the git version this was written against (2.39), so
    # attach detached, then check out the orphan branch inside the new worktree.
    worktree.parent.mkdir(parents=True, exist_ok=True)
    r = _git(repo_root, "worktree", "add", "--detach", str(worktree), "HEAD")
    if r.returncode != 0:
        return False, f"worktree add (detached) failed: {r.stderr.strip()[:300]}"
    r = _git(worktree, "checkout", "--orphan", META_BRANCH)
    if r.returncode != 0:
        return False, f"orphan checkout failed: {r.stderr.strip()[:300]}"
    _git(worktree, "rm", "-rf", "--quiet", ".")
    readme = worktree / "README.md"
    readme.write_text(
        "# meta\n\n"
        "Process docs for this project: handoffs, plans, progress, lessons. Not part of\n"
        "the portfolio history on main/master — never merge this branch there.\n"
    )
    _git(worktree, "add", "README.md")
    r = _git(worktree, "commit", "-m", "chore(meta): initialize orphan meta branch")
    if r.returncode != 0:
        return False, f"initial commit failed: {r.stderr.strip()[:300]}"
    return True, f"created new orphan branch and worktree at {worktree}"


def sync_meta(repo_root: Path, paths: tuple[str, ...] = META_PATHS) -> tuple[bool, str]:
    """Copy the current content of `paths` from the main tree into the meta worktree
    and commit if anything changed. One-way, main tree to meta: nothing changes about
    how a human or agent writes a handoff — they still just edit the file at its usual
    path in the main tree. This is what turns that edit into committed history.
    Returns (committed, detail)."""
    worktree = meta_worktree_path(repo_root)
    if not worktree.is_dir():
        return False, "no meta worktree — call ensure_meta_branch first"

    existing = [rel for rel in paths if (repo_root / rel).exists()]
    for rel in existing:
        src = repo_root / rel
        dst = worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    if not existing:
        return False, "no process-doc paths present yet"

    _git(worktree, "add", "-A", "--", *existing)
    status = _git(worktree, "status", "--porcelain", "--", *existing)
    if not status.stdout.strip():
        return False, "meta worktree already up to date"
    r = _git(worktree, "commit", "-m", "chore(meta): sync process docs")
    if r.returncode != 0:
        return False, f"commit failed: {r.stderr.strip()[:300]}"
    return True, "synced and committed"


def push_meta(repo_root: Path, remote: str = "origin") -> tuple[bool, str]:
    """Push the meta worktree's branch. Checked, unlike the two `git push origin main`
    call sites in merge.py this module exists to fix — a failed push here is reported,
    never silently swallowed."""
    worktree = meta_worktree_path(repo_root)
    if not worktree.is_dir():
        return False, "no meta worktree to push"
    # Per-clone opt-out: process docs stay local-only (e.g. a public remote).
    optout = _git(repo_root, "config", "--bool", "maestro.metaPush")
    if optout.returncode == 0 and optout.stdout.strip() == "false":
        return True, "push disabled by maestro.metaPush"
    ahead = _git(worktree, "rev-list", "--count", f"{remote}/{META_BRANCH}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return True, "nothing to push"
    r = _git(worktree, "push", "-u", remote, META_BRANCH)
    if r.returncode != 0:
        return False, f"push failed: {r.stderr.strip()[:300]}"
    return True, "pushed"


def reconcile_meta(repo_root: Path, remote: str = "origin") -> str:
    """One call that makes the meta branch fully consistent with zero human git
    commands: attach or create if missing, sync any changed process docs, push if
    ahead. Safe to call at orchestrator startup, on every `doctor` run, and after
    every `main` push — each step is a no-op once converged. Returns a short
    human-readable summary; never raises, so it never breaks the caller's own
    startup or push path."""
    try:
        _created, detail0 = ensure_meta_branch(repo_root, remote)
        committed, detail1 = sync_meta(repo_root)
        pushed, detail2 = push_meta(repo_root, remote)
        parts = [detail0]
        if committed:
            parts.append(detail1)
        if not pushed:
            parts.append(f"WARNING: {detail2}")
        return "; ".join(parts)
    except Exception as exc:  # noqa: BLE001 — reconcile must never crash a startup/poll
        return f"meta reconcile error: {exc}"
