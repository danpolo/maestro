"""What an improvement changed, measured (P13 GDT-12 / W12, R59, R65's effects, C33).

Read-only. Every metric is lower-is-better, and none counts GREEN criteria or rewards blame
(`01` §10). The ledger numbers come from `usage_report.build_usage_report` over the same
tasks, so the cost, token and wall sums here equal `maestro report usage` (the W10 seam).
The goal numbers come from the goals store and the acceptance baseline. An unknown value
stays `None`, never 0; a count over measured tasks is a real 0.

- `measure(db, repo, tasks)` -> `{tasks, since, until, flags, metrics: {key: {value,
  known, total[, sum]}}}`. A key is a `METRICS` name, optionally qualified:
  `cost_units_per_task:<pool>` (pools are never summed together),
  `extra_steps_per_task:<fault>` and `extra_steps_per_task:<fault>/<cause>`.
- `windows(db, at, min_tasks=)`: the accepted tasks finished after `at`, paired by count
  with the same number finished just before it; `None` with too few on either side.
- `effect(before, after, targets)`: `improved` | `no_change` | `worse` |
  `insufficient_data`, with a quality guard: a cost saving with a quality drop is `worse`.
- `line(proposal, effect)`: the digest line.

Nothing here writes anything or decides a verdict of any task.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence

from maestro import acceptance, goal_store
from maestro.usage_report import build_usage_report, read_snapshot

#: The vocabulary. Every metric is lower-is-better; none counts GREEN criteria.
METRICS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "cost_units_per_task": {"better": "lower", "label": "cost units/task", "unit": ""},
    "tokens_per_task": {"better": "lower", "label": "tokens/task", "unit": ""},
    "wall_time_per_task": {"better": "lower", "label": "wall time/task", "unit": " s"},
    "gate_attempts_per_task": {"better": "lower", "label": "gate attempts/task", "unit": ""},
    "review_rounds_per_task": {"better": "lower", "label": "review rounds/task", "unit": ""},
    "extra_steps_per_task": {"better": "lower", "label": "extra steps/task", "unit": ""},
    "false_alarms_per_task": {"better": "lower", "label": "false alarms/task", "unit": ""},
    "false_hold_hours": {"better": "lower", "label": "false holds", "unit": " h"},
    "time_to_outcome_hours": {"better": "lower", "label": "time to outcome", "unit": " h"},
    "quality_defects_per_task": {"better": "lower", "label": "quality defects/task",
                                 "unit": ""},
})
#: The smallest relative change that counts as a move.
MIN_CHANGE = 0.10
_EPS = 1e-9
#: Extra-step causes that say an alarm was not needed (Dan allowed what was stopped).
FALSE_ALARM_CAUSES = frozenset({"runaway_false_alarm", "deny_approved", "permission_allowed"})
#: Extra-step causes that say a defect got past an approval.
QUALITY_CAUSES = frozenset({"escaped_defect", "accept_rejected_review", "accept_rejected_plan"})
QUALITY = "quality_defects_per_task"
COST = "cost_units_per_task"
_SPAN = object()


def _when(text: Any) -> Optional[datetime]:
    """An aware timestamp, or `None`."""
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


def _within(at: Optional[datetime], since: Optional[datetime], until: Optional[datetime]) -> bool:
    return at is not None and (since is None or at > since) and (until is None or at <= until)


def _runs(database) -> tuple[list[dict], bool]:
    """The `task_runs` rows, and whether the schema records run times."""
    with read_snapshot(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
        rows = [dict(r) for r in conn.execute("SELECT * FROM task_runs")]
    return rows, {"started_at", "finished_at"} <= columns


def _finished(runs: Iterable[Mapping]) -> dict[str, tuple[datetime, str]]:
    """`{task: (when, text)}`: each accepted task's latest succeeded run's finish."""
    out: dict[str, tuple[datetime, str]] = {}
    for run in runs:
        when = _when(run.get("finished_at"))
        if run.get("status") != "succeeded" or when is None:
            continue
        task = str(run["task_id"])
        if task not in out or when > out[task][0]:
            out[task] = (when, str(run["finished_at"]))
    return out


def _order(database) -> Optional[list[tuple[str, datetime, str]]]:
    runs, timed = _runs(database)
    if not timed:
        return None
    return sorted(((t, w, s) for t, (w, s) in _finished(runs).items()),
                  key=lambda item: (item[1], item[0]))


def accepted_after(database, at: str) -> list[str]:
    """The accepted tasks finished after `at`, in finish order (`[]` without run times)."""
    order, start = _order(database), _when(at)
    if not order or start is None:
        return []
    return [t for t, when, _ in order if when > start]


def windows(database, at: str, *, min_tasks: int) -> Optional[dict]:
    """`{before: {tasks, since, until}, after: {...}}`: the accepted tasks finished after
    `at` and the same number finished just before it (paired by count, not by time). When
    fewer finished before, the after window is its earliest tasks of that count. `None`
    when a side has fewer than `min_tasks`, or the schema has no run times."""
    order, start = _order(database), _when(at)
    if order is None or start is None:
        return None
    after = [item for item in order if item[1] > start]
    before = [item for item in order if item[1] <= start]
    n = min(len(after), len(before))
    if n < max(int(min_tasks), 1):
        return None
    after, chosen = after[:n], before[-n:]
    return {"before": {"tasks": [t for t, _, _ in chosen],
                       "since": before[-n - 1][2] if len(before) > n else None, "until": at},
            "after": {"tasks": [t for t, _, _ in after], "since": at, "until": after[-1][2]}}


# --- measure ---------------------------------------------------------------------------------


def _summed(values: Sequence[Optional[float]], total: int) -> dict:
    known = [v for v in values if v is not None]
    total_sum = sum(known) if known else None
    return {"value": total_sum / len(known) if known else None, "known": len(known),
            "total": total, "sum": total_sum}


def _per_task(count: float, total: int) -> dict:
    return {"value": count / total if total else None, "known": total, "total": total}


def _span(runs: list[dict], tasks: set[str], timed: bool):
    """The tasks' own runs: from the earliest start to the latest finish."""
    if not timed:
        return None, None
    mine = [r for r in runs if r.get("task_id") in tasks]
    starts = [_when(r.get("started_at")) for r in mine]
    ends = [_when(r.get("finished_at")) for r in mine]
    since = min(starts) if mine and None not in starts else None
    until = max(ends) if mine and None not in ends else None
    return since, until


