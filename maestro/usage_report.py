"""Read-only usage views over recorded workflow attempts.

This module reads durable facts; it never opens a controller or reads live policy.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterator


TOKEN_FIELDS = ("fresh_input", "cache_read", "cache_write", "output")
ATTEMPT_COLUMNS = (
    "backend_id", "model_id", "effort", "binding_json", "usage_start_json",
    "usage_settle_json", "wall_time_sec", "reset_crossing", "failure_kind",
    "failure_class",
)


@contextmanager
def read_snapshot(database: str | Path) -> Iterator[sqlite3.Connection]:
    """Open one read transaction without schema setup or controller ownership."""
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        # BEGIN alone is deferred; establish the snapshot before yielding it.
        conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def _document(raw, issues: set[str], field: str) -> dict:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        issues.add(f"invalid_{field}")
        return {}
    return value


def _number(value, issues: set[str], field: str, *, integer=False, positive=False):
    if value is None:
        return None
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0
                 and (not integer or isinstance(value, int))
                 and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        issues.add(f"invalid_{field}")
        return None
    return value


def _metric(values: list) -> dict:
    known = [value for value in values if value is not None]
    total = _number(sum(known), set(), "aggregate") if known else None
    return {"known_sum": total,
            "measured_attempts": len(known), "total_attempts": len(values),
            "complete": total is not None and len(known) == len(values)}


def _role(node: dict) -> str | None:
    if node.get("kind") == "agent":
        return _text(node.get("agent_definition_ref"))
    if node.get("kind") == "function" and node.get("handler_ref") == "maestro.handlers.verification":
        return "gate"
    if _text(node.get("handler_ref")):
        return node["handler_ref"]
    return None


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _node(dag: dict, node_id: str, issues: set[str]) -> dict:
    nodes = dag.get("nodes")
    if not isinstance(nodes, list):
        issues.add("invalid_workflow_nodes")
        return {}
    matching = []
    for node in nodes:
        if not isinstance(node, dict):
            issues.add("invalid_workflow_node")
        elif node.get("node_id") == node_id:
            matching.append(node)
    if len(matching) != 1:
        issues.add("missing_or_duplicate_workflow_node")
        return {}
    if _role(matching[0]) is None:
        issues.add("unclassified_role")
    return matching[0]


def _summarize(attempts: list[dict]) -> dict:
    counts = Counter(implementer_cycles=0, review_rounds=0, gate_attempts=0,
                     gate_failures=0, gate_holds=0, gate_unclassified_failures=0,
                     unclassified_attempts=0)
    for attempt in attempts:
        role = attempt["role"]
        if role == "implementer":
            counts["implementer_cycles"] += 1
        elif role == "reviewer":
            counts["review_rounds"] += 1
        elif role == "gate":
            counts["gate_attempts"] += 1
            if attempt.get("status") == "failed":
                if attempt.get("failure_class") == "bad_output" and attempt.get("failure_kind") == "verification_failed":
                    counts["gate_failures"] += 1
                elif attempt.get("failure_class") in {"blocked", "quota", "rate_limit", "endpoint"}:
                    counts["gate_holds"] += 1
                else:
                    counts["gate_unclassified_failures"] += 1
        elif role is None:
            counts["unclassified_attempts"] += 1
    measures = {field: _metric([a["metrics"].get(field) for a in attempts])
                for field in (*TOKEN_FIELDS, "tokens", "wall_time_sec", "cost_units")}
    return {"attempts": len(attempts), "counts": dict(counts),
            "outcomes": dict(sorted(Counter(a.get("status") for a in attempts).items())),
            "measurements": measures}


def _timestamp(raw) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _quota_observations(rows: list[dict]) -> dict[str, list[dict]]:
    """Keep account evidence separate; the full input includes other tasks."""
    result = defaultdict(list)
    observations = []
    for row in rows:
        issues: set[str] = set()
        binding = _document(row.get("binding_json"), issues, "binding_json")
        start = _document(row.get("usage_start_json"), issues, "usage_start_json")
        end = _document(row.get("usage_settle_json"), issues, "usage_settle_json")
        for sample in (start, end):
            for field in ("source", "updated_at", "observed_at", "read_at", "pool_id", "error"):
                if sample.get(field) is not None and not isinstance(sample[field], str):
                    sample[field] = None
                    issues.add("invalid_sample_metadata")
            sample["staleness_bound"] = _number(sample.get("staleness_bound"), issues, "staleness_bound", positive=True)
        pool = _text(binding.get("usage_pool_id"))
        sw, ew = _window_sets(start, pool), _window_sets(end, pool)
        for scope, pool_id, window in sorted(set(sw) | set(ew), key=lambda k: (
                {"account": 0, "pool": 1}.get(k[0], 2), *tuple(str(v or "") for v in k))):
            s = sw.get((scope, pool_id, window), {})
            e = ew.get((scope, pool_id, window), {})
            s_used = _number(s.get("used_pct"), issues, "used_pct")
            e_used = _number(e.get("used_pct"), issues, "used_pct")
            s_reset = _number(s.get("resets_at"), issues, "resets_at")
            e_reset = _number(e.get("resets_at"), issues, "resets_at")
            begin = _timestamp(start.get("observed_at") or start.get("updated_at"))
            finish = _timestamp(end.get("observed_at") or end.get("updated_at"))
            reason = None
            if not s or not e:
                reason = "missing_sample"
            elif not window.isdecimal() or int(window) <= 0 or s_used is None or e_used is None or s_used > 100 or e_used > 100:
                reason = "invalid_window"
            elif "invalid_sample_metadata" in issues:
                reason = "invalid_sample_metadata"
            elif row.get("reset_crossing") == 1 or (s_reset is not None and e_reset is not None and abs(s_reset - e_reset) > 600):
                reason = "reset_crossing"
            elif s_reset is None or e_reset is None:
                reason = "missing_reset"
            elif e_used < s_used:
                reason = "counter_decreased"
            elif start.get("source") != end.get("source"):
                reason = "source_changed"
            elif begin is None or finish is None or finish <= begin:
                reason = "invalid_observation_interval"
            elif not pool_id:
                reason = "missing_pool"
            elif not start.get("source"):
                reason = "missing_source"
            elif start.get("error") or end.get("error"):
                reason = "sample_error"
            observation = {
                "attempt_id": row["attempt_id"], "run_id": row["run_id"],
                "scope": scope, "pool_id": pool_id,
                "window_minutes": window,
                "start": {"used_pct": s_used, "resets_at": s_reset,
                          "source": start.get("source"), "updated_at": start.get("updated_at"),
                          "observed_at": start.get("observed_at"), "staleness_bound": start.get("staleness_bound"),
                          "pool_id": start.get("pool_id"),
                          "read_at": start.get("read_at"), "error": start.get("error")},
                "settle": {"used_pct": e_used, "resets_at": e_reset,
                           "source": end.get("source"), "updated_at": end.get("updated_at"),
                           "observed_at": end.get("observed_at"), "staleness_bound": end.get("staleness_bound"),
                           "pool_id": end.get("pool_id"),
                           "read_at": end.get("read_at"), "error": end.get("error")},
                "observed_change_pct": e_used - s_used if reason is None else None,
                "unavailable_reason": reason, "attributable_to_attempt": False,
                "overlaps_other_attempts": None,
                "uncertainty": ["account_observation_not_task_consumption", "freshness_unverified"],
            }
            observation["uncertainty"].extend(sorted(issues))
            observations.append((observation, begin, finish))
            result[row["attempt_id"]].append(observation)
    for observation, begin, finish in observations:
        if begin is None or finish is None or finish <= begin or not observation["pool_id"]:
            continue
        peers = [(other, b, f) for other, b, f in observations
                 if other["attempt_id"] != observation["attempt_id"]
                 and other["scope"] == observation["scope"]
                 and other["pool_id"] == observation["pool_id"]
                 and other["window_minutes"] == observation["window_minutes"]]
        overlap = any(b is not None and f is not None and b < f and begin < f and b < finish for _, b, f in peers)
        unknown = any(b is None or f is None or f <= b for _, b, f in peers)
        observation["overlaps_other_attempts"] = True if overlap else (None if unknown else False)
        if overlap:
            observation["uncertainty"].append("overlapping_account_observations")
    return result


def _window_sets(sample: dict, pool: str | None) -> dict:
    result = {}
    def add(scope, pool_id, windows):
        if not isinstance(windows, dict):
            return
        for window, value in windows.items():
            result[(scope, pool_id, str(window))] = value if isinstance(value, dict) else {}
    add("account", pool, sample.get("windows"))
    for key, scope in (("pools", "pool"), ("scoped_windows", None)):
        groups = sample.get(key)
        if isinstance(groups, dict):
            for name, windows in groups.items():
                add(scope or name, name if scope else pool, windows)
    return result


def _routes(attempts: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for a in attempts:
        grouped[(a['role'], a.get('backend_id'), a.get('model_id'), a.get('effort'), a['pool_id'])].append(a)
    return [{'role': key[0], 'harness': key[1], 'model': key[2], 'effort': key[3],
             'pool_id': key[4], **_summarize(group)}
            for key, group in sorted(grouped.items(), key=lambda item: tuple(str(v or '') for v in item[0]))]


def _pool_summary(attempts: list[dict]) -> dict:
    result = _summarize(attempts)
    if len({a['pool_id'] for a in attempts if a['pool_id']}) > 1:
        result['measurements']['cost_units'] = _metric([None] * len(attempts))
    return result


def _w2_facts(events: list[dict], runs: set[str], attempts: list[dict], quota, issues: set[str]) -> dict:
    from maestro.workflows import telemetry
    from maestro.workflows.telemetry_capture import EXTRA_STEP_EVENT, REVIEW_EVENT

    latest = {}
    for event in sorted(events, key=lambda e: e['sequence']):
        if event.get('run_id') not in runs or event.get('type') not in (EXTRA_STEP_EVENT, REVIEW_EVENT):
            continue
        body = _document(event.get('payload'), issues, 'w2_event')
        aid = _text(event.get('attempt_id'))
        prefix = 'review' if event['type'] == REVIEW_EVENT else 'attempt'
        ledger_id = f'{prefix}:{aid}' if aid else _text(body.get('ledger_id'))
        if not ledger_id:
            issues.add('invalid_w2_ledger_id')
            continue
        latest[(event['type'], event['run_id'], ledger_id)] = (event, body)
    by_id = {a['attempt_id']: a for a in attempts}
    ledger, reviews = [], []
    for (kind, run_id, ledger_id), (event, body) in sorted(latest.items()):
        aid = _text(event.get('attempt_id'))
        attempt = by_id.get(aid)
        flags = body.get('flags')
        flags = [f for f in flags if isinstance(f, str)] if isinstance(flags, list) else []
        if aid and (attempt is None or attempt['run_id'] != run_id):
            flags.append('attempt_unavailable')
            attempt = None
        common = {'ledger_id': ledger_id, 'run_id': run_id, 'attempt_id': aid,
                  'node_id': attempt['node_id'] if attempt else _text(body.get('node_id')),
                  'recorded_at': _text(event.get('recorded_at'))}
        if kind == REVIEW_EVENT:
            score = telemetry.review_complexity(body)
            version = body.get('rubric_version')
            valid_version = type(version) is int and version > 0
            score['rubric_version'] = version if valid_version else None
            if not valid_version:
                score['flags'].append('rubric_version_missing' if version is None else 'rubric_version_invalid')
            raw = body.get('diff_stats')
            raw = raw if isinstance(raw, dict) else {}
            diff = {key: _number(raw.get(key), issues, f'diff_{key}', integer=True)
                    for key in ('files_changed', 'insertions', 'deletions', 'binary_files')}
            diff.update(base_sha=_text(raw.get('base_sha')), head_sha=_text(raw.get('head_sha')))
            raw_flags = raw.get('flags')
            diff['flags'] = sorted({f for f in raw_flags if isinstance(f, str)}) if isinstance(raw_flags, list) else []
            reviews.append({**common, **score, 'diff_stats': diff,
                            'flags': sorted(set(flags + score['flags']))})
            continue
        context = body.get('context')
        context = context if isinstance(context, dict) else {}
        # Rework is spent by this attempt, but the cause may belong to another role.
        cause_role = (context.get('role') if body.get('trigger_attempt_id') else
                      attempt['role'] if attempt else context.get('role'))
        fact = telemetry.extra_step(body.get('observed_cause', body.get('extra_step_cause')),
                                    role=cause_role,
                                    rebriefed_role=context.get('rebriefed_role'),
                                    gate_admitted=context.get('gate_admitted'))
        captured_flags = sorted(set(flags))
        observed = body.get('observed_causes')
        observed = [c if isinstance(c, str) else None for c in observed] if isinstance(observed, list) else []
        raw_contexts = body.get('cause_observations')
        contexts, invalid_context = [], False
        if raw_contexts is not None and not isinstance(raw_contexts, list):
            invalid_context = True
        for raw_context in raw_contexts if isinstance(raw_contexts, list) else []:
            if not isinstance(raw_context, dict):
                contexts.append({'cause': None})
                invalid_context = True
                continue
            clean = {}
            for key in ('cause', 'role', 'rebriefed_role', 'gate_admitted'):
                if key not in raw_context:
                    continue
                value = raw_context[key]
                valid = value is None or (type(value) is bool if key == 'gate_admitted'
                                         else isinstance(value, str))
                clean[key] = value if valid else None
                invalid_context |= not valid
            contexts.append(clean)
        if observed or contexts:
            from maestro.workflows.telemetry_observation import cause_observation
            allocation = cause_observation(
                body.get('observed_cause', body.get('extra_step_cause')),
                causes=() if contexts else observed, cause_observations=contexts,
                role=cause_role,
                rebriefed_role=context.get('rebriefed_role'),
                gate_admitted=context.get('gate_admitted'))
            fact['fault'] = allocation['fault']
            fact['flags'] = allocation['flags']
            # Captured allocation is provenance; current allocation uses every cause.
            flags = [f for f in flags if f not in {
                'multiple_faults', 'cause_missing', 'cause_unknown', 'fault_context_missing'}]
        elif 'multiple_faults' in flags:
            fact['fault'] = 'unclassified'
        if invalid_context:
            fact['fault'] = 'unclassified'
            fact['flags'].append('cause_context_invalid')
        ledger.append({**common, **fact, 'role': attempt['role'] if attempt else _text(body.get('role')),
                       'trigger_attempt_id': _text(body.get('trigger_attempt_id')),
                       'source_event_id': _text(body.get('source_event_id')),
                       'observed_causes': observed, 'captured_flags': captured_flags,
                       'cause_observations': contexts,
                       'captured_fault': _text(body.get('fault')),
                       'mapping_version': _number(body.get('mapping_version'), issues, 'mapping_version', integer=True),
                       'flags': sorted(set(flags + fact['flags']))})
    fault_rows = []
    for fault in sorted({row['fault'] for row in ledger}):
        records = [row for row in ledger if row['fault'] == fault]
        ids = {row['attempt_id'] for row in records if 'attempt_unavailable' not in row['flags']}
        group = [a for a in attempts if a['attempt_id'] in ids]
        fault_rows.append({'fault': fault, 'ledger_rows': len(records), **_pool_summary(group),
                           'routes': _routes(group), 'quota_consumption': None,
                           'quota_observations': [o for a in group for o in quota[a['attempt_id']]]})
    if not ledger:
        issues.add('blame_history_unavailable')
    if not reviews:
        issues.add('complexity_history_unavailable')
    return {'blame_ledger': ledger, 'faults': fault_rows, 'complexity_reviews': reviews}


def _goal_impacts(events: list[dict], runs: set[str], issues: set[str]) -> list[dict]:
    """GDT-12 (R58, R68): each goal-impact observation of the task's runs, one per ledger
    id. The latest row gives the current attribution (W12's amendments); the first row is
    the captured observation and its provenance."""
    from maestro.workflows.telemetry_capture import GOAL_IMPACT_EVENT

    grouped: dict[tuple, list[dict]] = {}
    for event in sorted(events, key=lambda e: e['sequence']):
        if event.get('type') != GOAL_IMPACT_EVENT or event.get('run_id') not in runs:
            continue
        body = _document(event.get('payload'), issues, 'goal_impact_event')
        ledger_id = _text(body.get('ledger_id'))
        if not ledger_id:
            issues.add('invalid_goal_impact_ledger_id')
            continue
        grouped.setdefault((event['run_id'], ledger_id), []).append(body)
    out = []
    for (run_id, ledger_id), rows in sorted(grouped.items()):
        first, last = rows[0], rows[-1]
        evidence = last.get('attribution_evidence')
        provenance = first.get('provenance')
        out.append({'ledger_id': ledger_id, 'run_id': run_id, 'goal': _text(last.get('goal')),
                    'impact': _text(last.get('impact')),
                    'attribution': _text(last.get('attribution')) or 'unattributed',
                    'cause': _text(last.get('attribution_cause')),
                    'rule': _text(last.get('attribution_rule')),
                    'evidence': [e for e in evidence if isinstance(e, str)]
                    if isinstance(evidence, list) else [],
                    'captured_attribution': _text(first.get('attribution')) or 'unattributed',
                    'provenance': provenance if isinstance(provenance, dict) else {},
                    'amendments': len(rows) - 1})
    return out


def build_usage_report(database: str | Path, *, task_id: str | None = None) -> dict:
    """Roll up all durable attempts across runs, grouped by recorded role and route."""
    with read_snapshot(database) as conn:
        runs = _rows(conn, "task_runs")
        revisions = {(r["workflow_id"], r["revision"]): r for r in _rows(conn, "workflow_revisions")}
        rows = _rows(conn, "attempts")
        states = _rows(conn, "node_states")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
        events = _rows(conn, 'events') if conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'events'").fetchone() else []
    by_run = {r["run_id"]: r for r in runs}
    quota = _quota_observations(rows)
    tasks = []
    for tid in sorted({r["task_id"] for r in runs}):
        if task_id is not None and tid != task_id:
            continue
        task_runs = [r for r in runs if r["task_id"] == tid]
        issues: set[str] = set()
        attempts = []
        for row in rows:
            run = by_run.get(row["run_id"])
            if not run or run["task_id"] != tid:
                continue
            attempt_issues: set[str] = set()
            revision = revisions.get((run["workflow_id"], run["workflow_revision"]), {})
            dag = _document(revision.get("dag_json"), attempt_issues, "dag_json")
            node = _node(dag, row["node_id"], attempt_issues)
            binding = _document(row.get("binding_json"), attempt_issues, "binding_json")
            settle = _document(row.get("usage_settle_json"), attempt_issues, "usage_settle_json")
            tokens = settle.get("token_split")
            if tokens is not None and not isinstance(tokens, dict):
                attempt_issues.add("invalid_token_split")
            tokens = tokens if isinstance(tokens, dict) else {}
            metrics = {f: _number(tokens.get(f), attempt_issues, f, integer=True) for f in TOKEN_FIELDS}
            metrics["tokens"] = sum(metrics.values()) if all(v is not None for v in metrics.values()) else None
            metrics["tokens"] = _number(metrics["tokens"], attempt_issues, "tokens", integer=True)
            metrics["wall_time_sec"] = _number(row.get("wall_time_sec"), attempt_issues, "wall_time_sec")
            cost = _number(binding.get("relative_cost"), attempt_issues, "relative_cost", positive=True)
            if cost is None:
                attempt_issues.add("missing_relative_cost")
            pool = _text(binding.get("usage_pool_id"))
            route_known = all(_text(row.get(f)) for f in ("backend_id", "model_id", "effort"))
            if not pool:
                attempt_issues.add("missing_pool")
            if not route_known:
                attempt_issues.add("missing_route")
            metrics["cost_units"] = cost * metrics["tokens"] if cost is not None and metrics["tokens"] is not None and pool and route_known else None
            metrics["cost_units"] = _number(metrics["cost_units"], attempt_issues, "cost_units")
            attempts.append({**row, "backend_id": _text(row.get("backend_id")),
                             "model_id": _text(row.get("model_id")), "effort": _text(row.get("effort")),
                             "role": _role(node), "pool_id": pool,
                             "metrics": metrics, "issues": sorted(attempt_issues)})
            issues.update(attempt_issues)
        task = {"task_id": tid, "run_statuses": dict(Counter(r["status"] for r in task_runs)),
                **_summarize(attempts)}
        for name, measure in task["measurements"].items():
            if measure["known_sum"] is None and measure["measured_attempts"]:
                issues.add(f"invalid_{name}_aggregate")
        if len({a["pool_id"] for a in attempts if a["pool_id"]}) > 1:
            issues.add("mixed_cost_pools")
            task["measurements"]["cost_units"] = _metric([None] * len(attempts))
        task["quota_consumption"] = None
        task["attempt_details"] = [
            {"attempt_id": a["attempt_id"], "run_id": a["run_id"], "node_id": a["node_id"],
             "status": a["status"], "role": a["role"], "harness": a["backend_id"],
             "model": a["model_id"], "effort": a["effort"], "pool_id": a["pool_id"],
             "measurements": a["metrics"],
             "missing_measurements": sorted(k for k, v in a["metrics"].items() if v is None),
             "issues": a["issues"]}
            for a in sorted(attempts, key=lambda a: a["attempt_id"])]
        task["node_states"] = sorted([
            {key: state.get(key) for key in ("run_id", "node_id", "phase", "last_attempt_id")}
            for state in states if state["run_id"] in {r["run_id"] for r in task_runs}
        ], key=lambda s: (s["run_id"], s["node_id"]))
        task["node_phases"] = dict(sorted(Counter(s["phase"] for s in task["node_states"]).items()))
        task["quota_observations"] = [o for a in sorted(attempts, key=lambda a: a["attempt_id"])
                                      for o in quota[a["attempt_id"]]]
        task['routes'] = _routes(attempts)
        task.update(_w2_facts(events, {r['run_id'] for r in task_runs}, attempts, quota, issues))
        task['goal_impacts'] = _goal_impacts(events, {r['run_id'] for r in task_runs}, issues)
        task["issues"] = sorted(issues)
        tasks.append(task)
    return {"schema_version": 1, "tasks": tasks,
            "missing_attempt_columns": sorted(set(ATTEMPT_COLUMNS) - columns)}


def render_usage_report(report: dict) -> str:
    """Deterministic JSON rendering with unknowns and coverage preserved."""
    return json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
