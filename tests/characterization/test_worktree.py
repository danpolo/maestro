"""Pinned behaviour of the tmux-window / git-worktree layer.

Characterisation, not specification. Every function here shells out, so the tests use two
techniques and never touch a live system:

* the git worktree lifecycle runs against a throwaway repo created with ``git init`` inside
  the sandbox tree (the ``sandbox`` fixture has already repointed the subject's ``REPO`` at
  ``tmp_path``, and ``create_worktree`` / ``remove_worktree`` run git with ``cwd=REPO``);
* the tmux helpers get a fake ``subprocess.run`` and are asserted on argv, on the keyword
  arguments, and on how return codes and output are interpreted. No tmux server is started.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.maestro_module("worktree")


# --- fakes -------------------------------------------------------------------------


class _Recorder:
    """Stand-in for subprocess.run that records argv/kwargs and replays canned results."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[SimpleNamespace] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(SimpleNamespace(argv=list(argv), kwargs=dict(kwargs)))
        return self._handler(list(argv), dict(kwargs))

    @property
    def argvs(self) -> list[list[str]]:
        return [c.argv for c in self.calls]


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _tmux_fake(monkeypatch, handler) -> _Recorder:
    recorder = _Recorder(handler)
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


def _windows_then_pane(windows: str, pane: str, pane_rc: int = 0):
    """Handler answering both `tmux list-windows` and `tmux capture-pane`."""

    def handler(argv, kwargs):
        if "list-windows" in argv:
            return _completed(argv, 0, windows)
        if "capture-pane" in argv:
            return _completed(argv, pane_rc, pane)
        raise AssertionError(f"unexpected tmux call: {argv}")

    return handler


# --- throwaway git repo ------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(subject, sandbox, tmp_path, monkeypatch) -> Path:
    """`subject.REPO` (already rebased into tmp_path) turned into a real git repo.

    The environment is pinned so the operator's global git config cannot influence the
    result, and so the subject's own un-configured `git` invocations stay isolated too.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")

    repo = subject.REPO
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _worktree_list(repo: Path) -> str:
    return _git(repo, "worktree", "list", "--porcelain").stdout


def _branches(repo: Path) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout
    return out.split()


# --- tmux_window_exists ------------------------------------------------------------


def test_tmux_window_exists_argv_and_flags(subject, sandbox, monkeypatch):
    rec = _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "w1\n"))
    subject.tmux_window_exists("w1")
    assert rec.argvs == [
        ["tmux", "list-windows", "-t", subject.TMUX_SESSION, "-F", "#{window_name}"]
    ]
    assert rec.calls[0].kwargs == {"capture_output": True, "text": True}


def test_tmux_window_exists_true_when_listed(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "a\nw1\nb\n"))
    assert subject.tmux_window_exists("w1") is True


def test_tmux_window_exists_false_when_absent(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "a\nb\n"))
    assert subject.tmux_window_exists("w1") is False


def test_tmux_window_exists_matches_whole_line_not_substring(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "w1-extra\n"))
    assert subject.tmux_window_exists("w1") is False


def test_tmux_window_exists_ignores_non_zero_returncode(subject, sandbox, monkeypatch):
    """Surprise: the exit status is never inspected — only stdout is."""
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 1, "w1\n", "boom"))
    assert subject.tmux_window_exists("w1") is True


def test_tmux_window_exists_no_server_reads_as_absent(subject, sandbox, monkeypatch):
    """Surprise: "tmux is not running" is indistinguishable from "window has exited"."""
    _tmux_fake(
        monkeypatch,
        lambda argv, kw: _completed(argv, 1, "", "no server running on /tmp/tmux-1000/default"),
    )
    assert subject.tmux_window_exists("w1") is False


def test_tmux_window_exists_empty_name_false_for_normal_output(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "a\nb\n"))
    assert subject.tmux_window_exists("") is False


def test_tmux_window_exists_empty_name_true_when_output_has_blank_line(
    subject, sandbox, monkeypatch
):
    """Surprise: an empty window name matches a blank line in tmux's output."""
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "a\n\nb\n"))
    assert subject.tmux_window_exists("") is True


def test_tmux_window_exists_propagates_missing_tmux_binary(subject, sandbox, monkeypatch):
    """Surprise: no try/except — a host without tmux raises out of a predicate."""

    def boom(argv, **kwargs):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        subject.tmux_window_exists("w1")


