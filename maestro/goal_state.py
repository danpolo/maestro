"""Durable goal progress: what the goals have reached, read back by any process (P13 GDT-9).

R35 / C28: a fresh process resumes from what is on disk, never from memory, and never says
"done" (the completion verdict is GDT-10's). The state is the acceptance baseline
(`acceptance.BASELINE`, written only by `maestro goals baseline`) under the goals store's
`criteria` and `contract` sections (`goal_store`, version 2):

- `criteria`: one observation per criterion and contract revision, upserted: the
  verification gate's reruns of an accepted task (`record_gate`, from the run's last
  accepted gate attempt, `collect_observed`) and a milestone checkpoint's reruns on the
  merged tree (`record_checkpoint`; a criterion the cap skipped is no observation), and
  since GDT-10 the completion gate's run at (revision, HEAD) (`record_finish`). Each
  says what was observed, by which source, when, after which task and run, on which commit,
  with the command that reproduces it.
- `contract`: each legal contract revision seen (`observe_contract`): its revision, hash and
  text. A snapshot advances only on a higher revision whose change has no gate problem
  (and no whole-contract one), so an illegal edit keeps failing until it is fixed, and an
  id seen at an earlier revision that is gone without being retired is a gate problem
  (`contract_problems`, GDT-1-FORMAT).

`results` is the baseline overlaid by the newer observations at the current revision only
(an older revision closes nothing, R67); a later baseline rerun wins again. Without a
baseline at this revision there are no results (DESIGN §19.4's "no baseline" stays), though
`progress` still shows the observations. `protected` is every criterion GREEN at the
baseline or observed GREEN since at this revision; it never shrinks, so a regression Dan
accepted stays bounded as GDT-7 left it (C12, C22). `progress` (`maestro goals status`, and
GDT-10's input) computes the rest on read: unmet criteria, Dan's decisions and replans, what
is held and why, the next work, what turned stale (R11), contract and store problems, and
how to reproduce. A reader never moves a corrupt store; the next writer does
(`goal_store.update`). Nothing is read or written without `docs/GOALS.md`.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from maestro import acceptance, goal_store
from maestro.docs import goals as doc_goals

SOURCES = ("gate", "checkpoint", "finish")
_GIT_TIMEOUT = 15
#: The gate states that are observations (review M1).
_CREDIBLE = (acceptance.RED, acceptance.GREEN)


def _doc(repo: Path, doc: Optional[doc_goals.Goals]) -> Optional[doc_goals.Goals]:
    return doc if doc is not None else doc_goals.load(repo)


def _when(text: Any) -> Optional[datetime]:
    """An aware timestamp, or `None` (unparseable or naive: never compared)."""
    try:
        found = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return found if found.tzinfo is not None else None


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _command(criterion: Any) -> str:
    return str(getattr(criterion, "test", "") or "").strip().strip("`").strip()


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def head(repo: Path) -> str:
    """The project's HEAD commit (the merged tree a checkpoint ran on), `""` without one."""
    return (_git(repo, "rev-parse", "HEAD") or "").strip()


def _revision_at(repo: Path, commit: str) -> Optional[int]:
    """The contract revision in `commit`'s `docs/GOALS.md` (`None` when it does not read)."""
    if not commit:
        return None
    text = _git(repo, "show", f"{commit}:{doc_goals.DOC}")
    return doc_goals.parse(text).revision if text else None


