"""`RepositoryQueries` — bounded, snapshot-pinned answers (`[P9]`).

`06_REPOSITORY_INTELLIGENCE.md` §3. Every query names a snapshot (a commit SHA or a
registered overlay id) and returns a `QueryResult` that says, beside its items, how much
to trust them:

* `freshness` — `exact_match`, or `partial` when the caller accepted a stale snapshot
  (`on_stale="flag"`). The default, `on_stale="reject"`, raises `StaleSnapshotError`:
  an answer about code that is no longer there is worse than no answer.
* `completeness` — `partial` when any file could not be parsed cleanly, the result was
  truncated at `limit`, or the answer is lexical by nature (references are word matches,
  not resolved bindings: Layer 2 is where precision would come from).
* `unresolved` — the specific files or questions that degraded the answer.

**Layer 0 first, always.** Candidates come from `git grep` over the base commit and `rg`
over overlay-changed files; only those candidates are parsed. No whole-repository index is
a precondition of any query, and a file that fails to parse is answered lexically rather
than dropped. `affected` is P10's and is deliberately not implemented here.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from maestro.repository.index import (
    GrepResult,
    RepositoryIndex,
    Snapshot,
    StaleSnapshotError,
    UnknownSnapshotError,
    resolve_commit,
)
from maestro.repository.providers import (
    ImportRelation,
    ProviderSelection,
    SymbolDefinition,
    lexical_definitions,
    module_name_for,
    select_provider,
)

__all__ = [
    "Reference",
    "TestLinkage",
    "QueryResult",
    "RepositoryQueries",
    "DIRECTIONS",
]

DIRECTIONS = ("in", "out")

_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
_LEXICAL_IMPORT = re.compile(r"^\s*(?:from\s+(?P<from>[\w.]+)\s+import\s+(?P<names>[\w\s,*()]+)|import\s+(?P<mods>[\w.,\s]+))")


@dataclass(frozen=True)
class Reference:
    symbol: str               # the symbol this row is a reference to (in) or from (out)
    path: str
    line: int
    text: str
    direction: str
    depth: int
    enclosing: Optional[str] = None   # qualname of the definition containing the line
    target: Optional[str] = None      # for `out`: the name being called
    provider: str = "search"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class TestLinkage:
    subject: str
    test_path: str
    reason: str               # 'imports' | 'name_match'

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class QueryResult:
    query: str
    snapshot_id: str
    items: tuple[Any, ...]
    freshness: str = "exact_match"
    completeness: str = "complete"
    unresolved: tuple[str, ...] = ()
    truncated: bool = False
    providers: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Any]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> Any:
        return self.items[index]


@dataclass
class _Acc:
    """Mutable accumulator a query fills and freezes into a `QueryResult`."""

    items: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    providers: set = field(default_factory=set)
    partial: bool = False
    truncated: bool = False

    def grep(self, result: GrepResult) -> GrepResult:
        if result.truncated:
            self.truncated = self.partial = True
        self.providers.update(h.source for h in result.hits)
        return result


class RepositoryQueries:
    def __init__(
        self,
        root: Path,
        *,
        index: Optional[RepositoryIndex] = None,
        selection: Optional[ProviderSelection] = None,
        limit: int = 200,
    ) -> None:
        self.root = Path(root).resolve()
        self.index = index if index is not None else RepositoryIndex()
        self.selection = selection if selection is not None else select_provider()
        self.limit = limit
        self.retrieval_log: list[str] = []

    # --- snapshots ------------------------------------------------------------------

    def overlay(self, worktree: Path, base_rev: str) -> Snapshot:
        return self.index.register_overlay(worktree, base_rev, root=self.root)

    def resolve(self, snapshot: str | Snapshot, *, on_stale: str = "reject") -> tuple[Snapshot, str, tuple[str, ...]]:
        """(snapshot, freshness, notes). Raises on a stale snapshot unless `on_stale='flag'`."""
        if on_stale not in ("reject", "flag"):
            raise ValueError(f"on_stale must be 'reject' or 'flag', not {on_stale!r}")
        snap = snapshot if isinstance(snapshot, Snapshot) else self.index.get(str(snapshot))
        if snap is None:
            if str(snapshot).startswith("ovl_"):
                raise UnknownSnapshotError(f"overlay {snapshot} is not registered in this index")
            sha = resolve_commit(self.root, str(snapshot))
            if sha is None or not sha.startswith(str(snapshot).lower()):
                # A branch name would resolve too — and move. Only a commit id pins a snapshot.
                raise UnknownSnapshotError(f"{snapshot!r} is not a commit id in {self.root}")
            snap = self.index.base(self.root, sha)
        freshness, drifted = self.index.freshness(snap)
        if freshness == "exact_match":
            return snap, "exact_match", ()
        message = (
            f"snapshot {snap.snapshot_id} is stale"
            + (f": drifted {', '.join(drifted)}" if drifted else "")
        )
        if on_stale == "reject":
            raise StaleSnapshotError(message, drifted=drifted)
        if not snap.is_overlay:
            # A base commit that no longer resolves cannot be read at all, flagged or not.
            raise UnknownSnapshotError(message)
        # Flagged: drop the drifted paths from the overlay and answer about the rest.
        kept = tuple((p, h) for p, h in snap.changes if p not in drifted)
        narrowed = Snapshot(**{**snap.__dict__, "changes": kept, "excluded": tuple(sorted({*snap.excluded, *drifted}))})
        return narrowed, "partial", (message,) + tuple(f"{p}: excluded, changed after registration" for p in drifted)

    def _finish(self, query: str, snap: Snapshot, freshness: str, notes: Sequence[str], acc: _Acc) -> QueryResult:
        self.retrieval_log.append(f"{query}@{snap.snapshot_id}")
        return QueryResult(
            query=query, snapshot_id=snap.snapshot_id, items=tuple(acc.items),
            freshness=freshness,
            completeness="partial" if (acc.partial or freshness != "exact_match") else "complete",
            unresolved=tuple(notes) + tuple(acc.unresolved), truncated=acc.truncated,
            providers=tuple(sorted(acc.providers)),
        )

    def _parse(self, snap: Snapshot, path: str, acc: _Acc):
        try:
            parsed = self.index.parse(snap, path, self.selection)
        except StaleSnapshotError as exc:
            acc.partial = True
            acc.unresolved.append(str(exc))
            return None
        if parsed is not None:
            acc.providers.add(parsed.provider)
            if parsed.completeness != "complete":
                acc.partial = True
                acc.unresolved.append(f"{path}: {parsed.completeness} parse ({parsed.detail})")
        return parsed

    # --- definitions ----------------------------------------------------------------

    def definitions(self, name: str, snapshot: str | Snapshot, *, on_stale: str = "reject") -> QueryResult:
        _require_identifier(name)
        snap, freshness, notes = self.resolve(snapshot, on_stale=on_stale)
        acc = _Acc()
        acc.items = self._definitions(snap, name, acc)
        return self._finish(f"definitions:{name}", snap, freshness, notes, acc)

    def _definitions(self, snap: Snapshot, name: str, acc: _Acc) -> list[SymbolDefinition]:
        header = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b")
        hits = acc.grep(self.index.grep(snap, name, globs=("*.py",), limit=self.limit * 4))
        out: list[SymbolDefinition] = []
        for path in sorted({h.path for h in hits.hits if header.match(h.text)}):
            parsed = self._parse(snap, path, acc)
            if parsed is None:
                continue
            if parsed.usable:
                structural = [d for d in parsed.definitions if d.name == name]
                out.extend(structural)
                if parsed.completeness != "complete":
                    # Error recovery can swallow a later header into an ERROR node; a header
                    # grep saw but the tree lost is still answered, lexically.
                    covered = {d.start_line for d in structural}
                    data = self.index.read(snap, path) or b""
                    lexical = [d for d in lexical_definitions(path, data, name) if d.start_line not in covered]
                    if lexical:
                        out.extend(lexical)
                        acc.providers.add("search")
            else:
                data = self.index.read(snap, path) or b""
                out.extend(lexical_definitions(path, data, name))
                acc.providers.add("search")
        return out[: self.limit]

    # --- references -----------------------------------------------------------------

    def references(
        self,
        symbol: str,
        snapshot: str | Snapshot,
        direction: str = "in",
        depth: int = 1,
        *,
        on_stale: str = "reject",
    ) -> QueryResult:
        """`in`: where `symbol` is named. `out`: what `symbol`'s bodies call. Lexical, so always `partial`."""
        _require_identifier(symbol)
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, not {direction!r}")
        if depth < 1:
            raise ValueError("depth must be >= 1")
        snap, freshness, notes = self.resolve(snapshot, on_stale=on_stale)
        acc = _Acc(partial=True)
        seen: set[str] = set()
        frontier = [symbol]
        for level in range(1, depth + 1):
            next_frontier: list[str] = []
            for name in frontier:
                if name in seen:
                    continue
                seen.add(name)
                found = (self._refs_in if direction == "in" else self._refs_out)(snap, name, level, acc)
                for ref in found:
                    if len(acc.items) >= self.limit:
                        acc.truncated = True
                        break
                    acc.items.append(ref)
                    follow = (ref.enclosing or "").rsplit(".", 1)[-1] if direction == "in" else ref.target
                    if follow and follow not in seen:
                        next_frontier.append(follow)
            frontier = next_frontier
            if not frontier or acc.truncated:
                break
        acc.unresolved.append("references are lexical word matches, not resolved bindings (Layer 0)")
        return self._finish(f"references:{symbol}:{direction}:{depth}", snap, freshness, notes, acc)

    def _enclosing(self, snap: Snapshot, path: str, acc: _Acc) -> list[SymbolDefinition]:
        if not path.endswith(".py"):
            return []
        parsed = self._parse(snap, path, acc)
        if parsed is None:
            return []
        if parsed.usable:
            return list(parsed.definitions)
        return list(lexical_definitions(path, self.index.read(snap, path) or b""))

    def _refs_in(self, snap: Snapshot, name: str, level: int, acc: _Acc) -> list[Reference]:
        header = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b")
        hits = acc.grep(self.index.grep(snap, name, limit=self.limit * 4))
        defs_by_path: dict[str, list[SymbolDefinition]] = {}
        out = []
        for hit in hits.hits:
            if header.match(hit.text):
                continue
            if hit.path not in defs_by_path:
                defs_by_path[hit.path] = self._enclosing(snap, hit.path, acc)
            containing = [d for d in defs_by_path[hit.path] if d.start_line <= hit.line <= d.end_line]
            enclosing = max(containing, key=lambda d: d.start_line).qualname if containing else None
            out.append(Reference(
                symbol=name, path=hit.path, line=hit.line, text=hit.text.strip(),
                direction="in", depth=level, enclosing=enclosing, provider=hit.source,
            ))
        return out

    def _refs_out(self, snap: Snapshot, name: str, level: int, acc: _Acc) -> list[Reference]:
        out = []
        for definition in self._definitions(snap, name, acc):
            data = self.index.read(snap, definition.path) or b""
            lines = data.decode("utf-8", "replace").splitlines()
            end = definition.end_line if definition.end_line > definition.start_line else len(lines)
            body = lines[definition.start_line - 1:end]
            calls = _calls_in("\n".join(body), definition.start_line)
            if calls is None:
                acc.unresolved.append(f"{definition.path}:{definition.start_line}: body unparseable, calls matched lexically")
                calls = [
                    (definition.start_line + i, m.group(1))
                    for i, text in enumerate(body) for m in _CALL.finditer(text)
                    if m.group(1) not in ("def", "class", "if", "while", "return", "print")
                ]
            for line, callee in calls:
                if callee == name and line == definition.start_line:
                    continue
                out.append(Reference(
                    symbol=name, path=definition.path, line=line,
                    text=lines[line - 1].strip() if line - 1 < len(lines) else "",
                    direction="out", depth=level, enclosing=definition.qualname, target=callee,
                    provider=definition.provider,
                ))
        return out

    # --- importers ------------------------------------------------------------------

    def importers(self, file: str, snapshot: str | Snapshot, *, on_stale: str = "reject") -> QueryResult:
        snap, freshness, notes = self.resolve(snapshot, on_stale=on_stale)
        acc = _Acc()
        module = module_name_for(file)
        if module is None:
            acc.partial = True
            acc.unresolved.append(f"{file}: not a Python module; importers are unknown for it")
            return self._finish(f"importers:{file}", snap, freshness, notes, acc)
        targets = {module}
        if module.startswith("src."):
            targets.add(module[len("src."):])
        leaf = module.rsplit(".", 1)[-1]
        hits = acc.grep(self.index.grep(snap, leaf, globs=("*.py",), limit=self.limit * 4))
        for path in sorted({h.path for h in hits.hits} - {file}):
            parsed = self._parse(snap, path, acc)
            if parsed is None:
                continue
            relations = parsed.imports if parsed.usable else _lexical_imports(path, self.index.read(snap, path) or b"")
            for relation in relations:
                if _imports_target(relation, targets):
                    acc.items.append(ImportRelation(**{**relation.__dict__, "resolved_path": file}))
        acc.items = acc.items[: self.limit]
        return self._finish(f"importers:{file}", snap, freshness, notes, acc)

    # --- tests_for ------------------------------------------------------------------

    def tests_for(self, subjects: Sequence[str], snapshot: str | Snapshot, *, on_stale: str = "reject") -> QueryResult:
        snap, freshness, notes = self.resolve(snapshot, on_stale=on_stale)
        acc = _Acc()
        files = self.index.list_files(snap)
        test_files = [f for f in files if f.endswith(".py") and _TEST_PATH.search(f)]
        found: dict[tuple[str, str], str] = {}
        for subject in subjects:
            stem = subject.rsplit("/", 1)[-1].removesuffix(".py")
            for path in test_files:
                name = path.rsplit("/", 1)[-1]
                if name in (f"test_{stem}.py", f"{stem}_test.py"):
                    found.setdefault((subject, path), "name_match")
            result = self.importers(subject, snap)
            acc.partial |= result.completeness != "complete"
            acc.unresolved.extend(result.unresolved)
            acc.providers.update(result.providers)
            for relation in result.items:
                if _TEST_PATH.search(relation.importer):
                    found[(subject, relation.importer)] = "imports"
        acc.items = [TestLinkage(s, p, r) for (s, p), r in sorted(found.items())]
        return self._finish(f"tests_for:{','.join(subjects)}", snap, freshness, notes, acc)

    # --- context --------------------------------------------------------------------

    def context(self, task, role, snapshot: str | Snapshot, policy, **material):
        from maestro.repository.context import build_context_manifest

        return build_context_manifest(task, role, snapshot, policy, queries=self, **material)


