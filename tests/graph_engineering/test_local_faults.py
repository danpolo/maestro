from __future__ import annotations

import time
from pathlib import Path

import pytest

from maestro import graph_experiments
from maestro.graph_experiments import (
    bwrap_confinement_probe,
    detached_child_probe,
    ensure_scratch_target,
    exclusive_writer_probe,
)


def test_scratch_guard_refuses_the_root_itself_and_escape_paths(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()

    with pytest.raises(ValueError, match="child"):
        ensure_scratch_target(root, root)
    with pytest.raises(ValueError, match="outside"):
        ensure_scratch_target(tmp_path / "outside", root)


def test_scratch_guard_refuses_a_symlink_escape(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside"):
        ensure_scratch_target(root / "escape" / "probe", root)


def test_exclusive_writer_fixture_blocks_replacement_until_owner_exits(tmp_path):
    result = exclusive_writer_probe(tmp_path / "writer-probe")

    assert result["owner_acquired"] is True
    assert result["concurrent_replacement_acquired"] is False
    assert result["post_exit_replacement_acquired"] is True


def test_exclusive_writer_owner_holds_the_lock_however_late_the_replacement_attempt(
    tmp_path, monkeypatch,
):
    # A loaded host can deschedule the prober between the owner's ready mark and its
    # replacement attempt. The owner must hold until the prober is done, not for a
    # fixed interval that a slow attempt can outlast.
    real_attempt = graph_experiments._lock_attempt
    attempts = []

    def late_attempt(path):
        if not attempts:
            time.sleep(1.0)
        attempts.append(path)
        return real_attempt(path)

    monkeypatch.setattr(graph_experiments, "_lock_attempt", late_attempt)
    result = exclusive_writer_probe(tmp_path / "writer-probe")

    assert len(attempts) == 2
    assert result["owner_acquired"] is True
    assert result["concurrent_replacement_acquired"] is False
    assert result["post_exit_replacement_acquired"] is True


def test_detached_grandchild_can_outlive_its_launcher_and_is_cleaned_up(tmp_path):
    result = detached_child_probe(tmp_path / "process-probe")

    assert result["launcher_exit_code"] == 0
    assert result["child_alive_after_launcher_exit"] is True
    assert result["child_cleaned_up"] is True


def test_bwrap_profile_confines_parent_and_child_and_read_only_mounts_git_metadata(tmp_path):
    result = bwrap_confinement_probe(tmp_path / "bwrap-probe")
    if not result["available"]:
        pytest.skip(result["detail"])

    assert result["inside_write"] is True
    assert result["outside_write"] is False
    assert result["outside_read"] is False
    assert result["child_outside_write"] is False
    assert result["linked_worktree_git_status"] is True, result["linked_worktree_git_status_detail"]
    assert result["linked_worktree_git_commit"] is False, result["linked_worktree_git_commit_detail"]
