"""The content-addressed artifact store (`[P1]`, `01_DATA_CONTRACTS_AND_SCHEMAS.md` §4)."""
from __future__ import annotations

import hashlib

import pytest

from maestro.control.artifacts import ArtifactStore, MissingArtifact, storage_rel_path
from maestro.control.models import EventType


# --- addressing -----------------------------------------------------------------------


def test_content_lands_at_its_sharded_sha256_path(artifacts):
    data = b"diff --git a/x b/x\n"
    digest = hashlib.sha256(data).hexdigest()
    ref = artifacts.put_bytes(data, type="git_patch")

    assert ref.content_hash == digest
    assert ref.storage_ref == f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    assert (artifacts.root / ref.storage_ref).read_bytes() == data


def test_the_artifact_id_carries_the_hash_prefix(artifacts):
    ref = artifacts.put_bytes(b"x", type="test_log")
    assert ref.artifact_id.startswith(f"art_{ref.content_hash[:8]}_")


def test_identical_bytes_are_stored_once_and_share_one_reference(artifacts):
    first = artifacts.put_bytes(b"same", type="test_log")
    second = artifacts.put_bytes(b"same", type="test_log")
    assert second.artifact_id == first.artifact_id
    rows = artifacts.store.conn.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()
    assert rows["n"] == 1


def test_different_types_of_the_same_bytes_are_separate_references(artifacts):
    patch = artifacts.put_bytes(b"same", type="git_patch")
    log = artifacts.put_bytes(b"same", type="test_log")
    assert patch.artifact_id != log.artifact_id
    assert patch.content_hash == log.content_hash


def test_storage_rel_path_is_pure(tmp_path):
    digest = "a" * 64
    assert storage_rel_path(digest) == f"sha256/aa/aa/{digest}"


# --- the write protocol ------------------------------------------------------------------


def test_no_temporary_file_survives_a_successful_write(artifacts):
    artifacts.put_bytes(b"payload", type="checkpoint")
    assert list(artifacts.tmp_dir.glob("tmp_*")) == []


def test_a_commit_records_the_reference_and_an_event(artifacts):
    ref = artifacts.put_bytes(b"payload", type="node_result", producer_attempt_id="att_1")
    row = artifacts.store.conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (ref.artifact_id,)
    ).fetchone()
    assert row["size_bytes"] == len(b"payload")

    event = artifacts.store.events()[-1]
    assert event.type == EventType.ARTIFACT_COMMITTED
    assert event.attempt_id == "att_1"
    assert event.artifact_refs == [ref.artifact_id]


def test_a_file_can_be_stored_by_path(artifacts, tmp_path):
    source = tmp_path / "results.log"
    source.write_bytes(b"12 passed\n")
    ref = artifacts.put_file(source, type="test_log")
    assert artifacts.read(ref.artifact_id) == b"12 passed\n"


def test_stored_content_is_never_modified_in_place(artifacts):
    ref = artifacts.put_bytes(b"original", type="test_log")
    path = artifacts.root / ref.storage_ref
    before = path.stat().st_mtime_ns

    artifacts.put_bytes(b"original", type="test_log")     # same bytes, stored again
    assert path.stat().st_mtime_ns == before
    assert path.read_bytes() == b"original"


# --- orphan safety -------------------------------------------------------------------------


def test_a_crash_before_the_commit_leaves_an_unreferenced_orphan(artifacts):
    """The direction a crash is allowed to fail in: file on disk, no row."""
    ref = artifacts.put_bytes(b"half-written", type="git_patch", commit=False)

    assert (artifacts.root / ref.storage_ref).exists()
    assert artifacts.ref(ref.artifact_id) is None
    assert artifacts.orphans() == [artifacts.root / ref.storage_ref]
    assert artifacts.missing_references() == []


def test_a_referenced_artifact_is_never_missing_after_a_commit(artifacts):
    ref = artifacts.put_bytes(b"committed", type="git_patch")
    assert artifacts.missing_references() == []
    assert artifacts.orphans() == []
    assert artifacts.read(ref.artifact_id) == b"committed"


def test_a_missing_referenced_artifact_raises_rather_than_returning_a_fallback(artifacts):
    ref = artifacts.put_bytes(b"committed", type="git_patch")
    (artifacts.root / ref.storage_ref).unlink()

    assert artifacts.missing_references() == [ref.artifact_id]
    with pytest.raises(MissingArtifact):
        artifacts.read(ref.artifact_id)


def test_an_unknown_artifact_id_raises(artifacts):
    with pytest.raises(MissingArtifact):
        artifacts.read("art_deadbeef_nope")


def test_content_that_does_not_match_its_hash_raises(artifacts):
    ref = artifacts.put_bytes(b"committed", type="git_patch")
    (artifacts.root / ref.storage_ref).write_bytes(b"tampered")
    with pytest.raises(MissingArtifact):
        artifacts.read(ref.artifact_id)


def test_scavenging_collects_orphans_and_leaves_references_alone(artifacts):
    kept = artifacts.put_bytes(b"kept", type="git_patch")
    orphan = artifacts.put_bytes(b"orphan", type="git_patch", commit=False)

    assert artifacts.scavenge() == 1
    assert not (artifacts.root / orphan.storage_ref).exists()
    assert artifacts.read(kept.artifact_id) == b"kept"


def test_scavenging_an_empty_store_is_a_no_op(repo, store):
    empty = ArtifactStore(repo / ".orchestrator" / "artifacts", store)
    assert empty.orphans() == []
    assert empty.scavenge() == 0
