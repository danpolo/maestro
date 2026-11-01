"""Pinned behaviour of the `meta` orphan-branch lifecycle (`maestro/metabranch.py`).

Characterisation, not specification. Every function under test shells out to a real
`git`, so these run against a throwaway repo created with ``git init`` inside `tmp_path`
(the ``repo`` fixture) plus, where a push needs somewhere real to land, a throwaway bare
remote (the ``bare_origin`` fixture — same two-fixture split `test_merge.py` uses, for the
same reason: a repo with no remote at all is what proves a failed push is reported, not
silently swallowed).

Unlike `merge.py`/`orchestrator.py`, `metabranch.py` has no module-level `REPO` global —
every function takes `repo_root` as an explicit argument — so there is nothing for the
`sandbox` fixture to rebase and it is not used here. `subject` (the `maestro.metabranch`
module) is used only to call the functions under test.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.maestro_module("metabranch")


# --- git plumbing for the throwaway repo -------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A real git repo on `main`, under a `repos/` parent so the meta worktree (a
    sibling of `repo`, per `meta_worktree_path`) has somewhere to land without escaping
    `tmp_path`. git config is pinned to nothing so the operator's global settings can't
    change a result; identity comes from the environment so un-configured `git commit`
    calls made by the subject succeed.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")

    r = tmp_path / "repos" / "proj"
    r.mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _write(r, "seed.txt", "seed\n")
    _git(r, "add", "--", "seed.txt")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def bare_origin(repo: Path, tmp_path: Path) -> Path:
    """A throwaway bare remote wired as `origin` on `repo` — for the tests that need a
    push, a fetch, or a fresh-clone reconnect to actually land somewhere.
    """
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    return remote


def _meta_worktree(repo: Path) -> Path:
    return repo.parent / f"{repo.name}-meta"


# --- meta_worktree_path --------------------------------------------------------------


def test_meta_worktree_path_is_a_sibling_not_nested(subject, repo):
    result = subject.meta_worktree_path(repo)
    assert result == repo.parent / f"{repo.name}-meta"
    assert result.parent == repo.parent
    assert not str(result).startswith(str(repo) + "/")


# --- ensure_meta_branch: nothing exists yet ------------------------------------------


def test_ensure_meta_branch_creates_orphan_branch_and_worktree(subject, repo):
    changed, detail = subject.ensure_meta_branch(repo)

    assert changed is True
    assert "created new orphan branch and worktree" in detail
    worktree = _meta_worktree(repo)
    assert worktree.is_dir()
    assert (worktree / ".git").exists()
    assert (worktree / "README.md").exists()

    branch = _git(worktree, "branch", "--show-current").stdout.strip()
    assert branch == "meta"
    # Orphan: no commit on meta shares history with main's initial commit.
    log = _git(worktree, "log", "--oneline").stdout
    assert "initialize orphan meta branch" in log


def test_ensure_meta_branch_orphan_has_no_shared_history_with_main(subject, repo):
    subject.ensure_meta_branch(repo)
    worktree = _meta_worktree(repo)
    main_root = _git(repo, "rev-list", "--max-parents=0", "main").stdout.strip()
    meta_root = _git(worktree, "rev-list", "--max-parents=0", "meta").stdout.strip()
    assert main_root != meta_root


# --- ensure_meta_branch: already attached --------------------------------------------


def test_ensure_meta_branch_is_a_noop_once_attached(subject, repo):
    subject.ensure_meta_branch(repo)
    changed, detail = subject.ensure_meta_branch(repo)

    assert changed is False
    assert "already attached" in detail


# --- ensure_meta_branch: local branch exists, worktree missing ----------------------


def test_ensure_meta_branch_reattaches_an_existing_local_branch(subject, repo):
    subject.ensure_meta_branch(repo)
    worktree = _meta_worktree(repo)
    # Detach the worktree without deleting the branch — e.g. someone removed the
    # directory by hand. `ensure_meta_branch` must still find `meta` locally and
    # re-attach a worktree for it, not try to recreate the orphan branch.
    _git(repo, "worktree", "remove", "--force", str(worktree))
    assert not worktree.exists()
    assert subject._branch_exists_local(repo, "meta")

    changed, detail = subject.ensure_meta_branch(repo)

    assert changed is True
    assert "attached existing local branch" in detail
    assert worktree.is_dir()


# --- ensure_meta_branch: fresh-clone reconnect (branch on remote only) --------------


def test_ensure_meta_branch_reconnects_after_a_fresh_clone(subject, repo, bare_origin, tmp_path):
    # Create and push meta from the "original" clone.
    subject.ensure_meta_branch(repo)
    subject.push_meta(repo)

    # A second, fresh clone of the same remote: main is there, meta is not (no
    # worktree, no local branch) — exactly the state a real `git clone` leaves.
    clone = tmp_path / "repos" / "clone"
    subprocess.run(["git", "clone", "-q", str(bare_origin), str(clone)], check=True)
    assert not subject._branch_exists_local(clone, "meta")

    changed, detail = subject.ensure_meta_branch(clone)

    assert changed is True
    assert "reconnected to origin/meta" in detail
    worktree = _meta_worktree(clone)
    assert worktree.is_dir()
    assert (worktree / "README.md").exists()


# --- sync_meta -------------------------------------------------------------------


def test_sync_meta_without_a_worktree_reports_the_precondition(subject, repo):
    changed, detail = subject.sync_meta(repo)
    assert changed is False
    assert "call ensure_meta_branch first" in detail


def test_sync_meta_with_nothing_present_is_a_noop(subject, repo):
    subject.ensure_meta_branch(repo)
    changed, detail = subject.sync_meta(repo, paths=("handoffs",))
    assert changed is False
    assert "no process-doc paths present" in detail


def test_sync_meta_copies_and_commits_changed_paths(subject, repo):
    subject.ensure_meta_branch(repo)
    _write(repo, "tasks/lessons.md", "lesson one\n")
    _write(repo, "handoffs/2026-01-01-x.md", "handoff\n")

    changed, detail = subject.sync_meta(repo, paths=("tasks/lessons.md", "handoffs"))

    assert changed is True
    assert detail == "synced and committed"
    worktree = _meta_worktree(repo)
    assert (worktree / "tasks/lessons.md").read_text() == "lesson one\n"
    assert (worktree / "handoffs/2026-01-01-x.md").read_text() == "handoff\n"


def test_sync_meta_is_a_noop_once_converged(subject, repo):
    subject.ensure_meta_branch(repo)
    _write(repo, "tasks/lessons.md", "lesson one\n")
    subject.sync_meta(repo, paths=("tasks/lessons.md",))

    changed, detail = subject.sync_meta(repo, paths=("tasks/lessons.md",))

    assert changed is False
    assert "already up to date" in detail


def test_sync_meta_picks_up_a_later_edit(subject, repo):
    subject.ensure_meta_branch(repo)
    _write(repo, "tasks/lessons.md", "lesson one\n")
    subject.sync_meta(repo, paths=("tasks/lessons.md",))

    _write(repo, "tasks/lessons.md", "lesson one\nlesson two\n")
    changed, detail = subject.sync_meta(repo, paths=("tasks/lessons.md",))

    assert changed is True
    assert detail == "synced and committed"
    worktree = _meta_worktree(repo)
    assert "lesson two" in (worktree / "tasks/lessons.md").read_text()


# --- push_meta -------------------------------------------------------------------


def test_push_meta_without_a_worktree_reports_the_precondition(subject, repo):
    changed, detail = subject.push_meta(repo)
    assert changed is False
    assert "no meta worktree to push" in detail


def test_push_meta_fails_loudly_with_no_remote(subject, repo):
    """The bug metabranch.py exists to stop repeating: `merge.py`'s two `git push origin
    main` sites used to swallow a failed push. `push_meta`'s return code must not be one
    of them — a repo with no remote must come back `False` with the stderr surfaced, not
    a silent `True`.
    """
    subject.ensure_meta_branch(repo)
    changed, detail = subject.push_meta(repo)
    assert changed is False
    assert "push failed" in detail


def test_push_meta_succeeds_against_a_real_remote(subject, repo, bare_origin):
    subject.ensure_meta_branch(repo)
    changed, detail = subject.push_meta(repo)
    assert changed is True
    assert detail == "pushed"

    remote_branches = subprocess.run(
        ["git", "branch", "--list", "meta"], cwd=str(bare_origin),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "meta" in remote_branches


def test_push_meta_respects_a_local_opt_out(subject, repo, bare_origin):
    """`git config maestro.metaPush false` keeps process docs local-only (Dan,
    2026-11-01: handoffs must not reach a public remote). Not a failure."""
    subject.ensure_meta_branch(repo)
    _git(repo, "config", "maestro.metaPush", "false")

    changed, detail = subject.push_meta(repo)

    assert changed is True
    assert detail == "push disabled by maestro.metaPush"
    remote_branches = subprocess.run(
        ["git", "branch", "--list", "meta"], cwd=str(bare_origin),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "meta" not in remote_branches


def test_push_meta_is_a_noop_once_nothing_is_ahead(subject, repo, bare_origin):
    subject.ensure_meta_branch(repo)
    subject.push_meta(repo)

    changed, detail = subject.push_meta(repo)

    assert changed is True
    assert detail == "nothing to push"


# --- reconcile_meta ----------------------------------------------------------------


def test_reconcile_meta_chains_all_three_end_to_end(subject, repo, bare_origin):
    # tasks/lessons.md is one of the real META_PATHS entries, so the default
    # reconcile_meta(repo) — no explicit `paths` override — picks it up.
    _write(repo, "tasks/lessons.md", "lesson one\n")

    summary = subject.reconcile_meta(repo)

    worktree = _meta_worktree(repo)
    assert worktree.is_dir()
    assert "WARNING" not in summary

    remote_branches = subprocess.run(
        ["git", "branch", "--list", "meta"], cwd=str(bare_origin),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "meta" in remote_branches


def test_reconcile_meta_surfaces_a_push_failure_as_a_warning_not_an_exception(subject, repo):
    # No bare_origin fixture here — repo has no remote, so the push leg fails. The
    # whole point of reconcile_meta is that this must come back as a string, not raise.
    summary = subject.reconcile_meta(repo)
    assert "WARNING" in summary
    assert "push failed" in summary


def test_reconcile_meta_never_raises_even_when_a_step_blows_up(subject, repo, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(subject, "ensure_meta_branch", _boom)

    summary = subject.reconcile_meta(repo)

    assert "meta reconcile error" in summary
    assert "disk on fire" in summary


def test_reconcile_meta_is_idempotent(subject, repo, bare_origin):
    _write(repo, "tasks/lessons.md", "lesson one\n")
    first = subject.reconcile_meta(repo)
    second = subject.reconcile_meta(repo)

    assert "WARNING" not in first
    assert "WARNING" not in second
    # Second call has nothing new to create, sync, or push: reconcile_meta only
    # appends the sync/push detail when something actually happened (see its
    # `if committed` / `if not pushed` guards), so a fully converged call reports
    # just the ensure_meta_branch no-op detail.
    assert second == "meta worktree already attached at " + str(_meta_worktree(repo))
