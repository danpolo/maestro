"""GDT-12 S2 (W12, R59, R65's measured part, C33 unit): the improvement metrics.

Every metric is lower-is-better and none counts GREEN criteria or rewards blame. The
cost, token and wall sums equal `maestro report usage` over the same tasks (the W10 seam).
Effects compare paired windows of accepted tasks, and a cost saving with a quality drop is
`worse`, never `improved`.

The ledger is a real `ControlStore` database built by the shared GDT-12 helper
(`tests/improvement_fixture.py`; S2's private copy of it was folded in there in S4).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from maestro import goal_store
from maestro.acceptance import GREEN, RED
from maestro.usage_report import build_usage_report

from tests import improvement_fixture as fx


class _Module:
    """`maestro.improvement_metrics`, imported on use so each test fails on its own."""

    def __getattr__(self, name):
        assert importlib.util.find_spec("maestro.improvement_metrics"), \
            "the S2 metrics module is missing"
        return getattr(importlib.import_module("maestro.improvement_metrics"), name)


im = _Module()


#: The shared GDT-12 ledger helper: a real controller database, one task run per call,
#: `(cause, role)` extra-step occurrences recorded through W2's writer.
_iso, _ledger, _add_task, _work = fx.iso, fx.ledger, fx.add_task, fx.work


def _goal_rows(repo: Path, section: str, rows: list[dict]) -> None:
    goal_store.update(repo, section, lambda stored: stored + rows)


def _baseline(repo: Path, ran_at: str, states: dict[str, str], revision: int = 1) -> None:
    path = repo / ".orchestrator" / "acceptance_baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "revision": revision, "format": 2, "ran_at": ran_at, "commit": "",
        "results": [{"id": cid, "state": state, "reason": "", "revision": revision}
                    for cid, state in states.items()]}), encoding="utf-8")


def _keep_going(rid, at, settled, held, task="t1", resolution="keep_going"):
    return {"id": rid, "goal": "G1", "task": task, "at": at, "status": "decided",
            "settled_at": settled, "decision": {"resolution": resolution, "at": settled},
            "scope": {"held_tasks": list(held)}}


def _value(measured, key):
    return measured["metrics"][key]["value"]


# --- 1. the W10 seam ---------------------------------------------------------------------------


def test_metrics_equal_the_usage_report_for_the_same_tasks(tmp_path):
    db, store = _ledger(tmp_path)
    _add_task(store, "t1", finished_at=_iso(1), attempts=_work(tokens=100, cost=2, reviews=2),
              causes=[("missed_earlier", "reviewer")])
    _add_task(store, "t2", finished_at=_iso(2),
              attempts=_work(tokens=50, cost=1) + [
                  {"node": "fixer", "role": "implementer", "tokens": 10, "cost": 3,
                   "pool": "claude-subscription"}])  # mixed pools: no cost, as W2
    _add_task(store, "t3", finished_at=_iso(3), attempts=_work(tokens=30, pool="claude-subscription",
                                                               gates=2))
    _add_task(store, "t4", finished_at=_iso(4), attempts=_work(tokens=None))  # unmeasured
    store.close()

    for tasks in (["t1", "t2", "t3", "t4"], ["t1", "t3"]):
        report = [t for t in build_usage_report(db)["tasks"] if t["task_id"] in tasks]
        measured = im.measure(db, tmp_path, tasks)
        m = measured["metrics"]

        def known(field):
            return [t["measurements"][field]["known_sum"] for t in report
                    if t["measurements"][field]["known_sum"] is not None]

        assert m["tokens_per_task"]["sum"] == sum(known("tokens"))
        assert m["tokens_per_task"]["known"] == len(known("tokens"))
        assert m["tokens_per_task"]["total"] == len(tasks)
        assert m["wall_time_per_task"]["sum"] == sum(known("wall_time_sec"))
        pools = {k: v for k, v in m.items() if k.startswith("cost_units_per_task:")}
        assert pools, "cost is measured per pool"
        assert sum(v["sum"] or 0 for v in pools.values()) == sum(known("cost_units"))
        assert m["gate_attempts_per_task"]["value"] == pytest.approx(
            sum(t["counts"]["gate_attempts"] for t in report) / len(tasks))
        assert m["review_rounds_per_task"]["value"] == pytest.approx(
            sum(t["counts"]["review_rounds"] for t in report) / len(tasks))
        assert m["extra_steps_per_task"]["value"] == pytest.approx(
            sum(len(t["blame_ledger"]) for t in report) / len(tasks))
    full = im.measure(db, tmp_path, ["t1", "t2", "t3", "t4"])["metrics"]
    # Pools are never summed together; a mixed-pool task's cost is unknown, never 0.
    assert full["cost_units_per_task:codex-subscription"]["sum"] == 600
    assert full["cost_units_per_task:claude-subscription"]["sum"] == 60
    assert full["extra_steps_per_task:reviewer"]["value"] == pytest.approx(0.25)
    assert full["extra_steps_per_task:reviewer/missed_earlier"]["value"] == pytest.approx(0.25)


# --- 2. the vocabulary -------------------------------------------------------------------------


def test_no_metric_counts_green_criteria_and_every_metric_is_lower_is_better(tmp_path):
    assert set(im.METRICS) == {
        "cost_units_per_task", "tokens_per_task", "wall_time_per_task",
        "gate_attempts_per_task", "review_rounds_per_task", "extra_steps_per_task",
        "false_alarms_per_task", "false_hold_hours", "time_to_outcome_hours",
        "quality_defects_per_task"}
    for name, spec in im.METRICS.items():
        assert spec["better"] == "lower", name
        for word in ("green", "pass", "blame", "met", "served"):
            assert word not in name.split("_"), name
    db, store = _ledger(tmp_path)
    _add_task(store, "t1", finished_at=_iso(2), attempts=_work())
    store.close()
    _baseline(tmp_path, _iso(0), {"AC-1": GREEN, "AC-2": GREEN})
    before = im.measure(db, tmp_path, ["t1"])
    # More GREEN observations of criteria already GREEN change no metric.
    _goal_rows(tmp_path, "criteria", [
        {"id": c, "revision": 1, "state": GREEN, "source": "gate", "at": _iso(1), "task": "t1"}
        for c in ("AC-1", "AC-2")])
    after = im.measure(db, tmp_path, ["t1"])
    assert after["metrics"] == before["metrics"]
    assert all(key.split(":")[0] in im.METRICS for key in after["metrics"])


# --- 3. false alarms and holds -----------------------------------------------------------------


def test_false_alarms_and_false_holds_come_from_keep_going_replans_and_their_held_hours(tmp_path):
    db, store = _ledger(tmp_path)
    _add_task(store, "t1", started_at=_iso(0), finished_at=_iso(10), attempts=_work(),
              causes=[("runaway_false_alarm", "implementer"), ("check_failed", "implementer")])
    _add_task(store, "t2", started_at=_iso(11), finished_at=_iso(20), attempts=_work(),
              causes=[("deny_approved", "implementer"), ("permission_allowed", "implementer")])
    store.close()
    _goal_rows(tmp_path, "replans", [
        _keep_going("RP-1", _iso(1), _iso(3), ["a", "b"]),          # 2 tasks x 2 h
        _keep_going("RP-2", _iso(12), _iso(12.5), ["c"]),           # 1 task x 0.5 h
        _keep_going("RP-3", _iso(5), _iso(6), ["d"], resolution="approve"),  # a real alarm
        _keep_going("RP-4", _iso(30), _iso(31), ["e"]),             # outside the window
    ])
    measured = im.measure(db, tmp_path, ["t1", "t2"], since=_iso(0), until=_iso(20))
    assert _value(measured, "false_alarms_per_task") == pytest.approx((2 + 3) / 2)
    assert _value(measured, "false_hold_hours") == pytest.approx(4.5)
    # Without bounds, the window spans the tasks' own runs: RP-4 is still outside.
    assert _value(im.measure(db, tmp_path, ["t1", "t2"]), "false_hold_hours") == pytest.approx(4.5)
    # A non-goals project has no replan and so no false hold: a measured 0, not unknown.
    plain = tmp_path / "plain"
    plain.mkdir()
    db2, store2 = _ledger(plain)
    _add_task(store2, "t1", finished_at=_iso(1), attempts=_work())
    store2.close()
    assert im.measure(db2, plain, ["t1"])["metrics"]["false_hold_hours"] == {
        "value": 0.0, "known": 0, "total": 0}


# --- 4. time to outcome ------------------------------------------------------------------------


def test_time_to_outcome_is_baseline_red_to_the_earliest_green_observation(tmp_path):
    db, store = _ledger(tmp_path)
    _add_task(store, "t1", started_at=_iso(0), finished_at=_iso(50), attempts=_work())
    store.close()
    _baseline(tmp_path, _iso(0), {"AC-1": RED, "AC-2": RED, "AC-3": RED, "AC-4": GREEN})
    _goal_rows(tmp_path, "criteria", [
        {"id": "AC-1", "revision": 1, "state": GREEN, "source": "gate", "at": _iso(10)},
        {"id": "AC-2", "revision": 1, "state": GREEN, "source": "gate", "at": _iso(40)},
        {"id": "AC-3", "revision": 2, "state": GREEN, "source": "gate", "at": _iso(1)},  # other rev
        {"id": "AC-4", "revision": 1, "state": GREEN, "source": "gate", "at": _iso(2)},  # never RED
    ])
    _goal_rows(tmp_path, "checkpoints", [
        {"id": "CP-1", "revision": 1, "at": _iso(5), "goals": {"G1": {"AC-1": GREEN, "AC-3": RED}}}])
    _goal_rows(tmp_path, "finish", [
        {"id": "FIN-1", "revision": 1, "started_at": _iso(29),
         "results": {"AC-2": {"state": GREEN, "at": _iso(30)}}}])
    whole = im.measure(db, tmp_path, ["t1"], since=_iso(0), until=_iso(50))
    assert whole["metrics"]["time_to_outcome_hours"] == {"value": pytest.approx(17.5),
                                                         "known": 2, "total": 3}
    # AC-1 first turned GREEN at 5 h, before this window: it does not count here.
    late = im.measure(db, tmp_path, ["t1"], since=_iso(6), until=_iso(50))
    assert late["metrics"]["time_to_outcome_hours"]["value"] is None  # one criterion < 2
    assert late["metrics"]["time_to_outcome_hours"]["known"] == 1
    bare = tmp_path / "bare"
    bare.mkdir()
    assert im.measure(db, bare, ["t1"])["metrics"]["time_to_outcome_hours"]["value"] is None


# --- 5. quality --------------------------------------------------------------------------------


def test_quality_counts_escaped_defects_rejections_after_approval_and_regressions(tmp_path):
    db, store = _ledger(tmp_path)
    _add_task(store, "t1", finished_at=_iso(1), attempts=_work(),
              causes=[("escaped_defect", "reviewer"), ("missed_earlier", "reviewer")])
    _add_task(store, "t2", finished_at=_iso(2), attempts=_work(),
              causes=[("accept_rejected_review", "reviewer"), ("accept_rejected_plan", "planner")])
    _add_task(store, "t3", finished_at=_iso(3), attempts=_work())
    store.close()
    _goal_rows(tmp_path, "episodes", [
        {"id": "E1", "task": "t1", "goal": "G1", "impact": "regressed", "at": _iso(1)},
        {"id": "E2", "task": "t2", "goal": "G1", "impact": "advanced", "at": _iso(2)},
        {"id": "E3", "task": "t3", "goal": "G1", "impact": "regressed", "at": _iso(3)},
    ])
    assert _value(im.measure(db, tmp_path, ["t1", "t2"]), "quality_defects_per_task") == \
        pytest.approx((3 + 1) / 2)
    assert _value(im.measure(db, tmp_path, ["t3"]), "quality_defects_per_task") == 1.0


# --- 6, 7. windows -----------------------------------------------------------------------------


def _eight(tmp_path):
    db, store = _ledger(tmp_path)
    for h in range(1, 9):
        _add_task(store, f"t{h}", started_at=_iso(h - 0.5), finished_at=_iso(h), attempts=_work())
    _add_task(store, "t-failed", finished_at=_iso(7.5), status="failed", attempts=_work())
    store.close()
    return db


def test_windows_pair_the_same_number_of_accepted_tasks_before_and_after(tmp_path):
    db = _eight(tmp_path)
    w = im.windows(db, _iso(5.5), min_tasks=3)
    assert w["after"]["tasks"] == ["t6", "t7", "t8"]  # the failed-only task is not accepted
    assert w["before"]["tasks"] == ["t3", "t4", "t5"]
    assert (w["before"]["since"], w["before"]["until"]) == (_iso(2), _iso(5.5))
    assert (w["after"]["since"], w["after"]["until"]) == (_iso(5.5), _iso(8))
    # Fewer tasks before than after: paired by count, the earliest after tasks.
    w = im.windows(db, _iso(2.5), min_tasks=2)
    assert w["before"]["tasks"] == ["t1", "t2"]
    assert w["after"]["tasks"] == ["t3", "t4"]
    assert w["before"]["since"] is None  # all history
    assert im.accepted_after(db, _iso(5.5)) == ["t6", "t7", "t8"]


def test_too_few_tasks_after_is_insufficient_data_never_a_verdict(tmp_path):
    db = _eight(tmp_path)
    assert im.windows(db, _iso(6.5), min_tasks=5) is None
    assert im.windows(db, _iso(1.5), min_tasks=5) is None  # too few before
    assert im.effect(None, None, ["review_rounds_per_task"])["verdict"] == "insufficient_data"
    w = im.windows(db, _iso(4.5), min_tasks=3)
    before = im.measure(db, tmp_path, w["before"]["tasks"], since=w["before"]["since"],
                        until=w["before"]["until"])
    after = im.measure(db, tmp_path, w["after"]["tasks"], since=w["after"]["since"],
                       until=w["after"]["until"])
    # time to outcome is unknown without a baseline: a target without a value decides nothing.
    assert im.effect(before, after, ["time_to_outcome_hours"])["verdict"] == "insufficient_data"
    line = im.line({"id": "IP-0001", "change": "reviewer strength → strong"},
                   {"verdict": "insufficient_data", "tasks": {"after": 2, "needed": 5}})
    assert "IP-0001" in line and "2 of 5 accepted tasks since applied" in line


# --- 8, 9. the effect verdict ------------------------------------------------------------------


def test_an_effect_that_saves_cost_but_lowers_quality_is_worse_never_better(tmp_path):
    db, store = _ledger(tmp_path)
    for h in range(1, 6):
        _add_task(store, f"b{h}", finished_at=_iso(h), attempts=_work(tokens=100, reviews=3),
                  causes=[("missed_earlier", "reviewer"), ("missed_earlier", "reviewer")])
    for h in range(6, 11):  # cheaper, fewer reviewer rounds, but a defect escaped
        _add_task(store, f"a{h}", finished_at=_iso(h), attempts=_work(tokens=50, reviews=1),
                  causes=[("escaped_defect", "reviewer")] if h == 8 else [])
    store.close()
    w = im.windows(db, _iso(5.5), min_tasks=5)
    before, after = (im.measure(db, tmp_path, w[k]["tasks"], since=w[k]["since"],
                                until=w[k]["until"]) for k in ("before", "after"))
    targets = ["extra_steps_per_task:reviewer", "review_rounds_per_task"]
    result = im.effect(before, after, targets)
    assert _value(after, "cost_units_per_task:codex-subscription") < \
        _value(before, "cost_units_per_task:codex-subscription")
    assert result["verdict"] == "worse"
    assert any("quality" in n for n in result["notes"])
    assert result["metrics"]["quality_defects_per_task"]["before"] == 0
    assert result["tasks"] == {"before": 5, "after": 5}
    assert im.line({"id": "IP-0002", "change": "reviewer effort → low"}, result).endswith(
        "→ worse.")


def _m(**values):
    return {"tasks": [f"t{i}" for i in range(5)], "flags": [],
            "metrics": {k.replace("__", ":"): {"value": v, "known": 5, "total": 5}
                        for k, v in values.items()}}


def test_an_effect_improves_only_when_a_target_moves_and_cost_and_quality_hold():
    base = dict(review_rounds_per_task=2.0, cost_units_per_task__pool=100.0,
                quality_defects_per_task=0.2, false_hold_hours=0.0)
    targets = ["review_rounds_per_task"]
    improved = im.effect(_m(**base), _m(**{**base, "review_rounds_per_task": 1.0}), targets)
    assert improved["verdict"] == "improved"
    small = im.effect(_m(**base), _m(**{**base, "review_rounds_per_task": 1.9}), targets)
    assert small["verdict"] == "no_change"
    costly = im.effect(_m(**base), _m(**{**base, "review_rounds_per_task": 1.0,
                                          "cost_units_per_task__pool": 120.0}), targets)
    assert costly["verdict"] == "no_change" and any("mixed" in n for n in costly["notes"])
    pricier = im.effect(_m(**base), _m(**{**base, "cost_units_per_task__pool": 120.0}), targets)
    assert pricier["verdict"] == "worse"
    other = im.effect(_m(**base, tokens_per_task=10.0),
                      _m(**{**base, "review_rounds_per_task": 1.0, "tokens_per_task": 20.0}),
                      targets + ["tokens_per_task"])
    assert other["verdict"] == "worse"
    tiny_rise = im.effect(_m(**base), _m(**{**base, "review_rounds_per_task": 1.0,
                                            "quality_defects_per_task": 0.21}), targets)
    assert tiny_rise["verdict"] == "no_change"  # quality rose a little: never improved
    from_zero = im.effect(_m(**{**base, "quality_defects_per_task": 0.0}),
                          _m(**{**base, "review_rounds_per_task": 1.0,
                                "quality_defects_per_task": 0.01}), targets)
    assert from_zero["verdict"] == "worse"
    line = im.line({"id": "IP-0001", "change": "reviewer strength → strong"}, improved)
    assert line.startswith("IP-0001 (reviewer strength → strong): ")
    assert "review rounds/task 2 → 1" in line
    assert "cost units/task +0% (pool)" in line
    assert "quality defects/task 0.2 → 0.2" in line
    assert line.endswith("→ improved.")


# --- 10. unknowns and old schemas --------------------------------------------------------------


def test_unknown_measurements_stay_null_and_an_old_schema_without_run_times_is_flagged(tmp_path):
    db, store = _ledger(tmp_path)
    _add_task(store, "t1", finished_at=_iso(1), attempts=[
        {"node": "implementer", "role": "implementer", "tokens": None, "cost": 1, "wall": None}])
    store.close()
    m = im.measure(db, tmp_path, ["t1"])["metrics"]
    assert m["tokens_per_task"] == {"value": None, "known": 0, "total": 1, "sum": None}
    assert m["wall_time_per_task"]["value"] is None
    assert m["cost_units_per_task:codex-subscription"]["value"] is None
    assert m["review_rounds_per_task"]["value"] == 0  # a count is measured: 0 is real
    assert m["time_to_outcome_hours"]["value"] is None

    old = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE task_runs(run_id TEXT PRIMARY KEY, task_id TEXT, workflow_id TEXT,
            workflow_revision INTEGER, status TEXT);
        CREATE TABLE workflow_revisions(workflow_id TEXT, revision INTEGER, dag_json TEXT);
        CREATE TABLE node_states(run_id TEXT, node_id TEXT, phase TEXT, last_attempt_id TEXT);
        CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT,
            status TEXT);
        INSERT INTO task_runs VALUES ('r1', 't1', 'w1', 1, 'succeeded');
        INSERT INTO workflow_revisions VALUES ('w1', 1, '{"nodes": []}');
        INSERT INTO attempts VALUES ('a1', 'r1', 'implementer', 'succeeded');
    """)
    conn.commit()
    conn.close()
    measured = im.measure(old, tmp_path, ["t1"])
    assert "run_times_unavailable" in measured["flags"]
    assert measured["metrics"]["tokens_per_task"]["value"] is None
    assert im.windows(old, _iso(0), min_tasks=1) is None
