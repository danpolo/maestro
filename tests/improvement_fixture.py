"""GDT-12's shared ledger and goal-project helpers (W12): not a test module.

`ledger(tmp)` opens a real `ControlStore` controller database, so the schema (with
`task_runs.finished_at`) is the one the loop writes. `add_task` writes one task's run, its
workflow revision and its attempts, and records its W2 observations through
`telemetry_capture` (the only writer the loop uses). `goals_project` is Recipe Box (the
GDT-14 demo, `tests/goals_demo`) with goals-store rows, an optional acceptance baseline,
registry and committed files. Every slice of GDT-12 (S1-S4) builds on these.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from maestro import goal_replan, goal_store
from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.docs import goals as doc_goals
from maestro.workflows import telemetry_capture

from tests import goals_demo

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def iso(hours: float) -> str:
    """`T0` plus `hours`, in the control store's stamp shape."""
    return (T0 + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger(tmp: Path) -> tuple[Path, ControlStore]:
    """`(database path, controller store)` under `tmp/.orchestrator/`. The caller closes it."""
    db = Path(tmp) / ".orchestrator" / "control.sqlite3"
    store = ControlStore.open_controller(db)
    return db, store


def artifacts(tmp: Path, store: ControlStore) -> ArtifactStore:
    return ArtifactStore(Path(tmp) / ".orchestrator" / "artifacts", store)


def work(tokens=100, cost=1, pool="codex-subscription", reviews=1, gates=1, wall=60,
         backend="codex", model="sol", effort="high") -> list[dict]:
    """An implementer attempt, `reviews` reviewer attempts and `gates` gate attempts."""
    route = {"backend": backend, "model": model, "effort": effort}
    out = [{"node": "implementer", "role": "implementer", "tokens": tokens, "cost": cost,
            "pool": pool, "wall": wall, **route}]
    out += [{"node": "reviewer", "role": "reviewer", "tokens": tokens, "cost": cost,
             "pool": pool, "wall": wall, **route} for _ in range(reviews)]
    out += [{"node": "gate", "role": "gate", "tokens": None, "cost": None, "pool": pool,
             "wall": 5} for _ in range(gates)]
    return out


def gate_outputs(states: Mapping[str, str], snapshot: str = "abc123") -> dict:
    """An accepted gate attempt's outputs (what `goal_state.collect_observed` reads)."""
    return {"decision": "accepted", "snapshot": snapshot, "evidence": [],
            "acceptance": dict(states)}


def add_task(store: ControlStore, task: str, *, finished_at: Optional[str],
             started_at: Optional[str] = None, status: str = "succeeded",
             attempts: Iterable[Mapping[str, Any]] = (), causes: Iterable = (),
             impacts: Iterable = (), run: Optional[str] = None,
             artifact_store: Optional[ArtifactStore] = None) -> str:
    """One task with one run; returns the run id (`run-<task>` by default).

    `attempts`: dicts with `node`, `role` and optionally `tokens`, `cost`, `pool`, `wall`,
    `status`, `backend`, `model`, `effort`, `failure_kind`, `failure_class`, and `outputs`
    (stored as the attempt's outcome artifact through `artifact_store`). Attempt `i` is
    `<run>-a<i>`.
    `causes`: `(cause, role)` (an occurrence row) or `(cause, role, i)` (attempt `i`'s row),
    recorded through `telemetry_capture.record_extra_step`.
    `impacts`: `(goal, impact, i)`, recorded through `record_goal_impact` on attempt `i`.
    """
    attempts = [dict(a) for a in attempts]
    run = run or f"run-{task}"
    wf = f"wf-{task}-{run}"
    nodes = []
    for a in attempts:
        if a["role"] == "gate":
            nodes.append({"node_id": a["node"], "kind": "function",
                          "handler_ref": "maestro.handlers.verification"})
        else:
            nodes.append({"node_id": a["node"], "kind": "agent",
                          "agent_definition_ref": a["role"]})
    unique = list({n["node_id"]: n for n in nodes}.values())
    start = started_at or finished_at or iso(0)
    refs = []
    for i, a in enumerate(attempts):
        ref = None
        if a.get("outputs") is not None and artifact_store is not None:
            body = {"run_id": run, "node_id": a["node"], "attempt_id": f"{run}-a{i}",
                    "status": a.get("status", "succeeded"), "outputs": a["outputs"]}
            ref = artifact_store.put_bytes(json.dumps(body).encode("utf-8"),
                                           type="node_result").artifact_id
        refs.append(ref)
    with store.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO tasks VALUES (?, 1, 'accepted', 0, ?, ?)",
                     (task, start, start))
        conn.execute(
            "INSERT INTO workflow_revisions (workflow_id, revision, task_id, task_spec_revision,"
            " compiler_version, policy_hash, code_base_sha, execution_objective, dag_json,"
            " created_at) VALUES (?, 1, ?, 1, 'fixture', '', '', 'paid_efficiency', ?, ?)",
            (wf, task, json.dumps({"nodes": unique}), start))
        conn.execute("INSERT INTO task_runs VALUES (?, ?, ?, 1, ?, ?, ?)",
                     (run, task, wf, status, start, finished_at))
        for i, a in enumerate(attempts):
            tokens = a.get("tokens")
            settle = ({"token_split": {"fresh_input": tokens, "cache_read": 0,
                                       "cache_write": 0, "output": 0}}
                      if tokens is not None else None)
            gate = a["role"] == "gate"
            conn.execute(
                "INSERT INTO attempts (attempt_id, run_id, node_id, status, outcome_ref,"
                " backend_id, model_id, effort, binding_json, usage_settle_json,"
                " wall_time_sec, failure_kind, failure_class)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{run}-a{i}", run, a["node"], a.get("status", "succeeded"), refs[i],
                 a.get("backend", None if gate else "codex"),
                 a.get("model", None if gate else "sol"),
                 a.get("effort", None if gate else "high"),
                 json.dumps({"relative_cost": a.get("cost", 1),
                             "usage_pool_id": a.get("pool", "codex-subscription")}),
                 json.dumps(settle) if settle else None, a.get("wall", 60),
                 a.get("failure_kind"), a.get("failure_class")))
    for n, item in enumerate(causes):
        cause, role = item[0], item[1]
        index = item[2] if len(item) > 2 else None
        kwargs = ({"attempt_id": f"{run}-a{index}", "node_id": attempts[index]["node"]}
                  if index is not None else {"occurrence": f"{run}:extra:{n}", "node_id": role})
        assert telemetry_capture.record_extra_step(store, run_id=run, cause=cause, role=role,
                                                   at=finished_at, **kwargs)
    for goal, impact, index in impacts:
        assert telemetry_capture.record_goal_impact(
            store, run_id=run, node_id=attempts[index]["node"], attempt_id=f"{run}-a{index}",
            goal=goal, impact=impact,
            evidence=[{"kind": "test", "id": "tests/acceptance/test_x.py::t",
                       "result": "failed"}], at=finished_at)
    return run


