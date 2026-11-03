"""The roadmap as a validated, revisioned dependency model (`[P2]`).

`docs/ROADMAP.md` is the project's statement of intent, and until now every consumer
re-implemented its own reading of it: `maestro.docs.roadmap` scanned ```` ```yaml ````
blocks with one regex, `maestro.docs.depmap` and `maestro.status` with a slightly
different one plus a hand-rolled fallback parser, `maestro.docs.consistency` with a third
copy of the dependency-flattening rule. Five readings of one document is five chances for
them to disagree, and the disagreements were real (they are catalogued in
`tests/characterization/`).

This module is the single reading. It has two faces, and the split is deliberate:

* **The lenient face** (`scan_tasks`, `iter_yaml_blocks`, `legacy_deps`,
  `normalize_legacy_task`, `manual_parse_block`) is the behaviour the existing document
  engine already had, extracted verbatim, quirks included — a malformed block is skipped,
  a block without an `id` is skipped, `deps: "T-1"` is one dependency and not five
  characters. The legacy modules now call these instead of carrying private copies, so
  there is one place where those quirks live and one place to change them. Every
  assertion in `tests/characterization/` still holds byte-for-byte; that is the point.

* **The strict face** (`load_document`, `validate`, `build_graph`) refuses. Malformed
  YAML, a duplicate `id`, a dependency on a task that does not exist, a dependency cycle:
  each is an error carrying the block index and the exact source line, not a silently
  dropped task. `graphlib.TopologicalSorter` decides whether the graph is acyclic; when
  it is not, every elementary cycle is reported as a concrete path.

On top of that sits the mutation API (`RoadmapTransaction`) and the compare-and-swap
protocol (`apply`, `record_applied`, `drift`). Two rules govern every write:

1. **All or nothing.** A transaction validates the graph it would produce *before* it
   touches the file. A mutation that would introduce a cycle, a duplicate or a dangling
   prerequisite writes nothing at all.
2. **Never overwrite a human.** Writes are compare-and-swap on the document hash. If
   `docs/ROADMAP.md` moved under us — Dan editing it by hand while the controller runs —
   the write is refused with `DocumentDriftError`, and `drift()` names which tasks the
   edit touched so the controller can block exactly those dispatches and leave the rest
   running. Blocks the transaction does not modify are spliced around, byte-for-byte: a
   mutation to one task cannot reformat, reorder or de-comment any other.

Legacy completions (`.orchestrator/completed_tasks.json`, and `status: complete` left in
the document) are imported as `legacy_accepted`. They satisfy dependencies — the work
really was done — but `Acceptance.supplies_evidence` and `Acceptance.cache_eligible` are
both False for them, so they can never manufacture fresh test evidence or a cache hit.
`[INV-04]`, `D06`.
"""
from __future__ import annotations

import graphlib
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML

#: libyaml when PyYAML was built with it: every read parses every block of the ROADMAP.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

SCHEMA_VERSION = 1

#: The fence pattern `maestro.docs.roadmap`, `.complete` and `.consistency` have always
#: used. Not anchored, so ```` x```yaml ```` on one line still opens a block, and the
#: closing fence is matched without requiring the newline before it — both pinned by
#: `tests/characterization/test_roadmap.py`.
YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)

#: The variant `maestro.docs.depmap` and `maestro.status` use: trailing spaces after the
#: language tag are tolerated and the newline before the closing fence is consumed.
YAML_BLOCK_TRIMMED_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)

#: The spelling `maestro.docs.upcoming` searches each document segment with: a loose
#: opener like `trimmed`, but the closing fence is matched as `strict` does.
YAML_BLOCK_LOOSE_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)

_FLAVOURS = {
    "strict": YAML_BLOCK_RE,
    "trimmed": YAML_BLOCK_TRIMMED_RE,
    "loose": YAML_BLOCK_LOOSE_RE,
}

#: Legacy keys this module maps onto a `TaskSpec` field and therefore consumes. Every
#: other key survives in `legacy_fields` so a spec round-trips without loss.
_CONSUMED_KEYS = frozenset({
    "id", "title", "short_desc", "goal", "deps", "hold", "self_modifying",
    "group_id", "risk", "scope", "constraints", "acceptance_criteria",
    "task_class", "required_artifact_types", "declared_effects", "spec_revision",
    "resources", "fallback",
})

#: The values a task's `fallback:` may take (P12B F5). `step_down` lets a node whose
#: equal-or-better routes are all paused run one rung weaker, under a full-suite gate.
FALLBACK_VALUES = ("step_down",)

#: The counted host dimensions a task may request (`[P10B]`), in `maestro.scheduling`'s
#: vocabulary. Agent slots and account pools are deliberately absent: they belong to the
#: launch lane, not to the task, which is what keeps a script task off them.
RESOURCE_KEYS = ("cpu", "ram_mb", "vram_mb")

#: Keys that *inform* a field but are not consumed: `execution_kind` is derived from
#: `mode`/`dispatch`/`kind`, and the derivation is lossy, so the originals stay in
#: `legacy_fields` and remain the authority for the legacy code paths.
_DERIVED_FROM = ("mode", "dispatch", "kind", "status", "verifications")

#: `high` = the task can cause real outside cost: API keys, connections to third-party
#: providers with limited quota, or calling agents outside the Maestro workflow, where it
#: must choose models and effort itself. Everything else is `low` (Dan, 2026-11-02).
RISKS = ("low", "high")
#: What older ROADMAPs wrote, read as the option that replaced it.
_RETIRED_RISKS = {"medium": "low", "critical": "high"}
KNOWN_RISKS = (*RISKS, *_RETIRED_RISKS)
TASK_CLASSES = (
    "bug", "feature", "documentation", "difficult_debugging", "manual_prep", "recovery",
)
EXECUTION_KINDS = ("ordinary", "script", "manual", "async", "resumable")


# ══════════════════════════════════════════════════════════════════════════
# errors
# ══════════════════════════════════════════════════════════════════════════


class TaskGraphError(RuntimeError):
    """Base class for every refusal this module makes."""


class RoadmapValidationError(TaskGraphError):
    """The document does not describe a valid dependency model.

    Carries every issue found, not just the first: an agent fixing a roadmap should see
    the whole list in one pass.
    """

    def __init__(self, issues: Sequence["ValidationIssue"]):
        self.issues = tuple(issues)
        super().__init__(format_issues(self.issues))


class DocumentDriftError(TaskGraphError):
    """A compare-and-swap write saw a document other than the one it read. `[P2] §2`"""

    def __init__(self, message: str, drift: "DocumentDrift | None" = None):
        self.drift = drift
        super().__init__(message)


class PrerequisiteCycleError(TaskGraphError):
    """A discovered prerequisite that would close a dependency cycle (`[P10]`).

    Not a `MutationError` subclass on purpose: a malformed request and a well-formed one
    that happens to be cyclic call for different responses — the second is a plan to
    repair, and `cycle` is the evidence the planner needs to repair it.
    """

    def __init__(self, task_id: str, prerequisite: str, cycle: Sequence[str]):
        self.task_id = task_id
        self.prerequisite = prerequisite
        self.cycle = tuple(cycle)
        super().__init__(
            f"{task_id} cannot depend on {prerequisite}: cycle "
            f"{' -> '.join([*self.cycle, self.cycle[0]])}"
        )


class MutationError(TaskGraphError):
    """A mutation named something that is not there, or asked for something incoherent."""


class ResourceSpecError(TaskGraphError):
    """A task's `resources:` block is not a mapping of known, non-negative amounts (`[P10B]`)."""


class FallbackSpecError(TaskGraphError):
    """A task's `fallback:` is not one of `FALLBACK_VALUES` (P12B F5)."""


# ══════════════════════════════════════════════════════════════════════════
# the lenient face — one copy of the quirks the document engine already had
# ══════════════════════════════════════════════════════════════════════════


def iter_yaml_matches(text: str, *, flavour: str = "strict") -> Iterator[re.Match]:
    """Every fenced yaml block as a `re.Match`, in document order.

    Callers that need source offsets (`maestro.docs.complete` splices sections out of the
    document around them) take the matches; callers that only need the text take
    `iter_yaml_blocks`.
    """
    return _FLAVOURS[flavour].finditer(text)


def iter_yaml_blocks(text: str, *, flavour: str = "strict") -> list[str]:
    return _FLAVOURS[flavour].findall(text)


def normalize_legacy_task(task):
    """needs-dan ⇒ auto-prep.

    A `mode: needs-dan` task with no explicit dispatch is treated as `dispatch: manual`,
    so it flows through the B14 prep-and-hand pipeline (one action + /ask + /approve)
    instead of being launched as a normal implementer, failing the human step, and
    dead-parking. Mutates its argument in place and returns it — both pinned. None-safe
    (`yaml.safe_load` may return None) and non-dicts pass straight through.
    """
    if isinstance(task, dict) and task.get("mode") == "needs-dan" and not task.get("dispatch"):
        task["dispatch"] = "manual"
    return task


