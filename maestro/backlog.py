"""The review follow-up backlog (P13 GDT-11 / W7): every accepted task's follow-ups wait
here for the end-of-project polish stage instead of each queueing a micro-task.

`.orchestrator/backlog.json` is `{"version": 1, "items": [...]}`, written through the goals
store's locked, atomic writer (`goal_store.update_file`): a corrupt file is moved aside,
unknown keys are kept. It is a separate file because W7 serves every graph project, with
or without `docs/GOALS.md`. Graduation fills it (`docs.followups.record`):

- an ordinary task's follow-ups and a `<id>-followups` task's own are `open`;
- a polish or triage task's own are born `deferred`: polish never chains, which bounds
  the loop.

An item is keyed by its raising task and its normalised text, so a graduation retried
after a lost loop adds nothing twice. Ids (`BL-0001`) are sequential and never reused.

The polish stage (S2) opens at queue exhaustion (`stage`, called by the graph loop before
the completion gate). Two or more open items go to one triage task, `polish-triage-<n>`:
its writer decides keep, duplicate or moot for each in `docs/polish/triage-<n>.json`
(`check_triage` is its gate check), and its graduation applies the decisions
(`apply_triage`). Kept items are grouped by area, the scope their raising task declared
(D20: overlapping scopes join, an undeclared one joins all), into one polish task each,
`polish-<n>-<letters>` (a..z, aa, ab, ...); a single open item goes straight to its polish task. A polish task
does or declines each item with a reason in `docs/polish/<id>.json` (`check_report`), and
its graduation settles them (`settle`). Item states: `open` → `triage` → `merged` | `moot`
| `assigned` → `done` | `declined` | `deferred`.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shlex
import sys
from pathlib import Path
from string import ascii_lowercase
from typing import Iterable, Sequence

from maestro import goal_store, writeset

#: Relative to the project root.
STORE = ".orchestrator/backlog.json"
SECTIONS = ("items",)
VERSION = 1
#: A polish or triage task's own follow-ups: recorded, never polished again.
DEFERRED_REASON = "raised by a polish/triage review; polish never chains"
#: Where the triage decisions and the polish reports are written, in the project.
POLISH_DIR = "docs/polish"
DECISIONS = ("keep", "duplicate", "moot")
OUTCOMES = ("done", "declined")
#: Items a stage task holds: reopened when that task is gone (`stage`).
_HELD = ("triage", "assigned")


def path_for(repo: Path) -> Path:
    return Path(repo) / STORE


def is_triage(task: dict | None) -> bool:
    task = task or {}
    return task.get("polish_stage") is not None and task.get("polish_role") == "triage"


def is_polish(task: dict | None) -> bool:
    task = task or {}
    return task.get("polish_stage") is not None and task.get("polish_role") != "triage"


def _key(task_id: str, text: str) -> tuple[str, str]:
    return str(task_id), " ".join(str(text).split()).lower()


def _origin(task: dict) -> str:
    from maestro.docs import followups

    if is_triage(task):
        return "triage"
    if is_polish(task):
        return "polish"
    return "followups" if followups.is_followup_task(task) else "task"


def items(repo: Path, *states: str) -> list[dict]:
    """Every stored item (only those in `states`, when given); `[]` when there is no
    store or it does not read (the next write moves it aside)."""
    try:
        rows = goal_store.read_file(path_for(repo), SECTIONS)["items"]
    except (OSError, ValueError):
        return []
    return [r for r in rows if not states or r.get("state") in states]


def add(repo: Path, task: dict, follow_ups: Iterable, *, at: str) -> list[str]:
    """Record `task`'s follow-ups (`docs.followups.FollowUp`s); returns the ids added.

    Each item keeps what a polish task later needs from its raising task: the declared
    `scope:` (its area), its `kind: auto` checks without `await:` (as `build_task` keeps
    them) and its `criteria:` and `tests:` (the product tests of its area)."""
    task_id = str(task["id"])
    origin = _origin(task)
    deferred = origin in ("polish", "triage")
    scope = writeset.declared_scope(task)
    checks = [dict(v) for v in task.get("verifications") or ()
              if isinstance(v, dict) and v.get("kind") == "auto" and not v.get("await")]
    criteria: list[str] = []
    for key in ("criteria", "tests"):
        raw = task.get(key) or []
        for cid in [raw] if isinstance(raw, str) else raw:
            if str(cid) not in criteria:
                criteria.append(str(cid))
    added: list[str] = []

    def mutate(rows: list[dict]) -> list[dict]:
        seen = {_key((r.get("source") or {}).get("task", ""), r.get("text", "")) for r in rows}
        last = max((int(str(r.get("id", ""))[3:]) for r in rows
                    if str(r.get("id", "")).startswith("BL-")
                    and str(r.get("id"))[3:].isdigit()), default=0)
        for item in follow_ups:
            if not item.text.strip() or _key(task_id, item.text) in seen:
                continue
            seen.add(_key(task_id, item.text))
            last += 1
            added.append(f"BL-{last:04d}")
            rows.append({
                "id": added[-1], "text": item.text,
                "source": {"task": task_id, "round": item.round, "node": item.node,
                           "commit": item.commit},
                "source_task": {key: copy.deepcopy(task[key]) for key in (
                    'id', 'status', 'mode', 'hold', 'self_modifying', 'deps', 'resource',
                    'resources', 'serves', 'contribution', 'dispatch') if key in task},
                "origin": origin,
                "scope": list(scope) if scope is not None else None,
                "checks": [dict(c) for c in checks], "criteria": list(criteria),
                "state": "deferred" if deferred else "open",
                "stage": None, "task": None, "of": None,
                "reason": DEFERRED_REASON if deferred else None,
                "at": at, "settled_at": at if deferred else None,
            })
        return rows

    goal_store.update_file(path_for(repo), SECTIONS, "items", mutate, version=VERSION)
    return added


# ── the polish stage (S2) ──────────────────────────────────────────────────────────────


def triage_file(stage_no: int) -> str:
    return f"{POLISH_DIR}/triage-{stage_no}.json"


def report_file(task_id: str) -> str:
    return f"{POLISH_DIR}/{task_id}.json"


def _check_cmd(sub: str, rel: str, ids: Sequence[str]) -> str:
    """A gate check that reads only argv and the tree: the expected ids ride in argv."""
    return (f"env PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -m maestro.backlog "
            f"{sub} {rel} --items {','.join(ids)}")


def _where(item: dict) -> str:
    src = item.get("source") or {}
    if src.get("round") == 0:       # a goal checkpoint's (GDT-8), not a review's
        return f"{src.get('task')}, {src.get('node') or 'goal checkpoint'}"
    where = f"{src.get('task')}, review round {src.get('round')}, {src.get('node')}"
    return where + (f" @ {src['commit']}" if src.get("commit") else "")


def _line(item: dict, merged: Sequence[dict] = ()) -> str:
    line = f"{item['id']} ({_where(item)}) {item['text']}"
    if merged:
        # The merged text too, so the polish reviewer can catch a wrong merge (review L2).
        line += " — also raised as " + ", ".join(f'{m["id"]} ({_where(m)}): "{m["text"]}"'
                                                 for m in merged)
    return line


def suffix(k: int) -> str:
    """The `k`th polish task's letter suffix (0 → a, 25 → z, 26 → aa, ...): unbounded, so
    no area is dropped past `z` (review M2)."""
    out = ""
    k += 1
    while k:
        k, r = divmod(k - 1, 26)
        out = ascii_lowercase[r] + out
    return out


_STAGE_ID = re.compile(r"polish-(?:triage-(\d+)|(\d+)-[a-z]+)")


def _stage_no(task_id) -> int:
    m = _STAGE_ID.fullmatch(str(task_id))
    return int(m.group(1) or m.group(2)) if m else 0


def _area(scope) -> str:
    if scope is None:
        return "undeclared scope"
    return ", ".join(p for p in scope if not str(p).startswith(f"{POLISH_DIR}/")) or "no files"


def _scope_note(item: dict) -> str:
    scope = item.get("scope")
    return (f"{item['id']}: raised by {(item.get('source') or {}).get('task')}, scope "
            + ("undeclared" if scope is None else ", ".join(scope) or "none"))


def triage_task(stage_no: int, rows: Sequence[dict]) -> dict:
    """The triage task's ROADMAP block for `rows` (polish stage `stage_no`)."""
    ids = [r["id"] for r in rows]
    rel = triage_file(stage_no)
    return {
        "id": f"polish-triage-{stage_no}", "status": "open", "mode": "autonomous",
        "deps": [], "hold": False, "serves": [],
        "polish_stage": stage_no, "polish_role": "triage", "polish_items": ids,
        "title": f"Triage the review follow-ups (polish stage {stage_no})",
        "short_desc": "Merge duplicates and decline moot items, each with a reason; change "
                      "no code.",
        # W9's scope gate keeps the writer out of the code.
        "scope": [rel],
        "acceptance_criteria": [_line(r) for r in rows],
        # INV-04: its required gate check validates the decisions.
        "verifications": [{"id": "V1", "kind": "auto",
                           "cmd": _check_cmd("check-triage", rel, ids)}],
        "notes": "\n".join(_scope_note(r) for r in rows),
    }