# --- goal projects ---------------------------------------------------------------------------


def episode(doc: doc_goals.Goals, task: str, goal: str, *,
            impact: str = "no_progress_unexpected", contribution: str = "direct",
            served: bool = True, run: Optional[str] = None, at: Optional[str] = None,
            terms: Optional[str] = None, criteria: Iterable[str] = ()) -> dict:
    """A goals-store episode as `goal_replan.episodes` writes it, under `goal`'s current
    terms unless `terms` is given."""
    return {"id": f"{task}:{goal}", "task": task, "goal": goal, "run_id": run or f"run-{task}",
            "workflow_id": f"wf-{task}", "at": at or iso(1), "revision": doc.revision,
            "terms": goal_replan.terms(doc).get(goal) if terms is None else terms,
            "contribution": contribution, "served": served, "impact": impact,
            "claimed": impact,
            "evidence": [{"kind": "test", "id": f"tests/acceptance/{c}.py::t",
                          "result": "failed", "criterion": c} for c in criteria]}


def baseline(repo: Path, states: Mapping[str, str], *, ran_at: str = "2026-09-30T00:00:00Z",
             revision: Optional[int] = None) -> None:
    """A stored acceptance baseline (`acceptance.BASELINE`) with `states` by criterion."""
    if revision is None:
        revision = doc_goals.load(repo).revision
    path = Path(repo) / ".orchestrator" / "acceptance_baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "revision": revision, "format": 2, "ran_at": ran_at, "commit": "",
        "results": [{"id": cid, "state": state, "reason": "", "revision": revision}
                    for cid, state in states.items()]}), encoding="utf-8")


def registry(repo: Path, ids: Iterable[str]) -> None:
    path = Path(repo) / ".orchestrator" / "completed_tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": i, "title": i} for i in ids]), encoding="utf-8")


def commit_file(repo: Path, rel: str, text: str) -> None:
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"docs: {rel}"], cwd=str(repo), check=True,
                   capture_output=True)


def goals_project(tmp: Path, episodes: Iterable[Mapping] = (), replans: Iterable[Mapping] = (),
                  criteria: Iterable[Mapping] = ()) -> Path:
    """Recipe Box (`tests/goals_demo`, contract revision 2) as a git repo at `tmp/project`,
    with these goals-store rows."""
    repo = goals_demo.materialize(Path(tmp) / "project")
    for section, rows in (("episodes", episodes), ("replans", replans),
                          ("criteria", criteria)):
        rows = [dict(r) for r in rows]
        if rows:
            goal_store.update(repo, section, lambda stored, rows=rows: stored + rows)
    return repo


def add_rows(repo: Path, section: str, rows: Iterable[Mapping]) -> None:
    rows = [dict(r) for r in rows]
    goal_store.update(repo, section, lambda stored: stored + rows)


def diagnosis_replan(goal: str, episodes: Iterable[str], task: str, *,
                     at: str = "2026-10-01T02:00:00Z") -> dict:
    """A routine replan (GDT-8) on `goal` whose signal names `episodes` and which added the
    diagnosis task `task`."""
    return {"id": "RP-" + task.rsplit("-", 1)[-1], "goal": goal, "status": "applied",
            "at": at, "settled_at": at,
            "trigger": {"goal": goal, "kind": "repeated_misses", "facts": list(episodes),
                        "episodes": list(episodes)},
            "applied": [task],
            "alternatives": [{"label": "Add a diagnosis task",
                              "changes": {"add_tasks": [{"id": task}], "deps_rewire": [],
                                          "contract_text": None}}],
            "scope": {"held_tasks": []}}
