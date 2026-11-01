"""The task worktree the graph loop hands its nodes (`_p12_open_threads` #1).

One git worktree per task (per candidate, for a widened stage), at the path
`confinement.worktree_for` fixes and on the branch `confinement.branch_name` fixes
(`[INV-A08]`, `[INV-05]`). The graph runner creates it when a writer is claimed, hands it
to every node that reads the task's tree, and releases it when the run settles.

Deliberately not `maestro.worktree.create_worktree`, the legacy helper: that one deletes
an existing tree before creating it, which is right for a fresh legacy launch and wrong
here — a P12B F6 retry must find its predecessor's tree (and `start_sha`) exactly where it
was, and a repaired run (W7) inherits the canceled run's tree. So `ensure` reuses what is
there and refuses what it cannot vouch for, and `release` never discards uncommitted work.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from maestro import confinement


class TaskTreeError(RuntimeError):
    """The task's worktree could not be created or reused as it stands."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60
    )


def is_repository(repo: Path) -> bool:
    """Whether `repo` is a git repository the loop can hang worktrees off."""
    return (Path(repo) / ".git").exists()


def path_for(repo: Path, task_id: str, *, candidate: str = "") -> Path:
    return confinement.worktree_for(repo, task_id, candidate=candidate)


def _registered(repo: Path) -> set[Path]:
    proc = _git(repo, "worktree", "list", "--porcelain")
    return {
        Path(line.split(" ", 1)[1]).resolve()
        for line in proc.stdout.splitlines()
        if line.startswith("worktree ")
    }


def ensure(repo: Path, task_id: str, *, candidate: str = "", base: str = "") -> Path:
    """The task's worktree, created on its namespaced branch if it is not there yet.

    An existing registered worktree is reused as it stands. A branch that already exists
    (an earlier run of the task, parked with its commits) is checked out rather than
    recreated, so its commits are never orphaned. `base` is the run's `code_base_sha`; an
    empty one means the repository's `HEAD`.
    """
    repo = Path(repo)
    tree = path_for(repo, task_id, candidate=candidate)
    if tree.exists():
        if tree.resolve() in _registered(repo):
            return tree
        raise TaskTreeError(
            f"{tree} exists but is not a registered git worktree; refusing to reuse or "
            "delete it"
        )
    branch = confinement.branch_name("impl", task_id, candidate=candidate)
    # A tree deleted behind git's back leaves a registration that makes `add` fail.
    _git(repo, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if exists.returncode == 0:
        proc = _git(repo, "worktree", "add", str(tree), branch)
    else:
        proc = _git(repo, "worktree", "add", "-b", branch, str(tree), base or "HEAD")
    if proc.returncode != 0:
        raise TaskTreeError(
            f"git worktree add {tree} ({branch}) failed: {proc.stderr.strip()[:500]}"
        )
    _link_project_venv(repo, tree)
    return tree


def _link_project_venv(repo: Path, tree: Path) -> None:
    """Give a new worktree the project's `.venv`, so `.venv/bin/python` verifications run.

    A venv is untracked, so `git worktree add` never copies it (G10, itv 04: the gate
    failed with exit 127 and the agent asked Dan for `ln -s`). The link is excluded
    through the repository's `info/exclude`, since a `.venv/` ignore pattern matches
    directories and not a symlink, and the gate refuses an uncommitted tree.
    """
    venv = repo / ".venv"
    link = tree / ".venv"
    if not venv.is_dir() or link.exists() or link.is_symlink():
        return
    link.symlink_to(venv.resolve(), target_is_directory=True)
    proc = _git(repo, "rev-parse", "--git-path", "info/exclude")
    if proc.returncode != 0:
        return
    exclude = Path(proc.stdout.strip())
    if not exclude.is_absolute():
        exclude = repo / exclude
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".venv" not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".venv\n",
                               encoding="utf-8")
    except OSError:
        pass


def release(repo: Path, task_id: str, *, candidate: str = "", merged: bool) -> str:
    """Remove a settled run's worktree when that loses nothing. Returns what was done.

    A tree with uncommitted changes is kept: its commits live on the branch, but its
    uncommitted work lives only here (MIGRATION_GUARDRAILS §2). The branch is deleted
    only when `merged` — the target branch then holds every commit on it, and `git branch
    -d` (not `-D`) refuses anyway if that is not so.
    """
    repo = Path(repo)
    tree = path_for(repo, task_id, candidate=candidate)
    branch = confinement.branch_name("impl", task_id, candidate=candidate)
    if tree.exists():
        status = _git(tree, "status", "--porcelain")
        if status.returncode != 0:
            return f"kept {tree}: git status failed ({status.stderr.strip()[:200]})"
        if status.stdout.strip():
            return f"kept {tree}: uncommitted changes"
        removed = _git(repo, "worktree", "remove", str(tree))
        if removed.returncode != 0:
            return f"kept {tree}: {removed.stderr.strip()[:200]}"
    if merged:
        deleted = _git(repo, "branch", "-d", branch)
        if deleted.returncode != 0:
            return f"removed {tree}; kept {branch}: {deleted.stderr.strip()[:200]}"
        return f"removed {tree} and {branch}"
    return f"removed {tree}; kept {branch}"


def uncommitted(tree: Path) -> list[str] | None:
    """The paths `git status --porcelain` lists in `tree`, or `None` when git cannot say.

    Ignored files are not listed, so the loop's own `.orchestrator/` never counts.
    """
    status = _git(Path(tree), "status", "--porcelain")
    if status.returncode != 0:
        return None
    return [line[3:] for line in status.stdout.splitlines() if line.strip()]


#: Who a harness commit is attributed to: the harness, not whoever configured the repo.
HARNESS_AUTHOR = ("maestro harness", "maestro-harness@localhost")


def commit_all(worktree: Path, message: str) -> str | None:
    """Commit everything in `worktree`; the new HEAD sha, or `None` when nothing changed.

    For a writer that cannot commit for itself: a model-endpoint harness (four tools, no
    shell) and a `kind: script` lane (the legacy loop committed after a clean run too).
    Merge freezes the branch and refuses an uncommitted tree, so without this neither
    could land. Raises `TaskTreeError` when git refuses.
    """

    def git(*argv: str) -> subprocess.CompletedProcess:
        name, email = HARNESS_AUTHOR
        return subprocess.run(
            ["git", "-c", f"user.name={name}", "-c", f"user.email={email}", *argv],
            cwd=worktree, capture_output=True, text=True, timeout=60,
        )

    done = git("add", "-A")
    if done.returncode != 0:
        raise TaskTreeError(done.stderr.strip() or "git add failed")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return None
    done = git("commit", "-q", "-m", message)
    if done.returncode != 0:
        raise TaskTreeError(done.stderr.strip() or "git commit failed")
    return git("rev-parse", "HEAD").stdout.strip()