def _overlap(a: dict, b: dict) -> bool:
    if a.get("scope") is None or b.get("scope") is None:
        return True                 # unknown overlaps everything (D20)
    return bool(writeset.write_sets_overlap(a["scope"], b["scope"]))


def groups(rows: Sequence[dict]) -> list[list[dict]]:
    """Kept items by area: the connected components of overlapping scopes, in first-item
    order. Distinct groups have disjoint scopes, so their polish tasks merge trivially."""
    root = list(range(len(rows)))

    def find(i: int) -> int:
        while root[i] != i:
            i = root[i]
        return i

    for i in range(len(rows)):
        for j in range(i):
            if _overlap(rows[i], rows[j]):
                root[find(i)] = find(j)
    out: dict[int, list[dict]] = {}
    for i, row in enumerate(rows):
        out.setdefault(find(i), []).append(row)
    return sorted(out.values(), key=lambda g: rows.index(g[0]))


def provenance_rows(task: dict, rows: Sequence[dict]) -> list[dict]:
    """Kept items and their directly merged duplicates, with original provenance."""
    ids = set(task.get('polish_items') or ())
    return [row for row in rows if row.get('id') in ids
            or (row.get('state') == 'merged' and row.get('of') in ids)]


def inherit_resources(task: dict, sources: Iterable[dict]) -> dict:
    """Resource minima for sequential follow-ups; preserve existing task minima."""
    from maestro.taskgraph import parse_resources

    task = copy.deepcopy(task)
    labels: set[str] = set()
    resources: dict = {}
    for source in (task, *sources):
        raw = source.get('resource') or []
        labels.update([raw] if isinstance(raw, str) else raw)
        for dimension, value in parse_resources(source.get('resources')).items():
            resources[dimension] = max(resources.get(dimension, 0), value)
    if labels:
        task['resource'] = sorted(labels)
    if resources:
        task['resources'] = resources
    return task