# ── helpers ─────────────────────────────────────────────────────────────────────────


def _require_identifier(name: str) -> None:
    if not _IDENT.match(name or ""):
        raise ValueError(f"{name!r} is not an identifier")


def _calls_in(source: str, first_line: int) -> Optional[list[tuple[int, str]]]:
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name:
                calls.append((first_line + node.lineno - 1, name))
    return sorted(set(calls))


def _imports_target(relation: ImportRelation, targets: set[str]) -> bool:
    if relation.module in targets:
        return True
    return any(f"{relation.module}.{name}" in targets for name in relation.names)


def _lexical_imports(path: str, data: bytes) -> list[ImportRelation]:
    out = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
        match = _LEXICAL_IMPORT.match(line)
        if not match:
            continue
        if match.group("from"):
            raw = match.group("from")
            level = len(raw) - len(raw.lstrip("."))
            from maestro.repository.providers import _absolute_module

            names = tuple(n.strip() for n in re.sub(r"[()]", "", match.group("names")).split(",") if n.strip())
            names = tuple(n.split()[0] for n in names)
            out.append(ImportRelation(path, _absolute_module(path, raw.lstrip(".") or None, level), names, number, "search"))
        else:
            for module in match.group("mods").split(","):
                if module.strip():
                    out.append(ImportRelation(path, module.split()[0], (), number, "search"))
    return out