def _baseline_meta(repo: Path) -> dict:
    try:
        payload = json.loads(acceptance.path_for(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _rows(store: Mapping[str, Any], revision: Optional[int]) -> dict[str, dict]:
    return {str(r["id"]): r for r in store.get("criteria") or ()
            if r.get("id") and r.get("revision") == revision}


def _label(row: Mapping[str, Any]) -> str:
    commit = str(row.get("commit") or "")[:12]
    source = row.get("source") or "gate"
    # A finish run follows no task: it is named by its own run id (`FIN-…`).
    where = (f"observed by the finish run {row.get('run_id') or '?'}" if source == "finish"
             else f"observed by the {source} after {row.get('task') or '?'}")
    return where + (f" ({commit})" if commit else "")


def _overlay(repo: Path, doc: doc_goals.Goals, store: Optional[Mapping[str, Any]] = None):
    """`(baseline, shown, won)`: the current-revision baseline by id, it overlaid by every
    newer observation at this revision, and the observation rows that won. An observation
    wins over a baseline result only when it is strictly later than the baseline run;
    without a current baseline every observation shows."""
    baseline = acceptance.current(acceptance.load(repo), doc)
    store = goal_store.read(repo) if store is None else store
    ran_at = _when(_baseline_meta(repo).get("ran_at"))
    by_id = {c.id: c for c in doc.criteria}
    shown: dict[str, acceptance.Result] = dict(baseline)
    won: dict[str, dict] = {}
    for cid, row in _rows(store, doc.revision).items():
        base = baseline.get(cid)
        seen = _when(row.get("at"))
        if base is not None and (ran_at is None or seen is None or seen <= ran_at):
            continue
        criterion = by_id.get(cid)
        reason = str(row.get("reason") or "")
        won[cid] = row
        shown[cid] = acceptance.Result(
            id=cid, state=str(row.get("state")), revision=doc.revision,
            reason=_label(row) + (f": {reason}" if reason else ""),
            goals=base.goals if base else tuple(getattr(criterion, "goals", ()) or ()),
            scenarios=base.scenarios if base else tuple(
                getattr(criterion, "scenarios", ()) or ()),
            command=base.command if base else ())
    return baseline, shown, won


def results(repo: Path, doc: Optional[doc_goals.Goals] = None) -> list[acceptance.Result]:
    """The acceptance results at the current revision: the baseline overlaid by newer
    observations. `[]` without a baseline at this revision or without `docs/GOALS.md`."""
    doc = _doc(repo, doc)
    if doc is None:
        return []
    baseline, shown, _ = _overlay(repo, doc)
    if not baseline:
        return []
    return [shown[i] for i in baseline] + [r for i, r in shown.items() if i not in baseline]


def sources(repo: Path, doc: Optional[doc_goals.Goals] = None) -> dict[str, str]:
    """`{criterion: label}` for each result an observation (not the baseline) gives."""
    doc = _doc(repo, doc)
    if doc is None:
        return {}
    baseline, _, won = _overlay(repo, doc)
    return {cid: _label(row) for cid, row in won.items()} if baseline else {}


def _protected(baseline: Mapping[str, acceptance.Result], rows: Mapping[str, dict]) -> set[str]:
    if not baseline:
        return set()
    out = {cid for cid, r in baseline.items() if r.state == acceptance.GREEN}
    return out | {cid for cid, row in rows.items()
                  if row.get("state") == acceptance.GREEN or row.get("protected")}


def protected(repo: Path, doc: Optional[doc_goals.Goals] = None) -> set[str]:
    """Every criterion GREEN at the current baseline or observed GREEN since at this
    revision; never shrinks (an accepted regression keeps GDT-7's bound). Empty without a
    baseline at this revision."""
    doc = _doc(repo, doc)
    if doc is None:
        return set()
    baseline = acceptance.current(acceptance.load(repo), doc)
    return _protected(baseline, _rows(goal_store.read(repo), doc.revision))


def protected_states(repo: Path, doc: doc_goals.Goals) -> dict[str, str]:
    """The current baseline's states with every protected criterion GREEN: what the gate
    reruns and the reviewer reads as regression protection (C12). `{}` without a current
    baseline."""
    baseline = acceptance.current(acceptance.load(repo), doc)
    states = {i: r.state for i, r in baseline.items()}
    for cid in _protected(baseline, _rows(goal_store.read(repo), doc.revision)):
        states[cid] = acceptance.GREEN
    return states


# --- writers ---------------------------------------------------------------------------------


def _record(repo: Path, rows: list[dict]) -> list[dict]:
    """Upsert one row per (criterion, revision); `protected` sticks once GREEN was seen. The
    same observation recorded again (a repair re-graduation of the same run: same source,
    run, commit and state) keeps its first `at`, so a baseline rerun since still wins."""
    if not rows:
        return []

    def key(r: Mapping[str, Any]) -> tuple[str, Any]:
        return str(r.get("id")), r.get("revision")

    def same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
        return all(a.get(k) == b.get(k) for k in ("source", "run_id", "commit", "state"))

    def mutate(stored: list[dict]) -> list[dict]:
        prior = {key(r): r for r in stored}
        for row in rows:
            before = prior.get(key(row)) or {}
            row["protected"] = bool(before.get("protected")) or row["state"] == acceptance.GREEN
            if before and same(before, row) and before.get("at"):
                row["at"] = before["at"]
        new = {key(r) for r in rows}
        return [r for r in stored if key(r) not in new] + rows

    goal_store.update(repo, "criteria", mutate)
    return rows


def record_gate(repo: Path, doc: Optional[doc_goals.Goals], observed: Mapping[str, Any], *,
                task: str, run_id: str, at: str) -> list[dict]:
    """Store the accepted run's gate observations (`collect_observed`). The revision is the
    one in the gate tree's own `docs/GOALS.md` (the commit the gate ran on), else the
    current one. Only a credible RED or GREEN is an observation: the gate's sandbox is not
    the controller's environment, so its BLOCKED/UNKNOWN (a note in the guard) never
    replaces a credible state. Returns the rows written."""
    doc = _doc(repo, doc)
    states = (observed or {}).get("acceptance") if isinstance(observed, Mapping) else None
    if doc is None or not isinstance(states, Mapping) or not states:
        return []
    snapshot = str(observed.get("snapshot") or "")
    revision = _revision_at(repo, snapshot)
    revision = doc.revision if revision is None else revision
    by_id = {c.id: c for c in doc.criteria}
    return _record(repo, [
        {"id": cid, "revision": revision, "state": str(state), "source": "gate", "at": at,
         "task": task, "run_id": run_id, "commit": snapshot,
         "command": _command(by_id[cid]),
         "reason": "rerun by the verification gate on the accepted task's tree"}
        for cid, state in states.items() if cid in by_id and state in _CREDIBLE])


def record_checkpoint(repo: Path, doc: Optional[doc_goals.Goals], point: Mapping[str, Any],
                      *, commit: str = "") -> list[dict]:
    """Store a milestone checkpoint's reruns (`goal_milestones.checkpoint`'s record) on
    the merged tree `commit`; a criterion the cap skipped is no observation."""
    doc = _doc(repo, doc)
    if doc is None or not isinstance(point, Mapping):
        return []
    skipped = {str(c) for ids in (point.get("skipped") or {}).values() for c in ids or ()}
    reasons = {str(u.get("criterion")): str(u.get("reason") or "")
               for u in point.get("unknown") or () if isinstance(u, Mapping)}
    states: dict[str, str] = {}
    for per_goal in (point.get("goals") or {}).values():
        if isinstance(per_goal, Mapping):
            states.update({str(c): str(s) for c, s in per_goal.items()})
    by_id = {c.id: c for c in doc.criteria}
    revision = point.get("revision", doc.revision)
    return _record(repo, [
        {"id": cid, "revision": revision, "state": state, "source": "checkpoint",
         "at": str(point.get("at") or ""), "task": str(point.get("task") or ""),
         "run_id": str(point.get("run_id") or ""), "commit": commit,
         "command": _command(by_id[cid]),
         "reason": reasons.get(cid) or f"rerun at checkpoint {point.get('id') or '?'}"}
        for cid, state in states.items()
        if cid in by_id and cid not in skipped and state in acceptance.STATES])


def record_finish(repo: Path, doc: Optional[doc_goals.Goals], row: Mapping[str, Any]
                  ) -> list[dict]:
    """Store a completed completion-gate run (`goal_completion`'s `finish` row, GDT-10): one
    observation per criterion at the run's revision, source `finish`, on its commit."""
    doc = _doc(repo, doc)
    if doc is None or not isinstance(row, Mapping):
        return []
    by_id = {c.id: c for c in doc.criteria}
    results = row.get("results") if isinstance(row.get("results"), Mapping) else {}
    return _record(repo, [
        {"id": cid, "revision": row.get("revision", doc.revision), "state": str(r["state"]),
         "source": "finish", "at": str(r.get("at") or row.get("started_at") or _now()),
         "task": "finish", "run_id": str(row.get("id") or ""),
         "commit": str(row.get("commit") or ""), "command": _command(by_id[cid]),
         "reason": str(r.get("reason") or "rerun by the completion gate")}
        for cid, r in results.items()
        if cid in by_id and isinstance(r, Mapping) and r.get("state") in acceptance.STATES])


def collect_observed(store, artifacts, run_id: str) -> dict:
    """`{"acceptance": {id: state}, "snapshot": commit}` from the run's last succeeded gate
    attempt (a rejected gate or one that asked Dan says nothing); `{}` without one, or when
    the run's attempts cannot be read. Mirrors `goal_replan.collect_progress`."""
    if artifacts is None or not run_id:
        return {}
    try:
        rows = store.conn.execute(
            "SELECT outcome_ref FROM attempts WHERE run_id = ? AND outcome_ref IS NOT NULL"
            " ORDER BY rowid", (run_id,)).fetchall()
    except Exception:  # noqa: BLE001 — an unreadable store observes nothing
        return {}
    found: dict = {}
    for row in rows:
        try:
            result = json.loads(artifacts.read(row["outcome_ref"]).decode("utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable outcome says nothing
            continue
        if not isinstance(result, dict) or result.get("status") != "succeeded":
            continue
        outputs = result.get("outputs")
        if not isinstance(outputs, dict) or outputs.get("decision") != "accepted" \
                or "snapshot" not in outputs or "evidence" not in outputs:
            continue        # not a gate attempt (the gate's outputs carry all three)
        states = outputs.get("acceptance")
        found = ({"acceptance": dict(states), "snapshot": str(outputs.get("snapshot") or "")}
                 if isinstance(states, dict) and states else {})
    return found


# --- the contract ----------------------------------------------------------------------------


def _last(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    numbered = [r for r in rows if isinstance(r.get("revision"), int)]
    return max(reversed(numbered), key=lambda r: r["revision"]) if numbered else None


def observe_contract(repo: Path, doc: Optional[doc_goals.Goals] = None,
                     text: Optional[str] = None) -> bool:
    """Record the contract as a snapshot when it is the first one seen or a legal change
    from the last (a higher revision whose change has no gate problem). An illegal edit is
    not recorded, so it keeps failing (`contract_problems`) until it is fixed. A gate
    problem scoped to some ids (an open question, a missing target or oracle: R05/R66 run
    beside it) or a section-level open question does not stop a snapshot; a
    whole-contract one (its revision or history) does. Whether one was added."""
    path = Path(repo) / doc_goals.DOC
    if text is None:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
    doc = doc if doc is not None else doc_goals.parse(text)
    # A section-level open question names no goal, so like a scoped one it blocks nothing here.
    asked = {f"{section}: open question for Dan: {q}" for section, q in doc.open_questions}
    if doc.format != 2 or not isinstance(doc.revision, int) \
            or any(not p.ids and p.text not in asked for p in doc.gate):
        return False
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    last = _last(goal_store.read(repo)["contract"])
    if last is not None:
        if last.get("sha256") == digest:
            return False
        old = doc_goals.parse(str(last.get("text") or ""))
        if doc.revision <= last["revision"] or doc_goals.changes(old, doc).problems:
            return False
    row = {"revision": doc.revision, "sha256": digest, "text": text, "at": _now()}
    goal_store.update(repo, "contract", lambda rows: [
        *[r for r in rows if r.get("revision") != doc.revision], row])
    return True


def contract_problems(repo: Path, doc: Optional[doc_goals.Goals] = None) -> list[str]:
    """Gate problems against the snapshots: the change from the last legal revision seen
    (`goals.changes`: a change without a new revision, a removal without retiring, a
    revision going back), and an id seen at an earlier revision that is gone now without
    being retired. `[]` without snapshots."""
    doc = _doc(repo, doc)
    if doc is None or doc.format != 2:
        return []
    rows = [r for r in goal_store.read(repo)["contract"] if isinstance(r.get("revision"), int)]
    last = _last(rows)
    if last is None:
        return []
    old = doc_goals.parse(str(last.get("text") or ""))
    out = [p.text for p in doc_goals.changes(old, doc).problems]
    known = set(doc_goals._entities(doc)) | set(doc.retired) | set(doc_goals._entities(old))
    for row in sorted(rows, key=lambda r: r["revision"]):
        for nid in doc_goals._entities(doc_goals.parse(str(row.get("text") or ""))):
            if nid not in known:
                known.add(nid)
                out.append(f"{nid} was in the contract at revision {row['revision']} and is "
                           "gone without being retired")
    return out


# --- progress --------------------------------------------------------------------------------


def _roadmap_tasks(repo: Path) -> list[dict]:
    from maestro import taskgraph

    roadmap = Path(repo) / "docs" / "ROADMAP.md"
    return taskgraph.scan_tasks(roadmap.read_text(encoding="utf-8")) if roadmap.is_file() \
        else []


def _named_ids(task: Mapping[str, Any]) -> list[str]:
    """The ids a task's evidence rests on: its `criteria:`, and any id its `evidence:` or
    `tests:` names."""
    named = list(doc_goals.normalise_criteria(task.get("criteria"))[0])
    tests = task.get("tests") if isinstance(task.get("tests"), list) else []
    text = " ".join([str(task.get("evidence") or ""), *(str(t) for t in tests)])
    return list(dict.fromkeys([*named, *doc_goals._ids(text)]))


def _decision_criteria(d: Mapping[str, Any]) -> list[str]:
    raw = d.get("criteria")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Mapping):
        return [str(k) for k in raw]
    return [str(c) for c in raw or ()]


def _parked(repo: Path) -> set[str]:
    """The parked tasks in the controller's `state.json` (read only, never the controller
    store's lock); none when it does not read."""
    from maestro.paths import Paths

    try:
        state = json.loads(Paths(repo=Path(repo)).state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    raw = state.get("parked_tasks") if isinstance(state, dict) else None
    return {str(t) for t in raw} if isinstance(raw, list) else set()


def _holds(repo: Path, doc: doc_goals.Goals, tasks: Sequence[Mapping[str, Any]],
           parked: set[str]) -> list[dict]:
    from maestro import goal_replan

    reasons: dict[str, list[str]] = {}
    for tid, problems in doc_goals.execution_holds(doc, tasks).items():
        reasons.setdefault(tid, []).append("; ".join(p.text for p in problems))
    for tid, rp in goal_replan.held(repo, tasks).items():
        reasons.setdefault(tid, []).append(goal_replan.describe_holds(repo, {tid: rp}, parked))
    order = [str(t.get("id")) for t in tasks]
    return [{"task": tid, "reason": "; ".join(reasons[tid])}
            for tid in sorted(reasons, key=lambda t: order.index(t) if t in order else len(order))]


def _stale(repo: Path, doc: doc_goals.Goals, store: Mapping[str, Any],
           tasks: Sequence[Mapping[str, Any]]) -> dict:
    observations = [{k: r.get(k) for k in ("id", "revision", "state", "source", "task", "at")}
                    for r in store.get("criteria") or () if r.get("revision") != doc.revision]
    episodes = [str(e.get("id")) for e in store.get("episodes") or ()
                if "revision" in e and e.get("revision") != doc.revision]
    earlier = [r for r in store.get("contract") or ()
               if isinstance(r.get("revision"), int) and isinstance(doc.revision, int)
               and r["revision"] < doc.revision]
    ids: list[str] = []
    if earlier:
        old = doc_goals.parse(str(_last(earlier).get("text") or ""))
        ids = list(doc_goals.changes(old, doc).stale)
    touched = [str(t.get("id")) for t in tasks if t.get("status") != "complete"
               and set(_named_ids(t)) & set(ids)]
    return {"ids": ids, "tasks": touched, "observations": observations, "episodes": episodes}


def progress(repo: Path, doc: Optional[doc_goals.Goals] = None,
             tasks: Optional[Sequence[Mapping[str, Any]]] = None,
             parked: Optional[Iterable[str]] = None) -> Optional[dict]:
    """The goals' durable progress, computed on read (`maestro goals status`; GDT-10 reads
    it). `None` without `docs/GOALS.md`. It has no done or complete field: that verdict is
    GDT-10's, and an operator check is never an automated pass (R14). `parked`: the parked
    tasks (default: the controller's `state.json`)."""
    from maestro import goal_replan

    doc = _doc(repo, doc)
    if doc is None:
        return None
    tasks = _roadmap_tasks(repo) if tasks is None else list(tasks)
    problem = goal_store.problem(repo)
    store = goal_store.read(repo)
    meta = _baseline_meta(repo)
    baseline, shown, won = _overlay(repo, doc, store)
    guarded = _protected(baseline, _rows(store, doc.revision))
    decisions = list(store.get("decisions") or ())
    entries: list[dict] = []
    for c in doc.criteria:
        result, row, base = shown.get(c.id), won.get(c.id), baseline.get(c.id)
        entries.append({
            "id": c.id, "kind": "criterion", "automated": True,
            "state": result.state if result else None,
            "source": row.get("source") if row else ("baseline" if base else None),
            "at": row.get("at") if row else (meta.get("ran_at") if base else None),
            "task": str(row.get("task") or "") if row else "",
            "run_id": str(row.get("run_id") or "") if row else "",
            "commit": str(row.get("commit") or "") if row else "",
            "command": _command(c), "reason": result.reason if result else "",
            "mandatory": c.mandatory, "protected": c.id in guarded,
            "decisions": [{"id": d.get("id"), "resolution": d.get("resolution")}
                          for d in decisions if c.id in _decision_criteria(d)]})
    for o in doc.operator_checks:
        base = baseline.get(o.id)
        entries.append({
            "id": o.id, "kind": "operator_check", "automated": False,
            "state": base.state if base else None, "source": "baseline" if base else None,
            "at": meta.get("ran_at") if base else None, "task": "", "run_id": "",
            "commit": "", "command": "", "reason": f"signed off by {o.sign_off or 'Dan'}",
            "mandatory": o.mandatory, "protected": False, "decisions": []})
    unmet = [e["id"] for e in entries if e["mandatory"] and (
        not e["automated"] or e["state"] != acceptance.GREEN)]
    term = goal_replan.terms(doc)
    replans = [{"id": r.get("id"), "goal": r.get("goal"), "status": r.get("status"),
                "kind": (r.get("trigger") or {}).get("kind"),
                "question_id": r.get("question_id"),
                "resolution": (r.get("decision") or {}).get("resolution"),
                "awaiting_revision": goal_replan._awaits_revision(r, doc, term),
                "applied": list(r.get("applied") or ())}
               for r in store.get("replans") or ()]
    parked = _parked(repo) if parked is None else set(parked)
    holds = _holds(repo, doc, tasks, parked)
    held = {h["task"] for h in holds}
    pending = set(unmet)
    # Next safe work (R35): open, not held, not parked, every dependency complete.
    blocked = parked | {str(t.get("id")) for t in tasks if t.get("status") != "complete"}
    next_work = []
    for t in tasks:
        tid = str(t.get("id") or "")
        names = [i for i in doc_goals.normalise_criteria(t.get("criteria"))[0] if i in pending]
        deps = t.get("deps") or []
        deps = [deps] if isinstance(deps, str) else deps
        if tid and t.get("status") != "complete" and tid not in held and tid not in parked \
                and names and not any(str(d) in blocked for d in deps):
            next_work.append({"task": tid, "criteria": names})
    return {
        "path": str(Path(repo) / doc_goals.DOC), "revision": doc.revision,
        "format": doc.format,
        "baseline": {"present": bool(meta), "revision": meta.get("revision"),
                     "format": meta.get("format"), "ran_at": meta.get("ran_at"),
                     "stale": bool(meta) and meta.get("revision") != doc.revision},
        "criteria": entries, "unmet": unmet,
        "decisions": [{k: d.get(k) for k in ("id", "resolution", "criteria", "regressed",
                                              "at")} for d in decisions],
        "replans": replans, "holds": holds, "next_work": next_work,
        "stale": _stale(repo, doc, store, tasks),
        "contract_problems": contract_problems(repo, doc), "store_problem": problem,
        "reproduce": ["maestro goals baseline"] + [f"{c.id}: {_command(c)}"
                                                   for c in doc.criteria if _command(c)],
    }


def _join(items: Iterable[str]) -> str:
    return ", ".join(items) or "none"


def render(shown: Mapping[str, Any]) -> str:
    """`progress(...)` as text for `maestro goals status`."""
    base = shown["baseline"]
    lines = [f"{shown['path']}: contract revision {shown['revision']} "
             f"(format {shown['format']})"]
    if base["present"]:
        lines.append(f"baseline: revision {base['revision']}, ran {base['ran_at']}"
                     + (" (stale: another revision; run `maestro goals baseline`)"
                        if base["stale"] else ""))
    else:
        lines.append("baseline: none (run `maestro goals baseline`)")
    lines.append("criteria:")
    for e in shown["criteria"]:
        where = (f"{e['source']}" + (f" after {e['task']}" if e["task"] else "")
                 + (f" ({e['commit'][:12]})" if e["commit"] else "")
                 + (f" at {e['at']}" if e["at"] else "")) if e["source"] else "not observed"
        flags = [f for f, on in (("mandatory", e["mandatory"]), ("protected", e["protected"]),
                                 ("operator check", not e["automated"])) if on]
        lines.append(f"  {e['id']} {e['state'] or 'no result'}: {where}"
                     + (f" [{', '.join(flags)}]" if flags else ""))
        lines += [f"    decision {d['id']}: {d['resolution']}" for d in e["decisions"]]
    lines.append("unmet (mandatory, not GREEN; operator checks are Dan's): "
                 + _join(shown["unmet"]))
    lines.append("replans: " + _join(
        f"{r['id']} {r['goal']} {r['status']}"
        + (" (awaiting Dan's revision)" if r["awaiting_revision"] else "")
        for r in shown["replans"]))
    lines.append("held: " + _join(f"{h['task']} ({h['reason']})" for h in shown["holds"]))
    lines.append("next work: " + _join(f"{w['task']} ({', '.join(w['criteria'])})"
                                      for w in shown["next_work"]))
    stale = shown["stale"]
    if stale["ids"] or stale["observations"] or stale["episodes"]:
        lines.append(f"stale: ids {_join(stale['ids'])}; tasks {_join(stale['tasks'])}; "
                     f"{len(stale['observations'])} observation(s) and "
                     f"{len(stale['episodes'])} episode(s) at other revisions")
    if shown["contract_problems"]:
        lines.append("contract problems: " + "; ".join(shown["contract_problems"]))
    if shown["store_problem"]:
        lines.append("goals store: " + shown["store_problem"])
    lines.append("reproduce: " + "; ".join(shown["reproduce"]))
    return "\n".join(lines) + "\n"
