"""`maestro.selfupdate` — DESIGN.md §9's git-worktree self-update, tested for real.

New M3 behaviour, not an extraction, so ordinary TDD-style tests against the new module
are correct here. Everything runs against **scratch git repositories built under
`tmp_path`** — never a real maestro checkout, and `MAESTRO_HOME` is monkeypatched to a
`tmp_path` subdirectory for every test, so `~/.maestro/versions/` and `~/.maestro/current`
on the real machine are never touched.

`maestro.state.STATE_JSON` / `.JOURNAL` are monkeypatched the same way the rest of this
suite already does it (see `tests/test_switch.py::_sealed`), so state.json/journal.ndjson
land under `tmp_path` too, never in a real project's `.orchestrator/`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from maestro import selfupdate, state
from tests.hang_bound import HANG_SECONDS


# --- scratch git repo helpers -----------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)


def _commit_test_file(root: Path, *, passing: bool, message: str) -> str:
    """Write a single trivial test file and commit it; return the new HEAD sha.

    The commit message is embedded as a comment so successive calls always produce a
    genuine content change (otherwise `git commit` has nothing to commit and fails).
    """
    body = "# %s\ndef test_it():\n    assert %s\n" % (message, "True" if passing else "False")
    (root / "test_it.py").write_text(body)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", message, cwd=root)
    return _git("rev-parse", "HEAD", cwd=root).stdout.strip()


def _journal_events(journal: Path) -> list[dict]:
    if not journal.exists():
        return []
    return [json.loads(ln) for ln in journal.read_text().splitlines() if ln.strip()]


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    """State/journal and the self-update home all live under `tmp_path`."""
    # These scenarios exercise global adoption. An inherited project setting would
    # redirect adopt() to a project pin while their assertions read the global one.
    monkeypatch.delenv("MAESTRO_REPO", raising=False)
    project = tmp_path / "project" / ".orchestrator"
    project.mkdir(parents=True)
    monkeypatch.setattr(state, "STATE_JSON", project / "state.json")
    monkeypatch.setattr(state, "JOURNAL", project / "journal.ndjson")
    # read_state() requires the file to already exist (verbatim M1 behaviour) — seed an
    # empty-but-valid document with no `maestro_version` key, i.e. the "first run" shape.
    state.write_state({})
    monkeypatch.setenv(selfupdate.ENV_VAR, str(tmp_path / "maestro_home"))
    yield


# --- candidate_version -------------------------------------------------------------------


def test_candidate_version_is_none_before_any_version_is_recorded(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_test_file(repo, passing=True, message="v1")
    assert selfupdate.candidate_version(repo) is None


def test_candidate_version_is_none_when_head_matches_the_recorded_version(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha)
    selfupdate.adopt(sha)
    assert selfupdate.candidate_version(repo) is None


def test_candidate_version_returns_the_new_head_sha(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha1)
    selfupdate.adopt(sha1)

    sha2 = _commit_test_file(repo, passing=True, message="v2")
    assert selfupdate.candidate_version(repo) == sha2


# --- materialize_worktree -----------------------------------------------------------------


def test_materialize_worktree_checks_out_the_requested_sha(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")

    worktree = selfupdate.materialize_worktree(repo, sha)

    assert worktree.exists()
    assert (worktree / "test_it.py").read_text() == "# v1\ndef test_it():\n    assert True\n"
    paths = selfupdate.SelfUpdatePaths.from_env()
    assert worktree == paths.worktree_path(sha)


def test_materialize_worktree_is_idempotent(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")

    first = selfupdate.materialize_worktree(repo, sha)
    second = selfupdate.materialize_worktree(repo, sha)

    assert first == second
    assert second.exists()


# --- self_test -------------------------------------------------------------------------


def test_self_test_reports_a_passing_worktree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")
    worktree = selfupdate.materialize_worktree(repo, sha)

    result = selfupdate.self_test(worktree)

    assert result.passed is True
    assert "passed" in result.summary


def test_self_test_reports_a_failing_worktree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=False, message="v1-broken")
    worktree = selfupdate.materialize_worktree(repo, sha)

    result = selfupdate.self_test(worktree)

    assert result.passed is False
    assert "failed" in result.summary


def test_self_test_does_not_leak_this_machines_operating_pointers(tmp_path, monkeypatch):
    """The candidate's suite runs against **its own checkout**, never the project this
    orchestrator happens to be driving.

    `self_test` passes `cwd=worktree` to say exactly that, but `Paths.from_env()` prefers
    `$MAESTRO_REPO` over cwd — and every live launch path sets that variable to the
    consuming project on purpose (`templates/launch.sh.tmpl`, `watchdog.py`'s two tmux
    spawns, every `cmd_*` in `cli.py`), because a fresh tmux window inherits the tmux
    *server's* environment rather than its parent's. Inheriting it here silently repoints
    the candidate's own tests at that project's filesystem: code from the worktree, paths
    from somewhere else.

    That is not hypothetical. From 2026-08-31 a live loop held **14 consecutive** candidate
    versions red on one test asserting the repo under test ships no
    `scripts/check_notebook.py` — true of every maestro checkout, false of the project that
    loop was driving. The gate was reading the wrong repo, so that project stayed pinned to
    a version from 2026-08-26 for two days.

    `MAESTRO_HOME` names the same kind of thing (*where this machine's maestro is
    operating*), but absence is not a safe value for it: unset means `~/.maestro`, the
    operator's real home. See the next test.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "test_it.py").write_text(
        "import os\n"
        "def test_no_operating_pointers():\n"
        "    assert os.environ.get('MAESTRO_REPO') is None\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "env probe", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    worktree = selfupdate.materialize_worktree(repo, sha)

    # What the live watchdog exports; `_sealed` above already exports MAESTRO_HOME.
    monkeypatch.setenv("MAESTRO_REPO", str(tmp_path / "some" / "other" / "project"))

    result = selfupdate.self_test(worktree)

    assert result.passed is True, result.output_tail