def manual_parse_block(text: str, *, skip_comments: bool = True) -> dict:
    """Extremely small `key: value` reader for the single-level shape a roadmap block
    uses, for hosts with no PyYAML installed. `deps` is a bracketed list.

    This is `maestro.docs.depmap._manual_parse` and `maestro.status._manual_yaml`, which
    were the same function twice with one difference: depmap skips comment lines and
    status does not. That difference is real behaviour — `status` would report a task
    whose `# id: X` comment shadows nothing — so it is a parameter here rather than a
    silent unification.
    """
    out: dict = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        if skip_comments and line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "deps":
            out[key] = _bracketed_list(val)
        else:
            out[key] = val.strip().strip("'\"")
    return out


def _bracketed_list(value: str) -> list[str]:
    """`"[A, B]"` → `["A", "B"]`, the coercion depmap and status both apply to a `deps`
    that arrived as a string."""
    inner = value.strip().lstrip("[").rstrip("]").strip()
    return [d.strip().strip("'\"") for d in inner.split(",") if d.strip()]


def coerce_deps(task: dict) -> dict:
    """Normalise `deps` in place the way the tolerant readers do: a bracketed string
    becomes a list, and a missing key becomes an empty one."""
    if isinstance(task.get("deps"), str):
        task["deps"] = _bracketed_list(task["deps"])
    task.setdefault("deps", [])
    return task


def parse_block(text: str) -> dict | None:
    """One block → dict, or None when it is not a usable mapping.

    The strict-fence posture: a `yaml.YAMLError` or a non-mapping means the block is
    skipped entirely. `maestro.docs.roadmap`, `.consistency` and `.complete` all read
    this way.
    """
    try:
        data = yaml.load(text, Loader=_YAML_LOADER)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def parse_block_tolerant(text: str, *, skip_comments: bool = True) -> dict | None:
    """One block → dict, the way `maestro.docs.depmap` and `maestro.status` read it.

    Three differences from `parse_block`, each of them characterised behaviour: the
    import of PyYAML is inside the guard (so a host without it still renders a map), any
    exception — not only `YAMLError` — drops to `manual_parse_block`, and the `id` test
    is key *presence*, so a task written `id:` with no value survives here and is dropped
    by the strict reader.
    """
    try:
        import yaml as _yaml  # noqa: PLC0415 — the guard is the point; see docstring

        data = _yaml.safe_load(text)
    except Exception:
        data = manual_parse_block(text, skip_comments=skip_comments)
    if not isinstance(data, dict) or "id" not in data:
        return None
    return data


def legacy_deps(task: Mapping) -> list[str]:
    """`deps` flattened the way every legacy caller flattens it.

    Missing or falsy ⇒ `[]`. A bare string is *one* dependency, never five characters.
    Everything is stringified, because a roadmap id may be written `01` and read as int.
    """
    deps = task.get("deps") or []
    if isinstance(deps, str):
        deps = [deps] if deps else []
    return [str(d) for d in deps]


def scan_tasks(
    text: str,
    *,
    normalize: bool = True,
    require_id: bool = True,
) -> list[dict]:
    """Every task block in `text`, in document order, read strictly-but-leniently.

    `normalize` applies the needs-dan promotion; `require_id` drops a block whose `id` is
    missing or falsy. Both name a place where the existing readers genuinely differed,
    and the differences are characterised behaviour this extraction may not change.
    """
    tasks: list[dict] = []
    for raw in iter_yaml_blocks(text):
        task = parse_block(raw)
        if task is None:
            continue
        if normalize:
            task = normalize_legacy_task(task)
        if require_id and not task.get("id"):
            continue
        tasks.append(task)
    return tasks


def scan_tasks_tolerant(text: str, *, skip_comments: bool = True) -> list[dict]:
    """Every task block, the depmap/status reading: trimmed fences, a hand-parser
    fallback, `deps` coerced out of its bracketed-string spelling."""
    tasks: list[dict] = []
    for raw in iter_yaml_blocks(text, flavour="trimmed"):
        task = parse_block_tolerant(raw, skip_comments=skip_comments)
        if task is None:
            continue
        tasks.append(coerce_deps(task))
    return tasks


def search_yaml_block(text: str) -> re.Match | None:
    """The first fenced block under the loose fence spelling (trailing spaces after the
    language tag tolerated, closing fence not required to sit on its own line).
    `maestro.docs.upcoming` walks the document segment by segment and needs this one."""
    return YAML_BLOCK_LOOSE_RE.search(text)


# ══════════════════════════════════════════════════════════════════════════
# TaskSpec — `01_DATA_CONTRACTS_AND_SCHEMAS.md` §2.1
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TaskSpec:
    """The canonical parsed representation of one roadmap task."""

    task_id: str
    spec_revision: int = 1
    title: str = ""
    goal: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    group_id: str | None = None
    risk: str = "low"
    task_class: str = "feature"
    execution_kind: str = "ordinary"
    required_artifact_types: tuple[str, ...] = ()
    declared_effects: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    hold: bool = False
    self_modifying: bool = False
    resources: dict = field(default_factory=dict)
    fallback: str | None = None
    legacy_fields: dict = field(default_factory=dict)

    # --- derived views the legacy paths still ask for -------------------------------

    @property
    def is_group(self) -> bool:
        return bool(self.legacy_fields.get("group") is True)

    @property
    def is_complete(self) -> bool:
        return self.legacy_fields.get("status") == "complete"

    @property
    def is_superseded(self) -> bool:
        return self.legacy_fields.get("status") == "superseded"

    @property
    def notes(self) -> str:
        """The ROADMAP `notes:`, verbatim (P12 pilot finding D28: every brief carries them)."""
        raw = self.legacy_fields.get("notes")
        if isinstance(raw, (list, tuple)):
            return "\n".join(str(line) for line in raw).strip()
        return str(raw).strip() if raw else ""

    @property
    def dispatchable(self) -> bool:
        """A group container and a tombstone are structure, not work."""
        return not (self.is_group or self.is_superseded or self.is_complete)

    def with_dependencies(self, deps: Iterable[str]) -> "TaskSpec":
        seen: list[str] = []
        for dep in deps:
            dep = str(dep)
            if dep and dep != self.task_id and dep not in seen:
                seen.append(dep)
        return replace(self, depends_on=tuple(seen))

    def with_legacy(self, **updates) -> "TaskSpec":
        merged = dict(self.legacy_fields)
        for key, value in updates.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return replace(self, legacy_fields=merged)

    def to_yaml_dict(self) -> dict:
        """Back to the document's own vocabulary, losslessly.

        Consumed keys are re-emitted under the names the roadmap uses; everything else
        comes back out of `legacy_fields` in the order it went in. `normalize_spec` of
        this dict reproduces `self` — `tests/test_taskgraph.py` asserts the round-trip on
        the real roadmap shapes.
        """
        out: dict = {"id": self.task_id}
        if self.title:
            out["title"] = self.title
        if self.goal:
            out["short_desc"] = self.goal
        for key, value in self.legacy_fields.items():
            out[key] = value
        out["deps"] = list(self.depends_on)
        if self.group_id:
            out["group_id"] = self.group_id
        if self.risk != "low":
            out["risk"] = self.risk
        if self.task_class != "feature":
            out["task_class"] = self.task_class
        if self.acceptance_criteria:
            out["acceptance_criteria"] = list(self.acceptance_criteria)
        if self.required_artifact_types:
            out["required_artifact_types"] = list(self.required_artifact_types)
        if self.declared_effects:
            out["declared_effects"] = list(self.declared_effects)
        if self.scope:
            out["scope"] = list(self.scope)
        if self.constraints:
            out["constraints"] = list(self.constraints)
        if self.hold:
            out["hold"] = True
        if self.self_modifying:
            out["self_modifying"] = True
        if self.resources:
            out["resources"] = dict(self.resources)
        if self.fallback:
            out["fallback"] = self.fallback
        if self.spec_revision != 1:
            out["spec_revision"] = self.spec_revision
        return out

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.to_yaml_dict(), sort_keys=False, default_flow_style=False,
            allow_unicode=True, width=100,
        )


def _as_tuple(value) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(str(v) for v in value.values())
    return tuple(str(v) for v in value)


