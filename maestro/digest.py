"""P13 W10: the weekly digest, rendered only from `maestro report usage`.

`build` takes the report `usage_report.build_usage_report` returns and adds no other number, so
the digest equals the report by construction (`tests/test_w10_visibility.py` compares them). It
covers everything the report covers, not one week: the report has no time filter, and a window
of its own would make the two disagree. What changed in the week is the difference from the totals
the last digest recorded.

Usage is the report's routes that name a harness. A gate, merge or acceptance attempt runs no model
and has no tokens, so counting it would make every average unknown. A partly measured sum is shown
as a lower bound with its coverage, because one unmeasured attempt (agy has no token reader) would
otherwise hide a total for good. Nothing measured stays unknown.

An accepted task is one with a succeeded run. Usage per accepted task divides the spend of every
task, failed ones included, by the accepted tasks, because failed work is part of what an accepted
task cost. Cost units stay per pool, as in the report.

Route comparison: for each route, the cost units of the tasks with a reviewed complexity divided
by the sum of those tasks' complexity points, taking each task's last reviewed complexity. A task's
points count for every route that worked on it, because the ledger does not say which route
produced the reviewed work, so a route that failed and escalated still gets the points: read the
attempts per task beside the figure. Tasks with no review are left out and counted as unscored.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

STORE = ".orchestrator/digest.json"
TASK_LINES = 15
RETRY_AFTER = timedelta(hours=1)
_UNKNOWN_POOL = "unknown pool"
#: Raised by any attempt that runs no model, or by the task-level cost the digest does not use.
_NO_MODEL_ISSUES = ("missing_route", "missing_pool", "missing_relative_cost", "mixed_cost_pools")


def _merge(metrics: Iterable[Mapping]) -> dict:
    """The sum of `_metric` documents, keeping coverage; `known_sum` is `None` with no value."""
    rows = list(metrics)
    known = [m["known_sum"] for m in rows if m.get("known_sum") is not None]
    measured = sum(m["measured_attempts"] for m in rows)
    total = sum(m["total_attempts"] for m in rows)
    return {"known_sum": sum(known) if known else None, "measured_attempts": measured,
            "total_attempts": total, "complete": bool(known) and measured == total}


def _model_routes(owner: Mapping) -> list[Mapping]:
    return [r for r in owner.get("routes") or [] if r.get("harness")]


def _measure(routes: Sequence[Mapping], name: str) -> dict:
    return _merge(r["measurements"][name] for r in routes)


def _per_pool(routes: Sequence[Mapping]) -> dict[str, dict]:
    grouped = defaultdict(list)
    for route in routes:
        grouped[route.get("pool_id") or _UNKNOWN_POOL].append(route["measurements"]["cost_units"])
    return {pool: _merge(rows) for pool, rows in sorted(grouped.items())}


def _average(metric: Mapping, count: int) -> Optional[float]:
    return metric["known_sum"] / count if count and metric["known_sum"] is not None else None


def _task_complexity(task: Mapping) -> Optional[int]:
    scored = [r for r in task.get("complexity_reviews") or [] if r.get("complexity") is not None]
    if not scored:
        return None
    latest = max(range(len(scored)), key=lambda i: (str(scored[i].get("recorded_at") or ""), i))
    return scored[latest]["complexity"]


def _route_rows(tasks: Sequence[Mapping]) -> list[dict]:
    keys = ("role", "harness", "model", "effort", "pool_id")
    grouped: dict[tuple, dict] = {}
    for task in tasks:
        complexity = _task_complexity(task)
        for route in _model_routes(task):
            row = grouped.setdefault(tuple(route.get(k) for k in keys), {
                **{k: route.get(k) for k in keys}, "attempts": 0, "tasks": 0, "unscored_tasks": 0,
                "complexity_points": 0, "_costs": []})
            row["attempts"] += route["attempts"]
            if complexity is None:
                row["unscored_tasks"] += 1
                continue
            row["tasks"] += 1
            row["complexity_points"] += complexity
            row["_costs"].append(route["measurements"]["cost_units"])
    rows = []
    for key, row in sorted(grouped.items(), key=lambda item: tuple(str(v or "") for v in item[0])):
        cost = _merge(row.pop("_costs"))
        points = row["complexity_points"]
        rows.append({**row, "cost_units": cost,
                     "cost_per_point": cost["known_sum"] / points
                     if points and cost["complete"] else None})
    return rows


def _fault_rows(tasks: Sequence[Mapping]) -> list[dict]:
    grouped = defaultdict(list)
    for task in tasks:
        for fault in task.get("faults") or []:
            grouped[fault["fault"]].append(fault)
    result = []
    for name, rows in sorted(grouped.items()):
        routes = [r for f in rows for r in _model_routes(f)]
        result.append({"fault": name, "ledger_rows": sum(f["ledger_rows"] for f in rows),
                       "tokens": _measure(routes, "tokens"),
                       "wall_time_sec": _measure(routes, "wall_time_sec"),
                       "cost_by_pool": _per_pool(routes)})
    return result


def _task_issues(task: Mapping) -> list[str]:
    """The task's issues, keeping a route or pool complaint only when a model attempt raised it."""
    of_models = {issue for a in task.get("attempt_details") or [] if a.get("harness")
                 for issue in a.get("issues") or []}
    return [i for i in task.get("issues") or [] if i not in _NO_MODEL_ISSUES
            or (i in of_models and i != "mixed_cost_pools")]


