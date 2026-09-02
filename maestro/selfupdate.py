"""Self-update: hot-upgrading maestro itself via git worktrees.

DESIGN.md §9 ("Self-update (D5)") — single channel, git worktrees, adoption gated on
maestro's own test suite:

    ~/.maestro/versions/<sha>/      git worktrees of this maestro repo, one per adopted
                                     or candidate commit (materialised lazily, on demand)
    ~/.maestro/current              a plain text file — **not** a symlink — holding the
                                     absolute path of the worktree a project should import
                                     maestro through right now

A plain text file was chosen over a symlink: it needs no filesystem symlink support
(irrelevant here, but keeps the mechanism portable), and any consumer can `read_text()`
it with no readlink/realpath dance. The tradeoff (one extra file read versus following a
link) is negligible next to that simplicity.

Nothing in this module is safe to call mid-task. `maybe_self_update` is the only entry
point meant to be wired into a live loop, and only from an idle branch where the caller
has already confirmed nothing is in flight — this module has no visibility into
`in_flight` and cannot enforce that itself; see its docstring and `adopt`'s.

`~/.maestro` (or the self-update home directory) is overridable via the `MAESTRO_HOME`
environment variable, the same override-by-env-var convention `maestro.paths.Paths` uses
for `MAESTRO_REPO` — kept as a separate dataclass here rather than folded into `Paths`
because the two roots are conceptually different: `Paths` is always relative to *a
project's* repository, while the self-update layout is a single per-machine location
that outlives any one project.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from maestro import bootstrap
from maestro.paths import ENV_VAR as PROJECT_ENV_VAR
from maestro.state import append_journal, read_state, write_state

#: Re-exported from `maestro.bootstrap`, which owns it. The resolver that reads `current`
#: has to run before any `cmd_*` sets `$MAESTRO_REPO`, so it cannot live in a module that
#: reaches `maestro.state` — this one does, two lines up.
ENV_VAR = bootstrap.ENV_VAR

#: The environment variables naming *where this machine's maestro is operating*: the
#: project being orchestrated (`maestro.paths.ENV_VAR`) and the self-update home
#: (`ENV_VAR` above). Every live launch path exports the first deliberately — see
#: `maestro/watchdog.py`'s module docstring on why a fresh tmux window cannot inherit it —
#: so any subprocess spawned from the loop gets it for free. `self_test` is the one child
#: spawn that is not orchestrating a project, so for it the correct value of both is
#: *unset*; see `_self_test_env`.
OPERATING_POINTERS = (PROJECT_ENV_VAR, ENV_VAR)

# Bounded so a held-red journal entry (and any future alert built on it) stays a
# reasonable size regardless of how chatty the candidate's test run was.
OUTPUT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class SelfUpdatePaths:
    """The self-update layout, rooted at `~/.maestro` (or `$MAESTRO_HOME`)."""

    home: Path

    @classmethod
    def from_env(cls) -> "SelfUpdatePaths":
        return cls(home=bootstrap.home())

    @property
    def versions_dir(self) -> Path:
        return self.home / "versions"

    @property
    def current_file(self) -> Path:
        return self.home / "current"

    def worktree_path(self, sha: str) -> Path:
        return self.versions_dir / sha


@dataclass
class SelfTestResult:
    """The outcome of running maestro's own suite inside a candidate worktree."""

    passed: bool
    summary: str        # the raw pass/fail line, e.g. "12 passed in 0.34s"
    output_tail: str     # bounded stdout+stderr tail, for a red-run alert/journal entry


def _git_head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _default_repo() -> Path:
    """This maestro checkout's own root (`selfupdate.py`'s grandparent directory)."""
    return Path(__file__).resolve().parent.parent


def candidate_version(repo: Path) -> str | None:
    """`repo`'s current HEAD sha, or `None` when there is nothing to adopt.

    `None` covers two distinct cases the caller must tell apart itself (this function
    deliberately does not, since it has only `state`, not the caller's intent, to go on):
    `repo`'s HEAD already matches `state["maestro_version"]`, **or** no version has ever
    been recorded (first run — `maybe_self_update` adopts that case silently, with no
    test-and-adopt ceremony, since whatever is currently running already *is* the
    candidate).
    """
    sha = _git_head(repo)
    state = read_state()
    recorded = state.get("maestro_version")
    if recorded is None or sha == recorded:
        return None
    return sha


def materialize_worktree(repo: Path, sha: str) -> Path:
    """`git worktree add` for `sha` under `~/.maestro/versions/<sha>/`.

    Idempotent: a second call for a sha that already has a worktree is a no-op, not an
    error.
    """
    paths = SelfUpdatePaths.from_env()
    target = paths.worktree_path(sha)
    if target.exists():
        return target
    paths.versions_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), sha],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return target


