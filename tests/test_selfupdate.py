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
import subprocess
from pathlib import Path

import pytest

from maestro import selfupdate, state


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
