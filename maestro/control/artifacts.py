"""The content-addressed artifact store (`[P1]`).

Every patch, log, checkpoint and node result lives at
`.orchestrator/artifacts/sha256/{h[0:2]}/{h[2:4]}/{h}` and is immutable once written.
The write protocol is §4 of `01_DATA_CONTRACTS_AND_SCHEMAS.md` in order — tmp file,
fsync, digest, `os.replace`, parent-directory fsync, then the SQL row — and the order is
load-bearing rather than ceremonial:

* The file is durable *before* the database references it, so a crash at any point can
  only ever leave an unreferenced orphan. The reverse order can leave a committed row
  pointing at nothing, and the specification prohibits reading through that hole with an
  empty-dict or null fallback — `get()` raises `MissingArtifact` instead.
* `os.replace` onto an existing path is a no-op in content terms: identical bytes hash to
  the same name, so re-storing is idempotent and never mutates a stored artifact.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

from maestro.control.models import ArtifactRef, EventType, artifact_id, nanoid, now_iso
from maestro.control.store import ControlStore

CHUNK = 1 << 20


class ArtifactStoreError(RuntimeError):
    """Base class for artifact-store refusals."""


class MissingArtifact(ArtifactStoreError):
    """A referenced artifact is not on disk.

    Fails closed by design: the orphan direction is harmless, this direction blocks
    execution and must never be papered over with a default value.
    """


class ImmutableArtifact(ArtifactStoreError):
    """Something tried to overwrite stored content with different bytes."""


def storage_rel_path(content_hash: str) -> str:
    return f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"


class ArtifactStore:
    """Files on disk, references in SQLite."""

    def __init__(self, root: Path, store: ControlStore | None = None):
        self.root = Path(root)
        self.store = store
        self.tmp_dir = self.root / "tmp"

    # --- reads ------------------------------------------------------------------------

    def path_for(self, content_hash: str) -> Path:
        return self.root / storage_rel_path(content_hash)

    def ref(self, artifact_id_: str) -> ArtifactRef | None:
        if self.store is None:
            return None
        row = self.store.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id_,)
        ).fetchone()
        if row is None:
            return None
        return ArtifactRef(
            artifact_id=row["artifact_id"], type=row["type"],
            content_hash=row["content_hash"], storage_ref=row["storage_rel_path"],
            size_bytes=int(row["size_bytes"]), created_at=row["created_at"],
        )

    def read(self, artifact_id_: str) -> bytes:
        """Bytes of a referenced artifact, or `MissingArtifact`. Never a fallback."""
        ref = self.ref(artifact_id_)
        if ref is None:
            raise MissingArtifact(f"no artifact reference {artifact_id_!r}")
        path = self.root / ref.storage_ref
        if not path.exists():
            raise MissingArtifact(
                f"artifact {artifact_id_!r} references {ref.storage_ref} which is not on disk"
            )
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.content_hash:
            raise MissingArtifact(
                f"artifact {artifact_id_!r} hashes to {digest}, reference says {ref.content_hash}"
            )
        return data

    def exists(self, content_hash: str) -> bool:
        return self.path_for(content_hash).exists()

    # --- writes -----------------------------------------------------------------------

    def put_bytes(
        self, data: bytes, *, type: str, producer_attempt_id: str | None = None,
        source_snapshot_id: str | None = None, validity_dependencies=(),
        commit: bool = True, at: str | None = None,
    ) -> ArtifactRef:
        """Store `data` and, unless `commit=False`, reference it in the database.

        `commit=False` is what makes the orphan-safety rule testable: it performs every
        filesystem step and stops immediately before the SQL commit, which is exactly the
        window a crash can land in.
        """
        content_hash, size = self._write_atomically(data)
        ref = ArtifactRef(
            artifact_id=artifact_id(content_hash), type=type, content_hash=content_hash,
            storage_ref=storage_rel_path(content_hash), size_bytes=size,
            created_at=at or now_iso(), source_snapshot_id=source_snapshot_id,
            producer_attempt_id=producer_attempt_id,
            validity_dependencies=tuple(validity_dependencies),
        )
        if commit and self.store is not None:
            ref = self.commit_ref(ref)
        return ref

    def put_file(self, path: Path, *, type: str, **kwargs) -> ArtifactRef:
        return self.put_bytes(Path(path).read_bytes(), type=type, **kwargs)

    def commit_ref(self, ref: ArtifactRef) -> ArtifactRef:
        """Write the `artifacts` row and its event in one transaction."""
        if self.store is None:
            raise ArtifactStoreError("no control store bound; cannot commit an artifact reference")
        existing = self.store.conn.execute(
            "SELECT artifact_id FROM artifacts WHERE content_hash = ? AND type = ?",
            (ref.content_hash, ref.type),
        ).fetchone()
        if existing is not None:
            # Identical content already referenced: reuse the reference rather than
            # minting a second identity for the same immutable bytes.
            return replace(ref, artifact_id=existing["artifact_id"])
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts"
                " (artifact_id, content_hash, type, storage_rel_path, size_bytes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ref.artifact_id, ref.content_hash, ref.type,
                    ref.storage_ref, ref.size_bytes, ref.created_at,
                ),
            )
            self.store.append_event(
                EventType.ARTIFACT_COMMITTED, ref.artifact_id,
                payload={
                    "content_hash": ref.content_hash, "type": ref.type,
                    "storage_ref": ref.storage_ref, "size_bytes": ref.size_bytes,
                    "artifact_refs": [ref.artifact_id],
                },
                attempt_id=ref.producer_attempt_id, recorded_at=ref.created_at, conn=conn,
            )
        return ref

    def _write_atomically(self, data: bytes) -> tuple[str, int]:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"tmp_{nanoid(16)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)                                   # step 2
        finally:
            os.close(fd)
        content_hash = hashlib.sha256(data).hexdigest()    # step 3
        target = self.path_for(content_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Same name means same bytes; keep the stored copy untouched (immutability).
            tmp.unlink()
        else:
            os.replace(tmp, target)                        # step 4
            self._fsync_dir(target.parent)                 # step 5
        return content_hash, len(data)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # --- maintenance --------------------------------------------------------------------

    def orphans(self) -> list[Path]:
        """Stored files no `artifacts` row references.

        Harmless by the orphan-safety rule; collected offline rather than on the hot path.
        """
        if self.store is None:
            return []
        referenced = {
            row["content_hash"]
            for row in self.store.conn.execute("SELECT content_hash FROM artifacts")
        }
        found = []
        sha_root = self.root / "sha256"
        if not sha_root.exists():
            return []
        for path in sha_root.rglob("*"):
            if path.is_file() and path.name not in referenced:
                found.append(path)
        return sorted(found)

    def scavenge(self) -> int:
        """Delete orphans. Returns how many went."""
        removed = 0
        for path in self.orphans():
            path.unlink()
            removed += 1
        for leftover in sorted(self.tmp_dir.glob("tmp_*")) if self.tmp_dir.exists() else []:
            leftover.unlink()
            removed += 1
        return removed

    def missing_references(self) -> list[str]:
        """Referenced artifact IDs whose file is gone — these block execution."""
        if self.store is None:
            return []
        gone = []
        for row in self.store.conn.execute("SELECT artifact_id, storage_rel_path FROM artifacts"):
            if not (self.root / row["storage_rel_path"]).exists():
                gone.append(row["artifact_id"])
        return gone
