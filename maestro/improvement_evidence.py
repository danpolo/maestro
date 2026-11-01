"""W12's ledger side: goal-miss attribution by evidence, and the analyst's evidence pack (GDT-12).

**Attribution (R58, R68, R70).** `attribute` considers each goals-store episode that is a
real miss: `no_progress_unexpected` on a goal the task served `direct`, the goal not
operator-only (enabling and maintenance misses are already `unchanged_expected`, GDT-6;
`serves: []` serves nothing). Rules over recorded facts, the first that holds wins:

- A1 `gate_saw_green`: the episode run's own accepted gate rerun shows a criterion the task
  named for this goal GREEN that was not GREEN at the baseline of that revision →
  `review_misclassified` → `reviewer`;
- A2 `unmeasured`: every named criterion in that rerun is blocked or unknown →
  `measurement_unavailable` → `none`;
- A3 `terms_revised`: Dan revised the goal's terms since → `contract_revised` → `none`;
- A4 `diagnosed`: a replan raised on this episode added a diagnosis task that graduated, and
  its cause file at HEAD starts with `Cause: <token>` → `review_origins.DIAGNOSIS_FAULT`;
- otherwise `pending_evidence` → `unattributed`. Never a planner fault by default.

The fact is appended as an amendment of the episode's `GoalImpactObserved` row (same ledger
id, `telemetry_capture.record_goal_attribution`), only when it differs from the latest row;
the first row stays as the observation and its provenance. Nothing here writes a run
status, the completion registry, the ROADMAP, the goals store or an artifact, and nothing
reads these rows to decide anything.

**The evidence pack (R38, R70).** `window_tasks` (the tasks with a succeeded run finished in
the window), `goal_section` (per goal the six impacts, legacy apart, misses by current
attribution, and what is excluded), `patterns` (deterministic, ranked, spend kept per pool,
B20), `route_stats` (per role and route, for the analyst only) and `pack`, all of it within
`BRIEF_LIMIT` characters. Read-only but for `attribute`'s amendments.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from maestro import acceptance, goal_replan, goal_store
from maestro.docs import goals as doc_goals
from maestro.workflows import review_origins, telemetry_capture
from maestro.workflows.goal_progress import IMPACTS, LEGACY_NO_PROGRESS

#: The analyst's input bound, in characters of compact JSON (R65).
BRIEF_LIMIT = 12000
#: The spec's lever types (P13 §W12); S3's `check` validates against the same tuple.
LEVERS = ("role_prompt", "model_effort", "graph_order", "gate", "human_step", "context_skill",
          "planner_rule")
TOP_PATTERNS = 8
TOP_ROUTES = 10
TOP_EARLIER = 10
SHOWN_EVIDENCE = 5
#: The earlier proposals the analyst sees: Dan's answers and measured effects.
EARLIER_STATES = ("declined", "noted", "measured", "kept", "rolled_back")
MISS = "no_progress_unexpected"
_GIT_TIMEOUT = 15
_CAUSE_LINE = re.compile(r"^Cause: ([a-z_]+)$")
_UNKNOWN_STATES = (acceptance.BLOCKED, acceptance.UNKNOWN)


def _noop(event: str, detail: str) -> None:
    return None


def _when(text: Any) -> Optional[datetime]:
    """An aware timestamp (a naive one is read as UTC), `None` when it does not parse."""
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=timezone.utc)


def _in(at: Any, since: Optional[str], until: Optional[str]) -> bool:
    """`at` in `(since, until]`; an open end is unbounded. An unreadable time is only in
    the unbounded window."""
    when = _when(at)
    if when is None:
        return since is None and until is None
    low, high = _when(since), _when(until)
    return (low is None or when > low) and (high is None or when <= high)


# --- attribution ---------------------------------------------------------------------------


def _considered(ep: Mapping[str, Any], doc: doc_goals.Goals) -> bool:
    """A real miss (R70): never enabling, maintenance, `serves: []`, an operator-only goal
    or an expected status."""
    goal = str(ep.get("goal") or "")
    return (ep.get("impact") == MISS and bool(ep.get("served"))
            and ep.get("contribution") == "direct" and goal in doc.ids
            and not goal_replan._operator_only(doc, goal))


def _named(ep: Mapping[str, Any], doc: doc_goals.Goals,
           tasks: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The goal's criteria the task named: its episode evidence's criteria and, while its
    block is still in the ROADMAP, its `criteria:` and the criteria its `tests:` bind; all
    of the goal's criteria when it named none."""
    goal = str(ep.get("goal") or "")
    mine = [c.id for c in doc.criteria if c.id in doc.verifiers(goal)]
    named = {str(i.get("criterion")) for i in ep.get("evidence") or ()
             if isinstance(i, Mapping) and i.get("criterion")}
    task = tasks.get(str(ep.get("task") or ""))
    if task:
        named |= {str(c) for c in doc_goals.normalise_criteria(task.get("criteria"))[0]}
        tests = doc_goals.task_tests(task) or ()
        named |= {c.id for c in doc.criteria for t in tests
                  if c.test and t and t in c.test}
    return [c for c in mine if c in named] or mine