def _derive_execution_kind(data: Mapping) -> str:
    """`mode`/`dispatch`/`kind` → one of `EXECUTION_KINDS`.

    `dispatch: manual` wins over everything: B9 made it the permanent non-dispatchable
    marker, and `normalize_legacy_task` has already promoted a bare `mode: needs-dan`
    into it by the time this runs.
    """
    if data.get("dispatch") == "manual" or data.get("mode") == "needs-dan":
        return "manual"
    # `dispatch: async-job` is the fourth lane of the event loop (a long read-only job
    # that holds no session and no slot). The derivation missed it until `[P3]` needed it
    # to select a template; `manual` still wins above, which is what keeps a block
    # carrying both `async-job` and `needs-dan` reading as manual exactly as before.
    if data.get("dispatch") == "async-job":
        return "async"
    kind = str(data.get("kind") or "").strip()
    if kind in EXECUTION_KINDS:
        return kind
    if kind == "resumable" or data.get("resumable"):
        return "resumable"
    return "ordinary"


#: Appended to a `kind: dan` verification's check when it becomes a criterion (D25).
DAN_CRITERION_NOTE = (
    " (Dan verifies this by hand after the gate; do not attempt it, and do not fail "
    "the task because you cannot.)"
)


def verification_brief(data: Mapping) -> str:
    """Keep prep and its gate on the same declared commands and referenced paths."""
    rows = []
    for item in data.get('verifications') or ():
        if not isinstance(item, Mapping):
            continue
        rows.append(json.dumps(dict(item), ensure_ascii=False, sort_keys=True))
    return 'VERIFICATION CONTRACT (exact commands and paths):\n' + '\n'.join(rows) if rows else ''


def _derive_acceptance_criteria(data: Mapping) -> tuple[str, ...]:
    """Criteria come from `acceptance_criteria` when present, else from the `check` line
    of each declared verification — which is what the roadmap has always used to say what
    "done" means. The verifications themselves stay in `legacy_fields`; this is a view of
    them, not a move.
    """
    explicit = data.get("acceptance_criteria")
    if explicit:
        return _as_tuple(explicit)
    checks: list[str] = []
    verifications = data.get("verifications")
    if isinstance(verifications, list):
        for item in verifications:
            if isinstance(item, Mapping) and item.get("check"):
                check = str(item["check"])
                if str(item.get("kind") or "") == "dan":
                    # P12 pilot finding D25: an agent told "a real Duet exists in Spotify"
                    # tried to make it so, and failed the task for want of leaving the
                    # worktree. Dan's checks are his, done by hand after the gate.
                    check += DAN_CRITERION_NOTE
                checks.append(check)
    return tuple(checks)


def parse_fallback(value, *, task_id: str = "") -> str | None:
    """A task's `fallback:` → `None` or one of `FALLBACK_VALUES` (P12B F5)."""
    if value is None:
        return None
    if isinstance(value, str) and value in FALLBACK_VALUES:
        return value
    where = f"{task_id}: " if task_id else ""
    raise FallbackSpecError(
        f"{where}fallback must be one of {', '.join(FALLBACK_VALUES)}, got {value!r}"
    )


def parse_resources(value, *, task_id: str = "") -> dict:
    """A task's `resources:` block → `{dimension: amount}`, only the non-zero amounts.

    Absent or null requests nothing. Anything else must be a mapping over `RESOURCE_KEYS`
    with finite, non-negative numbers; an unknown key is refused rather than ignored,
    because a misspelt `ram: 8000` silently requesting nothing is the oversubscription
    this block exists to prevent. Amounts keep the type they were written with, so a
    round-trip through `TaskSpec.to_yaml_dict` does not rewrite `8000` as `8000.0`.
    """
    where = f"task {task_id!r}: " if task_id else ""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ResourceSpecError(
            f"{where}resources must be a mapping of {', '.join(RESOURCE_KEYS)}, "
            f"not {type(value).__name__}"
        )
    unknown = sorted(str(k) for k in value if k not in RESOURCE_KEYS)
    if unknown:
        raise ResourceSpecError(
            f"{where}unknown resources key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(RESOURCE_KEYS)}"
        )
    out: dict = {}
    for key in RESOURCE_KEYS:
        if key not in value:
            continue
        amount = value[key]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ResourceSpecError(f"{where}resources.{key} must be a number, got {amount!r}")
        if amount != amount or amount in (float("inf"), float("-inf")) or amount < 0:
            raise ResourceSpecError(
                f"{where}resources.{key} must be a finite non-negative number, got {amount!r}"
            )
        if amount:
            out[key] = amount
    return out


#: The spec keys a strict read drops (with an issue) rather than refusing the whole task.
_DROPPABLE = (
    (ResourceSpecError, "resources", "invalid_resources"),
    (FallbackSpecError, "fallback", "invalid_fallback"),
)


def _normalize_dropping_invalid(data: Mapping) -> tuple["TaskSpec", list[tuple[str, str]]]:
    """`normalize_spec(data)`, re-normalized without each invalid droppable key.

    Returns the spec and one `(issue_code, message)` per key that was dropped.
    """
    data = dict(data)
    refusals: list[tuple[str, str]] = []
    while True:
        try:
            return normalize_spec(data), refusals
        except tuple(error for error, _, _ in _DROPPABLE) as exc:
            for error, key, code in _DROPPABLE:
                if isinstance(exc, error):
                    refusals.append((code, str(exc)))
                    data.pop(key, None)
                    break


def normalize_spec(data: Mapping) -> TaskSpec:
    """One legacy task dict → `TaskSpec`, keeping every unmapped field."""
    if not isinstance(data, Mapping):
        raise MutationError(f"task block is {type(data).__name__}, not a mapping")
    task_id = str(data.get("id") or "").strip()
    if not task_id:
        raise MutationError("task block has no id")
    # `group` is two things in the wild: `group: true` marks a container (what
    # `split_task` writes), `group: <id>` names a parent. Only the second is a group_id,
    # and either way the key stays in `legacy_fields` — `TaskSpec.is_group` reads it back.
    legacy = {k: v for k, v in data.items() if k not in _CONSUMED_KEYS}
    risk = str(data.get("risk") or "low")
    risk = _RETIRED_RISKS.get(risk, risk)
    task_class = str(data.get("task_class") or "feature")
    group_id = data.get("group_id") or (
        data.get("group") if isinstance(data.get("group"), str) else None
    )
    return TaskSpec(
        task_id=task_id,
        spec_revision=int(data.get("spec_revision") or 1),
        title=str(data.get("title") or ""),
        goal=str(data.get("goal") or data.get("short_desc") or ""),
        acceptance_criteria=_derive_acceptance_criteria(data),
        depends_on=tuple(dict.fromkeys(legacy_deps(data))),
        group_id=str(group_id) if isinstance(group_id, str) and group_id else None,
        risk=risk if risk in RISKS else "low",
        task_class=task_class if task_class in TASK_CLASSES else "feature",
        execution_kind=_derive_execution_kind(data),
        required_artifact_types=_as_tuple(data.get("required_artifact_types")),
        declared_effects=_as_tuple(data.get("declared_effects")),
        scope=_as_tuple(data.get("scope")),
        constraints=_as_tuple(data.get("constraints")),
        hold=bool(data.get("hold")),
        self_modifying=bool(data.get("self_modifying")),
        resources=parse_resources(data.get("resources"), task_id=task_id),
        fallback=parse_fallback(data.get("fallback"), task_id=task_id),
        legacy_fields=legacy,
    )


# ══════════════════════════════════════════════════════════════════════════
# the document — blocks with source positions, spliced rather than rewritten
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TaskBlock:
    """One fenced yaml block, with enough source position to report a line and to splice
    a replacement in without disturbing a single byte around it."""

    index: int
    raw: str
    body_start: int          # char offset of the first byte of yaml
    body_end: int            # char offset one past the last byte of yaml
    start_line: int          # 1-based line of the first yaml line

    def line_of(self, offset_line: int) -> int:
        """Absolute document line for a 0-based line number inside this block."""
        return self.start_line + max(offset_line, 0)


@dataclass(frozen=True)
class RoadmapDocument:
    """`docs/ROADMAP.md` as read, with its blocks located."""

    text: str
    blocks: tuple[TaskBlock, ...]
    path: Path | None = None

    @property
    def document_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def block_hashes(self) -> dict[str, str]:
        """`task_id → sha256 of its block`, for the ids that parse. The per-task half of
        the CAS record: a document hash says *something* changed, these say what."""
        out: dict[str, str] = {}
        for block in self.blocks:
            data = parse_block(block.raw)
            if isinstance(data, dict) and data.get("id"):
                out[str(data["id"])] = hashlib.sha256(block.raw.encode("utf-8")).hexdigest()
        return out