def polish_task(stage_no: int, letter: str, kept: Sequence[dict],
                merged: dict | None = None, *, after: str = "") -> dict:
    """The polish task's ROADMAP block for one area's kept items; `merged` maps a kept id
    to the items merged into it, whose provenance joins its criterion line."""
    merged = merged or {}
    task_id = f"polish-{stage_no}-{letter}"
    ids = [r["id"] for r in kept]
    scope: list[str] | None = []
    for r in kept:
        if r.get("scope") is None:
            scope = None
            break
        scope += [p for p in r["scope"] if p not in scope]
    criteria: list[str] = []
    checks: list[dict] = []
    for r in kept:
        criteria += [c for c in r.get("criteria") or () if c not in criteria]
        for c in r.get("checks") or ():
            if c.get("cmd") not in {k.get("cmd") for k in checks}:
                checks.append(dict(c))
    rel = report_file(task_id)
    # INV-04: the report check always, last; D23's "do or decline each" made mechanical.
    checks.append({"kind": "auto", "cmd": _check_cmd("check-report", rel, ids)})
    task: dict = {
        "id": task_id, "status": "open", "mode": "autonomous", "deps": [], "hold": False,
        "serves": [], "polish_stage": stage_no, "polish_items": ids,
        "title": f"Polish: {_area(scope)}",
        "short_desc": "Do or decline, with a reason, each listed review follow-up; no new "
                      "scope.",
    }
    provenance = [*kept, *(row for duplicates in merged.values() for row in duplicates)]
    task = inherit_resources(task, (row.get('source_task') or {} for row in provenance))
    if scope is not None:
        task["scope"] = [*scope, rel]
    if criteria:
        # The area's product acceptance tests, rerun by the goals guard (`tests:`, never
        # `criteria:`, which would add contract holds).
        task["tests"] = criteria
    task["acceptance_criteria"] = [_line(r, merged.get(r["id"], ())) for r in kept]
    task["verifications"] = [{"id": f"V{i}", **{k: v for k, v in c.items() if k != "id"}}
                             for i, c in enumerate(checks, 1)]
    task["notes"] = "\n".join([f"Polish stage {stage_no}" + (f", after triage {after}."
                                                            if after else "."),
                               *(_scope_note(r) for r in kept)])
    return task


