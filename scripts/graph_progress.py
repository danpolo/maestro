#!/usr/bin/env python3
"""Plain-English progress of a project's graph task, one line per attempt.

    python3 scripts/graph_progress.py [REPO] [--task TASK_ID] [--watch SECONDS]

REPO defaults to $MAESTRO_REPO, then the current directory. Read-only on the control DB.
Shows every run of the newest task (or --task): each implementer cycle, gate and review with
its model, effort and duration, and what the reviewer found.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def _dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d} min"


def _result(orch: Path, conn, ref):
    if not ref:
        return None
    row = conn.execute("select storage_rel_path from artifacts where artifact_id=?", (ref,)).fetchone()
    if not row:
        return None
    try:
        return json.loads((orch / "artifacts" / row[0]).read_text())
    except (OSError, ValueError):
        return None


def _describe(node, status, res, failure_kind):
    if status in ("claimed", "running"):
        return "running now"
    if failure_kind and failure_kind not in ("quality", "verification_failed"):
        return f"{status} ({failure_kind.replace('_', ' ')})"
    if res is None:
        return status
    outputs = res.get("outputs") or {}
    review = outputs.get("review")
    if isinstance(review, dict):
        blocking, follow = len(review.get("findings") or []), len(review.get("follow_ups") or [])
        verdict = review.get("verdict", status)
        return f"{verdict}: {blocking} blocking finding(s), {follow} follow-up(s)"
    if node == "gate":
        return "passed" if status == "succeeded" else f"FAILED: {(res.get('detail') or '')[:120]}"
    if outputs.get("plan_repair"):
        verdict = outputs["plan_repair"].get("verdict", "?") if isinstance(outputs["plan_repair"], dict) else "raw"
        return f"{status}, plan_repair verdict={verdict}"
    return status


def report(repo: Path, task: str | None) -> str:
    orch = repo / ".orchestrator"
    conn = sqlite3.connect(f"file:{orch / 'control.sqlite3'}?mode=ro", uri=True)
    if task is not None:
        tasks = [task]
    else:
        # Every task with a live run (several run in parallel), else the newest one.
        tasks = [r[0] for r in conn.execute(
            "select task_id from task_runs where status='running' order by started_at")]
        if not tasks:
            row = conn.execute("select task_id from task_runs order by started_at desc limit 1").fetchone()
            if not row:
                return "no graph run yet"
            tasks = [row[0]]
    now = datetime.now(timezone.utc)
    head = [f"{repo.name}  (now {now:%H:%M:%S}Z)"]
    if (orch / "HALT").exists():
        head.append("! HALT is set")
    return "\n\n".join(["\n".join(head)] + [_task_report(orch, conn, t, now) for t in tasks])


def _task_report(orch: Path, conn, task: str, now) -> str:
    lines = [f"Task {task}"]
    runs = conn.execute(
        "select run_id, workflow_revision, status, started_at, finished_at from task_runs"
        " where task_id=? order by started_at", (task,)).fetchall()
    for run_id, rev, rstatus, rstart, rend in runs:
        span = (_ts(rend) or now) - _ts(rstart)
        lines.append(f"Run {run_id} (graph revision {rev}): {rstatus.upper()}, {_dur(span.total_seconds())}"
                     + ("" if rend else " so far"))
        cycle = 0
        for node, status, model, effort, backend, start, beat, wall, ref, fkind in conn.execute(
                "select node_id, status, model_id, effort, backend_id, started_at, heartbeat_at,"
                " wall_time_sec, outcome_ref, failure_kind from attempts where run_id=?"
                " order by coalesce(started_at, heartbeat_at)", (run_id,)):
            if node == "implementer":
                cycle += 1
            if status in ("claimed", "running") and start:
                wall = (now - _ts(start)).total_seconds()
            who = f"{model} ({backend}, effort {effort})" if model else "controller"
            label = f"cycle {cycle}" if cycle else "prep"
            lines.append(f"  [{label}] {node:<13} {who:<40} {_dur(wall):>9}  "
                         f"{_describe(node, status, _result(orch, conn, ref), fkind)}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", nargs="?", default=os.environ.get("MAESTRO_REPO", "."))
    ap.add_argument("--task")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="refresh every N seconds")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    while True:
        text = report(repo, args.task)
        if not args.watch:
            print(text)
            return 0
        sys.stdout.write("\033[2J\033[H" + text + "\n")
        sys.stdout.flush()
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