def parse_document(text: str, *, path: Path | None = None) -> RoadmapDocument:
    blocks: list[TaskBlock] = []
    for index, match in enumerate(iter_yaml_matches(text)):
        body_start = match.start(1)
        blocks.append(TaskBlock(
            index=index,
            raw=match.group(1),
            body_start=body_start,
            body_end=match.end(1),
            start_line=text.count("\n", 0, body_start) + 1,
        ))
    return RoadmapDocument(text=text, blocks=tuple(blocks), path=path)


def load_document(path: Path) -> RoadmapDocument:
    return parse_document(Path(path).read_text(encoding="utf-8"), path=Path(path))


# ══════════════════════════════════════════════════════════════════════════
# validation
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    block_index: int | None = None
    line: int | None = None
    task_id: str | None = None
    cycle: tuple[str, ...] = ()

    def __str__(self) -> str:
        where = []
        if self.block_index is not None:
            where.append(f"block {self.block_index}")
        if self.line is not None:
            where.append(f"line {self.line}")
        prefix = f"{'/'.join(where)}: " if where else ""
        return f"{prefix}[{self.code}] {self.message}"


def format_issues(issues: Sequence[ValidationIssue]) -> str:
    if not issues:
        return "roadmap is valid"
    body = "\n".join(f"  - {issue}" for issue in issues)
    return f"{len(issues)} roadmap problem(s):\n{body}"


def _yaml_issue(block: TaskBlock, exc: yaml.YAMLError) -> ValidationIssue:
    mark = getattr(exc, "problem_mark", None)
    line = block.line_of(mark.line) if mark is not None else block.start_line
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    return ValidationIssue(
        code="malformed_yaml",
        message=f"block does not parse as YAML: {problem}",
        block_index=block.index,
        line=line,
    )


def find_cycles(edges: Mapping[str, Iterable[str]]) -> list[tuple[str, ...]]:
    """Every elementary cycle, each as a concrete path, deterministically ordered.

    `graphlib` decides *whether* the graph is acyclic — that is what the spec names and
    it is the authority here. It reports one cycle, though, and a roadmap can hold
    several independent ones; an agent asked to fix a tangle deserves all of them, so
    once graphlib has said "cyclic" this walks the graph itself and enumerates the rest.
    """
    nodes = sorted(edges)
    successors = {n: sorted({str(d) for d in edges.get(n, ()) if str(d) in edges}) for n in nodes}
    cycles: set[tuple[str, ...]] = set()

    def walk(start: str, node: str, path: list[str], on_path: set[str]) -> None:
        for nxt in successors[node]:
            if nxt == start:
                cycles.add(_canonical_cycle(path))
            elif nxt not in on_path and nxt > start:
                # `nxt > start` keeps each elementary cycle to a single enumeration,
                # rooted at its smallest member.
                path.append(nxt)
                on_path.add(nxt)
                walk(start, nxt, path, on_path)
                on_path.discard(nxt)
                path.pop()

    for node in nodes:
        walk(node, node, [node], {node})
    return sorted(cycles)


def _canonical_cycle(path: Sequence[str]) -> tuple[str, ...]:
    """Rotate a cycle so it starts at its smallest member — two spellings of one cycle
    must not be reported as two findings."""
    if not path:
        return ()
    pivot = min(range(len(path)), key=lambda i: path[i])
    return tuple(path[pivot:]) + tuple(path[:pivot])


def prerequisite_cycle(
    edges: Mapping[str, Iterable[str]], child: str, prerequisite: str
) -> tuple[str, ...] | None:
    """The cycle `child -> prerequisite` would close, as a path, or `None`.

    `edges` maps a task to what it depends on. The new edge closes a cycle exactly when
    `prerequisite` already (transitively) depends on `child`; the shortest such chain is
    returned, starting at `child`, so the refusal names the concrete tasks involved.
    """
    child, prerequisite = str(child), str(prerequisite)
    if child == prerequisite:
        return (child,)
    parents: dict[str, str] = {prerequisite: ""}
    frontier = [prerequisite]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for dep in sorted(str(d) for d in edges.get(node, ())):
                if dep in parents:
                    continue
                parents[dep] = node
                if dep == child:
                    path = [child]
                    step = node
                    while step:
                        path.append(step)
                        step = parents[step]
                    # path is child <- ... <- prerequisite; the cycle runs child -> prerequisite -> ... -> child
                    return (child, *reversed(path[1:]))
                nxt.append(dep)
        frontier = nxt
    return None


def transitive_reduction(
    deps: Mapping[str, Iterable[str] | str],
    *,
    known: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, list[str]]:
    """`deps` with every transitively redundant dependency removed (P12 finding D21).

    `A -> C` is redundant when another dep `B` of `A` already reaches `C`: `A` waits for
    `C` through `B` anyway, and the extra edge is only noise on the map. Only the tasks in
    `deps` (the blocks present in the ROADMAP) are reduced. `known` holds the deps of
    graduated tasks, from the completion registry: extra reachability, never reduced, and
    never into a task still in `deps` — a finished task cannot make anyone wait any more.

    Deterministic, order-preserving; an unknown dep id is kept unless another dep reaches
    it; a task on a dependency cycle keeps its deps untouched, and two deps on one cycle
    never remove each other.
    """
    present = {str(t): legacy_deps({"deps": d}) for t, d in deps.items()}
    edges: dict[str, list[str]] = dict(present)
    for task_id, extra in (known or {}).items():
        if str(task_id) not in present:
            edges[str(task_id)] = [d for d in legacy_deps({"deps": extra}) if d not in present]
    reach_cache: dict[str, set[str]] = {}

    def reach(node: str) -> set[str]:
        """Every node reachable from `node` over one or more edges."""
        if node not in reach_cache:
            seen: set[str] = set()
            frontier = list(edges.get(node, ()))
            while frontier:
                nxt = frontier.pop()
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.extend(edges.get(nxt, ()))
            reach_cache[node] = seen
        return reach_cache[node]

    out: dict[str, list[str]] = {}
    for task_id, task_deps in present.items():
        if task_id in reach(task_id):
            out[task_id] = list(task_deps)
            continue
        out[task_id] = [
            dep for dep in task_deps
            if not any(
                other != dep and dep in reach(other) and other not in reach(dep)
                for other in task_deps
            )
        ]
    return out


_DEPS_LINE_RE = re.compile(r"^deps\s*:(.*)$")
_DEPS_ITEM_RE = re.compile(r"^(\s*)-\s*(.*?)\s*$")


def _unquote(token: str) -> str:
    return token.strip().strip("'\"")


def _without_deps(raw: str, removed: set[str]) -> str | None:
    """`raw` with `removed` taken out of its top-level `deps:` — that line or those list
    items only, every other byte kept. None for a spelling this cannot edit exactly."""
    lines = raw.splitlines(keepends=True)
    for i, line in enumerate(lines):
        match = _DEPS_LINE_RE.match(line.rstrip("\n"))
        if match is None:
            continue
        value = match.group(1).strip()
        if value.startswith("[") and value.endswith("]"):
            kept = [t.strip() for t in value[1:-1].split(",")
                    if t.strip() and _unquote(t) not in removed]
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"{line[:line.index(':') + 1]} [{', '.join(kept)}]{newline}"
            return "".join(lines)
        if value:
            return None
        j = i + 1
        while j < len(lines):
            item = _DEPS_ITEM_RE.match(lines[j].rstrip("\n"))
            if item is None:
                break
            if _unquote(item.group(2)) in removed:
                del lines[j]
            else:
                j += 1
        return "".join(lines)
    return None


def reduce_document_deps(
    text: str,
    *,
    known: Mapping[str, Iterable[str]] | None = None,
    skip: Iterable[str] = (),
) -> tuple[str, dict[str, list[str]]]:
    """`text` with `transitive_reduction` applied to its blocks' `deps`, and what went.

    A minimal edit: only the `deps:` line (or list items) of a block that loses a dep
    changes. A block whose rewrite does not parse back to exactly the reduced deps with
    every other field equal is left as it was; so is a block named in `skip`. Returns
    `(new_text, {task_id: [removed deps]})`; `(text, {})` when nothing is redundant.
    """
    document = parse_document(text)
    blocks: dict[str, tuple[TaskBlock, dict]] = {}
    for block in document.blocks:
        data = parse_block(block.raw)
        if isinstance(data, dict) and data.get("id") and str(data["id"]) not in blocks:
            blocks[str(data["id"])] = (block, data)
    reduced = transitive_reduction(
        {tid: legacy_deps(data) for tid, (_, data) in blocks.items()}, known=known)
    skip = {str(s) for s in skip}
    edits: list[tuple[int, int, str]] = []
    removed_by_id: dict[str, list[str]] = {}
    for task_id, (block, data) in blocks.items():
        removed = [d for d in legacy_deps(data) if d not in reduced[task_id]]
        if not removed or task_id in skip:
            continue
        raw = _without_deps(block.raw, set(removed))
        check = parse_block(raw) if raw is not None else None
        if (not isinstance(check, dict) or legacy_deps(check) != reduced[task_id]
                or {k: v for k, v in check.items() if k != "deps"}
                != {k: v for k, v in data.items() if k != "deps"}):
            continue
        edits.append((block.body_start, block.body_end, raw))
        removed_by_id[task_id] = removed
    for start, end, body in sorted(edits, reverse=True):
        text = text[:start] + body + text[end:]
    return text, removed_by_id


