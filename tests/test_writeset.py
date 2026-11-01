"""P12 pilot finding D20: write-set overlap keeps colliding tasks out of parallel runs."""
from __future__ import annotations

import subprocess

import pytest

from maestro import writeset as ws


@pytest.mark.parametrize("a, b, expected", [
    (["src/a.py"], ["src/a.py"], ("src/a.py",)),
    (["src/a.py"], ["src/b.py"], ()),
    (["src"], ["src/pkg/a.py"], ("src/pkg/a.py",)),
    (["src/"], ["./src/a.py"], ("src/a.py",)),
    (["src/a"], ["src/ab.py"], ()),                       # a directory is by component
    (["src/*.py"], ["src/a.py"], ("src/a.py",)),
    (["src/*.py"], ["tests/a.py"], ()),
    (["src"], ["src/**/*.py"], ("src/**/*.py",)),        # a glob under a directory
    (["src/pk?"], ["src/pkg/a.py"], ("src/pkg/a.py",)),  # a glob naming the directory
    (["src/*.py"], ["src/a*"], ("src/*.py & src/a*",)),
    (["src/*.py"], ["tests/*.py"], ()),
    (["."], ["anything.txt"], ("anything.txt",)),
    ([], ["src/a.py"], ()),
])
def test_write_sets_overlap(a, b, expected):
    assert ws.write_sets_overlap(a, b) == expected
    assert ws.write_sets_overlap(b, a) == expected


def test_overlaps_are_sorted_and_deduplicated():
    assert ws.write_sets_overlap(["b.py", "a.py", "src"], ["a.py", "b.py", "src/x.py"]) == (
        "a.py", "b.py", "src/x.py")


def test_declared_scope_distinguishes_absent_from_empty():
    assert ws.declared_scope({"id": "x"}) is None
    assert ws.declared_scope({"scope": None}) is None
    assert ws.declared_scope({"scope": []}) == ()
    assert ws.declared_scope({"scope": "src/a.py"}) == ("src/a.py",)
    assert ws.declared_scope({"scope": ["a", "b/"]}) == ("a", "b/")


def test_unknown_write_sets_conflict_with_everything():
    running = {"T1": ("src/a.py",), "T2": None}
    assert ws.conflicts(None, running) == {"T1": (ws.UNDECLARED,), "T2": (ws.UNDECLARED,)}
    assert ws.conflicts(("docs/x.md",), running) == {"T2": (ws.UNDECLARED,)}
    assert ws.conflicts(("src",), running) == {"T1": ("src/a.py",), "T2": (ws.UNDECLARED,)}
    assert ws.conflicts((), running) == {}, "scope: [] writes nothing"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "base.txt").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_a_live_runs_write_set_is_its_scope_plus_its_commits_and_worktree(repo, tmp_path):
    assert ws.changed_paths(repo, "T1") == (), "no branch yet: nothing changed"
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "impl/T1", str(tree), "main")
    (tree / "committed.py").write_text("x\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "work")
    (tree / "base.txt").write_text("edited\n")
    (tree / "untracked.py").write_text("y\n")

    assert ws.changed_paths(repo, "T1") == ("base.txt", "committed.py", "untracked.py")
    assert ws.run_write_set(repo, "T1", ("src",)) == (
        "base.txt", "committed.py", "src", "untracked.py")
    assert ws.run_write_set(repo, "T1", None) is None


def test_git_failure_is_unknown(tmp_path):
    assert ws.changed_paths(tmp_path, "T1") is None
    assert ws.run_write_set(tmp_path, "T1", ("a",)) is None