def test_self_test_gives_the_candidate_a_private_empty_home(tmp_path):
    """The candidate's suite never reads or writes the operator's `~/.maestro`.

    The suite's slow tier starts real `maestro run` processes. With `MAESTRO_HOME` unset
    they resolved the real home: its `latest` announcement, its version pins, its host
    ledger. A release being judged must not see the machine it is being judged on, in
    either direction, so the child gets a fresh empty home that is removed afterwards.
    """
    operator_home = Path(os.environ[selfupdate.ENV_VAR])
    operator_home.mkdir(parents=True, exist_ok=True)
    (operator_home / "latest").write_text("0" * 40 + "\n")
    repo = tmp_path / "repo"
    _init_repo(repo)
    seen = tmp_path / "seen_home.txt"
    (repo / "test_it.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def test_private_home():\n"
        "    home = Path(os.environ['MAESTRO_HOME'])\n"
        f"    Path({str(seen)!r}).write_text(str(home))\n"
        f"    assert home.resolve() != Path({str(operator_home)!r}).resolve()\n"
        "    assert home.is_dir() and list(home.iterdir()) == []\n"
        "    (home / 'scribble').write_text('left behind')\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "home probe", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    worktree = selfupdate.materialize_worktree(repo, sha)

    result = selfupdate.self_test(worktree)

    assert result.passed is True, result.output_tail
    assert not Path(seen.read_text()).exists()          # removed with what the suite left
    assert sorted(p.name for p in operator_home.iterdir()) == ["latest", "versions"]
    assert os.environ[selfupdate.ENV_VAR] == str(operator_home)


def _worker_probe_worktree(tmp_path: Path, monkeypatch, *, expect_worker: bool) -> Path:
    # This suite itself may run under xdist; the child would inherit our worker's name.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    check = "is not None" if expect_worker else "is None"
    (repo / "test_it.py").write_text(
        "import os\n"
        "def test_worker():\n"
        f"    assert os.environ.get('PYTEST_XDIST_WORKER') {check}\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "worker probe", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return selfupdate.materialize_worktree(repo, sha)


def test_self_test_spreads_the_suite_over_the_hosts_cores(tmp_path, monkeypatch):
    """A release ran the whole suite on one core: about eleven minutes on this 4-core host
    (2026-10-14, `33a170d`), against under four with xdist. The gate runs it in parallel."""
    worktree = _worker_probe_worktree(tmp_path, monkeypatch, expect_worker=True)

    result = selfupdate.self_test(worktree)

    assert result.passed is True, result.output_tail
    assert "passed" in result.summary


@pytest.mark.parametrize("how", ["asked", "no_xdist"])
def test_self_test_runs_serially_when_asked_or_without_xdist(tmp_path, monkeypatch, how):
    worktree = _worker_probe_worktree(tmp_path, monkeypatch, expect_worker=False)
    if how == "no_xdist":
        monkeypatch.setattr(selfupdate, "_xdist_available", lambda: False)

    result = selfupdate.self_test(worktree, parallel=how != "asked")

    assert result.passed is True, result.output_tail


def test_self_test_pins_the_candidate_in_place_against_the_adopted_version(tmp_path):
    """The gate must grade the candidate, not whatever is already deployed.

    `maestro.bootstrap` redirects any `maestro` invocation into the checkout named by
    `~/.maestro/current`. A candidate's own suite spawns real `maestro` subprocesses (the
    slow tier in `tests/test_cli.py`), so without `bootstrap.SENTINEL` those would re-exec
    into the **deployed** version and the gate would report its verdict as the candidate's —
    adopting a version on a green run that never executed a line of it. The failure mode is
    the same one that made this gate read the wrong repo, one level up.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "test_it.py").write_text(
        "import os\n"
        "def test_pinned_in_place():\n"
        "    assert os.environ.get('MAESTRO_BOOTSTRAPPED')\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "sentinel probe", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    worktree = selfupdate.materialize_worktree(repo, sha)

    result = selfupdate.self_test(worktree)

    assert result.passed is True, result.output_tail


# --- maybe_self_update: the Done-when scenarios -----------------------------------------


def test_first_run_adopts_silently_with_no_self_test_ceremony(tmp_path):
    """No version recorded yet: adopt HEAD directly, even though its own tests fail.

    This is the literal "no test-and-adopt ceremony needed" case: the candidate is
    whatever is already running, so `self_test` is never invoked for it at all — proven
    here by giving it a deliberately failing test and confirming adoption happens anyway.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=False, message="v1-broken-but-first-run")

    result = selfupdate.maybe_self_update(repo)

    assert result == f"adopted:{sha}"
    assert state.read_state()["maestro_version"] == sha

    paths = selfupdate.SelfUpdatePaths.from_env()
    assert paths.current_file.read_text().strip() == str(paths.worktree_path(sha))

    events = _journal_events(state.JOURNAL)
    assert [e["event"] for e in events] == ["self_update_adopted"]
    assert sha in events[0]["detail"]


def test_a_passing_candidate_adopts_cleanly(tmp_path):
    """The component's headline Done-when: a real candidate, tested for real, adopted."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha1)
    selfupdate.adopt(sha1)  # simulate an already-adopted baseline, out of band

    sha2 = _commit_test_file(repo, passing=True, message="v2")

    result = selfupdate.maybe_self_update(repo)

    assert result == f"adopted:{sha2}"
    assert state.read_state()["maestro_version"] == sha2

    paths = selfupdate.SelfUpdatePaths.from_env()
    assert paths.current_file.read_text().strip() == str(paths.worktree_path(sha2))

    events = _journal_events(state.JOURNAL)
    adopted = [e for e in events if e["event"] == "self_update_adopted"]
    assert len(adopted) == 2  # the out-of-band baseline adopt, then sha2
    assert sha2 in adopted[-1]["detail"]


def test_a_red_candidate_is_held_and_current_stays_put(tmp_path):
    """The literal M3 Done-when: a deliberately-red candidate is refused, current unchanged.

    Run for real — a scratch repo, a real `git worktree add`, a real `pytest` subprocess
    against a candidate whose test file was edited to fail — not asserted from reading
    the code.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha1)
    selfupdate.adopt(sha1)

    paths = selfupdate.SelfUpdatePaths.from_env()
    current_before = paths.current_file.read_text()
    state_before = state.read_state()

    sha2 = _commit_test_file(repo, passing=False, message="v2-broken")

    result = selfupdate.maybe_self_update(repo)  # must not raise

    assert result == f"held_red:{sha2}"
    assert paths.current_file.read_text() == current_before
    assert state.read_state()["maestro_version"] == state_before["maestro_version"] == sha1

    events = _journal_events(state.JOURNAL)
    held = [e for e in events if e["event"] == "self_update_held"]
    assert len(held) == 1
    assert sha2 in held[0]["detail"]
    assert not any(e["event"] == "self_update_adopted" and sha2 in e["detail"] for e in events)