def _dep_token(dep: str, *, flow: bool) -> str:
    """`dep` as a yaml scalar that reads back as this string: plain when it does
    (`T1-followups`), single-quoted otherwise (`'10'`, which would read as an int)."""
    probe = f"[{dep}]" if flow else f"- {dep}"
    try:
        if yaml.safe_load(probe) == [dep]:
            return dep
    except yaml.YAMLError:
        pass
    return "'" + dep.replace("'", "''") + "'"


def _with_deps(raw: str, extra: list[str]) -> str | None:
    """`raw` with `extra` appended to its top-level `deps:` — that line, or one more list
    item each, every other byte kept. None for a spelling this cannot edit exactly."""
    lines = raw.splitlines(keepends=True)
    for i, line in enumerate(lines):
        match = _DEPS_LINE_RE.match(line.rstrip("\n"))
        if match is None:
            continue
        value = match.group(1).strip()
        if value.startswith("[") and value.endswith("]"):
            kept = [t.strip() for t in value[1:-1].split(",") if t.strip()]
            kept += [_dep_token(d, flow=True) for d in extra]
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"{line[:line.index(':') + 1]} [{', '.join(kept)}]{newline}"
            return "".join(lines)
        if value:
            return None
        j, indent = i + 1, None
        while j < len(lines):
            item = _DEPS_ITEM_RE.match(lines[j].rstrip("\n"))
            if item is None:
                break
            indent = item.group(1)
            j += 1
        if indent is None:
            return None
        if not lines[j - 1].endswith("\n"):
            lines[j - 1] += "\n"
        lines[j:j] = [f"{indent}- {_dep_token(d, flow=False)}\n" for d in extra]
        return "".join(lines)
    return None


def add_document_deps(
    text: str,
    wanted: Mapping[str, Iterable[str]],
    *,
    skip: Iterable[str] = (),
) -> tuple[str, dict[str, list[str]]]:
    """`text` with each `wanted[task]` dep added to that block's `deps` (P13 GDT-11: a
    dependent waits for a `before_dependents` follow-up task); the twin of
    `reduce_document_deps`.

    A minimal edit of the `deps:` line or list. A dep already there is not added again; a
    block whose rewrite does not parse back to exactly the old deps plus the new, with
    every other field equal, is left as it was; so is a block named in `skip`. Returns
    `(new_text, {task_id: [added deps]})`.
    """
    skip = {str(s) for s in skip}
    wanted = {str(t): [str(d) for d in deps] for t, deps in wanted.items()}
    edits: list[tuple[int, int, str]] = []
    added_by_id: dict[str, list[str]] = {}
    seen: set[str] = set()
    for block in parse_document(text).blocks:
        data = parse_block(block.raw)
        if not isinstance(data, dict) or not data.get("id"):
            continue
        task_id = str(data["id"])
        if task_id in seen or task_id not in wanted or task_id in skip:
            seen.add(task_id)
            continue
        seen.add(task_id)
        old = legacy_deps(data)
        extra = list(dict.fromkeys(d for d in wanted[task_id] if d not in old))
        if not extra:
            continue
        raw = _with_deps(block.raw, extra)
        check = parse_block(raw) if raw is not None else None
        if (not isinstance(check, dict) or legacy_deps(check) != old + extra
                or {k: v for k, v in check.items() if k != "deps"}
                != {k: v for k, v in data.items() if k != "deps"}):
            continue
        edits.append((block.body_start, block.body_end, raw))
        added_by_id[task_id] = extra
    for start, end, body in sorted(edits, reverse=True):
        text = text[:start] + body + text[end:]
    return text, added_by_id


def validate(
    document: RoadmapDocument,
    *,
    known_ids: Iterable[str] = (),
) -> list[ValidationIssue]:
    """Every problem in `document`, located. `known_ids` are ids that exist outside the
    document — graduated tasks in the completion registry — and satisfy a dependency
    without appearing as a block.
    """
    issues: list[ValidationIssue] = []
    specs: dict[str, TaskSpec] = {}
    first_seen: dict[str, TaskBlock] = {}
    external = {str(i) for i in known_ids}

    for block in document.blocks:
        try:
            data = yaml.load(block.raw, Loader=_YAML_LOADER)
        except yaml.YAMLError as exc:
            issues.append(_yaml_issue(block, exc))
            continue
        if data is None:
            # Legacy ROADMAPs include fully commented examples, not task blocks.
            lines = [line.strip() for line in block.raw.splitlines() if line.strip()]
            if lines and all(line.startswith("#") for line in lines):
                continue
            issues.append(ValidationIssue(
                code="empty_block", message="fenced yaml block is empty",
                block_index=block.index, line=block.start_line,
            ))
            continue
        if not isinstance(data, dict):
            issues.append(ValidationIssue(
                code="not_a_mapping",
                message=f"fenced yaml block is a {type(data).__name__}, not a task mapping",
                block_index=block.index, line=block.start_line,
            ))
            continue
        normalize_legacy_task(data)
        if not data.get("id"):
            issues.append(ValidationIssue(
                code="missing_id", message="task block declares no id",
                block_index=block.index, line=block.start_line,
            ))
            continue
        task_id = str(data["id"])
        if task_id in specs:
            issues.append(ValidationIssue(
                code="duplicate_id",
                message=(
                    f"task id {task_id!r} is declared twice; first declaration is at line "
                    f"{first_seen[task_id].start_line} (block {first_seen[task_id].index})"
                ),
                block_index=block.index, line=block.start_line, task_id=task_id,
            ))
            continue
        specs[task_id], refusals = _normalize_dropping_invalid(data)
        for code, message in refusals:
            issues.append(ValidationIssue(
                code=code, message=message,
                block_index=block.index, line=block.start_line, task_id=task_id,
            ))
        first_seen[task_id] = block

    for task_id, spec in specs.items():
        block = first_seen[task_id]
        for dep in spec.depends_on:
            if dep in specs or dep in external:
                continue
            issues.append(ValidationIssue(
                code="missing_prerequisite",
                message=(
                    f"task {task_id!r} depends on {dep!r}, which is neither a task in this "
                    "document nor a graduated task in the completion registry"
                ),
                block_index=block.index, line=block.start_line, task_id=task_id,
            ))

    edges = {tid: spec.depends_on for tid, spec in specs.items()}
    if not _is_acyclic(edges):
        for cycle in find_cycles(edges):
            head = cycle[0]
            block = first_seen.get(head)
            issues.append(ValidationIssue(
                code="cycle",
                message="dependency cycle: " + " -> ".join([*cycle, head]),
                block_index=block.index if block else None,
                line=block.start_line if block else None,
                task_id=head,
                cycle=cycle,
            ))
    return issues


def _is_acyclic(edges: Mapping[str, Iterable[str]]) -> bool:
    """`graphlib.TopologicalSorter` is the authority on acyclicity, per `[P2] §2`."""
    sorter = graphlib.TopologicalSorter(
        {tid: [d for d in deps if d in edges] for tid, deps in edges.items()}
    )
    try:
        sorter.prepare()
    except graphlib.CycleError:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════
# the graph
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Acceptance:
    """How a task came to be considered done, and what that entitles it to.

    `legacy_accepted` is the import of a completion this system never witnessed: the
    registry says the task graduated, and that is enough to unblock its dependents. It is
    not enough to stand in for a verification receipt, and it may never satisfy a cache
    probe — `[INV-04]` fails closed on missing evidence, and a historical claim is not
    evidence.
    """

    task_id: str
    kind: str                       # 'legacy_accepted' | 'in_document_complete'
    satisfies_dependencies: bool = True
    supplies_evidence: bool = False
    cache_eligible: bool = False