def stage(repo: Path, tasks: Iterable[dict], completed: Iterable[str], *,
          at: str, eligible_sources: Iterable[str] | None = None) -> list[dict]:
    """At queue exhaustion: the ROADMAP blocks of the next polish stage, `[]` when no item
    is open. Two or more open items go to one triage task, a single one straight to its
    polish task. Items a stage task holds return to `open` when that task is not in the
    ROADMAP (`tasks`; Dan deleted it), or when it graduated (`completed`) and still holds
    them: its apply_triage or settle failed (review L1). The store is written before the
    caller appends the blocks, so a crash in between heals here on the next exhaustion.
    Stage numbers may skip, never collide: they also count the stage ids in the ROADMAP
    and the completion registry, so a lost or corrupt store reuses none (review M1)."""
    done = {str(c) for c in completed}
    eligible = None if eligible_sources is None else {str(t) for t in eligible_sources}
    listed = {str(t.get("id")) for t in tasks}
    used = max((_stage_no(i) for i in listed | done), default=0)

    def gone(r: dict) -> bool:
        task = str(r.get("task"))
        return r.get("state") in _HELD and (task in done or task not in listed)

    if not any(r.get("state") == "open" or gone(r) for r in items(repo)):
        return []                   # nothing to do: no write, no store file
    out: list[dict] = []

    def mutate(rows: list[dict]) -> list[dict]:
        for r in rows:
            if gone(r):
                r.update(state="open", task=None)
        pending = [r for r in rows if r.get("state") == "open"
                   and (eligible is None or str((r.get("source") or {}).get("task")) in eligible)]
        if not pending:
            return rows
        n = 1 + max([used, *(r["stage"] for r in rows if isinstance(r.get("stage"), int))])
        if len(pending) >= 2:
            out.append(triage_task(n, pending))
            state = "triage"
        else:
            out.append(polish_task(n, "a", pending))
            state = "assigned"
        for r in pending:
            r.update(state=state, stage=n, task=out[0]["id"])
        return rows

    goal_store.update_file(path_for(repo), SECTIONS, "items", mutate, version=VERSION)
    return out


