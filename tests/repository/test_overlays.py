"""P9: worktree overlays sit on a base commit, stay isolated, and go stale loudly."""
from __future__ import annotations

import sqlite3

import pytest

from maestro.repository.index import (
    INDEX_RELPATH,
    RepositoryIndex,
    StaleSnapshotError,
    UnknownSnapshotError,
)
from maestro.repository.queries import RepositoryQueries

from .conftest import run_git


@pytest.fixture
def worktrees(repo, tmp_path):
    root, sha = repo
    made = []
    for name in ("cand_a", "cand_b"):
        path = tmp_path / name
        run_git(root, "worktree", "add", "-q", "-b", f"impl/{name}", str(path), sha)
        made.append(path)
    return made


def test_an_overlay_sees_its_worktree_edit_and_the_base_does_not(repo, worktrees, selection):
    root, sha = repo
    wt = worktrees[0]
    (wt / "pkg/core.py").write_text((wt / "pkg/core.py").read_text() + "\n\ndef added():\n    return 3\n")
    q = RepositoryQueries(root, selection=selection)
    overlay = q.overlay(wt, sha)
    assert overlay.snapshot_id.startswith(f"ovl_{sha[:12]}_")
    assert [d.start_line for d in q.definitions("added", overlay.snapshot_id)] == [13]
    assert len(q.definitions("added", sha)) == 0
    # Unchanged paths fall through to the base commit.
    assert len(q.importers("pkg/core.py", overlay.snapshot_id)) == 3


def test_two_worktree_overlays_never_see_each_other(repo, worktrees, selection):
    root, sha = repo
    a, b = worktrees
    (a / "pkg/core.py").write_text("def only_in_a():\n    return 'a'\n")
    (b / "pkg/core.py").write_text("def only_in_b():\n    return 'b'\n")
    q = RepositoryQueries(root, selection=selection)
    oa, ob = q.overlay(a, sha), q.overlay(b, sha)
    assert oa.snapshot_id != ob.snapshot_id
    assert len(q.definitions("only_in_a", oa.snapshot_id)) == 1
    assert len(q.definitions("only_in_a", ob.snapshot_id)) == 0
    assert len(q.definitions("only_in_b", oa.snapshot_id)) == 0
    assert len(q.definitions("only_in_b", ob.snapshot_id)) == 1
    assert len(q.definitions("helper", oa.snapshot_id)) == 0
    assert len(q.definitions("helper", sha)) == 1


def test_identical_edits_in_two_worktrees_still_get_distinct_overlays(repo, worktrees, selection):
    root, sha = repo
    for wt in worktrees:
        (wt / "pkg/core.py").write_text("def same():\n    return 1\n")
    q = RepositoryQueries(root, selection=selection)
    oa, ob = (q.overlay(wt, sha) for wt in worktrees)
    assert oa.fingerprint == ob.fingerprint
    assert oa.snapshot_id != ob.snapshot_id
    (worktrees[1] / "pkg/core.py").write_text("def moved_on():\n    return 2\n")
    assert len(q.definitions("same", oa.snapshot_id)) == 1, "b moving on must not stale a"
    with pytest.raises(StaleSnapshotError):
        q.definitions("same", ob.snapshot_id)


def test_deleted_and_untracked_files_are_part_of_the_overlay(repo, worktrees, selection):
    root, sha = repo
    wt = worktrees[0]
    (wt / "pkg/app.py").unlink()
    (wt / "pkg/extra.py").write_text("from pkg.core import helper\n\ndef extra():\n    return helper(0)\n")
    q = RepositoryQueries(root, selection=selection)
    overlay = q.overlay(wt, sha)
    assert overlay.changed == {"pkg/app.py": None, "pkg/extra.py": overlay.changed["pkg/extra.py"]}
    files = q.index.list_files(overlay)
    assert "pkg/app.py" not in files and "pkg/extra.py" in files
    importers = {r.importer for r in q.importers("pkg/core.py", overlay.snapshot_id)}
    assert importers == {"pkg/extra.py", "tests/test_core.py"}
    refs = {r.path for r in q.references("helper", overlay.snapshot_id)}
    assert "pkg/app.py" not in refs and "pkg/extra.py" in refs


