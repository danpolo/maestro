"""Write sets: tasks that would write the same files never run in parallel (D20).

P12 pilot finding D20: DuetFlow 03 and 04 ran at the same time, both edited one file,
and 04's approved work could not merge. Dan's rule: tasks that run in parallel must
merge trivially, so the graph loop does not open a run whose predicted writes overlap a
live run's. The prediction is the task's declared `scope:` (files, directories, globs);
a live run's write set is its scope plus every file it has actually changed so far.

Unknown is conservative everywhere: a task with no `scope:` key, or a live run git
cannot describe, overlaps everything and so runs alone. `scope: []` is a declaration
that the task writes nothing.
"""
from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

#: The overlap reported against a run whose write set is unknown.
UNDECLARED = "undeclared scope"

_GLOB = "*?["


def declared_scope(task: Mapping) -> tuple[str, ...] | None:
    """The ROADMAP `scope:` as a tuple, `None` when the key is absent (unknown)."""
    if "scope" not in task or task.get("scope") is None:
        return None
    raw = task["scope"]
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(entry) for entry in raw if str(entry).strip())


def _norm(pattern: str) -> str:
    pattern = str(pattern).strip().replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern.rstrip("/") or "."


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB)


def _literal_prefix(pattern: str) -> str:
    cut = min((pattern.index(ch) for ch in _GLOB if ch in pattern), default=len(pattern))
    return pattern[:cut]


def _contains(outer: str, inner: str) -> bool:
    """`inner` is `outer`, or under it by path components. `.` holds everything."""
    return outer == "." or inner == outer or inner.startswith(outer + "/")


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _pair(a: str, b: str) -> str:
    """The overlap of two patterns, as the path to report, or `""`."""
    glob_a, glob_b = _is_glob(a), _is_glob(b)
    if not glob_a and not glob_b:
        if _contains(a, b):
            return b
        if _contains(b, a):
            return a
        return ""
    if glob_a and glob_b:
        if a == b:
            return a
        pa, pb = _literal_prefix(a), _literal_prefix(b)
        if (fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)
                or pa.startswith(pb) or pb.startswith(pa)):
            return " & ".join(sorted((a, b)))
        return ""
    glob, literal = (a, b) if glob_a else (b, a)
    # The glob names the literal, or a directory holding it; or the literal is a
    # directory the glob's files live under.
    if any(fnmatch.fnmatchcase(p, glob) for p in _ancestors(literal)):
        return literal
    if literal == "." or _literal_prefix(glob).startswith(literal + "/"):
        return glob
    return ""


def write_sets_overlap(a: Iterable[str], b: Iterable[str]) -> tuple[str, ...]:
    """Every overlap between two sets of path patterns, sorted; `()` when disjoint.

    Patterns are exact files, directories (by path component, so `src/a` does not hold
    `src/ab.py`) or `fnmatch` globs. Glob against glob is decided by their literal
    prefixes, which over-reports rather than miss a real overlap.
    """
    left = {_norm(p) for p in a if str(p).strip()}
    right = {_norm(p) for p in b if str(p).strip()}
    found = {hit for x in left for y in right if (hit := _pair(x, y))}
    return tuple(sorted(found))


def conflicts(
    candidate: tuple[str, ...] | None, running: Mapping[str, tuple[str, ...] | None]
) -> dict[str, tuple[str, ...]]:
    """`{running task: overlapping paths}` for a candidate's predicted write set.

    `None` is unknown on either side. A candidate that declares it writes nothing
    (`scope: []`) cannot collide with anything, known or not.
    """
    if candidate is not None and not candidate:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for task_id, writes in sorted(running.items()):
        if candidate is None or writes is None:
            out[task_id] = (UNDECLARED,)
        elif hits := write_sets_overlap(candidate, writes):
            out[task_id] = hits
    return out


def _git(repo: Path, *args: str, runner=subprocess.run):
    return runner(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60)


def changed_paths(
    repo: Path, task_id: str, *, target: str = "main", runner=subprocess.run
) -> tuple[str, ...] | None:
    """Files a live run has changed so far, against `target`; `None` if git cannot say.

    Its `impl/<task_id>` branch (and any candidate branch under it) since the fork
    point, plus uncommitted and untracked files in a worktree that has one checked out.
    A run with no branch yet has changed nothing.
    """
    try:
        refs = _git(repo, "for-each-ref", "--format=%(refname)",
                    f"refs/heads/impl/{task_id}", runner=runner)
        if refs.returncode != 0:
            return None
        branches = [r for r in refs.stdout.split() if r]
        paths: set[str] = set()
        for ref in branches:
            diff = _git(repo, "diff", "--name-only", f"{target}...{ref}", runner=runner)
            if diff.returncode != 0:
                return None
            paths.update(diff.stdout.splitlines())
        listing = _git(repo, "worktree", "list", "--porcelain", runner=runner)
        if listing.returncode != 0:
            return None
        tree = ""
        for line in listing.stdout.splitlines():
            if line.startswith("worktree "):
                tree = line[len("worktree "):]
            elif line.startswith("branch ") and line[len("branch "):] in branches and tree:
                for args in (("diff", "--name-only", "HEAD"),
                             ("ls-files", "--others", "--exclude-standard")):
                    proc = _git(Path(tree), *args, runner=runner)
                    if proc.returncode != 0:
                        return None
                    paths.update(proc.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return None
    return tuple(sorted(p for p in paths if p))


def run_write_set(
    repo: Path, task_id: str, scope: tuple[str, ...] | None, *, target: str = "main",
    runner=subprocess.run,
) -> tuple[str, ...] | None:
    """A live run's write set: its declared scope plus what it has changed; `None` = unknown."""
    if scope is None:
        return None
    changed = changed_paths(repo, task_id, target=target, runner=runner)
    if changed is None:
        return None
    return tuple(sorted(set(scope) | set(changed)))