@dataclass(frozen=True)
class TaskGraph:
    """A validated roadmap: specs, a topological order, and the completions that unblock
    it."""

    specs: dict[str, TaskSpec]
    order: tuple[str, ...]
    acceptances: dict[str, Acceptance] = field(default_factory=dict)
    document_hash: str = ""

    # --- queries --------------------------------------------------------------------

    def __contains__(self, task_id: object) -> bool:
        return str(task_id) in self.specs

    def __len__(self) -> int:
        return len(self.specs)

    def spec(self, task_id: str) -> TaskSpec:
        try:
            return self.specs[str(task_id)]
        except KeyError:
            raise MutationError(f"no task {task_id!r} in the roadmap") from None

    @property
    def completed_ids(self) -> frozenset[str]:
        return frozenset(self.acceptances)

    def prerequisites(self, task_id: str) -> tuple[str, ...]:
        return self.spec(task_id).depends_on

    def dependents(self, task_id: str) -> tuple[str, ...]:
        tid = str(task_id)
        return tuple(sorted(t for t, s in self.specs.items() if tid in s.depends_on))

    def transitive_dependents(self, task_id: str) -> tuple[str, ...]:
        seen: set[str] = set()
        frontier = [str(task_id)]
        while frontier:
            current = frontier.pop()
            for child in self.dependents(current):
                if child not in seen:
                    seen.add(child)
                    frontier.append(child)
        return tuple(sorted(seen))

    def unmet(self, task_id: str) -> tuple[str, ...]:
        done = self.completed_ids
        return tuple(d for d in self.prerequisites(task_id) if d not in done)

    def ready(self) -> tuple[str, ...]:
        """Tasks whose prerequisites are all discharged, in topological order.

        Holds, groups, tombstones and already-complete tasks are not ready — they are
        structure or they are finished. Dispatch policy beyond that (autonomous vs
        manual, self-modifying, parked) stays where it already lives, in
        `maestro.docs.roadmap`; this is the dependency answer, not the scheduling one.
        """
        done = self.completed_ids
        return tuple(
            tid for tid in self.order
            if tid not in done
            and self.specs[tid].dispatchable
            and not self.specs[tid].hold
            and all(d in done for d in self.specs[tid].depends_on)
        )

    def supplies_evidence(self, task_id: str) -> bool:
        acceptance = self.acceptances.get(str(task_id))
        return bool(acceptance and acceptance.supplies_evidence)

    def cache_eligible(self, task_id: str) -> bool:
        acceptance = self.acceptances.get(str(task_id))
        return bool(acceptance and acceptance.cache_eligible)


def _topological_order(specs: Mapping[str, TaskSpec]) -> tuple[str, ...]:
    sorter = graphlib.TopologicalSorter(
        {tid: sorted(d for d in spec.depends_on if d in specs) for tid, spec in specs.items()}
    )
    return tuple(sorter.static_order())


def build_graph(
    document: RoadmapDocument,
    *,
    registry_ids: Iterable[str] = (),
    strict: bool = True,
) -> TaskGraph:
    """Validate `document` and turn it into a `TaskGraph`.

    `registry_ids` are the graduated tasks from `.orchestrator/completed_tasks.json`.
    They are both the `known_ids` that make a dependency on a graduated task legal and
    the seed of the `legacy_accepted` set.
    """
    registry = {str(i) for i in registry_ids}
    if strict:
        issues = validate(document, known_ids=registry)
        if issues:
            raise RoadmapValidationError(issues)

    specs: dict[str, TaskSpec] = {}
    for block in document.blocks:
        data = parse_block(block.raw)
        if not isinstance(data, dict) or not data.get("id"):
            continue
        normalize_legacy_task(data)
        task_id = str(data["id"])
        if task_id in specs:
            continue
        # Only refused with strict=False: a lenient reader still gets the dependency
        # structure. The launch loop re-parses the block and refuses to admit it.
        specs[task_id], _refusals = _normalize_dropping_invalid(data)

    acceptances: dict[str, Acceptance] = {
        tid: Acceptance(task_id=tid, kind="legacy_accepted") for tid in sorted(registry)
    }
    for tid, spec in specs.items():
        if spec.is_complete:
            acceptances[tid] = Acceptance(task_id=tid, kind="in_document_complete")

    return TaskGraph(
        specs=specs,
        order=_topological_order(specs),
        acceptances=acceptances,
        document_hash=document.document_hash,
    )


# ══════════════════════════════════════════════════════════════════════════
# mutation
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class _Edit:
    block_index: int | None       # None ⇒ a new section appended at the end
    spec: TaskSpec