def _cause_line(repo: Path, task_id: str) -> Optional[str]:
    """The `Cause:` token on the first line of the diagnosis's cause file at HEAD."""
    try:
        proc = subprocess.run(["git", "show", f"HEAD:{goal_replan.cause_file(task_id)}"],
                              cwd=str(repo), capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines() if proc.returncode == 0 else []
    found = _CAUSE_LINE.match(lines[0].strip()) if lines else None
    token = found.group(1) if found else None
    return token if token in review_origins.DIAGNOSIS_FAULT else None


def _fact(attribution: str, cause: str, rule: str, evidence: Iterable[str]) -> dict:
    return {"attribution": attribution, "cause": cause, "rule": rule,
            "evidence": [str(e) for e in evidence]}


def _rule(repo: Path, ep: Mapping[str, Any], doc: doc_goals.Goals, store, artifacts,
          context: Mapping[str, Any]) -> dict:
    from maestro import goal_state

    run_id = str(ep.get("run_id") or "")
    goal = str(ep.get("goal") or "")
    named = _named(ep, doc, context["tasks"])
    observed = goal_state.collect_observed(store, artifacts, run_id) if run_id else {}
    states = observed.get("acceptance") or {}
    where = f"{run_id}'s gate rerun" + (f" ({str(observed.get('snapshot'))[:12]})"
                                        if observed.get("snapshot") else "")
    base = {r.id: r.state for r in context["baseline"] if r.revision == ep.get("revision")}
    turned = [c for c in named if states.get(c) == acceptance.GREEN
              and base.get(c) != acceptance.GREEN]
    if turned:                                                           # A1
        return _fact("reviewer", "review_misclassified", "gate_saw_green", [
            f"{c} GREEN in {where}; {base.get(c) or 'no baseline'} at revision "
            f"{ep.get('revision')}'s baseline" for c in turned])
    seen = [c for c in named if c in states]
    if seen and all(states[c] in _UNKNOWN_STATES for c in seen):          # A2
        return _fact("none", "measurement_unavailable", "unmeasured",
                     [f"{c} {states[c]} in {where}" for c in seen])
    current = context["terms"].get(goal)
    if ep.get("terms") != current:                                       # A3
        return _fact("none", "contract_revised", "terms_revised",
                     [f"{goal}'s terms {ep.get('terms')} at the review, {current} now"])
    eid = str(ep.get("id") or "")
    for r in context["replans"]:                                         # A4
        trigger = r.get("trigger") or {}
        raised = {str(f) for key in ("facts", "episodes") for f in trigger.get(key) or ()}
        if eid not in raised:
            continue
        for tid in goal_replan._added_tasks(r):
            if "-diagnose-" not in tid or tid not in context["registry"]:
                continue
            token = _cause_line(repo, tid)
            if token:
                return _fact(review_origins.DIAGNOSIS_FAULT[token], f"diagnosis_{token}",
                             "diagnosed", [f"{tid} ({r.get('id')}): Cause: {token} in "
                                           f"{goal_replan.cause_file(tid)} at HEAD"])
    return _fact(review_origins.UNATTRIBUTED, "pending_evidence", "pending_evidence", [])


def _observations(store, run_id: str, goal: str) -> dict[str, list[dict]]:
    """`{ledger_id: rows in order}` of the run's goal-impact observations of `goal`."""
    out: dict[str, list[dict]] = {}
    rows = store.conn.execute(
        "SELECT payload FROM events WHERE type = ? AND run_id = ? ORDER BY sequence",
        (telemetry_capture.GOAL_IMPACT_EVENT, run_id)).fetchall()
    for row in rows:
        try:
            body = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and body.get("goal") == goal and body.get("ledger_id"):
            out.setdefault(str(body["ledger_id"]), []).append(body)
    return out


def _latest(row: Mapping[str, Any]) -> tuple:
    return (row.get("attribution"), row.get("attribution_cause"), row.get("attribution_rule"),
            list(row.get("attribution_evidence") or ()))


def attribute(repo: Path, store, artifacts, *, at: str,
              journal: Optional[Callable[[str, str], None]] = None) -> list[dict]:
    """Attribute every considered episode and append the amendments that changed. Returns
    one fact per considered episode: `{episode, task, goal, run_id, attribution, cause,
    rule, evidence, linked, amended, ledger_ids}`. Never raises: a failing episode is
    skipped and stays as it was."""
    journal = journal or _noop
    try:
        doc = doc_goals.load(repo)
        if doc is None:
            return []
        stored = goal_store.read(repo)
        from maestro import goal_state, taskgraph

        try:
            tasks = {str(t.get("id")): t for t in goal_state._roadmap_tasks(repo)}
        except Exception:  # noqa: BLE001 — a ROADMAP that does not read names nothing
            tasks = {}
        context = {"terms": goal_replan.terms(doc), "replans": list(stored["replans"]),
                   "registry": set(taskgraph.read_registry_ids(repo)),
                   "baseline": acceptance.load(repo), "tasks": tasks}
    except Exception as exc:  # noqa: BLE001
        journal("goal_attribution_failed", f"{type(exc).__name__}: {exc}"[:300])
        return []
    facts: list[dict] = []
    for ep in stored["episodes"]:
        try:
            if not _considered(ep, doc):
                continue
            fact = _rule(repo, ep, doc, store, artifacts, context)
            fact.update(episode=str(ep.get("id")), task=str(ep.get("task")),
                        goal=str(ep.get("goal")), run_id=str(ep.get("run_id") or ""),
                        linked=False, amended=False, ledger_ids=[])
            found = _observations(store, fact["run_id"], fact["goal"])
            if not found:
                journal("goal_attribution_unlinked",
                        f"{fact['episode']}: no observation row in {fact['run_id'] or '?'}")
                facts.append(fact)
                continue
            fact["linked"] = True
            want = (fact["attribution"], fact["cause"], fact["rule"], fact["evidence"])
            for ledger_id, rows in sorted(found.items()):
                fact["ledger_ids"].append(ledger_id)
                if _latest(rows[-1]) != want and telemetry_capture.record_goal_attribution(
                        store, rows[0], fact, at=at):
                    fact["amended"] = True
            facts.append(fact)
        except Exception as exc:  # noqa: BLE001 — one episode never stops the others
            journal("goal_attribution_failed",
                    f"{ep.get('id')}: {type(exc).__name__}: {exc}"[:300])
    amended = [f for f in facts if f["amended"]]
    if amended:
        tally = Counter((f["goal"], f["attribution"]) for f in amended)
        by_goal: dict[str, list[str]] = defaultdict(list)
        for (goal, attribution), n in sorted(tally.items()):
            by_goal[goal].append(f"{attribution} {n}")
        journal("goal_attribution_recorded", f"{len(amended)} amended: " + "; ".join(
            f"{goal} {', '.join(parts)}" for goal, parts in by_goal.items()))
    return facts


# --- the window and the goal section ------------------------------------------------------


def window_tasks(db, since: Optional[str], until: Optional[str]) -> tuple[list[str], bool]:
    """`(task ids with a succeeded run finished in (since, until], run_times_unavailable)`.
    A schema without `finished_at` puts every succeeded run in the window, flagged."""
    from maestro.usage_report import read_snapshot

    with read_snapshot(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM task_runs WHERE status = 'succeeded'")]
    if "finished_at" not in columns:
        return sorted({str(r["task_id"]) for r in rows}), True
    return sorted({str(r["task_id"]) for r in rows
                   if _in(r.get("finished_at"), since, until)}), False


def _attributions(report: Optional[Mapping[str, Any]]) -> dict[tuple, dict]:
    """`{(run_id, goal): {attribution, cause, ledger_ids}}` from the report's views."""
    out: dict[tuple, dict] = {}
    for task in (report or {}).get("tasks") or ():
        for view in task.get("goal_impacts") or ():
            key = (view.get("run_id"), view.get("goal"))
            item = out.setdefault(key, {"attribution": view.get("attribution"),
                                        "cause": view.get("cause"), "ledger_ids": []})
            item["ledger_ids"].append(view.get("ledger_id"))
    return out


def _excluded(ep: Mapping[str, Any], doc: doc_goals.Goals) -> Optional[str]:
    if goal_replan._operator_only(doc, str(ep.get("goal") or "")):
        return "operator_only"
    contribution = ep.get("contribution")
    if not ep.get("served") or contribution not in doc_goals.CONTRIBUTIONS:
        return "serves_empty"
    if contribution in ("enabling", "maintenance"):
        return str(contribution)
    return None


def goal_section(repo: Path, doc: Optional[doc_goals.Goals], since: Optional[str],
                 until: Optional[str], *, report: Optional[Mapping[str, Any]] = None) -> dict:
    """Per goal, the window episodes' six impacts, `no_progress_legacy` apart (never a
    miss), and the misses by their current attribution (from `report`'s `goal_impacts`;
    `unattributed` without it); `excluded` counts the episodes that never form a miss;
    `misses` and `unknown` list the miss and evidence-gap episodes. `{}` without
    `docs/GOALS.md`."""
    doc = doc if doc is not None else doc_goals.load(repo)
    if doc is None:
        return {}
    attributed = _attributions(report)
    goals = {g: {"impacts": {i: 0 for i in IMPACTS}, "legacy": 0, "misses": {}}
             for g in doc.ids}
    excluded = {"enabling": 0, "maintenance": 0, "serves_empty": 0, "operator_only": 0}
    misses: list[dict] = []
    unknown: list[dict] = []
    for ep in goal_store.read(repo)["episodes"]:
        goal = str(ep.get("goal") or "")
        if goal not in goals or not _in(ep.get("at"), since, until):
            continue
        impact = ep.get("impact")
        if impact == LEGACY_NO_PROGRESS:
            goals[goal]["legacy"] += 1
        elif impact in IMPACTS:
            goals[goal]["impacts"][impact] += 1
        kind = _excluded(ep, doc)
        if kind:
            excluded[kind] += 1
            continue
        item = {"episode": str(ep.get("id")), "task": str(ep.get("task")), "goal": goal,
                "run_id": str(ep.get("run_id") or "")}
        if impact == MISS:
            found = attributed.get((item["run_id"], goal)) or {}
            attribution = found.get("attribution") or review_origins.UNATTRIBUTED
            item.update(attribution=attribution,
                        cause=found.get("cause") or "pending_evidence",
                        evidence=sorted(found.get("ledger_ids") or [f"episode:{item['episode']}"]))
            goals[goal]["misses"][attribution] = goals[goal]["misses"].get(attribution, 0) + 1
            misses.append(item)
        elif impact == "unknown":
            item["evidence"] = [f"episode:{item['episode']}"]
            unknown.append(item)
    return {"window": {"since": since, "until": until}, "goals": goals, "excluded": excluded,
            "misses": misses, "unknown": unknown}


# --- patterns -------------------------------------------------------------------------------


def _pattern_id(kind: str, key: Sequence[Any]) -> str:
    return "PAT-" + hashlib.sha1(json.dumps([kind, list(key)]).encode()).hexdigest()[:8]


def _spend(details: Mapping[str, Mapping[str, Any]], attempt_ids: Iterable[str]) -> dict:
    """Per pool, the known cost units and tokens of these attempts; pools never summed."""
    pools: dict[str, dict] = {}
    for aid in sorted(set(attempt_ids)):
        detail = details.get(aid)
        if detail is None:
            continue
        pool = detail.get("pool_id") or "unpooled"
        item = pools.setdefault(pool, {"cost_units": None, "tokens": None})
        measured = detail.get("measurements") or {}
        for field in ("cost_units", "tokens"):
            value = measured.get(field)
            if value is not None:
                item[field] = (item[field] or 0) + value
    return pools


def patterns(report: Mapping[str, Any], goals: Mapping[str, Any], replans: Sequence[Mapping],
             tasks: Iterable[str], *, misses: int = goal_replan.DEFAULT_MISSES) -> list[dict]:
    """The ranked deterministic patterns of the window `tasks` (`extra_step` from the W2
    ledger), of `goals` (`goal_section`: `goal_miss`, `evidence_gap`) and of the window's
    keep-going `replans` (`false_alarm`). Rows descending, then tokens, then id."""
    window = {str(t) for t in tasks}
    details: dict[str, dict] = {}
    by_run: dict[str, list[str]] = defaultdict(list)
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for task in (report or {}).get("tasks") or ():
        if task.get("task_id") not in window:
            continue
        for d in task.get("attempt_details") or ():
            details[d["attempt_id"]] = d
            by_run[d.get("run_id")].append(d["attempt_id"])
        for row in task.get("blame_ledger") or ():
            spent = ([row["attempt_id"]] if row.get("attempt_id")
                     and "attempt_unavailable" not in (row.get("flags") or ()) else [])
            groups[("extra_step", (row.get("fault"), row.get("extra_step_cause"),
                                   row.get("role")))].append(
                (task["task_id"], [row["ledger_id"]], spent))
    if goals:
        # A goal-section run outside the report's window still has its attempts listed.
        for task in (report or {}).get("tasks") or ():
            for d in task.get("attempt_details") or ():
                if d["attempt_id"] not in details:
                    details[d["attempt_id"]] = d
                    by_run[d.get("run_id")].append(d["attempt_id"])
    for m in (goals or {}).get("misses") or ():
        groups[("goal_miss", (m["goal"], m["attribution"], m["cause"]))].append(
            (m["task"], list(m["evidence"]), by_run.get(m["run_id"], [])))
    for u in (goals or {}).get("unknown") or ():
        groups[("evidence_gap", (u["goal"],))].append(
            (u["task"], list(u["evidence"]), by_run.get(u["run_id"], [])))
    for r in replans or ():
        if (r.get("decision") or {}).get("resolution") == "keep_going":
            groups[("false_alarm", ("replan_keep_going",))].append(
                (str(r.get("task") or r.get("id")), [f"replan:{r.get('id')}"], []))
    out = []
    for (kind, key), rows in groups.items():
        n, distinct = len(rows), len({t for t, _, _ in rows})
        if kind == "extra_step" and not (n >= 3 and distinct >= 2):
            continue
        if kind == "goal_miss" and not (n >= misses and distinct >= 2):
            continue
        if kind == "evidence_gap" and n < 3:
            continue
        if kind == "false_alarm" and n < 2:
            continue
        out.append({"id": _pattern_id(kind, key), "kind": kind, "key": list(key), "rows": n,
                    "tasks": distinct,
                    "evidence": sorted({e for _, ids, _ in rows for e in ids}),
                    "spend": _spend(details, [a for _, _, spent in rows for a in spent])})

    def tokens(p: Mapping[str, Any]) -> float:
        return sum(v["tokens"] or 0 for v in p["spend"].values())

    return sorted(out, key=lambda p: (-p["rows"], -tokens(p), p["id"]))


# --- route statistics ----------------------------------------------------------------------


def _per(value, n: int):
    return None if value is None or not n else round(value / n, 3)


def route_stats(report: Mapping[str, Any], tasks: Iterable[str]) -> list[dict]:
    """Per (role, harness, model, effort, pool) over the window tasks: attempts, review
    rounds and gate failures per task, extra steps per task by fault, cost units and tokens
    per task with coverage, and the mean reviewer complexity. For the analyst only: the
    complexity normalisation and the route comparison are W10's."""
    window = {str(t) for t in tasks}
    acc: dict[tuple, dict] = {}
    for task in (report or {}).get("tasks") or ():
        tid = task.get("task_id")
        if tid not in window:
            continue
        scores = [r.get("complexity") for r in task.get("complexity_reviews") or ()
                  if isinstance(r.get("complexity"), int)]
        routes = {}
        for d in task.get("attempt_details") or ():
            routes[d["attempt_id"]] = (d.get("role"), d.get("harness"), d.get("model"),
                                       d.get("effort"), d.get("pool_id"))
        for route in task.get("routes") or ():
            key = (route.get("role"), route.get("harness"), route.get("model"),
                   route.get("effort"), route.get("pool_id"))
            item = acc.setdefault(key, {"tasks": set(), "attempts": 0, "review_rounds": 0,
                                        "gate_failures": 0, "faults": Counter(),
                                        "complexity": {},
                                        **{f: {"sum": None, "measured": 0, "total": 0}
                                           for f in ("cost_units", "tokens")}})
            item["tasks"].add(tid)
            item["attempts"] += route.get("attempts") or 0
            counts = route.get("counts") or {}
            item["review_rounds"] += counts.get("review_rounds") or 0
            item["gate_failures"] += counts.get("gate_failures") or 0
            for field in ("cost_units", "tokens"):
                measure = (route.get("measurements") or {}).get(field) or {}
                if measure.get("known_sum") is not None:
                    item[field]["sum"] = (item[field]["sum"] or 0) + measure["known_sum"]
                item[field]["measured"] += measure.get("measured_attempts") or 0
                item[field]["total"] += measure.get("total_attempts") or 0
            if scores:
                item["complexity"][tid] = scores[-1]
        for row in task.get("blame_ledger") or ():
            key = routes.get(row.get("attempt_id"))
            if key in acc and "attempt_unavailable" not in (row.get("flags") or ()):
                acc[key]["faults"][row.get("fault")] += 1
    out = []
    for key, item in acc.items():
        n = len(item["tasks"])
        complexity = list(item["complexity"].values())
        out.append({
            "role": key[0], "harness": key[1], "model": key[2], "effort": key[3],
            "pool_id": key[4], "tasks": n, "attempts": item["attempts"],
            "attempts_per_task": _per(item["attempts"], n),
            "review_rounds_per_task": _per(item["review_rounds"], n),
            "gate_failures_per_task": _per(item["gate_failures"], n),
            "extra_steps_per_task": {str(f): _per(c, n) for f, c in sorted(
                item["faults"].items(), key=lambda kv: str(kv[0]))},
            "cost_units_per_task": _per(item["cost_units"]["sum"], n),
            "tokens_per_task": _per(item["tokens"]["sum"], n),
            "coverage": {f: {"measured_attempts": item[f]["measured"],
                             "total_attempts": item[f]["total"]}
                         for f in ("cost_units", "tokens")},
            "mean_complexity": round(sum(complexity) / len(complexity), 3)
            if complexity else None})
    return sorted(out, key=lambda r: (-r["tasks"], -r["attempts"],
                                      tuple(str(v or "") for v in (r["role"], r["harness"],
                                                                   r["model"], r["effort"],
                                                                   r["pool_id"]))))


# --- the pack ------------------------------------------------------------------------------


def config_view(repo: Path) -> list[dict]:
    """The config levers' allowlist (`roles.<role>.strength` for each shipped role): the
    effective value, the layer that sets it, and whether `project.yaml` masks the global
    leaf. `[]` when config does not read."""
    try:
        from maestro import config, model_policy

        merged, sources = config.effective_config(repo)
        strengths = model_policy.role_strengths(merged)
        out = []
        for role in sorted(model_policy.shipped_role_strengths()):
            path = f"roles.{role}.strength"
            out.append({"path": path, "value": strengths.get(role),
                        "source": sources.get(path, "shipped"),
                        "masked": sources.get(path) == "project"})
        return out
    except Exception:  # noqa: BLE001
        return []


def _shown(pattern: Mapping[str, Any]) -> dict:
    evidence = list(pattern.get("evidence") or ())
    return {**pattern, "evidence": evidence[:SHOWN_EVIDENCE], "evidence_total": len(evidence)}


def _earlier(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    keys = ("id", "state", "lever", "target", "change", "config", "pattern", "decision",
            "effect")
    out = []
    for r in rows:
        if r.get("state") in EARLIER_STATES:
            item = {k: r[k] for k in keys if k in r}
            if isinstance(item.get("pattern"), Mapping):
                item["pattern"] = {k: item["pattern"].get(k) for k in ("id", "kind", "key")}
            out.append(item)
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True))


def pack(repo: Path, db, *, since: Optional[str] = None, until: Optional[str] = None,
         earlier: Iterable[Mapping[str, Any]] = (), limit: Optional[int] = None) -> dict:
    """The analyst's input: the window, its task count, the goal section's counts, the top
    patterns (each with at most `SHOWN_EVIDENCE` evidence ids), the top route rows, this
    project's earlier answered or measured proposals, the lever vocabulary and the config
    allowlist. Its compact JSON fits `BRIEF_LIMIT` (or `limit`): whole entries are dropped
    from the tail of the lists (earlier proposals, then routes, then patterns), never part
    of a pattern; `trimmed` counts what was left out."""
    from maestro import config
    from maestro.usage_report import build_usage_report

    limit = BRIEF_LIMIT if limit is None else limit
    tasks, no_times = window_tasks(db, since, until)
    report = build_usage_report(db)
    doc = doc_goals.load(repo)
    section = goal_section(repo, doc, since, until, report=report) if doc else {}
    replans = [r for r in goal_store.read(repo)["replans"]
               if _in(r.get("settled_at"), since, until)] if doc else []
    try:
        misses = goal_replan.miss_threshold(config.effective_config(repo)[0])
    except Exception:  # noqa: BLE001
        misses = goal_replan.DEFAULT_MISSES
    found = patterns(report, section, replans, tasks, misses=misses)
    routes = route_stats(report, tasks)
    previous = _earlier(earlier)
    brief = {
        "window": {"since": since, "until": until}, "tasks": len(tasks),
        "flags": ["run_times_unavailable"] if no_times else [],
        "goals": {k: section[k] for k in ("goals", "excluded")} if section else {},
        "patterns": [_shown(p) for p in found[:TOP_PATTERNS]],
        "routes": routes[:TOP_ROUTES],
        "earlier": previous[:TOP_EARLIER],
        "levers": list(LEVERS),
        "config": config_view(repo),
        "trimmed": {"patterns": max(0, len(found) - TOP_PATTERNS),
                    "routes": max(0, len(routes) - TOP_ROUTES),
                    "earlier": max(0, len(previous) - TOP_EARLIER)},
    }
    for name in ("earlier", "routes", "patterns"):
        while brief[name] and _size(brief) > limit:
            brief[name].pop()
            brief["trimmed"][name] += 1
    return brief