def _totals(digest: Mapping) -> dict:
    """What the next digest needs to say what changed."""
    total = digest["total"]
    return {"accepted_tasks": digest["accepted_tasks"], "tokens": total["tokens"]["known_sum"] or 0,
            "implementer_cycles": total["implementer_cycles"],
            "review_rounds": total["review_rounds"],
            "cost_by_pool": {p: m["known_sum"] or 0 for p, m in digest["cost_by_pool"].items()}}


def _since_last(digest: Mapping, previous: Optional[Mapping]) -> Optional[dict]:
    """The difference from the totals the last digest recorded; `None` without any, or when a
    total went down (a rebuilt control database), since a negative week would be nonsense."""
    earlier = (previous or {}).get("totals")
    if not isinstance(earlier, Mapping):
        return None
    now = _totals(digest)
    try:
        change = {k: now[k] - earlier[k]
                  for k in ("accepted_tasks", "tokens", "implementer_cycles", "review_rounds")}
        pools = {p: v - (earlier.get("cost_by_pool") or {}).get(p, 0)
                 for p, v in now["cost_by_pool"].items()}
    except (KeyError, TypeError, AttributeError):
        return None
    if any(v < 0 for v in (*change.values(), *pools.values())):
        return None
    return {**change, "since": str(previous.get("sent_at") or "unknown date")[:10],
            "cost_by_pool": {p: v for p, v in pools.items() if v}}


def build(report: Mapping, improvement: Optional[Mapping] = None,
          previous: Optional[Mapping] = None) -> dict:
    """The digest's numbers, each one taken or summed from `report`, and the difference from
    `previous`, the record the last digest left."""
    tasks = report.get("tasks") or []
    accepted = [t for t in tasks if (t.get("run_statuses") or {}).get("succeeded")]
    count = len(accepted)
    routes = [r for t in tasks for r in _model_routes(t)]
    tokens = _measure(routes, "tokens")
    wall = _measure(routes, "wall_time_sec")
    pools = _per_pool(routes)
    total_cycles = sum(t["counts"]["implementer_cycles"] for t in tasks)
    total_rounds = sum(t["counts"]["review_rounds"] for t in tasks)
    built = {
        "all_tasks": len(tasks),
        "accepted_tasks": count,
        "accepted": [{"task_id": t["task_id"],
                      "tokens": _measure(_model_routes(t), "tokens"),
                      "wall_time_sec": _measure(_model_routes(t), "wall_time_sec"),
                      "cost_by_pool": _per_pool(_model_routes(t)),
                      "implementer_cycles": t["counts"]["implementer_cycles"],
                      "review_rounds": t["counts"]["review_rounds"]} for t in accepted],
        "total": {"tokens": tokens, "wall_time_sec": wall, "implementer_cycles": total_cycles,
                  "review_rounds": total_rounds},
        "cost_by_pool": pools,
        "per_accepted": {"tokens": _average(tokens, count),
                         "wall_time_sec": _average(wall, count),
                         "implementer_cycles": total_cycles / count if count else None,
                         "review_rounds": total_rounds / count if count else None,
                         "cost_by_pool": {p: _average(m, count) for p, m in pools.items()}},
        "faults": _fault_rows(tasks),
        "routes": _route_rows(tasks),
        "issues": dict(sorted(Counter(i for t in tasks for i in _task_issues(t)).items())),
        "missing_attempt_columns": list(report.get("missing_attempt_columns") or []),
        "improvement": improvement,
    }
    built["since_last"] = _since_last(built, previous)
    return built