def _self_test_env() -> dict:
    """`os.environ` with `OPERATING_POINTERS` removed.

    `cwd=worktree` alone does **not** make the candidate's suite run against the candidate:
    `Paths.from_env()` prefers `$MAESTRO_REPO` and only falls back to `Path.cwd()` when it
    is absent, so an inherited value wins over the cwd this module sets. The result is a
    run with code from the worktree and paths from whichever project the loop is driving.
    Observed for real: a live loop held 14 consecutive candidate versions red over two days
    on one test that read the repo around it
    (`tests/test_selfupdate.py::test_self_test_does_not_leak_this_machines_operating_pointers`).

    Scrubbed rather than overridden: there is no correct project root for a checkout that
    is being *evaluated* rather than run, so the honest value is absence — which is also
    what makes `cwd=worktree` load-bearing again instead of decorative.

    `bootstrap.SENTINEL` is *set* rather than scrubbed, and it is the same defect from the
    other direction. Any `maestro` subprocess the candidate's own tests spawn would otherwise
    resolve `~/.maestro/current` and re-exec into the **deployed** checkout — so the gate
    would run the candidate's test files against the code already in production and report
    the verdict as the candidate's. A version could then be adopted on a green run that never
    executed a line of it. Pinning the sentinel keeps the code under test in the worktree
    under test, which is the entire claim `self_test` makes.
    """
    env = dict(os.environ)
    for var in OPERATING_POINTERS:
        env.pop(var, None)
    env[bootstrap.SENTINEL] = "1"
    return env


def self_test(worktree: Path) -> SelfTestResult:
    """Runs maestro's own suite inside `worktree`.

    No extra flags are passed to pytest — `worktree`'s own `pyproject.toml` (it is a full
    checkout) already carries `addopts = "-q"`, so pytest applies that on its own; adding
    a second `-q` here would silently suppress the pass/fail summary line this function
    depends on to build `summary`.

    The child's environment is built explicitly (`_self_test_env`), not inherited: this is
    the only subprocess in maestro that evaluates a checkout instead of operating on a
    project, and the difference is exactly the two variables it drops.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=worktree, capture_output=True, text=True, env=_self_test_env(),
    )
    combined = proc.stdout + proc.stderr
    summary = _last_nonempty_line(proc.stdout) or _last_nonempty_line(proc.stderr)
    return SelfTestResult(
        passed=proc.returncode == 0,
        summary=summary,
        output_tail=combined[-OUTPUT_TAIL_CHARS:],
    )


def adopt(worktree_sha: str) -> None:
    """Repoint `current` at `worktree_sha`'s worktree and record the adoption.

    **Never call this mid-task.** This function has no way to check `in_flight` itself —
    that is the caller's job, enforced (for the one real call site) by `orchestrator.py`
    only calling `maybe_self_update` from its idle branch. Assumes
    `materialize_worktree(repo, worktree_sha)` has already been called for this sha.
    """
    paths = SelfUpdatePaths.from_env()
    paths.home.mkdir(parents=True, exist_ok=True)
    worktree = paths.worktree_path(worktree_sha)
    paths.current_file.write_text(str(worktree) + "\n")

    state = read_state()
    state["maestro_version"] = worktree_sha
    write_state(state)
    append_journal("self_update_adopted", f"sha={worktree_sha} current={worktree}")


def maybe_self_update(repo: Path | None = None) -> str:
    """The one entry point a live loop should call, from an idle moment only.

    **Never raises.** A self-update failure of any kind — a missing `git`, a repo that
    is not a git checkout, a candidate worktree that will not even build — must never
    crash the caller's loop; it is reported back as an `"error:<msg>"` string instead.

    Returns one of:
      - `"up_to_date"` — `repo`'s HEAD already matches the recorded version.
      - `"adopted:<sha>"` — a new candidate (or, on first run, the currently running
        HEAD) was adopted.
      - `"held_red:<sha>"` — a candidate's own test suite failed; `current` and the
        recorded version are left exactly as they were (see `self_test`, `adopt`).
      - `"error:<msg>"` — anything else went wrong before a verdict could be reached.

    **Never call this mid-task** — see `adopt`'s docstring; this function is just as
    unable to check `in_flight` as `adopt` is.
    """
    try:
        repo = Path(repo).resolve() if repo is not None else _default_repo()
        state = read_state()
        first_run = "maestro_version" not in state

        if first_run:
            sha = _git_head(repo)
        else:
            sha = candidate_version(repo)
            if sha is None:
                return "up_to_date"

        worktree = materialize_worktree(repo, sha)

        if first_run:
            # Nothing recorded yet: whatever is running right now already *is* the
            # candidate, so there is nothing to gain by re-running its own tests against
            # itself under a different cwd. Adopt directly.
            adopt(sha)
            return f"adopted:{sha}"

        result = self_test(worktree)
        if not result.passed:
            append_journal(
                "self_update_held",
                f"sha={sha} {result.summary}\n{result.output_tail}",
            )
            return f"held_red:{sha}"

        adopt(sha)
        return f"adopted:{sha}"
    except Exception as exc:  # pragma: no cover - exercised via test_maybe_self_update_never_raises_on_error
        return f"error:{exc}"