def _has_reason(row: dict) -> bool:
    return isinstance(row.get("reason"), str) and bool(row["reason"].strip())


def _entries(doc, ids: Sequence[str], what: str) -> tuple[dict, list[str]]:
    """`{id: entry}` of a decisions/report document and the shape problems."""
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return {}, ["not a JSON object with an `items` list"]
    seen: dict[str, dict] = {}
    problems: list[str] = []
    for row in doc["items"]:
        if not isinstance(row, dict) or not row.get("id"):
            problems.append(f"an entry has no id: {row!r}"[:200])
            continue
        rid = str(row["id"])
        if rid in seen:
            problems.append(f"{rid} is listed more than once")
        elif rid not in ids:
            problems.append(f"{rid} is not an item of this task: add no items")
        else:
            seen[rid] = row
    problems += [f"{i} is {what}" for i in ids if i not in seen]
    return seen, problems


def check_triage(doc, ids: Sequence[str]) -> list[str]:
    """The triage file's problems (`[]` when valid): every expected id exactly once and no
    other; a decision of keep, duplicate or moot; a duplicate with a reason and an `of`
    naming another kept item (no chains); a moot item with a reason. Zero kept is legal."""
    ids = [str(i) for i in ids]
    seen, problems = _entries(doc, ids, "missing: decide keep, duplicate or moot")
    kept = {rid for rid, row in seen.items() if row.get("decision") == "keep"}
    for rid, row in seen.items():
        decision = row.get("decision")
        if decision not in DECISIONS:
            problems.append(f"{rid}: decision {decision!r} is not keep, duplicate or moot")
        elif decision == "duplicate":
            if not _has_reason(row):
                problems.append(f"{rid}: a duplicate needs a reason")
            if str(row.get("of")) not in kept - {rid}:
                problems.append(f"{rid}: a duplicate's `of` must name another kept item "
                                f"(got {row.get('of')!r})")
        elif decision == "moot" and not _has_reason(row):
            problems.append(f"{rid}: a moot item needs a reason (where later work did or "
                            "removed it)")
    return problems