def _number(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _bound(metric: Mapping) -> str:
    return "at least " if metric["known_sum"] is not None and not metric["complete"] else ""


def _metric(metric: Mapping, unit: str = "") -> str:
    if metric["known_sum"] is None:
        return "unknown"
    suffix = f" {unit}" if unit else ""
    shown = f"{_bound(metric)}{_number(metric['known_sum'])}{suffix}"
    if not metric["complete"]:
        shown += f" ({metric['measured_attempts']}/{metric['total_attempts']} attempts measured)"
    return shown


def _pools(pools: Mapping[str, Mapping]) -> str:
    return "; ".join(f"{pool} {_metric(metric)}" for pool, metric in pools.items()) or "unknown"


def _improvement_lines(improvement: Optional[Mapping]) -> list[str]:
    if not improvement:
        return []
    from maestro.improvement_metrics import line

    proposals = improvement.get("proposals") or []
    by_id = {p.get("id"): p for p in proposals}
    lines = ["", f"Improvement: {len(improvement.get('runs') or [])} runs, "
                 f"{len(proposals)} proposals"]
    for row in improvement.get("effects") or []:
        lines.append("  " + line(by_id.get(row.get("id")) or {"id": row.get("id")}, row.get("effect")))
    return lines


def _since_line(change: Optional[Mapping]) -> list[str]:
    if not change:
        return []
    new = change["accepted_tasks"]
    each = f" ({_number(change['tokens'] / new)} per new accepted task)" if new else ""
    cost = "; ".join(f"{pool} +{_number(v)}" for pool, v in change["cost_by_pool"].items())
    return [f"Since the last digest ({change['since']}): +{new} accepted tasks, "
            f"+{_number(change['tokens'])} tokens{each}, "
            f"+{change['implementer_cycles']} implementer, +{change['review_rounds']} review"
            + (f"; cost {cost}" if cost else "")]


def _route_lines(routes: Sequence[Mapping]) -> list[str]:
    grouped = defaultdict(list)
    for row in routes:
        grouped[(row.get("role") or "unknown role", row.get("pool_id") or _UNKNOWN_POOL)].append(row)
    lines = []
    for (role, pool), rows in sorted(grouped.items()):
        lines.append(f"  {role}, {pool}:")
        ranked = sorted(rows, key=lambda r: (r["cost_per_point"] is None, r["cost_per_point"] or 0))
        for row in ranked:
            name = "/".join(str(row.get(k) or "unknown") for k in ("harness", "model", "effort"))
            per_task = _number(row["attempts"] / ((row["tasks"] + row["unscored_tasks"]) or 1))
            if not row["tasks"]:
                scored = "complexity unknown"
            elif row["cost_per_point"] is None:
                scored = "unknown: " + _metric(row["cost_units"])
            else:
                scored = (f"{_number(row['cost_per_point'])} per point over "
                          f"{row['complexity_points']} points, {row['tasks']} tasks")
            unscored = f", {row['unscored_tasks']} unscored" if row["tasks"] and row["unscored_tasks"] else ""
            lines.append(f"    {name}: {scored}, {per_task} attempts per task{unscored}")
    return lines


def render(digest: Mapping) -> str:
    average = digest["per_accepted"]
    total = digest["total"]
    pools = digest["cost_by_pool"]
    lines = [f"{digest['accepted_tasks']} accepted of {digest['all_tasks']} tasks, "
             "everything recorded so far", *_since_line(digest.get("since_last")), "",
             f"Tokens: {_metric(total['tokens'])}; per accepted task: "
             f"{_bound(total['tokens'])}{_number(average['tokens'])}",
             f"Wall time: {_metric(total['wall_time_sec'], 's')}; per accepted task: "
             f"{_bound(total['wall_time_sec'])}{_number(average['wall_time_sec'])}",
             f"Cost units: {_pools(pools)}"]
    lines += [f"  per accepted task, {pool}: {_bound(pools[pool])}{_number(value)}"
              for pool, value in average["cost_by_pool"].items()]
    lines += [f"Cycles: {total['implementer_cycles']} implementer, {total['review_rounds']} review; "
              f"per accepted task: {_number(average['implementer_cycles'])} and "
              f"{_number(average['review_rounds'])}"]
    if digest["accepted"]:
        lines += ["", "Accepted tasks:"]
        ranked = sorted(digest["accepted"], key=lambda t: -(t["tokens"]["known_sum"] or -1))
        for row in ranked[:TASK_LINES]:
            lines.append(f"  {row['task_id']}: {_metric(row['tokens'], 'tokens')}, "
                         f"cost {_pools(row['cost_by_pool'])}, "
                         f"{row['implementer_cycles']} implementer, {row['review_rounds']} review")
        if len(ranked) > TASK_LINES:
            lines.append(f"  … {len(ranked) - TASK_LINES} more accepted tasks")
    lines += ["", "Cost per fault:"]
    lines += [f"  {row['fault']}: {row['ledger_rows']} rows, {_metric(row['tokens'], 'tokens')}, "
              f"cost {_pools(row['cost_by_pool'])}" for row in digest["faults"]] or ["  none recorded"]
    lines += ["", "Routes, cost per complexity point, cheapest first within each role and pool "
                  "(a task's points count for every route that worked on it):"]
    lines += _route_lines(digest["routes"])
    lines += _improvement_lines(digest.get("improvement"))
    if digest["issues"] or digest["missing_attempt_columns"]:
        flagged = ", ".join(f"{name} ×{count}" for name, count in digest["issues"].items())
        missing = ", ".join(digest["missing_attempt_columns"])
        lines += ["", "Unknown or flagged: " + "; ".join(x for x in (flagged, missing) if x)]
    return "\n".join(lines) + "\n"


def _record(repo: Path) -> dict:
    try:
        record = json.loads((Path(repo) / STORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _due(repo: Path, at: str, record: Mapping) -> bool:
    from maestro import improvement

    days = improvement._threshold(repo, "improvement_interval_days",
                                  improvement.DEFAULT_INTERVAL_DAYS)
    now = improvement._when(at)
    if days <= 0 or now is None:
        return False
    tried = improvement._when(record.get("tried_at"))
    if tried is not None and timedelta(0) <= now - tried < RETRY_AFTER:
        return False
    last = improvement._when(record.get("sent_at"))
    return last is None or now - last >= timedelta(days=days)


def _write_record(repo: Path, record: Mapping) -> None:
    store = Path(repo) / STORE
    store.parent.mkdir(parents=True, exist_ok=True)
    temporary = store.with_suffix(".tmp")
    temporary.write_text(json.dumps(record) + "\n", encoding="utf-8")
    temporary.replace(store)


def send_if_due(repo: Path, database: Path | str, *, at: str, send: Callable[[str], bool]) -> bool:
    """Send the digest when none went out within `thresholds.improvement_interval_days` (0 turns
    it off) and the report has a task. Records the send only when `send` confirmed delivery. An
    undelivered digest is tried again after `RETRY_AFTER`, because a long digest whose later part
    keeps failing would otherwise repeat its first part at every idle check."""
    from maestro import improvement
    from maestro.usage_report import build_usage_report

    record = _record(repo)
    if not Path(database).exists() or not _due(repo, at, record):
        return False
    report = build_usage_report(database)
    if not report["tasks"]:
        return False
    built = build(report, improvement.report(repo), record)
    if not send(render(built)):
        _write_record(repo, {**record, "tried_at": at})
        return False
    _write_record(repo, {"sent_at": at, "totals": _totals(built)})
    return True
