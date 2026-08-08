"""The firewall that keeps the live reference repo read-only must itself be tested.

An untested guard is not a guard. These tests assert the firewall installed for every
session in `tests/conftest.py` actually refuses each mutation route, and that it leaves
reads and writes elsewhere alone.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.reference_repo import LEGACY_REPO, ReferenceRepoWriteAttempt

pytestmark = pytest.mark.skipif(
    LEGACY_REPO is None or not LEGACY_REPO.is_dir(),
    reason="reference repo not available",
)


@pytest.fixture
def victim() -> Path:
    """A path inside the reference repo that must never actually be created."""
    return LEGACY_REPO / ".orchestrator" / "maestro-firewall-probe.tmp"


def test_open_for_write_is_refused(victim):
    with pytest.raises(ReferenceRepoWriteAttempt):
        open(victim, "w")
    assert not victim.exists()


def test_append_is_refused(victim):
    with pytest.raises(ReferenceRepoWriteAttempt):
        open(victim, "a")
    assert not victim.exists()


def test_path_write_text_is_refused(victim):
    with pytest.raises(ReferenceRepoWriteAttempt):
        victim.write_text("nope")
    assert not victim.exists()


def test_path_open_for_write_is_refused(victim):
    with pytest.raises(ReferenceRepoWriteAttempt):
        victim.open("w")
    assert not victim.exists()


def test_mkdir_is_refused():
    with pytest.raises(ReferenceRepoWriteAttempt):
        (LEGACY_REPO / "maestro-firewall-probe-dir").mkdir()


def test_rename_onto_the_repo_is_refused(victim, tmp_path):
    source = tmp_path / "src.txt"
    source.write_text("payload", encoding="utf-8")
    with pytest.raises(ReferenceRepoWriteAttempt):
        os.rename(source, victim)
    assert not victim.exists()
    assert source.exists()


def test_path_rename_onto_the_repo_is_refused(victim, tmp_path):
    """The exact shape of `write_state`: a harmless temp source, a live destination."""
    source = tmp_path / "state.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ReferenceRepoWriteAttempt):
        source.rename(victim)
    assert not victim.exists()
    assert source.exists()


def test_shutil_copy_into_the_repo_is_refused(victim, tmp_path):
    source = tmp_path / "src.txt"
    source.write_text("payload", encoding="utf-8")
    with pytest.raises(ReferenceRepoWriteAttempt):
        shutil.copy(source, victim)
    assert not victim.exists()


def test_subprocess_with_cwd_in_the_repo_is_refused():
    with pytest.raises(ReferenceRepoWriteAttempt):
        subprocess.run(["true"], cwd=str(LEGACY_REPO))


def test_reads_from_the_repo_are_still_allowed():
    marker = LEGACY_REPO / "scripts" / "orchestrator_run.py"
    assert marker.is_file()
    with open(marker, "r", encoding="utf-8") as handle:
        assert handle.readline()


def test_writes_outside_the_repo_are_still_allowed(tmp_path):
    target = tmp_path / "nested" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("fine", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "fine"