class RoadmapTransaction:
    """Stage mutations, then commit them as one document rewrite — or not at all.

    Nothing reaches the text until `render()`/`commit()`, and both validate the resulting
    graph first. A `split_task` that would strand a downstream obligation, an
    `add_dependency` that would close a cycle: the transaction raises and the document on
    disk is exactly as it was.
    """

    def __init__(self, document: RoadmapDocument, *, registry_ids: Iterable[str] = ()):
        self.document = document
        self.registry_ids = {str(i) for i in registry_ids}
        self._blocks_by_id: dict[str, TaskBlock] = {}
        self.specs: dict[str, TaskSpec] = {}
        for block in document.blocks:
            data = parse_block(block.raw)
            if not isinstance(data, dict) or not data.get("id"):
                continue
            normalize_legacy_task(data)
            task_id = str(data["id"])
            if task_id in self.specs:
                continue
            self.specs[task_id] = normalize_spec(data)
            self._blocks_by_id[task_id] = block
        self._dirty: set[str] = set()
        self._added: list[str] = []
        self.affected: set[str] = set()
        self.log: list[str] = []

    # --- internals ------------------------------------------------------------------

    def _require(self, task_id: str) -> TaskSpec:
        tid = str(task_id)
        if tid not in self.specs:
            raise MutationError(f"no task {tid!r} in the roadmap")
        return self.specs[tid]

    def _set(self, spec: TaskSpec, *, bump: bool = True) -> None:
        previous = self.specs.get(spec.task_id)
        if bump and previous is not None and spec.spec_revision == previous.spec_revision:
            spec = replace(spec, spec_revision=previous.spec_revision + 1)
        self.specs[spec.task_id] = spec
        self._dirty.add(spec.task_id)
        self.affected.add(spec.task_id)

    # --- the five mutations ---------------------------------------------------------

    def add_task(self, spec: TaskSpec | Mapping) -> TaskSpec:
        if isinstance(spec, Mapping):
            spec = normalize_spec(spec)
        if spec.task_id in self.specs:
            raise MutationError(f"task {spec.task_id!r} already exists")
        self.specs[spec.task_id] = spec
        self._added.append(spec.task_id)
        self.affected.add(spec.task_id)
        self.log.append(f"add_task {spec.task_id}")
        return spec

    def add_dependency(self, child: str, parent: str) -> None:
        """`child` gains a hard prerequisite on `parent`.

        Both must exist — a prerequisite on a task nobody declared is exactly the dangling
        edge this phase exists to stop. A cycle is not refused here but at commit, so a
        transaction that closes a cycle and then reopens it is still legal.
        """
        child, parent = str(child), str(parent)
        spec = self._require(child)
        if parent not in self.specs and parent not in self.registry_ids:
            raise MutationError(
                f"cannot depend on {parent!r}: it is neither a task in the roadmap nor a "
                "graduated task in the completion registry"
            )
        if child == parent:
            raise MutationError(f"task {child!r} cannot depend on itself")
        if parent in spec.depends_on:
            return
        self._set(spec.with_dependencies([*spec.depends_on, parent]))
        self.log.append(f"add_dependency {child} -> {parent}")

    def discover_prerequisite(
        self, task_id: str, prerequisite: "TaskSpec | Mapping | str"
    ) -> TaskSpec:
        """A prerequisite found while `task_id` was running (`[P10]`, `§5.3`).

        Unlike `add_dependency`, a cycle is refused *now* rather than at commit: a
        discovered prerequisite is applied to a task that is already active, and a
        transaction left holding a cyclic edge until someone commits is a transaction some
        planner may keep building on. The edge is checked before anything is staged, so a
        refused discovery leaves the transaction exactly as it was.

        `prerequisite` is either an existing task id (roadmap or registry) or a new spec,
        which is added. Returns the prerequisite's spec (a registry-only id has none, and
        returns the child's instead).
        """
        child = self._require(task_id)
        new: TaskSpec | None = None
        if isinstance(prerequisite, str):
            parent_id = prerequisite
        else:
            new = normalize_spec(prerequisite) if isinstance(prerequisite, Mapping) else prerequisite
            parent_id = new.task_id
            if parent_id in self.specs:
                raise MutationError(
                    f"prerequisite {parent_id!r} already exists; discover it by id"
                )
        edges = {tid: set(spec.depends_on) for tid, spec in self.specs.items()}
        if new is not None:
            edges[parent_id] = set(new.depends_on)
        cycle = prerequisite_cycle(edges, child.task_id, parent_id)
        if cycle:
            raise PrerequisiteCycleError(child.task_id, parent_id, cycle)
        if new is not None:
            self.add_task(new)
        self.add_dependency(child.task_id, parent_id)
        self.log.append(f"discover_prerequisite {child.task_id} -> {parent_id}")
        return self.specs.get(parent_id, self.specs[child.task_id])

    def split_task(self, parent_id: str, child_specs: Sequence[TaskSpec | Mapping]) -> list[TaskSpec]:
        """Replace one task with several, preserving every obligation it carried.

        The parent stays in the document as a *group*: it is no longer dispatchable, but
        deleting it would erase the only record of what the split came from, and a
        dependent that a downstream reader has not re-read yet would lose its anchor.

        The two obligations are discharged explicitly:

        * **Incoming.** The parent's own prerequisites move to the *entry* children —
          those with no dependency on a sibling. If none qualifies (the children form a
          cycle among themselves) the split is refused.
        * **Outgoing.** Every task that depended on the parent is rewired onto the *exit*
          children — those no sibling depends on. Nothing downstream may start before the
          whole split has finished, which is precisely what the parent guaranteed.
        """
        parent = self._require(parent_id)
        if not child_specs:
            raise MutationError(f"split of {parent.task_id!r} needs at least one child")
        children = [normalize_spec(c) if isinstance(c, Mapping) else c for c in child_specs]
        child_ids = [c.task_id for c in children]
        if len(set(child_ids)) != len(child_ids):
            raise MutationError(f"split of {parent.task_id!r} declares a duplicate child id")
        for child in children:
            if child.task_id in self.specs:
                raise MutationError(f"task {child.task_id!r} already exists")

        sibling_ids = set(child_ids)
        entry = [c for c in children if not (set(c.depends_on) & sibling_ids)]
        depended_on = {d for c in children for d in c.depends_on if d in sibling_ids}
        exit_children = [c for c in children if c.task_id not in depended_on]
        if not entry:
            raise MutationError(
                f"split of {parent.task_id!r} has no entry child: every child depends on a "
                "sibling, so the parent's prerequisites could not be inherited"
            )
        if not exit_children:
            raise MutationError(
                f"split of {parent.task_id!r} has no exit child: every child is depended on "
                "by a sibling, so downstream obligations could not be satisfied"
            )

        dependents = [t for t, s in self.specs.items() if parent.task_id in s.depends_on]
        exit_ids = [c.task_id for c in exit_children]

        for child in children:
            inherited = list(child.depends_on)
            if child in entry:
                inherited = [*parent.depends_on, *inherited]
            spec = replace(child, group_id=parent.task_id).with_dependencies(inherited)
            self.add_task(spec)

        self._set(parent.with_legacy(group=True, split_into=list(child_ids)))
        for tid in dependents:
            spec = self.specs[tid]
            rewired = [d for d in spec.depends_on if d != parent.task_id] + exit_ids
            self._set(spec.with_dependencies(rewired))
        self.log.append(f"split_task {parent.task_id} -> {', '.join(child_ids)}")
        return [self.specs[cid] for cid in child_ids]

    def supersede_task(self, old_id: str, new_id: str) -> None:
        """`new_id` takes over from `old_id`: it inherits the prerequisites and every
        dependent, and `old_id` becomes a tombstone that can never be dispatched."""
        old = self._require(old_id)
        new = self._require(new_id)
        if old.task_id == new.task_id:
            raise MutationError(f"task {old.task_id!r} cannot supersede itself")
        inherited = [d for d in [*new.depends_on, *old.depends_on] if d != new.task_id]
        self._set(new.with_dependencies(inherited))
        for tid, spec in list(self.specs.items()):
            if tid in (old.task_id, new.task_id) or old.task_id not in spec.depends_on:
                continue
            rewired = [new.task_id if d == old.task_id else d for d in spec.depends_on]
            self._set(spec.with_dependencies(rewired))
        self._set(
            self.specs[old.task_id].with_legacy(status="superseded", superseded_by=new.task_id)
        )
        self.log.append(f"supersede_task {old.task_id} -> {new.task_id}")

    def invalidate_inputs(self, task_id: str) -> tuple[str, ...]:
        """Declare the task's inputs stale, and every downstream task's with them.

        Invalidation is transitive on purpose: if a prerequisite's output changed, the
        evidence a dependent produced against the old output proves nothing about the new
        one. `[INV-04]`/`D06` — an unchanged public signature is not proof of unchanged
        behaviour, so the sweep is the whole downstream cone, not the direct children.

        Returns every task whose cached evidence must now be treated as invalidated.
        """
        spec = self._require(task_id)
        cone = [spec.task_id, *self._transitive_dependents(spec.task_id)]
        stamp = _now_iso()
        for tid in cone:
            self._set(self.specs[tid].with_legacy(inputs_invalidated_at=stamp))
        self.log.append(f"invalidate_inputs {spec.task_id} -> {len(cone)} task(s)")
        return tuple(cone)

    def _transitive_dependents(self, task_id: str) -> tuple[str, ...]:
        seen: set[str] = set()
        frontier = [str(task_id)]
        while frontier:
            current = frontier.pop()
            for tid, spec in self.specs.items():
                if current in spec.depends_on and tid not in seen:
                    seen.add(tid)
                    frontier.append(tid)
        return tuple(sorted(seen))

    # --- commit ---------------------------------------------------------------------

    def validate(self) -> list[ValidationIssue]:
        return validate(parse_document(self.render()), known_ids=self.registry_ids)

    def render(self) -> str:
        """The document this transaction would write.

        Blocks it never touched are copied through untouched — not re-serialised — so a
        mutation to one task cannot reformat, reorder or strip the comments from another.
        The tasks it writes carry no transitively redundant dep (P12 finding D21).
        """
        reduced = transitive_reduction(
            {tid: spec.depends_on for tid, spec in self.specs.items()})

        def written(task_id: str) -> TaskSpec:
            spec = self.specs[task_id]
            if list(spec.depends_on) == reduced[task_id]:
                return spec
            return spec.with_dependencies(reduced[task_id])

        edits: list[tuple[int, int, str]] = []
        for task_id in sorted(self._dirty):
            block = self._blocks_by_id.get(task_id)
            if block is None:
                continue
            edits.append((block.body_start, block.body_end, written(task_id).to_yaml()))
        text = self.document.text
        for start, end, body in sorted(edits, reverse=True):
            text = text[:start] + body + text[end:]
        for task_id in self._added:
            text = _append_section(text, written(task_id))
        return text

    def commit(self) -> RoadmapDocument:
        """Validate the rendered document, then return it. Raises before any write."""
        text = self.render()
        document = parse_document(text, path=self.document.path)
        issues = validate(document, known_ids=self.registry_ids)
        if issues:
            raise RoadmapValidationError(issues)
        return document


def _append_section(text: str, spec: TaskSpec) -> str:
    heading = f"### {spec.task_id}"
    if spec.title:
        heading += f" — {spec.title}"
    block = f"\n{heading}\n\n```yaml\n{spec.to_yaml()}```\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════
# compare-and-swap, and the drift conflict
# ══════════════════════════════════════════════════════════════════════════


def roadmap_path(repo: Path) -> Path:
    return Path(repo) / "docs" / "ROADMAP.md"


def cas_path(repo: Path) -> Path:
    return Path(repo) / ".orchestrator" / "roadmap_cas.json"


def registry_path(repo: Path) -> Path:
    return Path(repo) / ".orchestrator" / "completed_tasks.json"


def read_registry_ids(repo: Path) -> frozenset[str]:
    """Graduated task ids. Absent or corrupt registry ⇒ empty, matching
    `maestro.docs.roadmap.get_completed_task_ids`, which every legacy caller already
    relies on to fail open rather than crash the loop."""
    try:
        data = json.loads(registry_path(repo).read_text())
        return frozenset(str(item["id"]) for item in data)
    except Exception:
        return frozenset()


def read_registry_deps(repo: Path) -> dict[str, list[str]]:
    """Graduated task id → the deps it had, as graduation recorded them (P12 finding
    D21). An entry from before deps were recorded maps to `[]`. Absent or corrupt
    registry ⇒ empty, failing open like `read_registry_ids`."""
    try:
        data = json.loads(registry_path(repo).read_text())
        return {str(item["id"]): legacy_deps(item) for item in data}
    except Exception:
        return {}


@dataclass(frozen=True)
class CasRecord:
    """The controller's last accepted view of the roadmap."""

    document_hash: str
    applied_at: str
    revision: int
    tasks: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "document_hash": self.document_hash,
            "applied_at": self.applied_at,
            "revision": self.revision,
            "tasks": dict(self.tasks),
        }