def check_report(doc, ids: Sequence[str]) -> list[str]:
    """The polish report's problems (`[]` when valid): every item exactly once, `done`, or
    `declined` with a reason (D23)."""
    ids = [str(i) for i in ids]
    seen, problems = _entries(doc, ids,
                              "not reported: record it as done, or declined with a reason")
    for rid, row in seen.items():
        if row.get("outcome") not in OUTCOMES:
            problems.append(f"{rid}: outcome {row.get('outcome')!r} is not done or declined")
        elif row["outcome"] == "declined" and not _has_reason(row):
            problems.append(f"{rid}: a declined item needs a reason")
    return problems


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def apply_triage(repo: Path, task: dict, *, at: str) -> tuple[list[dict], dict]:
    """At the triage task's graduation: apply its merged decisions file. `duplicate` →
    `merged` (with `of`), `moot` → `moot` (declined with its reason), `keep` → grouped by
    area into polish tasks (`assigned`). An unreadable or invalid file keeps every item
    (`result["unreadable"]` says why): the sound default loses none. Returns the polish
    tasks' blocks, rebuilt from the store, so a retried graduation gives the same ones, and
    `{"stage", "merged", "moot", "tasks", "unreadable", "text"}`."""
    n = int(task["polish_stage"])
    tid = str(task["id"])
    ids = [str(i) for i in task.get("polish_items") or ()]
    rel = triage_file(n)
    try:
        doc = _read_json(Path(repo) / rel)
        problems = check_triage(doc, ids)
    except (OSError, ValueError) as exc:
        doc, problems = None, [f"{rel} does not read ({type(exc).__name__}: {exc})"]
    decisions = {} if problems else {str(r["id"]): r for r in doc["items"]}
    blocks: list[dict] = []

    def mutate(rows: list[dict]) -> list[dict]:
        mine = [r for r in rows if r.get("id") in ids]
        pending = [r for r in mine if r.get("state") in ("open", "triage")]
        for r in pending:
            d = decisions.get(r["id"]) or {"decision": "keep"}
            if d["decision"] == "duplicate":
                r.update(state="merged", of=str(d["of"]), reason=d["reason"].strip())
            elif d["decision"] == "moot":
                r.update(state="moot", reason=d["reason"].strip())
            else:
                continue
            r.update(stage=n, task=tid, settled_at=at)
        kept = [r for r in pending if r["state"] in ("open", "triage")]
        for k, group in enumerate(groups(kept)):
            for r in group:
                r.update(state="assigned", stage=n, task=f"polish-{n}-{suffix(k)}")
        lost = [r["id"] for r in kept if r["state"] != "assigned"]
        if lost:                    # review M2: never drop a kept item silently
            raise RuntimeError(f"{tid}: kept items not assigned to a polish task: "
                               + ", ".join(lost))
        by_task: dict[str, list[dict]] = {}
        for r in mine:
            if r.get("state") == "assigned" and str(r.get("task")).startswith(f"polish-{n}-"):
                by_task.setdefault(r["task"], []).append(r)
        for task_id, group in by_task.items():
            merged = {k["id"]: [m for m in mine if m.get("state") == "merged"
                                and m.get("of") == k["id"]] for k in group}
            blocks.append(polish_task(n, task_id.rsplit("-", 1)[1], group, merged, after=tid))
        return rows

    goal_store.update_file(path_for(repo), SECTIONS, "items", mutate, version=VERSION)
    rows = {r["id"]: r for r in items(repo) if r.get("id") in ids}
    merged = [i for i in ids if rows.get(i, {}).get("state") == "merged"]
    moot = [(i, rows[i]["reason"]) for i in ids if rows.get(i, {}).get("state") == "moot"]
    parts = [f"{len(merged)} merged as duplicates"] if merged else []
    if moot:
        parts.append(f"{len(moot)} declined as moot ("
                     + "; ".join(f'{i}: "{why}"' for i, why in moot) + ")")
    tasks = [(b["id"], _area(b.get("scope"))) for b in blocks]
    parts.append(f"{len(tasks)} polish task{'s' if len(tasks) != 1 else ''}: "
                 + ", ".join(f"{t} ({area})" for t, area in tasks) if tasks
                 else "no polish task")
    text = (f"🧹 Triage (polish stage {n}): {len(ids)} follow-ups → " + ", ".join(parts)
            + ".")
    return blocks, {"stage": n, "merged": merged, "moot": moot, "tasks": tasks,
                    "unreadable": "; ".join(problems)[:500], "text": text}


def settle(repo: Path, task: dict, *, at: str) -> dict:
    """At a polish task's graduation: each of its items `done` or `declined` with the
    reason its merged report gives; one the report misses (unreachable past its gate
    check) is `deferred` ("not reported by <id>"). Returns `{"done", "declined",
    "deferred", "text"}`."""
    tid = str(task["id"])
    ids = [str(i) for i in task.get("polish_items") or ()]
    try:
        doc = _read_json(Path(repo) / report_file(tid))
        reported = {str(r.get("id")): r for r in doc.get("items") or () if isinstance(r, dict)}
    except (OSError, ValueError, AttributeError):
        reported = {}

    def mutate(rows: list[dict]) -> list[dict]:
        for r in rows:
            if r.get("id") not in ids or r.get("state") not in _HELD + ("open",):
                continue
            entry = reported.get(r["id"]) or {}
            outcome = entry.get("outcome")
            if outcome in OUTCOMES and (outcome == "done" or _has_reason(entry)):
                why = entry.get("reason")
                r.update(state=outcome, reason=why.strip() if isinstance(why, str) else None)
            else:
                r.update(state="deferred", reason=f"not reported by {tid}")
            r.update(task=tid, settled_at=at)
        return rows

    goal_store.update_file(path_for(repo), SECTIONS, "items", mutate, version=VERSION)
    rows = {r["id"]: r for r in items(repo) if r.get("id") in ids}
    out = {s: [i for i in ids if rows.get(i, {}).get("state") == s]
           for s in ("done", "declined", "deferred")}
    out["text"] = (f"{tid}: {len(out['done'])} done, {len(out['declined'])} declined, "
                   f"{len(out['deferred'])} deferred")
    return out


