"""G10: a task worktree gets the project's venv, so relative `.venv/bin/python` runs."""
import subprocess
from pathlib import Path

from maestro.workflows import task_tree


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    (repo / ".gitignore").write_text(".venv/\n.orchestrator/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_new_worktree_links_the_project_venv_and_stays_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    tree = task_tree.ensure(repo, "t1")
    assert (tree / ".venv").is_symlink()
    assert (tree / ".venv" / "bin" / "python").exists()
    assert _git(tree, "status", "--porcelain").stdout == ""


def test_no_project_venv_means_no_link(tmp_path):
    repo = _repo(tmp_path)
    tree = task_tree.ensure(repo, "t2")
    assert not (tree / ".venv").exists()