def read_cas(repo: Path) -> CasRecord | None:
    try:
        data = json.loads(cas_path(repo).read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("document_hash"):
        return None
    return CasRecord(
        document_hash=str(data["document_hash"]),
        applied_at=str(data.get("applied_at") or ""),
        revision=int(data.get("revision") or 1),
        tasks={str(k): str(v) for k, v in (data.get("tasks") or {}).items()},
    )


def record_applied(repo: Path, document: RoadmapDocument) -> CasRecord:
    previous = read_cas(repo)
    record = CasRecord(
        document_hash=document.document_hash,
        applied_at=_now_iso(),
        revision=(previous.revision + 1) if previous else 1,
        tasks=document.block_hashes(),
    )
    _atomic_write(cas_path(repo), json.dumps(record.to_json(), indent=2) + "\n")
    return record


@dataclass(frozen=True)
class DocumentDrift:
    """`docs/ROADMAP.md` no longer matches what the controller applied.

    This is a conflict, never a cue to overwrite. `blocked` is the closure the controller
    must refuse to dispatch: the tasks whose text actually moved, plus everything
    downstream of them, because a dependent compiled against the old wording of its
    prerequisite is compiled against a task that no longer exists in that form. Every
    other task keeps running.
    """

    recorded_hash: str
    current_hash: str
    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    blocked: frozenset[str] = frozenset()

    def __str__(self) -> str:
        parts = []
        for label, ids in (("changed", self.changed), ("added", self.added),
                           ("removed", self.removed)):
            if ids:
                parts.append(f"{label}: {', '.join(ids)}")
        detail = "; ".join(parts) or "no task block changed (prose edit)"
        return (
            f"docs/ROADMAP.md drifted from the applied revision "
            f"({self.recorded_hash[:12]} -> {self.current_hash[:12]}) — {detail}"
        )


def drift(repo: Path, document: RoadmapDocument | None = None) -> DocumentDrift | None:
    """The current drift conflict, or None.

    None is also the answer for a project that has never run `maestro roadmap apply`:
    with no recorded revision there is nothing to have drifted *from*, and a project that
    has not adopted `[P2]` keeps its legacy behaviour exactly (`D08`).
    """
    record = read_cas(repo)
    if record is None:
        return None
    if document is None:
        try:
            document = load_document(roadmap_path(repo))
        except OSError:
            return None
    current = document.document_hash
    if current == record.document_hash:
        return None

    now = document.block_hashes()
    changed = tuple(sorted(t for t, h in now.items() if t in record.tasks and record.tasks[t] != h))
    added = tuple(sorted(t for t in now if t not in record.tasks))
    removed = tuple(sorted(t for t in record.tasks if t not in now))

    blocked: set[str] = set(changed) | set(added) | set(removed)
    try:
        graph = build_graph(document, registry_ids=read_registry_ids(repo), strict=False)
    except Exception:
        graph = None
    if graph is not None:
        for task_id in list(blocked):
            if task_id in graph:
                blocked.update(graph.transitive_dependents(task_id))
    return DocumentDrift(
        recorded_hash=record.document_hash, current_hash=current,
        changed=changed, added=added, removed=removed, blocked=frozenset(blocked),
    )


def blocked_task_ids(repo: Path) -> frozenset[str]:
    """Tasks a drift conflict forbids dispatching. Never raises: a failure to read the
    conflict must not take the loop down, and an empty set is the legacy behaviour."""
    try:
        conflict = drift(Path(repo))
    except Exception:
        return frozenset()
    return conflict.blocked if conflict else frozenset()


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class ApplyResult:
    graph: TaskGraph
    document: RoadmapDocument
    record: CasRecord
    previous_hash: str | None
    affected: tuple[str, ...] = ()
    log: tuple[str, ...] = ()
    wrote_document: bool = False


def apply(
    repo: Path,
    mutate: Callable[[RoadmapTransaction], None] | None = None,
    *,
    expected_hash: str | None = None,
    allow_drift: bool = False,
) -> ApplyResult:
    """Compare-and-swap the roadmap.

    With no `mutate` this is the adoption path: validate the document as it stands, and
    record it as the applied revision. With one, it is the mutation path: stage, validate,
    and write — but only if the document on disk is still the one we read.

    `expected_hash` is the caller's compare operand. When it is omitted the recorded CAS
    revision is used instead, which is what makes an unnoticed hand edit fail closed
    rather than being silently clobbered.
    """
    repo = Path(repo)
    path = roadmap_path(repo)
    document = load_document(path)
    registry = read_registry_ids(repo)
    record = read_cas(repo)

    if expected_hash is not None and expected_hash != document.document_hash:
        raise DocumentDriftError(
            f"compare-and-swap failed: caller expected document {expected_hash[:12]}, "
            f"docs/ROADMAP.md is at {document.document_hash[:12]}"
        )
    if expected_hash is None and record is not None and not allow_drift:
        conflict = drift(repo, document)
        if conflict is not None:
            raise DocumentDriftError(str(conflict), conflict)

    affected: tuple[str, ...] = ()
    log: tuple[str, ...] = ()
    wrote = False
    if mutate is not None:
        txn = RoadmapTransaction(document, registry_ids=registry)
        mutate(txn)
        new_document = txn.commit()
        if load_document(path).document_hash != document.document_hash:
            raise DocumentDriftError("ROADMAP changed during mutation; operator edits preserved")
        if new_document.text != document.text:
            _atomic_write(path, new_document.text)
            wrote = True
        document = parse_document(new_document.text, path=path)
        affected = tuple(sorted(txn.affected))
        log = tuple(txn.log)

    graph = build_graph(document, registry_ids=registry)
    new_record = record_applied(repo, document)
    return ApplyResult(
        graph=graph, document=document, record=new_record,
        previous_hash=record.document_hash if record else None,
        affected=affected, log=log, wrote_document=wrote,
    )


# ══════════════════════════════════════════════════════════════════════════
# persistence into the control store
# ══════════════════════════════════════════════════════════════════════════


def sync_to_store(store, graph: TaskGraph, *, source_hash: str | None = None) -> dict[str, int]:
    """Persist the graph as task rows, task revisions and dependency edges.

    Revision control is the store's, not ours: a task already registered gets a new
    `task_revisions` row through `apply_task_revision`, which is a compare-and-set on
    `tasks.current_spec_revision` and refuses a stale writer. A spec whose YAML is
    unchanged is not re-revisioned — an idempotent `maestro roadmap apply` must not
    inflate the revision counter, or `[INV-08]`'s sibling rule (counters mean something)
    stops being true of this one.

    Returns `task_id → the revision now current`.
    """
    source = source_hash or graph.document_hash
    revisions: dict[str, int] = {}
    for task_id in graph.order:
        spec = graph.specs[task_id]
        spec_yaml = spec.to_yaml()
        row = store.task(task_id)
        if row is None:
            store.register_task(
                task_id, spec_revision=1, status="planned", hold=spec.hold,
            )
            _write_revision(store, task_id, 1, spec_yaml, source)
            revisions[task_id] = 1
            continue
        current = int(row["current_spec_revision"])
        if same_spec_yaml(_stored_yaml(store, task_id, current), spec_yaml):
            revisions[task_id] = current
            continue
        store.apply_task_revision(
            task_id, spec_revision=current + 1, spec_yaml=spec_yaml,
            source_hash=source, expected_revision=current,
        )
        revisions[task_id] = current + 1

    with store.transaction() as conn:
        for task_id, revision in revisions.items():
            conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
            for dep in graph.specs[task_id].depends_on:
                conn.execute(
                    "INSERT OR REPLACE INTO task_dependencies"
                    " (task_id, depends_on_task_id, spec_revision) VALUES (?, ?, ?)",
                    (task_id, dep, revision),
                )
    return revisions


def same_spec_yaml(stored: str | None, spec_yaml: str) -> bool:
    """Whether a stored spec says what `spec_yaml` says. Identical text, or text an older
    version wrote for the same spec (`risk: low` used to be spelled out, `risk: critical`
    now reads as `high`), which is no edit and must not count as a new revision."""
    if stored is None or stored == spec_yaml:
        return stored is not None
    try:
        return normalize_spec(yaml.safe_load(stored)).to_yaml() == spec_yaml
    except (yaml.YAMLError, TypeError, ValueError, AttributeError):
        return False


def _write_revision(store, task_id: str, revision: int, spec_yaml: str, source: str) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO task_revisions"
            " (task_id, spec_revision, spec_yaml, source_hash, applied_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, revision, spec_yaml, source, _now_iso()),
        )


def _stored_yaml(store, task_id: str, revision: int) -> str | None:
    row = store.conn.execute(
        "SELECT spec_yaml FROM task_revisions WHERE task_id = ? AND spec_revision = ?",
        (task_id, revision),
    ).fetchone()
    return row["spec_yaml"] if row else None