def contract(task: dict | None, *, reviewer: bool) -> str:
    """The triage or polish task's bar, for its writer's or its reviewer's brief (`''`
    for any other task); `docs.followups.contract` delegates here."""
    task = task or {}
    if is_triage(task):
        n = task["polish_stage"]
        rel = triage_file(n)
        head = (f"TRIAGE TASK (polish stage {n}): each acceptance criterion is one review "
                "follow-up raised while the main tasks were accepted. ")
        if reviewer:
            return head + (
                f"Approve only if every item appears once in {rel}, each duplicate really "
                "asks for its `of` item's change, and each moot reason is true in the code "
                "at HEAD (check it). A wrongly declined item is blocking. A kept item that "
                "could have been merged is a follow_up at most. Add no items.")
        return head + (
            "Change no code. For each item decide `keep`, `duplicate` (it asks for the same "
            "change as a kept item, maybe in other words: give that item's id as `of`), or "
            "`moot` (later work already did it, or removed what it is about: say where, with "
            f"a file:line or commit). Write {rel} as "
            f'`{{"stage": {n}, "items": [{{"id", "decision", "of"?, "reason"}}]}}`, every '
            "item exactly once. When unsure, keep: a kept item is still done or declined by "
            "its polish task. Never widen, split or add an item (D23).")
    if is_polish(task):
        n = task["polish_stage"]
        rel = report_file(str(task["id"]))
        head = (f"POLISH TASK (stage {n}, area {_area(writeset.declared_scope(task))}): each "
                "acceptance criterion is one review follow-up raised while the main tasks "
                "were accepted. ")
        if reviewer:
            return head + (
                f"Approve only if each item is done or declined with a sound reason in {rel} "
                "and the diff matches it. Any change beyond the listed items is new scope: "
                "blocking. Do not widen an item: an edge case or requirement the item does "
                "not name is not a finding of this task (D23); a defect the change itself "
                "introduces is. Your follow-ups are recorded but never polished again.")
        return head + (
            "Check first that each is still relevant. Do it, or decline it with a reason; "
            "nothing else: no new scope, no new edge cases (D23). Record each in "
            f'{rel} as `{{"items": [{{"id", "outcome": "done" | "declined", "reason"}}]}}`, '
            "every item exactly once. The project's regression checks and this area's "
            "product acceptance tests rerun on your change; one that breaks sends the task "
            "back.")
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m maestro.backlog check-triage|check-report <file> --items BL-…,…`: the
    triage and polish tasks' gate checks; exit 1 on any problem, each printed."""
    parser = argparse.ArgumentParser(prog="python -m maestro.backlog")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check-triage", "check-report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("file")
        cmd.add_argument("--items", required=True, help="the expected ids, comma-separated")
    args = parser.parse_args(argv)
    ids = [i.strip() for i in args.items.split(",") if i.strip()]
    try:
        doc = _read_json(Path(args.file))
    except (OSError, ValueError) as exc:
        print(f"{args.file} does not read ({type(exc).__name__}: {exc})")
        return 1
    problems = (check_triage if args.command == "check-triage" else check_report)(doc, ids)
    for problem in problems:
        print(f"{args.file}: {problem}")
    if not problems:
        print(f"{args.file}: ok, {len(ids)} item(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