# --- worktree_path_for -------------------------------------------------------------


def _prefix(subject) -> str:
    """The constant half of the worktree path, read off the subject rather than hardcoded."""
    return str(subject.worktree_path_for(""))


def test_worktree_path_for_is_prefix_plus_task_id(subject, sandbox):
    assert subject.worktree_path_for("T1") == Path(_prefix(subject) + "T1")


def test_worktree_path_for_is_absolute_and_outside_any_repo(subject, sandbox):
    """Surprise: the path is a hardcoded scratch location, not a module-level Path global.

    Nothing about it is derived from REPO, so the sandbox cannot rebase it: the value is
    identical inside and outside a test.
    """
    path = subject.worktree_path_for("T1")
    assert path.is_absolute()
    assert path.parent == Path("/tmp")
    assert subject.REPO not in path.parents


def test_worktree_path_for_is_deterministic_and_task_scoped(subject, sandbox):
    assert subject.worktree_path_for("T1") == subject.worktree_path_for("T1")
    assert subject.worktree_path_for("T1") != subject.worktree_path_for("T2")


def test_worktree_path_for_creates_nothing(subject, sandbox):
    assert not subject.worktree_path_for("no-such-task-id-9f3a").exists()


def test_worktree_path_for_does_not_sanitise_the_task_id(subject, sandbox):
    """Surprise: a task id containing a separator silently escapes the flat namespace."""
    nested = subject.worktree_path_for("a/b")
    assert nested.name == "b"
    assert nested.parent != Path("/tmp")


# --- create_worktree ---------------------------------------------------------------