def test_up_to_date_does_no_rework(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha)
    selfupdate.adopt(sha)

    result = selfupdate.maybe_self_update(repo)

    assert result == "up_to_date"


def test_maybe_self_update_never_raises_on_a_broken_repo(tmp_path):
    """Not a git repo at all: `git rev-parse HEAD` fails; must degrade to a string."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    result = selfupdate.maybe_self_update(not_a_repo)

    assert result.startswith("error:")


# --- release: the checkout announces the newest green commit -----------------------------


def test_release_announces_a_green_head(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")

    assert selfupdate.release(repo) == f"announced:{sha}"

    paths = selfupdate.SelfUpdatePaths.from_env()
    assert selfupdate.read_latest() == sha
    assert paths.worktree_path(sha).is_dir()
    assert not paths.held_file.exists()


def test_release_holds_a_red_head_and_keeps_the_last_good_one(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    good = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.release(repo)
    bad = _commit_test_file(repo, passing=False, message="v2-broken")

    assert selfupdate.release(repo) == f"held_red:{bad}"

    paths = selfupdate.SelfUpdatePaths.from_env()
    assert selfupdate.read_latest() == good
    held = paths.held_file.read_text().splitlines()
    assert held[0] == bad and "failed" in held[1]


def _scripted_self_test(monkeypatch, *verdicts: bool, modes: list | None = None) -> list[Path]:
    """Replace the suite run with scripted verdicts; returns the worktrees it was asked for.
    `modes`, if given, collects each run's `parallel` flag."""
    calls: list[Path] = []
    script = list(verdicts)

    def fake(worktree, *, parallel=True):
        calls.append(worktree)
        if modes is not None:
            modes.append(parallel)
        if not script:
            raise AssertionError("the suite ran more often than this test allows")
        passed = script.pop(0)
        return selfupdate.SelfTestResult(
            passed=passed, summary="9 passed" if passed else f"1 failed (run {len(calls)})",
            output_tail="tail")
    monkeypatch.setattr(selfupdate, "self_test", fake)
    return calls