def _bound(value, derived: Optional[datetime]) -> Optional[datetime]:
    return derived if value is _SPAN else _when(value)


def _goal_store(repo: Path, flags: list[str]) -> Optional[dict]:
    try:
        return goal_store.read_file(goal_store.path_for(repo), goal_store.SECTIONS)
    except (OSError, ValueError):
        flags.append("goal_store_unreadable")
        return None


def _keep_going(store: Optional[Mapping], since, until) -> Optional[list[dict]]:
    if store is None:
        return None
    out = []
    for replan in store.get("replans") or ():
        decision = replan.get("decision") if isinstance(replan.get("decision"), Mapping) else {}
        if decision.get("resolution") != "keep_going":
            continue
        settled = _when(replan.get("settled_at")) or _when(decision.get("at"))
        if _within(settled, since, until):
            out.append({**replan, "_settled": settled})
    return out


def _false_holds(replans: Optional[list[dict]]) -> dict:
    if replans is None:
        return {"value": None, "known": 0, "total": 0}
    hours, known = 0.0, 0
    for replan in replans:
        start = _when(replan.get("at"))
        scope = replan.get("scope") if isinstance(replan.get("scope"), Mapping) else {}
        held = scope.get("held_tasks")
        if start is None or not isinstance(held, list) or replan["_settled"] < start:
            continue
        known += 1
        hours += len(held) * (replan["_settled"] - start).total_seconds() / 3600
    value = hours if known or not replans else None
    return {"value": value, "known": known, "total": len(replans)}


