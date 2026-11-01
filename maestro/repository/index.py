"""Snapshots, worktree overlays and the disposable parse cache (`[P9]`).

`06_REPOSITORY_INTELLIGENCE.md` §3. Backed by `.orchestrator/repository.sqlite3`, which
may be deleted at any time: nothing in it is a fact that cannot be recomputed from git and
the worktree. Three things live here.

**Snapshots.** A *base* snapshot is a git commit SHA and is immutable by construction —
every read goes through `git cat-file` / `git grep` against that commit, never through
the checkout, so an operator editing the main tree cannot change what a query about
`abc123` answers. An *overlay* snapshot is one worktree's uncommitted edits on top of one
base: the changed paths with their content hashes (or a deletion mark).

**Overlay isolation.** An overlay id is derived from its base, its worktree's real path
and the fingerprint of its edits. Two worktrees therefore never share an id, and an
overlay's rows are only ever consulted for its own id — so one candidate's edit is
invisible to another's query ("overlays cannot cross-contaminate"). Paths an overlay did
not change fall through to the base commit, not to some other overlay.

**Freshness.** A base is `exact_match` while its commit resolves. An overlay is
`exact_match` only while its worktree still has exactly the edits it was registered with;
the moment the worktree moves on, the overlay is `stale` and `queries.py` rejects it (or,
when asked to, answers `partial` naming the drifted paths). Nothing is silently re-read.

**Parse cache.** Keyed by `(path, content_hash, extractor)`. The key names the content
being described, so a cached parse is correct for any snapshot whose view of that path
has that content — and wrong for none. The extractor is in the key so a Layer 0 parse is
never served once Layer 1 is enabled, or the reverse.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from maestro.repository.providers import (
    ImportRelation,
    ParseResult,
    ProviderSelection,
    SymbolDefinition,
    content_hash,
)

__all__ = [
    "INDEX_RELPATH",
    "SCHEMA_VERSION",
    "RepositoryError",
    "StaleSnapshotError",
    "UnknownSnapshotError",
    "Snapshot",
    "GrepHit",
    "RepositoryIndex",
    "git",
    "resolve_commit",
    "worktree_changes",
]

INDEX_RELPATH = Path(".orchestrator") / "repository.sqlite3"
SCHEMA_VERSION = 1

FRESHNESS = ("exact_match", "partial", "stale")


class RepositoryError(RuntimeError):
    pass


class StaleSnapshotError(RepositoryError):
    """A query named a snapshot whose facts no longer hold."""

    def __init__(self, message: str, *, drifted: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.drifted = tuple(drifted)


class UnknownSnapshotError(StaleSnapshotError):
    """Neither a registered overlay nor a commit this repository can resolve."""


# ── git ─────────────────────────────────────────────────────────────────────────────


def git(root: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False,
    )
    if check and proc.returncode != 0:
        raise RepositoryError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def resolve_commit(root: Path, rev: str) -> Optional[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip() or None


def _split_z(raw: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def worktree_changes(worktree: Path, base_sha: str) -> dict[str, Optional[str]]:
    """Every path whose worktree content differs from `base_sha`: path -> content hash, or None if deleted.

    Staged, unstaged and untracked (non-ignored) edits all count: a writer's uncommitted
    file is exactly the intermediate state an overlay exists to describe.
    """
    changed = set(_split_z(git(worktree, "diff", "--name-only", "--no-renames", "-z", base_sha)))
    changed |= set(_split_z(git(worktree, "ls-files", "--others", "--exclude-standard", "-z")))
    out: dict[str, Optional[str]] = {}
    for path in sorted(changed):
        target = worktree / path
        out[path] = content_hash(target.read_bytes()) if target.is_file() else None
    return out


def _fingerprint(changes: dict[str, Optional[str]]) -> str:
    body = "\n".join(f"{p}\0{h or '<deleted>'}" for p, h in sorted(changes.items()))
    return hashlib.sha256(body.encode("utf-8", "surrogateescape")).hexdigest()


# ── snapshot model ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    kind: str                    # 'base' | 'overlay'
    base_sha: str
    root: str
    worktree: Optional[str] = None
    fingerprint: str = ""
    changes: tuple[tuple[str, Optional[str]], ...] = ()
    #: Paths a caller accepted as drifted (`on_stale="flag"`): read from the base, and
    #: ignored when freshness is re-checked, so a narrowed view is not re-flagged forever.
    excluded: tuple[str, ...] = ()

    @property
    def is_overlay(self) -> bool:
        return self.kind == "overlay"

    @property
    def changed(self) -> dict[str, Optional[str]]:
        return dict(self.changes)


@dataclass(frozen=True)
class GrepHit:
    path: str
    line: int
    text: str
    source: str                  # 'git_grep' | 'rg' | 'read'


@dataclass
class GrepResult:
    hits: list[GrepHit] = field(default_factory=list)
    truncated: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('base', 'overlay')),
    base_sha TEXT NOT NULL,
    root TEXT NOT NULL,
    worktree TEXT,
    fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS overlay_files (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    content_hash TEXT,
    PRIMARY KEY (snapshot_id, path)
);
CREATE TABLE IF NOT EXISTS parses (
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extractor TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (path, content_hash, extractor)
);
"""