def test_create_worktree_creates_registered_worktree_and_branch(subject, git_repo, tmp_path):
    wt = tmp_path / "wt-1"
    subject.create_worktree("T1", wt, "impl/T1")
    assert (wt / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    assert str(wt.resolve()) in _worktree_list(git_repo)
    assert "impl/T1" in _branches(git_repo)


def test_create_worktree_ignores_its_task_id_argument(subject, git_repo, tmp_path):
    """Surprise: `task_id` is accepted and never used — the path comes only from `worktree`."""
    wt = tmp_path / "unrelated-name"
    subject.create_worktree("T1", wt, "impl/T1")
    assert wt.is_dir()
    assert not subject.worktree_path_for("T1").exists()


def test_create_worktree_runs_prune_then_add_when_path_is_free(subject, git_repo, tmp_path):
    real_run = subprocess.run
    rec = _Recorder(lambda argv, kw: real_run(argv, **kw))
    wt = tmp_path / "wt-2"
    try:
        subprocess.run = rec
        subject.create_worktree("T1", wt, "impl/T1")
    finally:
        subprocess.run = real_run
    assert rec.argvs == [
        ["git", "worktree", "prune"],
        ["git", "worktree", "add", "-b", "impl/T1", str(wt)],
    ]
    assert [c.kwargs["cwd"] for c in rec.calls] == [str(subject.REPO)] * 2


def test_create_worktree_removes_an_existing_path_first(subject, git_repo, tmp_path):
    real_run = subprocess.run
    rec = _Recorder(lambda argv, kw: real_run(argv, **kw))
    wt = tmp_path / "wt-3"
    wt.mkdir()
    (wt / "stray.txt").write_text("junk", encoding="utf-8")
    try:
        subprocess.run = rec
        subject.create_worktree("T1", wt, "impl/T1")
    finally:
        subprocess.run = real_run
    assert rec.argvs[0] == ["git", "worktree", "remove", "--force", str(wt)]
    assert rec.argvs[1] == ["git", "worktree", "prune"]
    assert not (wt / "stray.txt").exists()
    assert (wt / "seed.txt").is_file()


def test_create_worktree_prunes_a_stale_registration(subject, git_repo, tmp_path):
    """The documented reboot case: /tmp wiped, so the directory is gone but git still has it.

    `.exists()` is False, so the remove branch is skipped; the unconditional prune is what
    lets `git worktree add` succeed a second time at the same path.
    """
    wt = tmp_path / "wt-4"
    subject.create_worktree("T1", wt, "impl/T1a")
    shutil.rmtree(wt)
    assert str(wt.resolve()) in _worktree_list(git_repo)  # still registered
    subject.create_worktree("T1", wt, "impl/T1b")
    assert (wt / "seed.txt").is_file()


def test_create_worktree_raises_when_branch_already_exists(subject, git_repo, tmp_path):
    subject.create_worktree("T1", tmp_path / "wt-5a", "impl/T1")
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subject.create_worktree("T1", tmp_path / "wt-5b", "impl/T1")
    assert excinfo.value.returncode != 0
    assert not (tmp_path / "wt-5b").exists()


def test_create_worktree_failure_captures_git_output_instead_of_printing_it(
    subject, git_repo, tmp_path, capfd
):
    subject.create_worktree("T1", tmp_path / "wt-6a", "impl/T1")
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subject.create_worktree("T1", tmp_path / "wt-6b", "impl/T1")
    assert excinfo.value.stderr  # captured on the exception...
    out, err = capfd.readouterr()
    assert "impl/T1" not in err  # ...and not leaked to the orchestrator's own stderr


def test_create_worktree_raises_when_repo_is_not_a_git_repo(subject, sandbox, tmp_path):
    """No git repo at REPO at all: `git worktree add` exits non-zero and check=True bites."""
    subject.REPO.mkdir(parents=True, exist_ok=True)
    with pytest.raises(subprocess.CalledProcessError):
        subject.create_worktree("T1", tmp_path / "wt-7", "impl/T1")


# --- remove_worktree ---------------------------------------------------------------


def test_remove_worktree_deletes_and_deregisters(subject, git_repo, tmp_path):
    wt = tmp_path / "wt-10"
    subject.create_worktree("T1", wt, "impl/T1")
    subject.remove_worktree(wt)
    assert not wt.exists()
    assert str(wt.resolve()) not in _worktree_list(git_repo)


def test_remove_worktree_leaves_the_branch_behind(subject, git_repo, tmp_path):
    """Surprise: only the checkout is removed; the branch ref accumulates forever."""
    wt = tmp_path / "wt-11"
    subject.create_worktree("T1", wt, "impl/T1")
    subject.remove_worktree(wt)
    assert "impl/T1" in _branches(git_repo)


def test_recreating_the_same_branch_after_removal_raises(subject, git_repo, tmp_path):
    """Consequence of the leaked branch: the retry path cannot reuse the branch name."""
    wt = tmp_path / "wt-12"
    subject.create_worktree("T1", wt, "impl/T1")
    subject.remove_worktree(wt)
    with pytest.raises(subprocess.CalledProcessError):
        subject.create_worktree("T1", wt, "impl/T1")


def test_remove_worktree_on_missing_path_is_silent(subject, git_repo, tmp_path):
    subject.remove_worktree(tmp_path / "never-existed")  # no exception


def test_remove_worktree_deletes_a_directory_git_never_knew_about(subject, git_repo, tmp_path):
    """Surprise: the git failure is swallowed and the rmtree runs anyway.

    `remove_worktree` will therefore delete any directory it is handed, worktree or not.
    """
    stranger = tmp_path / "not-a-worktree"
    stranger.mkdir()
    (stranger / "precious.txt").write_text("data", encoding="utf-8")
    subject.remove_worktree(stranger)
    assert not stranger.exists()


def test_remove_worktree_silently_leaves_a_file_in_place(subject, git_repo, tmp_path):
    """Surprise: rmtree(ignore_errors=True) swallows NotADirectoryError — nothing happens."""
    victim = tmp_path / "a-file"
    victim.write_text("data", encoding="utf-8")
    subject.remove_worktree(victim)
    assert victim.is_file()


def test_remove_worktree_argv_and_cwd(subject, git_repo, tmp_path):
    wt = tmp_path / "wt-13"
    subject.create_worktree("T1", wt, "impl/T1")
    real_run = subprocess.run
    rec = _Recorder(lambda argv, kw: real_run(argv, **kw))
    try:
        subprocess.run = rec
        subject.remove_worktree(wt)
    finally:
        subprocess.run = real_run
    assert rec.argvs == [["git", "worktree", "remove", "--force", str(wt)]]
    assert rec.calls[0].kwargs["cwd"] == str(subject.REPO)
    assert rec.calls[0].kwargs["capture_output"] is True


# --- _kill_tmux_window -------------------------------------------------------------


def test_kill_tmux_window_argv_and_flags(subject, sandbox, monkeypatch):
    rec = _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0))
    assert subject._kill_tmux_window("w1") is None
    assert rec.argvs == [["tmux", "kill-window", "-t", f"{subject.TMUX_SESSION}:w1"]]
    assert rec.calls[0].kwargs == {"capture_output": True}