def test_release_retries_a_red_head_once_and_announces_it_when_the_retry_is_green(
    tmp_path, monkeypatch, capsys
):
    """One red run is not a verdict: the suite's slow tier shares the host with live loops.

    2026-10-09 a stale watchdog killed every `maestro run` on the machine, which held a
    green commit red; a held sha is never retested, so it stayed held until someone removed
    the file by hand. The first red is reported, so a flaky suite is not hidden by the
    retry.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")
    modes: list = []
    calls = _scripted_self_test(monkeypatch, False, True, modes=modes)

    assert selfupdate.release(repo) == f"announced:{sha}"

    paths = selfupdate.SelfUpdatePaths.from_env()
    assert calls == [paths.worktree_path(sha)] * 2
    # The first run is spread over the cores; the retry runs serially, so a red caused by
    # contention or by the parallel run itself is not repeated the same way.
    assert modes == [True, False]
    assert selfupdate.read_latest() == sha
    assert not paths.held_file.exists()
    err = capsys.readouterr().err
    assert sha[:12] in err and "1 failed (run 1)" in err and "retrying once" in err


def test_release_really_runs_a_red_suite_again_and_announces_the_green_retry(tmp_path, capsys):
    """The retry with nothing scripted: two real suite runs in the version's worktree.

    The head's one test fails the first time it runs and passes after, the way a suite
    that a live loop disturbed does.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    runs = tmp_path / "runs.log"
    (repo / "test_it.py").write_text(
        "from pathlib import Path\n"
        f"RUNS = Path({str(runs)!r})\n"
        "def test_it():\n"
        "    earlier = RUNS.read_text() if RUNS.exists() else ''\n"
        "    RUNS.write_text(earlier + str(Path.cwd()) + '\\n')\n"
        "    assert earlier\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "red once", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    assert selfupdate.release(repo) == f"announced:{sha}"

    paths = selfupdate.SelfUpdatePaths.from_env()
    assert runs.read_text().splitlines() == [str(paths.worktree_path(sha))] * 2
    assert selfupdate.read_latest() == sha
    assert not paths.held_file.exists()
    err = capsys.readouterr().err
    assert sha[:12] in err and "1 failed" in err and "retrying once" in err


def test_release_holds_a_head_that_is_red_twice_and_does_not_try_a_third_time(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    bad = _commit_test_file(repo, passing=False, message="v1-broken")
    calls = _scripted_self_test(monkeypatch, False, False)

    assert selfupdate.release(repo) == f"held_red:{bad}"

    assert len(calls) == 2
    held = selfupdate.SelfUpdatePaths.from_env().held_file.read_text().splitlines()
    assert held[0] == bad and held[1] == "1 failed (run 2)"   # the verdict that held it
    assert selfupdate.read_latest() is None


def test_release_runs_the_suite_once_for_a_green_head(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit_test_file(repo, passing=True, message="v1")
    calls = _scripted_self_test(monkeypatch, True)

    assert selfupdate.release(repo) == f"announced:{sha}"
    assert len(calls) == 1
    assert capsys.readouterr().err == ""


def test_release_does_not_retest_a_sha_it_already_judged(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_test_file(repo, passing=True, message="v1")
    selfupdate.release(repo)
    bad = _commit_test_file(repo, passing=False, message="v2-broken")
    selfupdate.release(repo)

    def no_rerun(_worktree):
        raise AssertionError("a judged sha must not run the suite again")
    monkeypatch.setattr(selfupdate, "self_test", no_rerun)

    assert selfupdate.release(repo) == f"held_red:{bad}"
    _git("reset", "-q", "--hard", "HEAD~1", cwd=repo)
    assert selfupdate.release(repo) == "up_to_date"  # back on the announced sha


def test_release_waits_for_a_running_release_then_tests_the_newest_head(tmp_path):
    """Commits in quick succession queue on the lock; the waiter tests HEAD as it is then."""
    import fcntl
    import threading

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_test_file(repo, passing=True, message="v1")
    paths = selfupdate.SelfUpdatePaths.from_env()
    paths.home.mkdir(parents=True, exist_ok=True)

    results = []
    with open(paths.home / "release.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiter = threading.Thread(target=lambda: results.append(selfupdate.release(repo)))
        waiter.start()
        waiter.join(timeout=1.0)
        assert waiter.is_alive() and not results, "release ran while another held the lock"
        newest = _commit_test_file(repo, passing=True, message="v2")
    waiter.join(timeout=HANG_SECONDS)

    assert results == [f"announced:{newest}"]


# --- maybe_self_update follows the announced version -------------------------------------


def test_an_announced_version_is_adopted_without_rerunning_the_suite(tmp_path, monkeypatch):
    """The controller's own checkout is a pinned worktree whose HEAD never moves; it still
    follows `latest`, and trusts the central test run instead of repeating it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _commit_test_file(repo, passing=True, message="v1")
    running = selfupdate.materialize_worktree(repo, sha1)
    selfupdate.adopt(sha1)
    sha2 = _commit_test_file(repo, passing=True, message="v2")
    selfupdate.release(repo)

    def no_rerun(_worktree):
        raise AssertionError("an announced version was already tested")
    monkeypatch.setattr(selfupdate, "self_test", no_rerun)

    assert selfupdate.maybe_self_update(running) == f"adopted:{sha2}"
    assert state.read_state()["maestro_version"] == sha2
    assert selfupdate.maybe_self_update(running) == "up_to_date"


def test_an_announced_version_moves_only_the_operating_projects_pin(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha1)
    selfupdate.adopt(sha1)
    sha2 = _commit_test_file(repo, passing=True, message="v2")
    selfupdate.release(repo)
    project = tmp_path / "project"

    assert selfupdate.maybe_self_update(repo, project_repo=project) == f"adopted:{sha2}"

    paths = selfupdate.SelfUpdatePaths.from_env()
    pin = project / ".orchestrator" / "current"
    assert pin.read_text().strip() == str(paths.worktree_path(sha2))


# --- a pilot's manifest records each idle adoption (G7 item 7) ---------------------------


def _two_versions(tmp_path) -> tuple[Path, str, str]:
    """A maestro repo running `sha1` with a green `sha2` announced."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _commit_test_file(repo, passing=True, message="v1")
    selfupdate.materialize_worktree(repo, sha1)
    selfupdate.adopt(sha1)
    sha2 = _commit_test_file(repo, passing=True, message="v2")
    selfupdate.release(repo)
    return repo, sha1, sha2


def _piloted_project(tmp_path, manifest_ref: str) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.yaml").write_text(f"pilot:\n  manifest: {manifest_ref}\n")
    return project


def test_an_idle_adoption_is_recorded_in_the_projects_pilot_manifest(tmp_path):
    """A pilot's manifest names the code under test; an idle adoption changes that code.

    Every project adopts the newest green release at idle by design, so during the P12
    pilot each adoption had to be written into the manifest by hand afterwards, and one
    that nobody noticed left results attributed to the wrong maestro. A project that
    declares `pilot.manifest` gets the amendment written by the adoption itself.
    """
    repo, sha1, sha2 = _two_versions(tmp_path)
    project = _piloted_project(tmp_path, "pilot/manifest.json")     # relative to the project
    manifest = project / "pilot" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({
        "phase": "a pilot", "maestro": {"head_sha": sha1},
        "amendments": [{"kind": "operator_config", "reason": "written by hand"}],
    }))

    assert selfupdate.maybe_self_update(repo, project_repo=project) == f"adopted:{sha2}"
    assert selfupdate.maybe_self_update(repo, project_repo=project) == "up_to_date"

    document = json.loads(manifest.read_text())
    assert document["phase"] == "a pilot" and document["maestro"] == {"head_sha": sha1}
    by_hand, recorded = document["amendments"]          # one entry for one adoption
    assert by_hand == {"kind": "operator_config", "reason": "written by hand"}
    at = recorded.pop("at")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", at)
    assert recorded == {
        "kind": "code_under_test",
        "project": "project",
        "maestro_from": sha1,
        "maestro_to": sha2,
        "adopted_by": str(selfupdate.SelfUpdatePaths.from_env().worktree_path(sha2)),
        "reason": "idle self-update adopted the announced release",
        "recorded_by": "maestro",
        "criteria_or_weights_changed": False,
    }
    assert [e["event"] for e in _journal_events(state.JOURNAL)].count("pilot_manifest_amended") == 1


def test_a_project_that_declares_no_pilot_adopts_without_writing_a_manifest(tmp_path):
    repo, _sha1, sha2 = _two_versions(tmp_path)
    project = tmp_path / "project"
    (project / "project.yaml").write_text("priority: 2\n")
    before = sorted(p.name for p in project.iterdir())

    assert selfupdate.maybe_self_update(repo, project_repo=project) == f"adopted:{sha2}"

    assert sorted(p.name for p in project.iterdir()) == before
    events = [e["event"] for e in _journal_events(state.JOURNAL)]
    assert "pilot_manifest_amended" not in events and "pilot_manifest_not_amended" not in events


def test_a_pilot_manifest_with_no_amendments_yet_gets_its_first(tmp_path):
    """A new pilot's manifest has not been amended; the first adoption starts the list."""
    repo, sha1, sha2 = _two_versions(tmp_path)
    project = _piloted_project(tmp_path, "manifest.json")
    (project / "manifest.json").write_text(json.dumps({"phase": "a pilot"}))

    assert selfupdate.maybe_self_update(repo, project_repo=project) == f"adopted:{sha2}"

    document = json.loads((project / "manifest.json").read_text())
    assert document["phase"] == "a pilot"
    assert [(a["kind"], a["maestro_to"]) for a in document["amendments"]] == [("code_under_test", sha2)]


@pytest.mark.parametrize("content", [
    None,                                   # the declared file does not exist
    "{not json",
    json.dumps(["a", "list"]),
    json.dumps({"amendments": "not a list"}),
])
def test_an_unusable_pilot_manifest_never_blocks_the_adoption(tmp_path, content):
    """The adoption is the operation; the manifest is its record. A manifest the loop
    cannot amend is left byte for byte as it was and said in the journal."""
    repo, _sha1, sha2 = _two_versions(tmp_path)
    manifest = tmp_path / "pilots" / "manifest.json"
    manifest.parent.mkdir()
    if content is not None:
        manifest.write_text(content)
    project = _piloted_project(tmp_path, str(manifest))               # absolute

    assert selfupdate.maybe_self_update(repo, project_repo=project) == f"adopted:{sha2}"

    pin = project / ".orchestrator" / "current"
    assert pin.read_text().strip() == str(selfupdate.SelfUpdatePaths.from_env().worktree_path(sha2))
    assert (manifest.read_text() if manifest.exists() else None) == content
    (refused,) = [e for e in _journal_events(state.JOURNAL)
                  if e["event"] == "pilot_manifest_not_amended"]
    assert str(manifest) in refused["detail"] and sha2 in refused["detail"]
