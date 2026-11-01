"""Self-update: hot-upgrading maestro itself via git worktrees.

DESIGN.md §9 ("Self-update (D5)") — single channel, git worktrees, adoption gated on
maestro's own test suite:

    ~/.maestro/versions/<sha>/      git worktrees of this maestro repo, one per adopted
                                     or candidate commit (materialised lazily, on demand)
    ~/.maestro/current              a plain text file — **not** a symlink — holding the
                                     absolute path of the worktree a project should import
                                     maestro through right now

`[P11]` adds a second pointer of the same shape, `<repo>/.orchestrator/current`, written
by `adopt` whenever the adopting process is operating a named project. That per-project pin
is what `maestro.bootstrap` consults first, so adopting a version while orchestrating one
project no longer moves any other project on the host (`[INV-09]`); see `adopt`.

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

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
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
#: spawn that is not orchestrating a project, so it inherits neither: the project is
#: *unset* and the home is a private empty one; see `_self_test_env`.
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

    @property
    def latest_file(self) -> Path:
        """The newest sha `release` tested green; every project follows it."""
        return self.home / "latest"

    @property
    def held_file(self) -> Path:
        """Why the newest commit was not announced (sha, summary, output tail)."""
        return self.home / "latest.held"

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


def read_latest() -> str | None:
    """The sha `release` last announced (`~/.maestro/latest`), or `None` if none yet."""
    try:
        text = SelfUpdatePaths.from_env().latest_file.read_text().strip()
    except OSError:
        return None
    return text or None


def release(repo: Path | None = None) -> str:
    """Test `repo`'s HEAD once and, if green, announce it as the version every project follows.

    The owner of "what is the newest good maestro" is the maestro checkout, not each
    project: this runs from its post-commit hook (`scripts/install-release-hook.sh`), takes
    a lock so commits in quick succession queue up and only the newest is tested, and writes
    `~/.maestro/latest`. Each project's controller adopts that sha at an idle moment
    (`maybe_self_update`) and never re-runs the suite, so N projects cost one run. Nothing
    here touches a project pin, so a controller mid-task is never moved. The suite runs
    in parallel; a red HEAD is run once more, serially, before it is held.

    Returns `up_to_date`, `announced:<sha>`, `held_red:<sha>` or `error:<msg>`; never raises.
    """
    import fcntl

    try:
        repo = Path(repo).resolve() if repo is not None else _default_repo()
        paths = SelfUpdatePaths.from_env()
        paths.home.mkdir(parents=True, exist_ok=True)
        with open(paths.home / "release.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            sha = _git_head(repo)
            if sha == read_latest():
                return "up_to_date"
            if paths.held_file.is_file() and paths.held_file.read_text().split("\n", 1)[0] == sha:
                return f"held_red:{sha}"
            worktree = materialize_worktree(repo, sha)
            result = self_test(worktree)
            if not result.passed:
                # One red run is not a verdict. The suite's slow tier shares the host with
                # live loops (tmux, watchdogs), and a held sha is never retested, so an
                # outside kill would otherwise hold a good commit until someone removed
                # the file by hand. Said on stderr (the hook's release.log) so a flaky
                # suite is not hidden by the retry.
                # The retry runs serially: less contention with those loops, and a red
                # that only the parallel run produces is not repeated the same way.
                print(f"[release] {sha[:12]} red on the first run ({result.summary}); "
                      f"retrying once, serially", file=sys.stderr)
                result = self_test(worktree, parallel=False)
            if not result.passed:
                paths.held_file.write_text(f"{sha}\n{result.summary}\n{result.output_tail}\n")
                return f"held_red:{sha}"
            tmp = paths.home / "latest.tmp"
            tmp.write_text(sha + "\n")
            os.replace(tmp, paths.latest_file)
            paths.held_file.unlink(missing_ok=True)
            return f"announced:{sha}"
    except Exception as exc:
        return f"error:{exc}"


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


def _self_test_env(private_home: Path) -> dict:
    """`os.environ` with neither of `OPERATING_POINTERS` inherited.

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

    The self-update home cannot be handled the same way, because for it absence has a
    meaning: unset is `~/.maestro`, the operator's real home. The suite's slow tier starts
    real `maestro run` processes, and those then read the machine's `latest` announcement,
    its pins and its host ledger (and could write them). So the child gets `private_home`,
    a fresh empty directory, instead.

    `bootstrap.SENTINEL` is *set* rather than scrubbed, and it is the same defect from the
    other direction. Any `maestro` subprocess the candidate's own tests spawn would otherwise
    resolve `~/.maestro/current` and re-exec into the **deployed** checkout — so the gate
    would run the candidate's test files against the code already in production and report
    the verdict as the candidate's. A version could then be adopted on a green run that never
    executed a line of it. Pinning the sentinel keeps the code under test in the worktree
    under test, which is the entire claim `self_test` makes.
    """
    env = dict(os.environ)
    env.pop(PROJECT_ENV_VAR, None)
    env[ENV_VAR] = str(private_home)
    env[bootstrap.SENTINEL] = "1"
    return env


def _xdist_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("xdist") is not None


def self_test(worktree: Path, *, parallel: bool = True) -> SelfTestResult:
    """Runs maestro's own suite inside `worktree`.

    `parallel` spreads it over the host's cores (`-n auto`) when pytest-xdist is installed:
    one core took about eleven minutes per release on a 4-core host, xdist under four. The
    suite is qualified with xdist anyway. Without xdist, or when asked, it runs serially.

    No output flag is passed to pytest — `worktree`'s own `pyproject.toml` (it is a full
    checkout) already carries `addopts = "-q"`, so pytest applies that on its own; adding
    a second `-q` here would silently suppress the pass/fail summary line this function
    depends on to build `summary`.

    The child's environment is built explicitly (`_self_test_env`), not inherited: this is
    the only subprocess in maestro that evaluates a checkout instead of operating on a
    project, and the difference is exactly the two variables it does not pass on. Its
    private home lives only for this run.
    """
    with tempfile.TemporaryDirectory(
        prefix="maestro-selftest-home-", ignore_cleanup_errors=True
    ) as private_home:
        workers = ["-n", "auto"] if parallel and _xdist_available() else []
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *workers],
            cwd=worktree, capture_output=True, text=True,
            env=_self_test_env(Path(private_home)),
        )
    combined = proc.stdout + proc.stderr
    summary = _last_nonempty_line(proc.stdout) or _last_nonempty_line(proc.stderr)
    return SelfTestResult(
        passed=proc.returncode == 0,
        summary=summary,
        output_tail=combined[-OUTPUT_TAIL_CHARS:],
    )


def adopt(worktree_sha: str, *, project_repo: Path | str | None = None) -> None:
    """Repoint the pointer that governs *this* runtime at `worktree_sha`'s worktree.

    **Which pointer, and never both (`[INV-09]`).** A process that is operating a named
    project (`--repo`, or `$MAESTRO_REPO` — `bootstrap.repo_from_argv`) writes that
    project's own pin, `<repo>/.orchestrator/current`, and leaves `~/.maestro/current`
    exactly as it was. A process operating no project writes the machine-global pointer,
    which is the host default for projects that have never pinned anything.

    The exclusivity is the invariant, not an optimisation: writing both would mean adopting
    a version while orchestrating project A silently moved every *unpinned* project on the
    host onto it too, mid-roadmap, which is the cross-project update drift this phase
    exists to end. The cost is that a host whose projects all pin stops advancing its
    global default — which is correct, because nothing runs from it.

    **Never call this mid-task.** This function has no way to check `in_flight` itself —
    that is the caller's job, enforced (for the one real call site) by `orchestrator.py`
    only calling `maybe_self_update` from its idle branch. Assumes
    `materialize_worktree(repo, worktree_sha)` has already been called for this sha.
    """
    paths = SelfUpdatePaths.from_env()
    worktree = paths.worktree_path(worktree_sha)

    repo = project_repo if project_repo is not None else bootstrap.repo_from_argv()
    pointer = bootstrap.project_pin_file(repo)
    if pointer is None:
        paths.home.mkdir(parents=True, exist_ok=True)
        pointer = paths.current_file
    else:
        pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(worktree) + "\n")

    state = read_state()
    previous = state.get("maestro_version")
    state["maestro_version"] = worktree_sha
    write_state(state)
    append_journal("self_update_adopted", f"sha={worktree_sha} current={worktree} pin={pointer}")
    if repo is not None and previous != worktree_sha:
        _amend_pilot_manifest(Path(repo), previous, worktree_sha, worktree)


def _amend_pilot_manifest(project: Path, previous: str | None, sha: str, worktree: Path) -> None:
    """Record an adoption in the manifest of the pilot `project` is in, if it is in one.

    A pilot's manifest names the code under test, and every project adopts the newest green
    release at idle by design, so an adoption during a pilot changes what its results
    measure. The amendment has the shape the P12 pilot's hand-written ones had
    (`kind: code_under_test`, `maestro_from`, `maestro_to`).

    **Never raises, and never blocks the adoption**, which has already happened: a manifest
    that is missing, is not a JSON object, or whose `amendments` is not a list is left
    exactly as it was and the journal says so. A manifest with no `amendments` yet gets
    the list.
    """
    try:
        from maestro import config

        manifest = config.pilot_manifest(project)
        if manifest is None:
            return
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
            amendments = document.setdefault("amendments", [])
            if not isinstance(amendments, list):
                raise ValueError("`amendments` is not a list")
            amendments.append({
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": "code_under_test",
                "project": project.resolve().name,
                "maestro_from": previous,
                "maestro_to": sha,
                "adopted_by": str(worktree),
                "reason": "idle self-update adopted the announced release",
                "recorded_by": "maestro",
                "criteria_or_weights_changed": False,
            })
            tmp = manifest.with_name(manifest.name + ".tmp")
            tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            os.replace(tmp, manifest)
        except Exception as exc:
            append_journal("pilot_manifest_not_amended",
                           f"manifest={manifest} from={previous} to={sha} reason={exc!r}")
            return
        append_journal("pilot_manifest_amended", f"manifest={manifest} from={previous} to={sha}")
    except Exception:
        pass


def maybe_self_update(repo: Path | None = None, *, project_repo: Path | str | None = None) -> str:
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
    def finish(status):
        project = project_repo if project_repo is not None else bootstrap.repo_from_argv()
        if project is not None:
            from maestro import config_slim, state
            # Reuse this process's existing writer; stopped-project operator calls
            # take the same lease. The slim independently proves authoritative idle.
            controller = state._CONTROL
            result = config_slim.slim_project_config(project, controller=controller)
            if result['status'] != 'unchanged' or result.get('notice_sent') is False:
                append_journal('config_slim', str(result))
        return status

    try:
        repo = Path(repo).resolve() if repo is not None else _default_repo()
        state = read_state()
        first_run = "maestro_version" not in state

        # Once the checkout announces versions (`release`), that is the only source: it was
        # tested once centrally, so a project adopts without re-running the suite, and a
        # controller running from a pinned worktree (whose own HEAD never moves) still sees
        # new commits.
        announced = read_latest()
        if announced and not first_run:
            if announced == state.get("maestro_version"):
                return finish("up_to_date")
            if not SelfUpdatePaths.from_env().worktree_path(announced).is_dir():
                return "up_to_date"
            adopt(announced, project_repo=project_repo)
            return finish(f"adopted:{announced}")

        if first_run:
            sha = _git_head(repo)
        else:
            sha = candidate_version(repo)
            if sha is None:
                return finish("up_to_date")

        worktree = materialize_worktree(repo, sha)

        if first_run:
            # Nothing recorded yet: whatever is running right now already *is* the
            # candidate, so there is nothing to gain by re-running its own tests against
            # itself under a different cwd. Adopt directly.
            adopt(sha, project_repo=project_repo)
            return finish(f"adopted:{sha}")

        result = self_test(worktree)
        if not result.passed:
            append_journal(
                "self_update_held",
                f"sha={sha} {result.summary}\n{result.output_tail}",
            )
            return f"held_red:{sha}"

        adopt(sha, project_repo=project_repo)
        return finish(f"adopted:{sha}")
    except Exception as exc:  # pragma: no cover - exercised via test_maybe_self_update_never_raises_on_error
        return f"error:{exc}"