def test_kill_tmux_window_ignores_failure(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 1, "", "can't find window"))
    assert subject._kill_tmux_window("gone") is None


def test_kill_tmux_window_empty_name_targets_the_whole_session(subject, sandbox, monkeypatch):
    """Surprise: no guard on the window name, so a blank one targets `session:`.

    tmux resolves that to the session's *current* window, i.e. an unrelated one.
    """
    rec = _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0))
    subject._kill_tmux_window("")
    assert rec.argvs[0][-1] == f"{subject.TMUX_SESSION}:"


def test_kill_tmux_window_propagates_missing_tmux_binary(subject, sandbox, monkeypatch):
    """Surprise: "best-effort kill" has no try/except."""

    def boom(argv, **kwargs):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        subject._kill_tmux_window("w1")


# --- _tail_tmux_pane ---------------------------------------------------------------


def test_tail_tmux_pane_empty_window_short_circuits(subject, sandbox, monkeypatch):
    rec = _tmux_fake(monkeypatch, lambda argv, kw: _completed(argv, 0, "x\n"))
    assert subject._tail_tmux_pane("") == ""
    assert rec.calls == []


def test_tail_tmux_pane_returns_empty_when_window_is_gone(subject, sandbox, monkeypatch):
    rec = _tmux_fake(monkeypatch, _windows_then_pane("other\n", "content\n"))
    assert subject._tail_tmux_pane("w1") == ""
    assert rec.argvs == [
        ["tmux", "list-windows", "-t", subject.TMUX_SESSION, "-F", "#{window_name}"]
    ]


def test_tail_tmux_pane_argv_and_timeout(subject, sandbox, monkeypatch):
    rec = _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "line\n"))
    subject._tail_tmux_pane("w1")
    assert rec.argvs[1] == [
        "tmux", "capture-pane", "-p", "-t", f"{subject.TMUX_SESSION}:w1"
    ]
    assert rec.calls[1].kwargs == {"capture_output": True, "text": True, "timeout": 5}


def test_tail_tmux_pane_returns_last_three_non_blank_lines_by_default(
    subject, sandbox, monkeypatch
):
    pane = "one\n\ntwo\n   \nthree\nfour\n\n"
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", pane))
    assert subject._tail_tmux_pane("w1") == "two\nthree\nfour"


def test_tail_tmux_pane_right_strips_each_line(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "  keep   \n"))
    assert subject._tail_tmux_pane("w1") == "  keep"


def test_tail_tmux_pane_honours_n(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "a\nb\nc\nd\n"))
    assert subject._tail_tmux_pane("w1", 1) == "d"


def test_tail_tmux_pane_returns_everything_when_n_is_zero(subject, sandbox, monkeypatch):
    """Surprise: `lines[-0:]` is the whole list, so n=0 returns the entire pane."""
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "a\nb\nc\n"))
    assert subject._tail_tmux_pane("w1", 0) == "a\nb\nc"


def test_tail_tmux_pane_returns_all_lines_when_pane_is_shorter_than_n(
    subject, sandbox, monkeypatch
):
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "only\n"))
    assert subject._tail_tmux_pane("w1") == "only"


def test_tail_tmux_pane_blank_pane_is_empty_string(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "\n   \n\n"))
    assert subject._tail_tmux_pane("w1") == ""


def test_tail_tmux_pane_returns_empty_on_capture_failure(subject, sandbox, monkeypatch):
    _tmux_fake(monkeypatch, _windows_then_pane("w1\n", "junk\n", pane_rc=1))
    assert subject._tail_tmux_pane("w1") == ""


def test_tail_tmux_pane_swallows_capture_timeout(subject, sandbox, monkeypatch):
    def handler(argv, kwargs):
        if "list-windows" in argv:
            return _completed(argv, 0, "w1\n")
        raise subprocess.TimeoutExpired(argv, 5)

    _tmux_fake(monkeypatch, handler)
    assert subject._tail_tmux_pane("w1") == ""


def test_tail_tmux_pane_does_not_swallow_a_missing_tmux_binary(subject, sandbox, monkeypatch):
    """Surprise: the try/except only wraps the capture — the existence probe is outside it."""

    def boom(argv, **kwargs):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        subject._tail_tmux_pane("w1")