class RepositoryIndex:
    """The disposable index. `":memory:"` is a valid location and loses nothing but speed."""

    def __init__(self, location: str | Path = ":memory:") -> None:
        self.location = str(location)
        if self.location != ":memory:":
            Path(self.location).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.location)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(_SCHEMA)
        found = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if found is None:
            self._db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif found[0] != str(SCHEMA_VERSION):
            # Disposable by contract: an index from another schema is dropped, not migrated.
            self._db.executescript(
                "DROP TABLE parses; DROP TABLE overlay_files; DROP TABLE snapshots; DROP TABLE meta;"
            )
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self._db.commit()

    @classmethod
    def for_repo(cls, root: Path) -> "RepositoryIndex":
        return cls(Path(root) / INDEX_RELPATH)

    def close(self) -> None:
        self._db.close()

    # --- snapshots ------------------------------------------------------------------

    def base(self, root: Path, rev: str) -> Snapshot:
        sha = resolve_commit(root, rev)
        if sha is None:
            raise UnknownSnapshotError(f"{rev!r} does not resolve to a commit in {root}")
        self._db.execute(
            "INSERT OR IGNORE INTO snapshots (snapshot_id, kind, base_sha, root) VALUES (?, 'base', ?, ?)",
            (sha, sha, str(Path(root).resolve())),
        )
        self._db.commit()
        return Snapshot(snapshot_id=sha, kind="base", base_sha=sha, root=str(Path(root).resolve()))

    def register_overlay(self, worktree: Path, base_rev: str, *, root: Optional[Path] = None) -> Snapshot:
        worktree = Path(worktree).resolve()
        base = self.base(root or worktree, base_rev)
        changes = worktree_changes(worktree, base.base_sha)
        fingerprint = _fingerprint(changes)
        identity = hashlib.sha256(
            f"{base.base_sha}\0{worktree}\0{fingerprint}".encode("utf-8", "surrogateescape")
        ).hexdigest()
        snapshot_id = f"ovl_{base.base_sha[:12]}_{identity[:16]}"
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?, 'overlay', ?, ?, ?, ?)",
                (snapshot_id, base.base_sha, base.root, str(worktree), fingerprint),
            )
            self._db.executemany(
                "INSERT OR IGNORE INTO overlay_files VALUES (?, ?, ?)",
                [(snapshot_id, p, h) for p, h in changes.items()],
            )
        return Snapshot(
            snapshot_id=snapshot_id, kind="overlay", base_sha=base.base_sha, root=base.root,
            worktree=str(worktree), fingerprint=fingerprint, changes=tuple(sorted(changes.items())),
        )

    def get(self, snapshot_id: str) -> Optional[Snapshot]:
        row = self._db.execute(
            "SELECT snapshot_id, kind, base_sha, root, worktree, fingerprint FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        changes: tuple = ()
        if row[1] == "overlay":
            changes = tuple(self._db.execute(
                "SELECT path, content_hash FROM overlay_files WHERE snapshot_id=? ORDER BY path",
                (snapshot_id,),
            ).fetchall())
        return Snapshot(*row, changes=changes)

    def freshness(self, snapshot: Snapshot) -> tuple[str, tuple[str, ...]]:
        """(`exact_match` | `stale`, drifted paths)."""
        if resolve_commit(Path(snapshot.root), snapshot.base_sha) != snapshot.base_sha:
            return "stale", ()
        if not snapshot.is_overlay:
            return "exact_match", ()
        worktree = Path(snapshot.worktree or "")
        if not worktree.is_dir():
            return "stale", tuple(snapshot.changed)
        try:
            now = worktree_changes(worktree, snapshot.base_sha)
        except RepositoryError:
            return "stale", tuple(snapshot.changed)
        now = {p: h for p, h in now.items() if p not in snapshot.excluded}
        then = snapshot.changed
        if _fingerprint(now) == _fingerprint(then):
            return "exact_match", ()
        drifted = sorted(p for p in set(now) | set(then) if now.get(p, "<base>") != then.get(p, "<base>"))
        return "stale", tuple(drifted)

    # --- reading a snapshot ---------------------------------------------------------

    def list_files(self, snapshot: Snapshot) -> list[str]:
        base = set(_split_z(git(Path(snapshot.root), "ls-tree", "-r", "--name-only", "-z", snapshot.base_sha)))
        for path, digest in snapshot.changes:
            if digest is None:
                base.discard(path)
            else:
                base.add(path)
        return sorted(base)

    def read(self, snapshot: Snapshot, path: str) -> Optional[bytes]:
        """The bytes of `path` as this snapshot sees them, or None if it has no such file."""
        changed = snapshot.changed
        if path in changed:
            if changed[path] is None:
                return None
            data = (Path(snapshot.worktree or "") / path).read_bytes()
            if content_hash(data) != changed[path]:
                raise StaleSnapshotError(
                    f"{path} changed in {snapshot.worktree} after overlay {snapshot.snapshot_id} was registered",
                    drifted=(path,),
                )
            return data
        proc = subprocess.run(
            ["git", "-C", snapshot.root, "cat-file", "blob", f"{snapshot.base_sha}:{path}"],
            capture_output=True, check=False,
        )
        return proc.stdout if proc.returncode == 0 else None

    def grep(
        self,
        snapshot: Snapshot,
        word: str,
        *,
        globs: Sequence[str] = (),
        limit: int = 500,
    ) -> GrepResult:
        """Bounded whole-word fixed-string search over the snapshot's view.

        Unchanged paths: `git grep` against the base commit. Overlay-changed paths: `rg`
        over the worktree files, or a plain read when `rg` is not installed — search must
        keep working with no optional tool present.
        """
        result = GrepResult()
        changed = snapshot.changed
        pathspecs = [f":(glob)**/{g}" if "/" not in g else f":(glob){g}" for g in globs] or ["."]
        raw = git(
            Path(snapshot.root), "grep", "-n", "-I", "-w", "-F", "-z", "-e", word,
            snapshot.base_sha, "--", *pathspecs, check=False,
        )
        prefix = f"{snapshot.base_sha}:"
        for record in raw.split(b"\n"):
            if not record:
                continue
            parts = record.split(b"\0", 2)
            if len(parts) != 3:
                continue
            path = parts[0].decode("utf-8", "surrogateescape").removeprefix(prefix)
            if path in changed:
                continue  # the overlay owns this path
            self._add(result, GrepHit(path, int(parts[1]), parts[2].decode("utf-8", "replace"), "git_grep"), limit)
        live = [p for p, h in changed.items() if h is not None and _glob_match(p, globs)]
        if live:
            for hit in _search_worktree(Path(snapshot.worktree or ""), live, word):
                self._add(result, hit, limit)
        result.hits.sort(key=lambda h: (h.path, h.line))
        return result

    @staticmethod
    def _add(result: GrepResult, hit: GrepHit, limit: int) -> None:
        if len(result.hits) >= limit:
            result.truncated = True
        else:
            result.hits.append(hit)

    # --- parse cache ----------------------------------------------------------------

    def parse(self, snapshot: Snapshot, path: str, selection: ProviderSelection) -> Optional[ParseResult]:
        data = self.read(snapshot, path)
        if data is None:
            return None
        provider = selection.provider if selection.provider.handles(path) else selection.fallback
        if not provider.handles(path):
            return None
        digest = content_hash(data)
        key = (path, digest, selection.fingerprint)
        row = self._db.execute(
            "SELECT body FROM parses WHERE path=? AND content_hash=? AND extractor=?", key
        ).fetchone()
        if row is not None:
            return _parse_from_json(row[0])
        parsed = provider.parse(path, data)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO parses VALUES (?, ?, ?, ?)", (*key, _parse_to_json(parsed)))
        return parsed


# ── helpers ─────────────────────────────────────────────────────────────────────────


def _glob_match(path: str, globs: Sequence[str]) -> bool:
    from fnmatch import fnmatch

    return not globs or any(fnmatch(path, g) or fnmatch(path.rsplit("/", 1)[-1], g) for g in globs)


def _search_worktree(worktree: Path, paths: Iterable[str], word: str) -> list[GrepHit]:
    paths = list(paths)
    rg = shutil.which("rg")
    if rg:
        proc = subprocess.run(
            [rg, "-n", "-w", "-F", "--no-heading", "--with-filename", "--null", "--no-config",
             "--color", "never", "-e", word, "--", *paths],
            cwd=worktree, capture_output=True, check=False,
        )
        if proc.returncode in (0, 1):
            hits = []
            for record in proc.stdout.split(b"\n"):
                if b"\0" not in record:
                    continue
                path, rest = record.split(b"\0", 1)
                line, _, text = rest.partition(b":")
                hits.append(GrepHit(path.decode("utf-8", "surrogateescape"), int(line),
                                    text.decode("utf-8", "replace"), "rg"))
            return hits
    import re

    pattern = re.compile(rf"(?<!\w){re.escape(word)}(?!\w)")
    hits = []
    for path in paths:
        try:
            text = (worktree / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(GrepHit(path, number, line, "read"))
    return hits


def _parse_to_json(parsed: ParseResult) -> str:
    return json.dumps({
        "path": parsed.path, "content_hash": parsed.content_hash, "provider": parsed.provider,
        "completeness": parsed.completeness, "parse_error": parsed.parse_error, "detail": parsed.detail,
        "definitions": [d.to_dict() for d in parsed.definitions],
        "imports": [i.to_dict() for i in parsed.imports],
    }, sort_keys=True)


def _parse_from_json(body: str) -> ParseResult:
    doc = json.loads(body)
    return ParseResult(
        path=doc["path"], content_hash=doc["content_hash"], provider=doc["provider"],
        completeness=doc["completeness"], parse_error=doc["parse_error"], detail=doc["detail"],
        definitions=tuple(SymbolDefinition(**d) for d in doc["definitions"]),
        imports=tuple(ImportRelation(**{**i, "names": tuple(i["names"])}) for i in doc["imports"]),
    )
