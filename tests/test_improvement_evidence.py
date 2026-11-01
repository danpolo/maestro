"""GDT-12 S1 (W12; R58, R62, R68, R38, R70): the ledger side of improvement proposals.

A goal miss is attributed by evidence only (`improvement_evidence.attribute`, rules A1-A4,
`unattributed` otherwise), and the attribution is an appended amendment of the observation,
never a rewrite and never a decision. The W11a `task_did_not_serve_goal` mapping reads
`unattributed` (mapping version 2) with its captured `planner` kept. The evidence pack
holds the goal section, the ranked deterministic patterns and the route statistics within
a bound. The ledger is a real `ControlStore` database (`tests/improvement_fixture.py`).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from maestro import acceptance, goal_replan, goal_store, improvement_evidence as ie
from maestro.docs import goals as doc_goals
from maestro.usage_report import build_usage_report
from maestro.workflows import review_origins, telemetry, telemetry_capture

from tests import improvement_fixture as fx

GREEN, RED = acceptance.GREEN, acceptance.RED


@pytest.fixture
def project(tmp_path):
    repo = fx.goals_project(tmp_path)
    db, store = fx.ledger(repo)
    arts = fx.artifacts(repo, store)
    yield repo, db, store, arts, doc_goals.load(repo)
    store.close()


@pytest.fixture
def bare(tmp_path):
    db, store = fx.ledger(tmp_path)
    yield db, store
    store.close()


def _miss(repo, store, arts, doc, task, *, gate=None, goal="G1", criteria=("AC-1",),
          episode=None, hours=1):
    """An accepted task whose approved review saw `no_progress_unexpected` on `goal`: its
    run (implementer, reviewer, gate), the observation row, and its goals-store episode."""
    attempts = fx.work()
    if gate is not None:
        attempts[2]["outputs"] = fx.gate_outputs(gate)
    run = fx.add_task(store, task, finished_at=fx.iso(hours), attempts=attempts,
                      impacts=[(goal, "no_progress_unexpected", 1)], artifact_store=arts)
    ep = episode or fx.episode(doc, task, goal, run=run, at=fx.iso(hours), criteria=criteria)
    fx.add_rows(repo, "episodes", [ep])
    return ep


def _impacts(db, task=None):
    report = build_usage_report(db, task_id=task)
    return [row for t in report["tasks"] for row in t["goal_impacts"]]


def _rows(store, kind=telemetry_capture.GOAL_IMPACT_EVENT):
    return [json.loads(r["payload"]) for r in store.conn.execute(
        "SELECT payload FROM events WHERE type = ? ORDER BY sequence", (kind,))]


def _attribute(repo, store, arts, at=None):
    seen = []
    facts = ie.attribute(repo, store, arts, at=at or fx.iso(10),
                         journal=lambda event, detail: seen.append((event, detail)))
    return facts, seen


# --- 1. mapping version 2 (R62) ---------------------------------------------------------------


def test_a_historical_task_did_not_serve_goal_row_reads_unattributed_with_its_captured_planner_fault(
        bare):
    db, store = bare
    assert telemetry.MAPPING_VERSION == 2
    assert review_origins.CAUSE_FAULT["task_did_not_serve_goal"] == "unattributed"
    assert "unattributed" in review_origins.FAULTS
    assert telemetry.extra_step("task_did_not_serve_goal") == {
        "extra_step_cause": "task_did_not_serve_goal", "fault": "unattributed", "flags": []}
    run = fx.add_task(store, "t1", finished_at=fx.iso(1), attempts=fx.work())
    # What W11a wrote before GDT-6 (09d2ad0): a planner fault under mapping version 1.
    store.append_event(telemetry_capture.EXTRA_STEP_EVENT, "planner", run_id=run, payload={
        "ledger_id": "goal:G1:t1", "extra_step_cause": "task_did_not_serve_goal",
        "observed_cause": "task_did_not_serve_goal", "fault": "planner", "role": "planner",
        "node_id": "planner", "mapping_version": 1, "flags": []})
    [row] = build_usage_report(db)["tasks"][0]["blame_ledger"]
    assert (row["fault"], row["captured_fault"], row["mapping_version"]) == (
        "unattributed", "planner", 1)
    assert row["extra_step_cause"] == "task_did_not_serve_goal"


# --- 2-6. attribution rules -------------------------------------------------------------------


def test_a_miss_the_task_s_own_gate_saw_turn_green_is_the_reviewer_s_misclassification(project):
    repo, db, store, arts, doc = project
    fx.baseline(repo, {"AC-1": RED, "AC-2": RED})
    ep = _miss(repo, store, arts, doc, "t1", gate={"AC-1": GREEN, "AC-2": RED})
    facts, seen = _attribute(repo, store, arts)
    [fact] = facts
    assert fact["episode"] == ep["id"]
    assert (fact["attribution"], fact["cause"], fact["rule"]) == (
        "reviewer", "review_misclassified", "gate_saw_green")
    assert any("AC-1" in e for e in fact["evidence"])
    [view] = _impacts(db)
    assert (view["attribution"], view["cause"], view["rule"]) == (
        "reviewer", "review_misclassified", "gate_saw_green")
    assert view["captured_attribution"] == "unattributed"
    assert [e for e, _ in seen] == ["goal_attribution_recorded"]
    assert "reviewer 1" in seen[0][1]


def test_a_miss_green_already_at_the_baseline_is_not_the_reviewer_s(project):
    repo, db, store, arts, doc = project
    fx.baseline(repo, {"AC-1": GREEN, "AC-2": RED})
    _miss(repo, store, arts, doc, "t1", gate={"AC-1": GREEN, "AC-2": RED})
    [fact], _ = _attribute(repo, store, arts)
    assert fact["rule"] != "gate_saw_green" and fact["attribution"] == "unattributed"


def test_a_miss_whose_criteria_could_not_be_measured_is_no_one_s_fault(project):
    repo, db, store, arts, doc = project
    fx.baseline(repo, {"AC-1": RED, "AC-2": RED})
    _miss(repo, store, arts, doc, "t1", criteria=("AC-1", "AC-2"),
          gate={"AC-1": acceptance.BLOCKED, "AC-2": acceptance.UNKNOWN})
    [fact], _ = _attribute(repo, store, arts)
    assert (fact["attribution"], fact["cause"], fact["rule"]) == (
        "none", "measurement_unavailable", "unmeasured")
    # One measured RED among them is a measurement: the rule does not hold.
    _miss(repo, store, arts, doc, "t2", criteria=("AC-1", "AC-2"), hours=2,
          gate={"AC-1": acceptance.BLOCKED, "AC-2": RED})
    facts, _ = _attribute(repo, store, arts)
    assert {f["task"]: f["rule"] for f in facts}["t2"] == "pending_evidence"


def test_a_miss_under_terms_dan_later_revised_is_no_one_s_fault(project):
    repo, db, store, arts, doc = project
    run = fx.add_task(store, "t1", finished_at=fx.iso(1), attempts=fx.work(),
                      impacts=[("G1", "no_progress_unexpected", 1)])
    fx.add_rows(repo, "episodes", [fx.episode(doc, "t1", "G1", run=run, terms="0ld7erms0000")])
    [fact], _ = _attribute(repo, store, arts)
    assert (fact["attribution"], fact["cause"], fact["rule"]) == (
        "none", "contract_revised", "terms_revised")


_TOKENS = {"plan": "planner", "scope": "planner", "goal_mapping": "planner",
           "dependency": "none", "constraint": "none", "harness": "none",
           "measurement": "none", "agent": "implementer", "unknown": "unattributed"}


@pytest.mark.parametrize("token,fault", sorted(_TOKENS.items()))
def test_a_reviewed_diagnosis_cause_line_attributes_by_the_table_and_unknown_stays_unattributed(
        project, token, fault):
    repo, db, store, arts, doc = project
    assert review_origins.DIAGNOSIS_FAULT[token] == fault
    assert set(review_origins.DIAGNOSIS_FAULT) == set(_TOKENS)
    a = _miss(repo, store, arts, doc, "t1")
    b = _miss(repo, store, arts, doc, "t2", hours=2)
    diag = "g1-diagnose-1234abcd"
    fx.add_rows(repo, "replans", [fx.diagnosis_replan("G1", [a["id"], b["id"]], diag)])
    fx.commit_file(repo, goal_replan.cause_file(diag),
                   f"Cause: {token}\n\nparse_recipe reads no caption (tests/x.py::t).\n")
    # Not yet in the registry (the diagnosis is not reviewed and accepted): no evidence.
    facts, _ = _attribute(repo, store, arts)
    assert {f["rule"] for f in facts} == {"pending_evidence"}
    fx.registry(repo, [diag])
    facts, _ = _attribute(repo, store, arts, at=fx.iso(11))
    assert len(facts) == 2
    for fact in facts:
        assert (fact["attribution"], fact["rule"]) == (fault, "diagnosed")
        assert fact["cause"] == f"diagnosis_{token}"
        assert any(diag in e for e in fact["evidence"])


def test_a_diagnosis_file_without_its_cause_line_is_pending_evidence(project):
    repo, db, store, arts, doc = project
    a = _miss(repo, store, arts, doc, "t1")
    diag = "g1-diagnose-1234abcd"
    fx.add_rows(repo, "replans", [fx.diagnosis_replan("G1", [a["id"]], diag)])
    fx.registry(repo, [diag])
    fx.commit_file(repo, goal_replan.cause_file(diag),
                   "parse_recipe is still a stub.\nCause: plan\n")
    [fact], _ = _attribute(repo, store, arts)
    assert (fact["attribution"], fact["rule"]) == ("unattributed", "pending_evidence")


def test_without_establishing_evidence_a_miss_stays_unattributed_never_planner(project):
    repo, db, store, arts, doc = project
    _miss(repo, store, arts, doc, "t1")
    _miss(repo, store, arts, doc, "t2", hours=2)
    facts, _ = _attribute(repo, store, arts)
    assert [(f["attribution"], f["cause"], f["rule"]) for f in facts] == [
        ("unattributed", "pending_evidence", "pending_evidence")] * 2
    assert {v["attribution"] for v in _impacts(db)} == {"unattributed"}
    assert "planner" not in {r.get("attribution") for r in _rows(store)}


def test_an_episode_without_its_observation_row_is_journaled_unlinked(project):
    repo, db, store, arts, doc = project
    run = fx.add_task(store, "t1", finished_at=fx.iso(1), attempts=fx.work())
    fx.add_rows(repo, "episodes", [fx.episode(doc, "t1", "G1", run=run)])
    facts, seen = _attribute(repo, store, arts)
    assert [f["linked"] for f in facts] == [False]
    assert ("goal_attribution_unlinked" in [e for e, _ in seen])
    assert _rows(store) == []


# --- 7-8. amendments, never decisions (R68) ---------------------------------------------------


def test_an_attribution_is_an_appended_amendment_and_the_observation_stays(project):
    repo, db, store, arts, doc = project
    a = _miss(repo, store, arts, doc, "t1")
    [first] = _rows(store)
    _attribute(repo, store, arts)
    rows = _rows(store)
    assert len(rows) == 2 and rows[0] == first
    amended = rows[1]
    assert amended["ledger_id"] == first["ledger_id"]
    assert {k: amended[k] for k in first if k != "attribution"} == {
        k: first[k] for k in first if k != "attribution"}
    assert (amended["attribution"], amended["attribution_rule"],
            amended["mapping_version"]) == ("unattributed", "pending_evidence", 2)
    assert amended["attributed_at"] == fx.iso(10)
    assert isinstance(amended["attribution_evidence"], list)
    # The same facts again, later: no new row.
    _attribute(repo, store, arts, at=fx.iso(12))
    assert len(_rows(store)) == 2
    # New evidence (an accepted diagnosis naming the plan) appends a second amendment.
    diag = "g1-diagnose-1234abcd"
    fx.add_rows(repo, "replans", [fx.diagnosis_replan("G1", [a["id"]], diag)])
    fx.registry(repo, [diag])
    fx.commit_file(repo, goal_replan.cause_file(diag), "Cause: plan\n")
    _attribute(repo, store, arts, at=fx.iso(13))
    rows = _rows(store)
    assert len(rows) == 3 and rows[0] == first
    assert rows[2]["attribution"] == "planner"
    [view] = _impacts(db)
    assert (view["attribution"], view["captured_attribution"], view["amendments"]) == (
        "planner", "unattributed", 2)
    assert view["provenance"] == first["provenance"]


def _snapshot(repo, db, store):
    tables = {t: [tuple(r) for r in store.conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
              for t in ("task_runs", "tasks", "attempts", "node_states", "artifacts")}
    files = {}
    for rel in (".orchestrator/completed_tasks.json", "docs/ROADMAP.md", goal_store.STORE):
        path = Path(repo) / rel
        files[rel] = path.read_bytes() if path.exists() else None
    art = Path(repo) / ".orchestrator" / "artifacts"
    files["artifacts"] = sorted((p.relative_to(art).as_posix(), p.read_bytes())
                                for p in art.rglob("*") if p.is_file())
    return tables, files


def test_attribution_changes_no_run_status_registry_roadmap_or_goal_store(project):
    repo, db, store, arts, doc = project
    fx.baseline(repo, {"AC-1": RED, "AC-2": RED})
    (Path(repo) / "docs" / "ROADMAP.md").write_text("# ROADMAP\n", encoding="utf-8")
    a = _miss(repo, store, arts, doc, "t1", gate={"AC-1": GREEN})
    _miss(repo, store, arts, doc, "t2", hours=2)
    diag = "g1-diagnose-1234abcd"
    fx.add_rows(repo, "replans", [fx.diagnosis_replan("G1", [a["id"]], diag)])
    fx.registry(repo, [diag])
    head = fx.goals_demo.head(repo)
    before = _snapshot(repo, db, store)
    facts, _ = _attribute(repo, store, arts)
    assert {f["attribution"] for f in facts} == {"reviewer", "unattributed"}
    assert _snapshot(repo, db, store) == before
    assert fx.goals_demo.head(repo) == head


# --- 9-10. what never forms a miss; the goal section (R38, R70) -------------------------------


def test_enabling_maintenance_operator_only_and_serves_empty_never_form_a_miss_or_an_attribution(
        project):
    repo, db, store, arts, doc = project
    eps = []
    for n, (goal, contribution, served) in enumerate((
            ("G1", "enabling", True), ("G1", "maintenance", True), ("G1", "none", False),
            ("G2", "direct", True))):
        task = f"x{n}"
        run = fx.add_task(store, task, finished_at=fx.iso(1 + n), attempts=fx.work(),
                          impacts=[(goal, "no_progress_unexpected", 1)])
        eps.append(fx.episode(doc, task, goal, run=run, at=fx.iso(1 + n),
                              contribution=contribution, served=served))
    fx.add_rows(repo, "episodes", eps)
    rows = _rows(store)
    facts, seen = _attribute(repo, store, arts)
    assert facts == [] and _rows(store) == rows
    section = ie.goal_section(repo, doc, None, fx.iso(20), report=build_usage_report(db))
    assert section["misses"] == []
    assert section["excluded"] == {"enabling": 1, "maintenance": 1, "serves_empty": 1,
                                   "operator_only": 1}
    found = ie.patterns(build_usage_report(db), section, [], [f"x{n}" for n in range(4)])
    assert [p for p in found if p["kind"] == "goal_miss"] == []


def test_the_goal_section_counts_the_six_impacts_and_keeps_legacy_apart(project, tmp_path):
    repo, db, store, arts, doc = project
    eps = [fx.episode(doc, f"t{n}", "G1", impact=impact, at=fx.iso(1 + n))
           for n, impact in enumerate((*fx_impacts(), "no_progress_legacy"))]
    eps.append(fx.episode(doc, "late", "G1", impact="advanced", at=fx.iso(50)))
    eps.append(fx.episode(doc, "early", "G1", impact="advanced", at=fx.iso(0)))
    fx.add_rows(repo, "episodes", eps)
    section = ie.goal_section(repo, doc, fx.iso(0), fx.iso(20))
    g1 = section["goals"]["G1"]
    assert g1["impacts"] == {i: 1 for i in fx_impacts()}
    assert g1["legacy"] == 1
    assert g1["misses"] == {"unattributed": 1}
    assert [m["task"] for m in section["misses"]] == ["t3"]
    assert [u["task"] for u in section["unknown"]] == ["t5"]
    assert section["goals"]["G3"]["impacts"] == {i: 0 for i in fx_impacts()}
    empty = tmp_path / "plain"
    empty.mkdir()
    assert ie.goal_section(empty, None, None, None) == {}


def fx_impacts():
    from maestro.workflows.goal_progress import IMPACTS
    assert IMPACTS == ("advanced", "enabled", "unchanged_expected", "no_progress_unexpected",
                       "regressed", "unknown")
    return IMPACTS


# --- 11-12. patterns (W12 proof part 1) -------------------------------------------------------


def test_a_ledger_dominated_by_reviewer_missed_earlier_ranks_the_reviewer_pattern_first(bare):
    db, store = bare
    tasks = []
    for n in range(4):
        task = f"t{n}"
        tasks.append(task)
        fx.add_task(store, task, finished_at=fx.iso(n + 1), attempts=fx.work(reviews=3),
                    causes=[("missed_earlier", "reviewer", 2), ("missed_earlier", "reviewer", 3)]
                    + ([("check_failed", "implementer", 0)] if n < 3 else []))
    report = build_usage_report(db)
    found = ie.patterns(report, {}, [], tasks)
    first = found[0]
    assert (first["kind"], first["key"]) == ("extra_step", ["reviewer", "missed_earlier", "reviewer"])
    assert (first["rows"], first["tasks"]) == (8, 4)
    assert first["id"] == "PAT-" + hashlib.sha1(json.dumps(
        ["extra_step", ["reviewer", "missed_earlier", "reviewer"]]).encode()).hexdigest()[:8]
    assert first["evidence"] == sorted(first["evidence"]) and len(first["evidence"]) == 8
    second = found[1]
    assert (second["key"], second["rows"]) == (["implementer", "check_failed", "implementer"], 3)
    # Stable across runs: the same ledger gives the same ids.
    assert [p["id"] for p in ie.patterns(build_usage_report(db), {}, [], tasks)] == [
        p["id"] for p in found]


def test_a_pattern_needs_three_rows_in_two_tasks_and_its_spend_stays_inside_its_pool(bare):
    db, store = bare
    # 3 rows, one task: no pattern.
    fx.add_task(store, "solo", finished_at=fx.iso(1), attempts=fx.work(reviews=3),
                causes=[("missed_earlier", "reviewer", i) for i in (1, 2, 3)])
    # 2 rows in 2 tasks: no pattern.
    fx.add_task(store, "p1", finished_at=fx.iso(2), attempts=fx.work(),
                causes=[("not_fixed", "implementer", 0)])
    fx.add_task(store, "p2", finished_at=fx.iso(3), attempts=fx.work(),
                causes=[("not_fixed", "implementer", 0)])
    tasks = ["solo", "p1", "p2"]
    assert ie.patterns(build_usage_report(db), {}, [], tasks) == []
    # A third row in a second pool makes the pattern; each pool keeps its own spend.
    fx.add_task(store, "p3", finished_at=fx.iso(4),
                attempts=fx.work(tokens=40, cost=3, pool="claude-subscription"),
                causes=[("not_fixed", "implementer", 0)])
    [pattern] = ie.patterns(build_usage_report(db), {}, [], tasks + ["p3"])
    assert (pattern["rows"], pattern["tasks"]) == (3, 3)
    assert pattern["spend"] == {
        "codex-subscription": {"cost_units": 200, "tokens": 200},
        "claude-subscription": {"cost_units": 120, "tokens": 40}}
    # A task outside the window does not count.
    assert ie.patterns(build_usage_report(db), {}, [], ["p1", "p2"]) == []


def test_goal_misses_evidence_gaps_and_false_alarms_form_patterns_at_their_minimums(project):
    repo, db, store, arts, doc = project
    _miss(repo, store, arts, doc, "t1")
    _miss(repo, store, arts, doc, "t2", hours=2)
    _attribute(repo, store, arts)
    unknown = [fx.episode(doc, f"u{n}", "G3", impact="unknown", at=fx.iso(3 + n))
               for n in range(3)]
    fx.add_rows(repo, "episodes", unknown)
    report = build_usage_report(db)
    section = ie.goal_section(repo, doc, None, fx.iso(20), report=report)
    keep = [{"id": f"RP-{n}", "goal": "G1", "status": "decided", "at": fx.iso(1),
             "settled_at": fx.iso(2), "decision": {"resolution": "keep_going"}}
            for n in range(2)]
    found = {p["kind"]: p for p in ie.patterns(report, section, keep, ["t1", "t2"])}
    assert found["goal_miss"]["key"] == ["G1", "unattributed", "pending_evidence"]
    assert (found["goal_miss"]["rows"], found["goal_miss"]["tasks"]) == (2, 2)
    assert all(e.startswith("goal:G1:review:") for e in found["goal_miss"]["evidence"])
    assert found["goal_miss"]["spend"]["codex-subscription"]["tokens"] == 400
    assert (found["evidence_gap"]["key"], found["evidence_gap"]["rows"]) == (["G3"], 3)
    assert (found["false_alarm"]["key"], found["false_alarm"]["rows"]) == (
        ["replan_keep_going"], 2)
    # One keep-going is below the minimum.
    assert "false_alarm" not in {p["kind"] for p in ie.patterns(report, section, keep[:1],
                                                                 ["t1", "t2"])}


# --- 13-14. route statistics and the pack ---------------------------------------------------


def test_route_stats_group_by_role_and_route_with_coverage(bare):
    db, store = bare
    fx.add_task(store, "t1", finished_at=fx.iso(1), attempts=fx.work(reviews=2),
                causes=[("missed_earlier", "reviewer", 2)])
    fx.add_task(store, "t2", finished_at=fx.iso(2), attempts=fx.work(tokens=None))
    fx.add_task(store, "t3", finished_at=fx.iso(3),
                attempts=fx.work(backend="claude", model="opus", pool="claude-subscription"))
    rows = ie.route_stats(build_usage_report(db), ["t1", "t2", "t3"])
    by = {(r["role"], r["harness"], r["model"], r["effort"], r["pool_id"]): r for r in rows}
    sol = by[("reviewer", "codex", "sol", "high", "codex-subscription")]
    assert sol["tasks"] == 2 and sol["attempts_per_task"] == 1.5
    assert sol["review_rounds_per_task"] == 1.5
    assert sol["extra_steps_per_task"] == {"reviewer": 0.5}
    assert sol["tokens_per_task"] == 100.0           # 200 known over 2 tasks
    assert sol["coverage"]["tokens"] == {"measured_attempts": 2, "total_attempts": 3}
    opus = by[("reviewer", "claude", "opus", "high", "claude-subscription")]
    assert opus["tasks"] == 1 and opus["cost_units_per_task"] == 100.0
    assert ("gate", None, None, None, "codex-subscription") in by
    assert by[("gate", None, None, None, "codex-subscription")]["gate_failures_per_task"] == 0
    assert all(r["pool_id"] == "claude-subscription" or r["role"] == "gate"
               for r in ie.route_stats(build_usage_report(db), ["t3"]))


def test_window_tasks_reads_succeeded_runs_finished_in_the_window(bare):
    db, store = bare
    fx.add_task(store, "t1", finished_at=fx.iso(1), attempts=fx.work())
    fx.add_task(store, "t2", finished_at=fx.iso(5), attempts=fx.work())
    fx.add_task(store, "t3", finished_at=fx.iso(6), status="failed", attempts=fx.work())
    assert ie.window_tasks(db, None, None) == (["t1", "t2"], False)
    assert ie.window_tasks(db, fx.iso(1), fx.iso(5)) == (["t2"], False)


def test_the_pack_fits_the_bound_with_a_large_ledger_and_never_cuts_a_pattern(
        project, monkeypatch):
    repo, db, store, arts, doc = project
    causes = sorted(c for c, f in review_origins.CAUSE_FAULT.items() if not f.startswith("$")
                    and c != "unclassified")[:14]
    tasks = []
    for n in range(24):
        task = f"t{n:02d}"
        tasks.append(task)
        fx.add_task(store, task, finished_at=fx.iso(n + 1),
                    attempts=fx.work(reviews=2, backend=f"h{n % 12}", model=f"m{n % 12}"),
                    causes=[(c, "reviewer") for c in causes[n % 7: n % 7 + 7]])
    earlier = [{"id": f"IP-{n:04d}", "state": "declined", "lever": "role_prompt",
                "target": "reviewer", "change": "x" * 400,
                "decision": {"resolution": "decline", "answer": "no " * 100}}
               for n in range(30)]
    pack = ie.pack(repo, db, earlier=earlier)
    text = json.dumps(pack, separators=(",", ":"), sort_keys=True)
    assert len(text) <= ie.BRIEF_LIMIT == 12000
    full = {p["id"]: p for p in ie.patterns(build_usage_report(db), {}, [], tasks)}
    assert len(full) > 8 and 1 <= len(pack["patterns"]) <= 8
    for shown in pack["patterns"]:
        whole = full[shown["id"]]
        assert {k: v for k, v in shown.items() if k != "evidence"} == {
            k: v for k, v in whole.items() if k != "evidence"} | {
            "evidence_total": len(whole["evidence"])}
        assert shown["evidence"] == whole["evidence"][:5]
    assert len(pack["routes"]) <= 10 and pack["tasks"] == 24
    assert pack["levers"] == list(ie.LEVERS)
    assert any(row["path"] == "roles.reviewer.strength" for row in pack["config"])
    # A tighter bound drops whole entries from the tail, never part of a pattern.
    monkeypatch.setattr(ie, "BRIEF_LIMIT", 3000)
    small = ie.pack(repo, db, earlier=earlier)
    assert len(json.dumps(small, separators=(",", ":"), sort_keys=True)) <= 3000
    assert [p["id"] for p in small["patterns"]] == [
        p["id"] for p in pack["patterns"]][:len(small["patterns"])]
    assert small["patterns"] and all(set(p) == set(pack["patterns"][0])
                                     for p in small["patterns"])
    assert small["trimmed"]["earlier"] > 0
