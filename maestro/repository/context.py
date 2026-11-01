"""Role-bounded `ContextManifest` assembly (`[P9]`).

`06_REPOSITORY_INTELLIGENCE.md` §4. An agent is never handed a directory. It is handed a
manifest: exact paths, line ranges and content hashes pinned to one snapshot, chosen by
what its role contract says it may see, and cut to its role's token budget.

**Two things are never truncated**: `mandatory_instructions` and `acceptance_criteria`.
They are placed first and counted first. If they alone exceed the budget the manifest
still carries them whole, drops everything optional, and says so in
`unresolved_queries` — a budget overrun is a visible fact, an instruction silently
cut in half is a defect nobody can see. The task's ROADMAP `notes:` (P12 pilot finding
D28) are counted with them and never dropped for the budget; they have a bound of their
own (`MAX_NOTES_TOKENS`), and a cut there is named in `omitted_material`.

**Withholding is structural, not a budget decision** (§4.1). A reviewer never receives
the implementer's narrative (confirmation bias); a test designer never receives the
candidate patch (copying its assumptions); a planner never receives full source bodies;
an implementer never receives broad architectural history. Those rules live in
`ROLE_PROFILES` keyed by `role_id` and are applied before the budget is consulted, so no
budget, however generous, can let withheld material through. A role with no profile fails
closed. Budget and AST depth are read from the role's own `context_assembly_rules`.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Optional

from maestro.repository.index import Snapshot
from maestro.repository.providers import SymbolDefinition

__all__ = [
    "ContextError",
    "ContextManifest",
    "RoleProfile",
    "ROLE_PROFILES",
    "MANIFEST_FIELDS",
    "build_context_manifest",
    "estimate_tokens",
    "role_definition_hash",
]

DEFAULT_MAX_CONTEXT_TOKENS = 100_000
MAX_SCOPE_FILES = 50
#: P12 pilot finding D28: a task's `notes:` are carried verbatim up to this bound; past
#: it they are cut at a line and the cut is reported in `omitted_material`.
MAX_NOTES_TOKENS = 4_000
#: The `retrieval_paths` prefix naming a file added because a blocking review finding
#: named it outside the task's `scope:` (Dan, 2026-09-28: auto-widen for the rework).
WIDENED_FOR_REWORK = "widened_for_rework:"


class ContextError(ValueError):
    pass


def estimate_tokens(text: str) -> int:
    """~4 characters per token. An estimate for budgeting, not a billing figure."""
    return math.ceil(len(text) / 4) if text else 0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def role_definition_hash(role: Any) -> str:
    body = role.to_dict() if hasattr(role, "to_dict") else dict(role)
    return _sha(json.dumps(body, sort_keys=True, ensure_ascii=False, default=list))


# ── role profiles (§4.1) ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoleProfile:
    #: How entrypoint source is represented: whole files, header-only signatures, or none.
    source: str                               # 'files' | 'signatures' | 'none'
    #: Material kinds this role may receive, in priority order under the budget.
    receives: tuple[str, ...]
    #: Material kinds this role must never receive, whatever the caller passes.
    withheld: tuple[str, ...]


#: Material kinds: diff, test_results, prior_failures, related_tests, decisions,
#: dependency_map, implementer_narrative, candidate_patch, source.
ROLE_PROFILES: Mapping[str, RoleProfile] = {
    "implementer": RoleProfile(
        source="files",
        receives=("prior_failures", "source", "related_tests", "test_results"),
        withheld=("decisions", "dependency_map"),
    ),
    "planner": RoleProfile(
        source="signatures",
        receives=("decisions", "dependency_map", "source", "prior_failures"),
        withheld=("candidate_patch", "implementer_narrative"),
    ),
    "reviewer": RoleProfile(
        source="signatures",
        receives=("diff", "test_results", "source", "related_tests"),
        withheld=("implementer_narrative", "candidate_patch"),
    ),
    "test_designer": RoleProfile(
        source="signatures",
        receives=("source", "related_tests"),
        withheld=("candidate_patch", "diff", "implementer_narrative"),
    ),
    "diagnoser": RoleProfile(
        source="files",
        receives=("prior_failures", "test_results", "diff", "source", "related_tests"),
        withheld=("implementer_narrative",),
    ),
}

MANIFEST_FIELDS = (
    "snapshot_id", "task_id", "role_definition_hash", "policy_hash",
    "mandatory_instructions", "acceptance_criteria", "task_notes", "entrypoints", "source_refs",
    "related_tests_and_contracts", "relevant_decisions", "prior_failure_refs",
    "retrieval_paths", "freshness", "unresolved_queries", "omitted_material",
    "context_estimate_tokens",
)


@dataclass(frozen=True)
class ContextManifest:
    snapshot_id: str
    task_id: str
    role_definition_hash: str
    policy_hash: str
    mandatory_instructions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    entrypoints: tuple[str, ...]
    source_refs: tuple[dict, ...]
    related_tests_and_contracts: tuple[dict, ...]
    relevant_decisions: tuple[dict, ...]
    prior_failure_refs: tuple[str, ...]
    retrieval_paths: tuple[str, ...]
    freshness: str
    unresolved_queries: tuple[str, ...]
    omitted_material: tuple[str, ...]
    context_estimate_tokens: int
    role_id: str = ""
    max_context_tokens: int = 0
    #: The task's ROADMAP `notes:`, verbatim, bounded by `MAX_NOTES_TOKENS` (D28).
    task_notes: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        for name in MANIFEST_FIELDS:
            value = getattr(self, name)
            out[name] = [dict(v) if isinstance(v, Mapping) else v for v in value] if isinstance(value, tuple) else value
        return out


# ── assembly ────────────────────────────────────────────────────────────────────────


@dataclass
class _Item:
    kind: str
    label: str
    tokens: int
    bucket: str                      # which manifest field it lands in
    payload: Any


@dataclass
class _Build:
    retrieval: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    partial: bool = False


def build_context_manifest(
    task: Any,
    role: Any,
    snapshot: str | Snapshot,
    policy: Any,
    *,
    queries: Any,
    diff: Optional[str] = None,
    candidate_patch: Optional[str] = None,
    implementer_narrative: Optional[str] = None,
    decisions: Sequence[Mapping[str, Any]] = (),
    prior_failures: Sequence[Mapping[str, Any]] = (),
    test_results: Sequence[Mapping[str, Any]] = (),
    rework_paths: Sequence[str] = (),
    on_stale: str = "reject",
) -> ContextManifest:
    role_id = str(getattr(role, "role_id", ""))
    profile = ROLE_PROFILES.get(role_id)
    if profile is None:
        raise ContextError(
            f"no context profile for role {role_id!r}; refusing to assemble an unbounded manifest"
        )
    rules = dict(getattr(role, "context_assembly_rules", {}) or {})
    budget = int(rules.get("max_context_tokens") or DEFAULT_MAX_CONTEXT_TOKENS)

    snap, freshness, notes = queries.resolve(snapshot, on_stale=on_stale)
    build = _Build(unresolved=list(notes))

    mandatory = tuple(_mandatory_instructions(task, role))
    criteria = tuple(str(c) for c in (getattr(task, "acceptance_criteria", ()) or ()))
    if not criteria:
        build.unresolved.append(f"{task.task_id}: task declares no acceptance criteria")
    omitted: list[str] = []
    notes = _bounded_notes(task, omitted)
    fixed_tokens = sum(estimate_tokens(t) for t in (*mandatory, *criteria, notes))

    entrypoints = _entrypoints(queries, snap, task, build)
    entrypoints += _widened_for_rework(queries, snap, rework_paths, entrypoints, build)
    supplied = {
        "diff": diff, "candidate_patch": candidate_patch,
        "implementer_narrative": implementer_narrative,
        "decisions": decisions, "prior_failures": prior_failures, "test_results": test_results,
    }
    for kind in profile.withheld:
        if supplied.get(kind):
            omitted.append(f"{kind}: withheld from role {role_id} (§4.1)")
    for kind, value in supplied.items():
        if value and kind not in profile.receives and kind not in profile.withheld:
            omitted.append(f"{kind}: not part of role {role_id}'s context")

    items: list[_Item] = []
    for kind in profile.receives:
        items.extend(_items_for(kind, profile, rules, queries, snap, entrypoints, supplied, build))

    used = fixed_tokens
    buckets: dict[str, list] = {k: [] for k in ("source_refs", "related", "decisions", "failures")}
    if fixed_tokens > budget:
        build.unresolved.append(
            f"mandatory instructions, acceptance criteria and task notes alone need "
            f"{fixed_tokens} tokens, "
            f"over role {role_id}'s budget of {budget}; carried whole, optional material dropped"
        )
    for item in items:
        if used + item.tokens > budget:
            omitted.append(f"{item.kind}: {item.label} ({item.tokens} tokens) over budget")
            continue
        used += item.tokens
        buckets[item.bucket].append(item.payload)

    if build.partial and freshness == "exact_match":
        freshness = "partial"
    return ContextManifest(
        snapshot_id=snap.snapshot_id,
        task_id=str(task.task_id),
        role_definition_hash=role_definition_hash(role),
        policy_hash=str(policy if isinstance(policy, str) else getattr(policy, "policy_hash", "")),
        mandatory_instructions=mandatory,
        acceptance_criteria=criteria,
        entrypoints=tuple(entrypoints),
        source_refs=tuple(buckets["source_refs"]),
        related_tests_and_contracts=tuple(buckets["related"]),
        relevant_decisions=tuple(buckets["decisions"]),
        prior_failure_refs=tuple(buckets["failures"]),
        retrieval_paths=tuple(build.retrieval),
        freshness=freshness,
        unresolved_queries=tuple(build.unresolved),
        omitted_material=tuple(omitted),
        context_estimate_tokens=used,
        role_id=role_id,
        max_context_tokens=budget,
        task_notes=notes,
    )


def _bounded_notes(task: Any, omitted: list[str]) -> str:
    """The task's `notes:` verbatim (never summarised: paraphrase loses criteria), cut at
    a line past `MAX_NOTES_TOKENS`, with the cut named in `omitted`."""
    text = str(getattr(task, "notes", "") or "")
    limit = MAX_NOTES_TOKENS * 4
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit + 1)
    kept = text[:cut if cut > limit // 2 else limit].rstrip()
    omitted.append(
        f"task_notes: the last {len(text) - len(kept)} of {len(text)} characters cut "
        f"(bound {MAX_NOTES_TOKENS} tokens); read the rest in task {task.task_id}'s "
        "`notes:` in docs/ROADMAP.md"
    )
    return kept


def _widened_for_rework(
    queries: Any, snap: Snapshot, paths: Sequence[str], entrypoints: Sequence[str],
    build: _Build,
) -> list[str]:
    """Files a blocking review finding named outside the task's `scope:` (Dan, 2026-09-28).

    Each path is matched exactly against the snapshot, or by a bare file name that names
    exactly one file; anything else is not a file of this repository and is ignored. Each
    added file is recorded in `retrieval_paths` so the widening is visible.
    """
    if not paths:
        return []
    files = list(queries.index.list_files(snap))
    known = set(files)
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    out: list[str] = []
    for raw in paths:
        path = str(raw).strip()
        while path.startswith("./"):
            path = path[2:]
        if path not in known:
            named = by_name.get(path, []) if "/" not in path else []
            path = named[0] if len(named) == 1 else ""
        if path and path not in entrypoints and path not in out:
            out.append(path)
    build.retrieval.extend(f"{WIDENED_FOR_REWORK}{p}" for p in out)
    return out


def _mandatory_instructions(task: Any, role: Any) -> list[str]:
    out = []
    layers = dict(getattr(role, "instruction_layers", {}) or {})
    for layer in ("system_contract", "project_rules"):
        if layers.get(layer):
            out.append(f"{layer}: {layers[layer]}")
    if layers.get("role_instructions"):
        out.append(str(layers["role_instructions"]).strip())
    out.extend(str(e) for e in (getattr(role, "exclusions", ()) or ()))
    if getattr(task, "goal", ""):
        out.append(f"goal: {task.goal}")
    out.extend(f"constraint: {c}" for c in (getattr(task, "constraints", ()) or ()))
    return out


def _entrypoints(queries: Any, snap: Snapshot, task: Any, build: _Build) -> list[str]:
    scope = [str(s) for s in (getattr(task, "scope", ()) or ())]
    if not scope:
        return []
    files = queries.index.list_files(snap)
    build.retrieval.append(f"list_files@{snap.snapshot_id}")
    out: list[str] = []
    for entry in scope:
        entry_clean = entry.rstrip("/")
        matched = [
            f for f in files
            if f == entry_clean or f.startswith(entry_clean + "/") or fnmatch(f, entry)
        ]
        if not matched:
            build.unresolved.append(f"scope entry {entry!r} matches no file in {snap.snapshot_id}")
        for f in matched:
            if f not in out:
                out.append(f)
    if len(out) > MAX_SCOPE_FILES:
        build.unresolved.append(f"scope expands to {len(out)} files; first {MAX_SCOPE_FILES} kept")
        out = out[:MAX_SCOPE_FILES]
    return out


def _slice_ref(kind: str, path: str, lines: list[str], start: int, end: int, **extra: Any) -> _Item:
    text = "\n".join(lines[start - 1:end])
    ref = {"kind": kind, "path": path, "start_line": start, "end_line": end,
           "content_hash": _sha(text), "tokens": estimate_tokens(text), **extra}
    return _Item(kind="source", label=f"{path}:{start}-{end}", tokens=ref["tokens"], bucket="source_refs", payload=ref)


def _signature_filter(rules: Mapping[str, Any], definition: SymbolDefinition) -> bool:
    mode = str(rules.get("ast_inclusion") or "")
    top_level = "." not in definition.qualname
    if mode == "module_level_signatures":
        return top_level
    if mode == "public_signatures_of_touched_modules":
        return not any(part.startswith("_") for part in definition.qualname.split("."))
    return True


def _items_for(kind, profile, rules, queries, snap, entrypoints, supplied, build) -> list[_Item]:
    if kind == "source":
        return _source_items(profile, rules, queries, snap, entrypoints, build)
    if kind == "related_tests":
        if not entrypoints:
            return []
        result = queries.tests_for(entrypoints, snap)
        _absorb(build, result)
        return [
            _Item("related_tests", link.test_path, 0, "related",
                  {"kind": "test", "path": link.test_path, "subject": link.subject, "reason": link.reason,
                   "content_hash": _sha((queries.index.read(snap, link.test_path) or b"").decode("utf-8", "replace"))})
            for link in result.items
        ]
    if kind == "dependency_map":
        out = []
        for path in entrypoints:
            result = queries.importers(path, snap)
            _absorb(build, result)
            for relation in result.items:
                out.append(_Item("dependency_map", f"{relation.importer}->{path}", estimate_tokens(relation.importer + path) + 4,
                                 "source_refs",
                                 {"kind": "import_edge", "path": relation.importer, "line": relation.line,
                                  "imports": path, "module": relation.module}))
        return out
    if kind == "decisions":
        return [_Item("decisions", str(d.get("id", "?")), estimate_tokens(json.dumps(dict(d), default=str)), "decisions", dict(d))
                for d in supplied["decisions"] or ()]
    if kind == "prior_failures":
        out = []
        for failure in supplied["prior_failures"] or ():
            ref = str(failure.get("ref") or failure.get("artifact_id") or "")
            if not ref:
                build.unresolved.append("a prior failure was supplied without a ref; dropped")
                continue
            out.append(_Item("prior_failures", ref, estimate_tokens(str(failure.get("summary", ""))) + 8, "failures", ref))
        limit = rules.get("include_prior_attempt_summary")
        return out[-int(limit):] if isinstance(limit, int) and limit > 0 else out
    if kind in ("diff", "test_results"):
        value = supplied[kind]
        if not value:
            return []
        text = value if isinstance(value, str) else json.dumps(list(value), default=str, sort_keys=True)
        return [_Item(kind, kind, estimate_tokens(text), "source_refs",
                      {"kind": kind, "content_hash": _sha(text), "tokens": estimate_tokens(text)})]
    return []


def _absorb(build: _Build, result: Any) -> None:
    build.retrieval.append(f"{result.query}@{result.snapshot_id}")
    if result.completeness != "complete":
        build.partial = True
    for note in result.unresolved:
        if note not in build.unresolved:
            build.unresolved.append(note)


def _source_items(profile, rules, queries, snap, entrypoints, build) -> list[_Item]:
    items: list[_Item] = []
    if profile.source == "none":
        return items
    for path in entrypoints:
        data = queries.index.read(snap, path)
        if data is None:
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        build.retrieval.append(f"read:{path}@{snap.snapshot_id}")
        if profile.source == "files":
            items.append(_slice_ref("file", path, lines, 1, max(1, len(lines))))
            continue
        parsed = queries.index.parse(snap, path, queries.selection)
        if parsed is None:
            build.unresolved.append(f"{path}: no structural provider handles it; signatures unavailable")
            continue
        if parsed.completeness != "complete":
            build.partial = True
            build.unresolved.append(f"{path}: {parsed.completeness} parse ({parsed.detail})")
        for definition in parsed.definitions:
            if _signature_filter(rules, definition):
                end = max(definition.start_line, definition.signature_end_line)
                items.append(_slice_ref("signature", path, lines, definition.start_line, end,
                                        symbol=definition.qualname, provider=definition.provider))
    return items