def _baseline(repo: Path) -> Optional[dict]:
    try:
        payload = json.loads(acceptance.path_for(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _greens(store: Mapping, revision) -> dict[str, datetime]:
    """`{criterion: earliest recorded GREEN}` at `revision`, from every observation source."""
    seen: list[tuple[str, Optional[datetime]]] = []
    for row in store.get("criteria") or ():
        if row.get("revision") == revision and row.get("state") == acceptance.GREEN:
            seen.append((str(row.get("id")), _when(row.get("at"))))
    for point in store.get("checkpoints") or ():
        if point.get("revision", revision) != revision:
            continue
        for states in (point.get("goals") or {}).values():
            if isinstance(states, Mapping):
                seen += [(str(c), _when(point.get("at")))
                         for c, s in states.items() if s == acceptance.GREEN]
    for run in store.get("finish") or ():
        if run.get("revision", revision) != revision:
            continue
        results = run.get("results") if isinstance(run.get("results"), Mapping) else {}
        for cid, result in results.items():
            if isinstance(result, Mapping) and result.get("state") == acceptance.GREEN:
                seen.append((str(cid), _when(result.get("at")) or _when(run.get("started_at"))))
    first: dict[str, datetime] = {}
    for cid, when in seen:
        if when is not None and (cid not in first or when < first[cid]):
            first[cid] = when
    return first


def _time_to_outcome(repo: Path, store: Optional[Mapping], since, until) -> dict:
    """Per criterion RED at the baseline, its earliest GREEN minus the baseline time; the
    median over the criteria first GREEN in the window (at least 2, else unknown)."""
    payload = _baseline(repo)
    ran_at = _when((payload or {}).get("ran_at"))
    if payload is None or ran_at is None or store is None:
        return {"value": None, "known": 0, "total": 0}
    revision = payload.get("revision")
    reds = {str(r.get("id")) for r in payload.get("results") or ()
            if isinstance(r, Mapping) and r.get("state") == acceptance.RED
            and r.get("revision", revision) == revision}
    first = _greens(store, revision)
    hours = [(first[c] - ran_at).total_seconds() / 3600 for c in sorted(reds)
             if c in first and first[c] >= ran_at and _within(first[c], since, until)]
    return {"value": statistics.median(hours) if len(hours) >= 2 else None,
            "known": len(hours), "total": len(reds)}


def measure(database, repo: Path, tasks: Iterable[str], *, since=_SPAN, until=_SPAN) -> dict:
    """The metrics of `tasks`. Facts with a time (keep-going replans, GREEN observations)
    count in `(since, until]`; by default the span of the tasks' own runs, and `None` is
    open. A schema without run times is flagged `run_times_unavailable` and its time
    facts are unbounded."""
    chosen = sorted({str(t) for t in tasks})
    wanted, n = set(chosen), len(chosen)
    runs, timed = _runs(database)
    flags = [] if timed else ["run_times_unavailable"]
    span_since, span_until = _span(runs, wanted, timed)
    start, end = _bound(since, span_since), _bound(until, span_until)
    rows = [t for t in build_usage_report(database)["tasks"] if t["task_id"] in wanted]
    missing = n - len(rows)

    def sums(field):
        return [t["measurements"][field]["known_sum"] for t in rows] + [None] * missing

    metrics: dict[str, dict] = {
        "tokens_per_task": _summed(sums("tokens"), n),
        "wall_time_per_task": _summed(sums("wall_time_sec"), n),
        "gate_attempts_per_task": _per_task(sum(t["counts"]["gate_attempts"] for t in rows), n),
        "review_rounds_per_task": _per_task(sum(t["counts"]["review_rounds"] for t in rows), n),
    }
    by_pool: dict[str, list] = {}
    for task in rows:
        pools = sorted({a["pool_id"] for a in task["attempt_details"] if a["pool_id"]})
        cost = task["measurements"]["cost_units"]["known_sum"] if len(pools) == 1 else None
        for pool in pools:  # a mixed-pool task's cost is unknown in each of its pools
            by_pool.setdefault(pool, []).append(cost)
    for pool, values in sorted(by_pool.items()):
        metrics[f"{COST}:{pool}"] = _summed(values, len(values))
    ledger = [row for t in rows for row in t["blame_ledger"]]
    metrics["extra_steps_per_task"] = _per_task(len(ledger), n)
    for key in sorted({f"{r['fault']}" for r in ledger} |
                      {f"{r['fault']}/{r['extra_step_cause']}" for r in ledger}):
        count = sum(1 for r in ledger if key in (r["fault"], f"{r['fault']}/{r['extra_step_cause']}"))
        metrics[f"extra_steps_per_task:{key}"] = _per_task(count, n)
    store = _goal_store(Path(repo), flags)
    replans = _keep_going(store, start, end)
    alarms = sum(1 for r in ledger if r.get("extra_step_cause") in FALSE_ALARM_CAUSES)
    metrics["false_alarms_per_task"] = (_per_task(alarms + len(replans), n) if replans is not None
                                        else {"value": None, "known": 0, "total": n})
    metrics["false_hold_hours"] = _false_holds(replans)
    metrics["time_to_outcome_hours"] = _time_to_outcome(Path(repo), store, start, end)
    defects = sum(1 for r in ledger if r.get("extra_step_cause") in QUALITY_CAUSES)
    regressed = sum(1 for e in (store or {}).get("episodes") or ()
                    if e.get("task") in wanted and e.get("impact") == "regressed")
    metrics[QUALITY] = (_per_task(defects + regressed, n) if store is not None
                        else {"value": None, "known": 0, "total": n})
    return {"tasks": chosen, "since": start.isoformat() if start else None,
            "until": end.isoformat() if end else None, "flags": flags,
            "metrics": dict(sorted(metrics.items()))}


# --- effect ----------------------------------------------------------------------------------


def _get(measured: Mapping, key: str) -> Optional[float]:
    row = (measured.get("metrics") or {}).get(key)
    if row is not None:
        return row.get("value")
    # A fault or cause with no row in a window is a measured 0 there.
    if key.startswith("extra_steps_per_task:") and measured.get("tasks"):
        return 0.0
    return None


def _rel(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None:
        return None
    if before == 0:
        return 0.0 if after == 0 else math.inf
    return after / before - 1


def _moved(rel: Optional[float], up: bool) -> bool:
    if rel is None:
        return False
    return rel >= MIN_CHANGE - _EPS if up else rel <= -MIN_CHANGE + _EPS


def effect(before: Optional[Mapping], after: Optional[Mapping], targets: Sequence[str]) -> dict:
    """The verdict of `targets` from `before` to `after` (two `measure` results)."""
    targets = list(dict.fromkeys(targets))
    tasks = {"before": len((before or {}).get("tasks") or ()),
             "after": len((after or {}).get("tasks") or ())}
    if before is None or after is None:
        return {"verdict": "insufficient_data", "targets": targets, "metrics": {},
                "notes": ["a window is missing"], "tasks": tasks}
    keys = sorted(set(before.get("metrics") or {}) | set(after.get("metrics") or {}) | set(targets))
    rows = {}
    for key in keys:
        b, a = _get(before, key), _get(after, key)
        rel = _rel(b, a)
        rows[key] = {"before": b, "after": a,
                     "change": rel if rel is not None and math.isfinite(rel) else None}

    def rel(key):
        return _rel(rows[key]["before"], rows[key]["after"]) if key in rows else None

    missing = [t for t in targets if rows[t]["before"] is None or rows[t]["after"] is None]
    if missing:
        return {"verdict": "insufficient_data", "targets": targets, "metrics": rows,
                "notes": [f"no measured value for {', '.join(missing)}"], "tasks": tasks}
    better = [t for t in targets if _moved(rel(t), up=False)]
    worse = [t for t in targets if _moved(rel(t), up=True)]
    cost_up = [k for k in keys if k.startswith(COST + ":") and k not in targets
               and _moved(rel(k), up=True)]
    q = rel(QUALITY)
    quality_known = q is not None
    quality_rose = quality_known and q > _EPS
    quality_worse = quality_rose and (rows[QUALITY]["before"] == 0 or _moved(q, up=True))
    notes = []
    if quality_worse:
        notes.append("quality defects rose: a saving never outweighs a quality drop")
    if worse:
        notes.append(f"worsened: {', '.join(worse)}")
    if cost_up:
        notes.append(f"cost rose: {', '.join(cost_up)}")
    if quality_worse or worse or (cost_up and not better):
        verdict = "worse"
    elif better and quality_known and not quality_rose and not cost_up:
        verdict = "improved"
    else:
        verdict = "no_change"
        if better:
            why = ("cost rose" if cost_up else "quality rose" if quality_rose
                   else "quality is unknown")
            notes.append(f"mixed: {', '.join(better)} improved but {why}")
    return {"verdict": verdict, "targets": targets, "metrics": rows, "notes": notes,
            "tasks": tasks}


def _number(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _label(key: str) -> str:
    base, _, qualifier = key.partition(":")
    label = METRICS.get(base, {}).get("label", base)
    return f"{label} ({qualifier})" if qualifier else label


def line(proposal: Mapping, effect: Optional[Mapping]) -> str:
    """The digest line of `proposal`'s effect. An `insufficient_data` effect carrying
    `tasks: {after, needed}` reads "IP-0001: 2 of 5 accepted tasks since applied"."""
    pid = str(proposal.get("id") or "?")
    change = proposal.get("change")
    head = f"{pid} ({change})" if change else pid
    effect = effect or {"verdict": "insufficient_data"}
    verdict = effect.get("verdict") or "insufficient_data"
    if verdict == "insufficient_data":
        counts = effect.get("tasks") or {}
        if "needed" in counts:
            return f"{pid}: {counts.get('after', 0)} of {counts['needed']} accepted tasks since applied"
        notes = "; ".join(effect.get("notes") or ())
        return f"{head}: insufficient data" + (f" ({notes})" if notes else "")
    rows = effect.get("metrics") or {}
    targets = list(effect.get("targets") or ())
    order = targets + sorted(k for k in rows if k.startswith(COST + ":")) + [
        "false_hold_hours", "time_to_outcome_hours", QUALITY]
    parts = []
    for key in dict.fromkeys(order):
        row = rows.get(key)
        if row is None:
            continue
        base, _, qualifier = key.partition(":")
        unit = METRICS.get(base, {}).get("unit", "")
        if base == COST and key not in targets:
            pct = row.get("change")
            amount = (f"{pct * 100:+.0f}%" if pct is not None
                      else f"{_number(row.get('before'))} → {_number(row.get('after'))}")
            parts.append(f"{METRICS[COST]['label']} {amount} ({qualifier})")
            continue
        parts.append(f"{_label(key)} {_number(row.get('before'))} → "
                     f"{_number(row.get('after'))}{unit}")
    return f"{head}: " + ", ".join(parts) + f" → {verdict}."