def test_a_worktree_that_moved_on_makes_its_overlay_stale(repo, worktrees, selection):
    root, sha = repo
    wt = worktrees[0]
    (wt / "pkg/core.py").write_text("def first_draft():\n    return 1\n")
    q = RepositoryQueries(root, selection=selection)
    overlay = q.overlay(wt, sha)
    (wt / "pkg/core.py").write_text("def second_draft():\n    return 2\n")
    with pytest.raises(StaleSnapshotError) as caught:
        q.definitions("first_draft", overlay.snapshot_id)
    assert caught.value.drifted == ("pkg/core.py",)


def test_a_stale_overlay_can_be_answered_as_partial_when_the_caller_asks(repo, worktrees, selection):
    root, sha = repo
    wt = worktrees[0]
    (wt / "pkg/core.py").write_text("def first_draft():\n    return 1\n")
    (wt / "pkg/fresh.py").write_text("def still_valid():\n    return 1\n")
    q = RepositoryQueries(root, selection=selection)
    overlay = q.overlay(wt, sha)
    (wt / "pkg/core.py").write_text("def second_draft():\n    return 2\n")
    flagged = q.definitions("first_draft", overlay.snapshot_id, on_stale="flag")
    assert flagged.freshness == "partial" and flagged.completeness == "partial"
    assert len(flagged) == 0, "the drifted edit is never read as if it were current"
    assert any("pkg/core.py" in note for note in flagged.unresolved)
    assert len(q.definitions("second_draft", overlay.snapshot_id, on_stale="flag")) == 0
    assert len(q.definitions("still_valid", overlay.snapshot_id, on_stale="flag")) == 1


def test_an_unregistered_overlay_id_is_rejected(repo, selection):
    root, sha = repo
    with pytest.raises(UnknownSnapshotError):
        RepositoryQueries(root, selection=selection).definitions("helper", f"ovl_{sha[:12]}_{'0' * 16}")


def test_a_mid_edit_worktree_still_yields_structure_or_a_lexical_answer(repo, worktrees, selection):
    root, sha = repo
    wt = worktrees[0]
    (wt / "pkg/core.py").write_text("def helper(x):\n    return x + 1\n\ndef typing_now(a, b\n\ndef later():\n    pass\n")
    q = RepositoryQueries(root, selection=selection)
    overlay = q.overlay(wt, sha)
    result = q.definitions("later", overlay.snapshot_id)
    assert [d.start_line for d in result] == [6]
    assert result.completeness == "partial"


def test_the_index_is_disposable(repo, worktrees, selection):
    root, sha = repo
    wt = worktrees[0]
    (wt / "pkg/core.py").write_text("def kept():\n    return 1\n")
    index = RepositoryIndex.for_repo(root)
    assert (root / INDEX_RELPATH).exists()
    q = RepositoryQueries(root, index=index, selection=selection)
    overlay = q.overlay(wt, sha)
    assert len(q.definitions("kept", overlay.snapshot_id)) == 1
    index.close()
    (root / INDEX_RELPATH).unlink()
    q2 = RepositoryQueries(root, index=RepositoryIndex.for_repo(root), selection=selection)
    with pytest.raises(UnknownSnapshotError):
        q2.definitions("kept", overlay.snapshot_id)
    assert len(q2.definitions("kept", q2.overlay(wt, sha).snapshot_id)) == 1


def test_an_index_from_another_schema_is_dropped_not_trusted(repo, tmp_path):
    location = tmp_path / "old.sqlite3"
    db = sqlite3.connect(location)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '0');"
        "CREATE TABLE snapshots (snapshot_id TEXT PRIMARY KEY, junk TEXT);"
        "CREATE TABLE overlay_files (x TEXT); CREATE TABLE parses (y TEXT);"
    )
    db.commit()
    db.close()
    index = RepositoryIndex(location)
    root, sha = repo
    assert index.base(root, sha).snapshot_id == sha
