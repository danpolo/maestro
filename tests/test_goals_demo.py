"""The GDT-14 demonstrator, setup path (P13 goal-driven TDD, R64; `02` §C).

One real sample project, "Recipe Box" (`tests/fixtures/goals/demo/`, helpers in
`tests/goals_demo.py`), taken from the grill's own contract through setup: `maestro goals
check`, `baseline`, `tasks`, doctor's goals check and `maestro goals finish`, all through the
real CLI on a real git repo whose acceptance tests really run. Nothing is faked here: the
graph lanes (`tests/graph_engineering/test_goals_demo_lanes.py`) fake only the agent CLI and
Telegram boundaries. `C_MAP` maps every C-scenario to its tests or a named limit.

Two module fixtures keep it cheap: `seeded` (the seed plus one real `goals baseline`) and
`solved` (every solving overlay committed plus one real `goals finish`); a test that changes
a project works on a copy.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from maestro import acceptance, cli, goal_completion, goal_roadmap, goal_store, taskgraph
from maestro.docs import goals

from . import goals_demo as demo
from . import improvement_fixture as fx
from .hang_bound import HANG_SECONDS

RED, GREEN, BLOCKED = acceptance.RED, acceptance.GREEN, acceptance.BLOCKED
OPERATOR, UNKNOWN = acceptance.OPERATOR, acceptance.UNKNOWN


def _cli(*args: str) -> tuple[int, str, str]:
    """`maestro <args>` in-process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


def _run(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], cwd=str(repo), capture_output=True,
                          text=True, timeout=HANG_SECONDS,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """The seed as setup leaves it, after one real `maestro goals baseline`."""
    repo = demo.materialize(tmp_path_factory.mktemp("demo-seed") / "recipebox")
    code, out, err = _cli("goals", "baseline", repo, "--json", "--timeout", "60")
    return SimpleNamespace(repo=repo, code=code, shown=json.loads(out), err=err,
                           commit=demo.head(repo))


@pytest.fixture(scope="module")
def solved(seeded, tmp_path_factory):
    """The seed's baseline, then every solving overlay committed, then one real
    `maestro goals finish` (no sign-off yet)."""
    repo = demo.copy(seeded.repo, tmp_path_factory.mktemp("demo-solved") / "recipebox")
    for name in demo.SOLVING:
        demo.apply_overlay(repo, name)
    code, out, err = _cli("goals", "finish", repo, "--json", "--timeout", "60")
    return SimpleNamespace(repo=repo, code=code, verdict=json.loads(out), err=err,
                           commit=demo.head(repo))


@pytest.mark.parametrize("grill", ["skill", "drive"])
def test_the_demo_contract_is_the_grill_s_contract_plus_setup_s_bindings(grill, tmp_path):
    """C1, C2: the demo's contract is the grill's output (either surface) with only setup's
    same-revision `Test:` binding added: nothing changed, added or removed, no problem, no
    question left for Dan, and no internal detail in a behaviour field (R15)."""
    grilled_text = demo.parity(grill)
    bound = demo.bind(grilled_text, "AC-2", demo.AC2_TEST)
    if grill == "skill":
        assert demo.contract_text() == bound
    repo = demo.materialize(tmp_path / "recipebox", contract=bound)
    grilled, set_up = goals.parse(grilled_text), goals.parse(
        (repo / goals.DOC).read_text(encoding="utf-8"))
    assert [p.text for p in set_up.issues] == []
    delta = goals.changes(grilled, set_up)
    assert (delta.changed, delta.added, delta.removed, delta.problems) == ((), (), (), ())
    assert set_up.revision == grilled.revision == 2 and set_up.ids == ("G1", "G2", "G3")
    assert {c.id: c.test for c in set_up.criteria} == {
        "AC-1": "python bench/import_ratio.py", "AC-2": demo.AC2_TEST,
        "AC-3": "pytest tests/acceptance/test_search.py", "AC-4": ""}
    assert [s.id for s in set_up.scenarios] == ["S-1", "S-2", "S-3"]
    assert [o.id for o in set_up.operator_checks] == ["OP-1"]
    assert [(i.id, i.checked_by) for i in set_up.invariants] == [("INV-1", ("AC-2",))]
    assert goals._internal_details(set_up) == []
    # Either surface's set-up contract is the same contract.
    same = goals.changes(goals.parse(demo.contract_text()), set_up)
    assert (same.changed, same.added, same.removed, same.problems) == ((), (), (), ())
    code, out, _ = _cli("goals", "check", repo, "--json")
    shown = json.loads(out)
    assert code == 0 and shown["revision"] == 2
    assert [i for i in shown["issues"] if i["ask"]] == []


def test_either_grill_s_contract_gives_the_same_baseline(seeded, tmp_path):
    """C2: the Drive-surface contract, set up the same way, gives the same baseline."""
    repo = demo.materialize(tmp_path / "recipebox", contract=demo.bind(
        demo.parity("drive"), "AC-2", demo.AC2_TEST))
    code, out, _ = _cli("goals", "baseline", repo, "--json", "--timeout", "60")
    drive = {r["id"]: (r["state"], r["reason"]) for r in json.loads(out)["results"]}
    skill = {r["id"]: (r["state"], r["reason"]) for r in seeded.shown["results"]}
    assert code == seeded.code == 1
    assert drive == skill


def test_the_baseline_tells_the_five_states_apart_on_one_project(seeded, tmp_path):
    """C6-C9 on one project: a working bench measures 0 (credible RED), the error handling
    already works (GREEN, regression protection), AC-3's harness is broken (BLOCKED, never
    counted RED), AC-4 has no test yet (unknown), OP-1 is Dan's (never an automated pass).
    `goals baseline` fails while AC-3 is not credible (R16); the harness overlay makes it a
    credible RED on the assertion."""
    assert seeded.code == 1
    shown = {r["id"]: r for r in seeded.shown["results"]}
    assert list(shown) == ["AC-1", "AC-2", "AC-3", "AC-4", "OP-1"]
    assert (shown["AC-1"]["state"], shown["AC-1"]["reason"]) == (
        RED, "measured 0, must be above 0.95 or equal")
    assert shown["AC-1"]["value"] == 0
    assert (shown["AC-2"]["state"], shown["AC-2"]["reason"]) == (GREEN, "passed")
    assert (shown["AC-3"]["state"], shown["AC-3"]["reason"]) == (
        BLOCKED, "pytest exit 2: interrupted (collection error)")
    assert (shown["AC-4"]["state"], shown["AC-4"]["reason"]) == (UNKNOWN, "no test bound yet")
    assert shown["OP-1"]["state"] == OPERATOR
    assert shown["OP-1"]["reason"].startswith("Dan: open three recipes")
    assert {r["state"] for r in shown.values()} == set(acceptance.STATES)
    assert all(r["revision"] == 2 for r in shown.values())

    stored = {r.id: r for r in acceptance.load(seeded.repo)}
    assert json.loads((seeded.repo / acceptance.BASELINE).read_text())["commit"] == \
        seeded.commit
    assert [cid for cid, r in stored.items() if acceptance.automated_pass(r)] == ["AC-2"]
    # The harness defect is the intended one: AC-3's helper module is missing.
    collected = _run(seeded.repo, "-m", "pytest", "tests/acceptance/test_search.py",
                     "-p", "no:cacheprovider")
    assert "ModuleNotFoundError: No module named 'cookbook500'" in collected.stdout
    assert _run(seeded.repo, "-m", "maestro.acceptance", "AC-3", "--credible"
                ).returncode == 1
    # The enabling harness alone makes AC-3 credible: RED on its assertion, not GREEN.
    fixed = demo.copy(seeded.repo, tmp_path / "harnessed")
    demo.apply_overlay(fixed, "harness")
    credible = _run(fixed, "-m", "maestro.acceptance", "AC-3", "--credible")
    assert credible.returncode == 0, credible.stdout + credible.stderr
    assert credible.stdout.startswith(f"AC-3 {RED}: ")
    assert "unknown command: search" in credible.stdout


def test_goals_tasks_turn_the_red_baseline_into_the_demo_roadmap(seeded):
    """C5, C11: `goals tasks` proposes a direct task per unmet mandatory criterion, AC-3's
    enabling harness first (its direct task waits on it), each linked to its goal,
    scenario, criterion and test, each with the gate check that proves it; GREEN AC-2,
    optional AC-4 and operator-only OP-1 get none. With the hand-written maintenance task
    the plan classifies every task: only the criterion-naming tasks are direct."""
    code, out, _ = _cli("goals", "tasks", seeded.repo, "--json")
    assert code == 0
    proposed = json.loads(out)
    assert [t["id"] for t in proposed] == [
        "ac-1-saved-posts-are-listed-within-an-hour", "ac-3-harness",
        "ac-3-search-lists-the-right-recipe-in-the-top"]
    assert [t["id"] for t in proposed] == [demo.AC1_TASK, demo.HARNESS, demo.AC3_TASK]
    assert proposed == demo.roadmap(seeded.repo, tidy=False)
    by = {t["id"]: t for t in proposed}
    # The blocks setup shows Dan ask for each task's `scope:` (review M2a): without one a
    # goal-proposed task runs alone, and waits while a gate question waits on Dan.
    code, shown, _ = _cli("goals", "tasks", seeded.repo)
    assert code == 0 and shown.count(goal_roadmap.SCOPE_HINT) == len(proposed)

    ac1, harness, ac3 = by[demo.AC1_TASK], by[demo.HARNESS], by[demo.AC3_TASK]
    assert (ac1["serves"], ac1["contribution"], ac1["criteria"], ac1["deps"]) == (
        ["G1"], "direct", ["AC-1", "S-1"], [])
    assert ac1["tests"] == ["python bench/import_ratio.py"]
    assert [v["cmd"] for v in ac1["verifications"]] == [
        f"env PYTHONDONTWRITEBYTECODE=1 {sys.executable} -m maestro.acceptance AC-1"]
    assert (harness["serves"], harness["contribution"], harness["criteria"],
            harness["deps"]) == (["G3"], "enabling", ["AC-3"], [])
    assert "blocked_environment: pytest exit 2" in harness["short_desc"]
    assert [v["cmd"] for v in harness["verifications"]] == [
        f"env PYTHONDONTWRITEBYTECODE=1 {sys.executable} -m maestro.acceptance AC-3 "
        "--credible"]
    assert (ac3["serves"], ac3["contribution"], ac3["criteria"], ac3["deps"]) == (
        ["G3"], "direct", ["AC-3", "S-3"], [demo.HARNESS])
    assert ac3["tests"] == ["pytest tests/acceptance/test_search.py"]
    assert [v["cmd"] for v in ac3["verifications"]] == [
        f"env PYTHONDONTWRITEBYTECODE=1 {sys.executable} -m pytest "
        "tests/acceptance/test_search.py -p no:cacheprovider"]

    plan = demo.roadmap(seeded.repo)
    assert [t["id"] for t in plan] == [*by, demo.TIDY_ID]
    # The rendered blocks read back as the same plan (setup pastes them into the ROADMAP).
    text = "# ROADMAP\n\n" + goal_roadmap.render_tasks(plan)
    assert [t["id"] for t in taskgraph.scan_tasks(text)] == [t["id"] for t in plan]
    doc = goals.load(seeded.repo)
    assert goals.contribution_map(doc, plan) == {
        "G1": {"direct": [demo.AC1_TASK]}, "G2": {},
        "G3": {"enabling": [demo.HARNESS], "direct": [demo.AC3_TASK]}}
    assert all(goals.normalise_criteria(t["criteria"])[0] for t in plan
               if t["contribution"] == "direct")


def test_doctor_accepts_the_demo_plan_and_holds_what_names_no_goal(seeded, tmp_path):
    """C4: doctor's goals check passes the demo plan at execution (what it reports are
    drafting warnings, never a required failure), and fails it, required, for a task that
    names no goal, one serving a goal the contract lacks, or a plan with no enabling
    harness for AC-3's broken test."""
    repo = demo.copy(seeded.repo, tmp_path / "recipebox")
    plan = demo.roadmap(repo)

    def doctor(tasks):
        demo.write_roadmap(repo, tasks)
        return cli._check_goals(repo)

    accepted = doctor(plan)
    assert accepted.required is False, accepted.detail
    assert "goal G2 is served by no open or completed task" in accepted.detail
    assert "blocks at execution" not in accepted.detail
    assert "not feasible" not in accepted.detail

    unnamed = doctor([{k: v for k, v in t.items() if k != "serves"}
                      if t["id"] == demo.TIDY_ID else t for t in plan])
    assert (unnamed.ok, unnamed.required) == (False, True)
    assert f"task {demo.TIDY_ID} names no goal it serves" in unnamed.detail

    unknown = doctor([{**t, "serves": ["G9"]} if t["id"] == demo.TIDY_ID else t
                      for t in plan])
    assert (unknown.ok, unknown.required) == (False, True)
    assert f"task {demo.TIDY_ID} serves unknown goal G9" in unknown.detail

    no_harness = doctor([{**t, "deps": []} for t in plan if t["id"] != demo.HARNESS])
    assert (no_harness.ok, no_harness.required) == (False, True)
    assert "not feasible: AC-3 is blocked_environment: no open enabling task makes its " \
           "test runnable" in no_harness.detail


def test_an_empty_queue_on_the_seed_is_incomplete_never_complete(seeded, tmp_path):
    """C30: with no task run, the completion gate reruns every mandatory criterion on the
    seed and says incomplete, naming AC-1 and AC-3 as the work; never complete."""
    repo = demo.copy(seeded.repo, tmp_path / "recipebox")
    code, out, _ = _cli("goals", "finish", repo, "--json", "--timeout", "60")
    v = json.loads(out)
    assert code == 1 and v["verdict"] == "incomplete"
    assert sorted(w["id"] for w in v["work"]) == ["AC-1", "AC-3"]
    assert [d["id"] for d in v["dan"]] == ["OP-1"]
    items = {i["id"]: i for i in v["items"]}
    assert (items["AC-2"]["status"], items["AC-2"]["state"]) == ("met", GREEN)
    assert items["AC-3"]["state"] == BLOCKED and items["AC-1"]["state"] == RED
    assert any(d["id"] == "AC-4" for d in v["deferred"])
    assert (repo / goal_completion.REPORT_JSON).is_file()


def test_the_solved_demo_waits_on_dan_s_operator_check(solved, tmp_path):
    """C9: every automated criterion GREEN on the finish run is still not done: OP-1 is
    Dan's sign-off, and only a Pass at this (revision, HEAD) completes; one given at
    another commit does not count (R67). The recorded run is reused, never rerun."""
    v = solved.verdict
    assert solved.code == 1 and v["verdict"] == "blocked"
    items = {i["id"]: i for i in v["items"]}
    for cid in ("AC-1", "AC-2", "AC-3"):
        assert (items[cid]["status"], items[cid]["state"]) == ("met", GREEN), items[cid]
    assert v["work"] == [] and [d["id"] for d in v["dan"]] == ["OP-1"]
    assert items["OP-1"]["state"] == OPERATOR
    assert (v["revision"], v["commit"]) == (2, solved.commit)

    repo = demo.copy(solved.repo, tmp_path / "recipebox")

    def sign(commit: str) -> dict:
        check = {"id": "OP-1", "revision": 2, "commit": commit, "finish": v["id"]}
        row = goal_completion._signoff(check, "Pass", "2026-10-22T10:00:00",
                                       f"{goal_completion.OP_PREFIX}demo")
        goal_store.update(repo, "operator", lambda rows: [*rows, row])
        return row

    sign(demo.git(repo, "rev-parse", "HEAD~1"))
    code, out, _ = _cli("goals", "finish", repo, "--json", "--timeout", "60")
    assert code == 1 and json.loads(out)["verdict"] == "blocked"
    sign(solved.commit)
    code, out, _ = _cli("goals", "finish", repo, "--json", "--timeout", "60")
    done = json.loads(out)
    assert code == 0 and done["verdict"] == "complete" and done["id"] == v["id"]
    assert {i["id"]: i["status"] for i in done["items"]}["OP-1"] == "met"


def test_a_revised_target_makes_the_old_proof_stale_and_a_retired_id_stays_retired(
        solved, tmp_path):
    """C3: Dan's revision 3 raises AC-1's threshold. Its earlier proof and what it links
    to are stale: status shows the baseline stale and AC-1 not GREEN, `goals tasks` refuses
    to plan from a baseline of another revision, and the finish runs anew at revision 3.
    Reusing the retired G4 is a gate problem."""
    repo = demo.copy(solved.repo, tmp_path / "recipebox")
    path = repo / goals.DOC
    rev2 = path.read_text(encoding="utf-8")
    rev3 = rev2
    for old, new in (
            ("Contract revision: 2 (approved 2026-10-20 by Dan)",
             "Contract revision: 3 (approved 2026-10-22 by Dan)"),
            ("- **Threshold:** 0.95\n", "- **Threshold:** 0.97\n"),
            ("| 2 | 2026-10-20 | G4 meal planning retired | it is a non-goal now | G4 | Dan "
             "| rev 1 G4 |\n",
             "| 2 | 2026-10-20 | G4 meal planning retired | it is a non-goal now | G4 | Dan "
             "| rev 1 G4 |\n| 3 | 2026-10-22 | AC-1 threshold 0.95 to 0.97 | fewer lost "
             "posts | AC-1 | Dan | rev 2 AC-1 |\n")):
        assert rev3.count(old) == 1, old
        rev3 = rev3.replace(old, new)
    delta = goals.changes(goals.parse(rev2), goals.parse(rev3))
    assert delta.changed == ("AC-1",) and delta.problems == ()
    assert {"AC-1", "G1", "S-1"} <= set(delta.stale)
    assert [p.text for p in goals.parse(rev3).issues] == []
    path.write_text(rev3, encoding="utf-8")
    demo.git(repo, "commit", "-q", "-am", "docs: contract revision 3")

    code, out, _ = _cli("goals", "status", repo, "--json")
    shown = json.loads(out)
    assert code == 0 and shown["revision"] == 3
    assert shown["baseline"]["stale"] is True and shown["baseline"]["revision"] == 2
    ac1 = next(c for c in shown["criteria"] if c["id"] == "AC-1")
    assert ac1["state"] != GREEN

    code, _, err = _cli("goals", "tasks", repo)
    assert code == 1 and "no acceptance baseline at contract revision 3" in err

    code, out, _ = _cli("goals", "finish", repo, "--json", "--timeout", "60")
    v = json.loads(out)
    assert v["revision"] == 3 and v["id"] != solved.verdict["id"]
    assert v["id"] == goal_completion.finish_id(3, demo.head(repo))
    assert v["verdict"] != "complete"
    assert {i["id"]: i.get("state") for i in v["items"]}["AC-1"] == GREEN

    path.write_text(rev3.replace(
        "## Final product",
        "### G4: Meal planning\n\n- **Outcome:** a week of meals is planned.\n"
        "- **Rationale:** Dan asked once.\n- **Baseline:** not_applicable\n"
        "- **Target:** a plan for 7 days.\n- **Verified by:** OP-1\n\n## Final product", 1),
        encoding="utf-8")
    code, out, _ = _cli("goals", "check", repo, "--gate")
    assert code == 1
    assert "G4 is retired (rev 2) and cannot be reused" in out


def test_a_change_that_advances_g1_but_breaks_inv_1_is_blocked_never_traded(
        seeded, tmp_path):
    """C23 on Recipe Box (GDT-14 review M1): one change on AC-1's direct task turns AC-1
    GREEN (G1 advances) and drops the not-a-recipe check, so AC-2 (INV-1's check) goes
    GREEN → RED. The gate's goals guard blocks it as `hard_invariant_regression` naming
    INV-1, and no question offers Dan to trade INV-1 for G1's progress."""
    from maestro.workflows import goal_guard

    repo = demo.copy(seeded.repo, tmp_path / "recipebox")
    task = next(t for t in demo.roadmap(repo) if t["id"] == demo.AC1_TASK)
    policy = goal_guard.policy(repo, task, demo.head(repo))
    demo.apply_overlay(repo, "import")
    demo.apply_overlay(repo, "tidy-break")
    check = goal_guard.regression_check(repo, policy)
    assert (check.observed["AC-1"], check.observed["AC-2"]) == (GREEN, RED)
    [blocking] = [f for f in check.findings if f.startswith("hard_invariant_regression")]
    assert "INV-1" in blocking and "AC-2" in blocking
    assert not any("INV-1" in json.dumps(q) or "AC-2" in json.dumps(q)
                   for q in check.questions)
    assert check.accepted == []


def test_a_tamper_that_makes_the_check_pass_is_blocked_by_the_guard_alone(seeded, tmp_path):
    """C13 on Recipe Box (GDT-14 review L2): AC-3's direct task implements search and also
    loosens AC-3's test (top 3 → top 500). Its check now passes, so only the goals guard
    stands between it and a fake completion: `acceptance_test_changed`, and no
    `test_corrections:` names AC-3. (Lane A's tamper round leaves search out, so AC-3's
    check fails there too.)"""
    from maestro.workflows import goal_guard

    repo = demo.copy(seeded.repo, tmp_path / "recipebox")
    demo.apply_overlay(repo, "harness")
    task = next(t for t in demo.roadmap(repo) if t["id"] == demo.AC3_TASK)
    policy = goal_guard.policy(repo, task, demo.head(repo))
    demo.apply_overlay(repo, "search")
    demo.apply_overlay(repo, "search-tamper")
    ac3 = next(c for c in goals.load(repo).criteria if c.id == "AC-3")
    assert acceptance.run_criterion(repo, ac3, revision=2, timeout=HANG_SECONDS).state == GREEN
    [finding] = goal_guard.tamper_findings(repo, policy)
    assert finding.startswith("acceptance_test_changed: tests/acceptance/test_search.py "
                              "binds AC-3")


class _Dan:
    """Dan's channel for the completion gate and the replan: records each question once,
    answers none (the real channel keeps the records on disk)."""

    def __init__(self):
        self.asked: list[dict] = []

    def poll_question(self, spec):
        if spec["id"] not in [s["id"] for s in self.asked]:
            self.asked.append(json.loads(json.dumps(dict(spec))))
        return None

    def mark_question_recorded(self, req_id, **fields):
        pass

    def records(self):
        return [{**s, "type": "spec-question"} for s in self.asked]

    def question_answer(self, req_id):
        return None


#: Only AC-1 (G1's import ratio) is still RED: AC-3's harness and search and the tidy landed.
_ALL_BUT_IMPORT = ("harness", "search", "tidy")
_AT = "2026-10-22T12:00:00+00:00"


def test_q1_misses_raise_one_replan_on_the_demo(seeded, tmp_path):
    """`02` §D bullet 9, Q1 modified-B on Recipe Box (GDT-14 review M1): AC-1's direct
    task merges without moving AC-1 (one unexpected miss: no replan); the enabling harness's
    miss on G3 never counts; a second direct G1 round that misses AC-1 again raises one
    replan for G1, naming AC-1 and both rounds. Real acceptance runs; Dan is the fake."""
    from maestro import goal_replan

    repo = demo.copy(seeded.repo, tmp_path / "recipebox")
    for name in _ALL_BUT_IMPORT:
        demo.apply_overlay(repo, name)
    plan = {t["id"]: t for t in demo.roadmap(repo)}
    retry = {**plan[demo.AC1_TASK], "id": "ac-1-retry", "deps": []}
    still_open = [{**plan[demo.AC1_TASK], "id": "ac-1-third", "status": "open"}]

    def miss(goal, cid):
        return {"goal": goal, "impact": "no_progress_unexpected",
                "claimed": "no_progress_unexpected", "served": True,
                "evidence": [{"kind": "test", "id": cid, "result": "failed",
                              "criterion": cid}]}

    dan = _Dan()

    def graduate(task, record):
        return goal_replan.on_graduation(
            repo, task, [record], still_open, dan, run_id=f"run-{task['id']}",
            workflow_id=f"wf-{task['id']}", at=_AT, misses=2, timeout=HANG_SECONDS)

    graduate(plan[demo.AC1_TASK], miss("G1", "AC-1"))
    graduate(plan[demo.HARNESS], miss("G3", "AC-3"))
    assert goal_store.read(repo)["replans"] == []
    graduate(retry, miss("G1", "AC-1"))
    [replan] = goal_store.read(repo)["replans"]
    assert (replan["goal"], replan["trigger"]["kind"]) == ("G1", "repeated_misses")
    assert replan["trigger"]["criteria"] == ["AC-1"]
    assert sorted(replan["trigger"]["facts"]) == sorted([f"{demo.AC1_TASK}:G1",
                                                         "ac-1-retry:G1"])


def test_the_bounded_final_repeat_gate_asks_dan_at_the_cap_on_the_demo(seeded, tmp_path):
    """`02` §D bullet 9, W11b's bounded final repeat gate on Recipe Box (GDT-14 review M1),
    C31: the queue is empty and AC-1 is still RED. The completion gate adds one corrective
    round (applied, no question); that round lands without fixing AC-1, and at the cap
    (1 round) the gate asks Dan, whose "Keep going" keeps the project blocked, never done."""
    from maestro import goal_replan

    repo = demo.copy(seeded.repo, tmp_path / "recipebox")
    for name in _ALL_BUT_IMPORT:
        demo.apply_overlay(repo, name)
    dan = _Dan()

    def finish():
        return goal_completion.evaluate(repo, channel=dan, at=_AT, budget=float("inf"),
                                        rounds_cap=1, records=dan.records,
                                        timeout=HANG_SECONDS)

    def finish_replans():
        return [r for r in goal_store.read(repo)["replans"]
                if (r.get("trigger") or {}).get("kind") == "finish_gap"]

    step = finish()
    assert step["kind"] == "working", step["text"]
    [first] = finish_replans()
    assert first["status"] == "applied" and first["applied"]
    assert not [s for s in dan.asked if s["id"].startswith("dq-replan-")]

    done = repo / ".orchestrator" / "completed_tasks.json"
    done.write_text(json.dumps([{"id": t} for t in first["applied"]]), encoding="utf-8")
    (repo / "NOTES.md").write_text("the corrective round landed\n", encoding="utf-8")
    demo.git(repo, "add", "-A")
    demo.git(repo, "commit", "-q", "-m", "corrective round")

    step = finish()
    v = step["verdict"]
    assert v["verdict"] == "blocked" and step["kind"] != "complete"
    assert {i["id"]: i for i in v["items"]}["AC-1"]["status"] == "dan"
    [asked] = [s for s in dan.asked if s["id"].startswith("dq-replan-")]
    assert "cap (1 round) is reached" in asked["spec"]["why_undefined"]
    assert [o["label"] for o in asked["spec"]["options"]][-1] == goal_replan.KEEP_GOING
    assert "the project stays blocked, never done" in asked["spec"]["options"][-1][
        "consequence"]
    assert len(finish_replans()) == 2


# --- W12 (GDT-12): improvement on the demo's own ledger (C20, C32, C33) ---------------------

#: The global config the demo's improvement lever edits (a tmp `MAESTRO_GLOBAL_CONFIG`).
_W12_CONFIG = "# Dan's global defaults\nroles:\n  reviewer:\n    strength: balanced\n"
#: The demo's accepted tasks, in the order they finished (`fx.iso(1)`, `fx.iso(2)`, ...).
_W12_TASKS = (demo.HARNESS, demo.AC3_TASK, demo.TIDY_ID, demo.AC1_TASK)
_W12_KEY = ["reviewer", "missed_earlier", "reviewer"]


class _W12Dan(_Dan):
    """Dan's channel for the improvement questions: answers what the test sets."""

    def __init__(self):
        super().__init__()
        self.answers: dict[str, str] = {}

    def poll_question(self, spec):
        super().poll_question(spec)
        return self.answers.get(spec["id"])


@pytest.fixture
def w12(seeded, tmp_path, monkeypatch):
    """`make(overlays)`: a copy of the seeded demo (its real baseline) with `overlays`
    committed, a real `ControlStore` ledger and artifact store in its `.orchestrator/`, and
    a tmp global config. The stores are closed at teardown."""
    opened = []

    def make(overlays=demo.SOLVING):
        repo = demo.copy(seeded.repo, tmp_path / "recipebox")
        for name in overlays:
            demo.apply_overlay(repo, name)
        config = tmp_path / "maestro-home" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(_W12_CONFIG, encoding="utf-8")
        monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(config))
        db, store = fx.ledger(repo)
        opened.append(store)
        return SimpleNamespace(repo=repo, db=db, store=store, arts=fx.artifacts(repo, store),
                               config=config, doc=goals.load(repo))

    yield make
    for store in opened:
        store.close()


def _w12_task(d, task, hour, *, misses=True, causes=(), miss_goal=None):
    """An accepted demo task finished at `fx.iso(hour)`: an implementer, three reviewer
    rounds (rounds 2 and 3 carry the reviewer's `missed_earlier` with `misses`) and a gate.
    With `miss_goal`, its approved review saw `no_progress_unexpected` on that goal (the
    observation row) and the goals store holds the episode."""
    rows = [("missed_earlier", "reviewer", i) for i in (2, 3)] if misses else []
    run = fx.add_task(d.store, task, finished_at=fx.iso(hour), attempts=fx.work(reviews=3),
                      causes=[*rows, *causes], artifact_store=d.arts,
                      impacts=[(miss_goal, "no_progress_unexpected", 1)] if miss_goal else [])
    if miss_goal:
        fx.add_rows(d.repo, "episodes", [fx.episode(
            d.doc, task, miss_goal, run=run, at=fx.iso(hour), criteria=("AC-1",))])
    return run


def _w12_after(d, task, hour, *, causes=()):
    """An accepted task after the lever: one review round, no reviewer miss."""
    return fx.add_task(d.store, task, finished_at=fx.iso(hour), attempts=fx.work(),
                       causes=list(causes))


def _w12_stronger(system, user):
    """The analyst double: raise the reviewer's strength, citing only the reviewer pattern's
    own evidence (the analyst is a bounded LLM call in the product, never here)."""
    pack = json.JSONDecoder().raw_decode(user)[0]
    [p] = [p for p in pack["patterns"] if p["key"] == _W12_KEY]
    return json.dumps({"proposals": [{
        "pattern": p["id"], "lever": "model_effort", "target": "reviewer",
        "change": "reviewer strength → strong",
        "config": {"path": "roles.reviewer.strength", "value": "strong"},
        "evidence": p["evidence"][:2],
        "expected_saving": {"cost_units": 2, "pool": "codex-subscription",
                            "basis": "the reviewer's late rounds"},
        "risk": "a slower review", "cost": "a stronger model per review",
        "measure": {"metrics": ["extra_steps_per_task:reviewer/missed_earlier",
                                "cost_units_per_task:codex-subscription"]},
        "rollback": "restore the reviewer's strength",
        "why": "the reviewer finds late what was there early",
        "alternatives": ["a reviewer prompt that lists its earlier findings"],
        "revises": None}]})


def _w12_run(d, at, dan, *, told=None):
    from maestro import improvement as imp

    return imp.run(d.repo, d.db, d.store, d.arts, kind="weekly", key=f"weekly:{at}", at=at,
                   analyse=_w12_stronger, channel=dan,
                   notify=(told.append if told is not None else None))


def _w12_poll(d, dan, at):
    from maestro import improvement as imp

    return imp.poll(d.repo, dan, at, apply=lambda row: imp.apply_config(d.repo, row, at=at),
                    queue=lambda row: None)


def _w12_proposal(d, ip="IP-0001"):
    from maestro import improvement as imp

    return next(r for r in imp.read(d.repo)["proposals"] if r["id"] == ip)


def _w12_observations(store):
    from maestro.workflows import telemetry_capture

    return [json.loads(r["payload"]) for r in store.conn.execute(
        "SELECT payload FROM events WHERE type = ? ORDER BY sequence",
        (telemetry_capture.GOAL_IMPACT_EVENT,))]


def _w12_impacts(d):
    from maestro.usage_report import build_usage_report

    return [row for t in build_usage_report(d.db)["tasks"] for row in t["goal_impacts"]]


def test_w12_attribution_analysis_and_a_rollback_leave_every_accepted_verdict_unchanged_on_the_demo(
        w12):
    """C20, R68 on Recipe Box: after the demo's accepted runs and one real `goals finish`,
    W12's attribution, an analysis, Dan's approval (the global config raised) and his
    rollback (the exact bytes back) write no run status, no registry, ROADMAP, goals store,
    outcome report or review artifact, and move no HEAD. AC-1's observation row stays as it
    was: attribution only appends amendments. A second real `goals finish` on the same tree
    gives the same verdict and fingerprint."""
    from maestro import improvement as imp

    d = w12()
    demo.write_roadmap(d.repo, demo.roadmap(d.repo))       # what `goals tasks` proposed
    demo.git(d.repo, "add", "docs/ROADMAP.md")
    demo.git(d.repo, "commit", "-q", "-m", "docs: the demo's plan")
    for hour, task in enumerate(_W12_TASKS, start=1):
        _w12_task(d, task, hour, miss_goal="G1" if task == demo.AC1_TASK else None)
    d.arts.put_bytes(b'{"verdict": "approved"}', type="node_result")
    fx.registry(d.repo, _W12_TASKS)
    code, out, _ = _cli("goals", "finish", d.repo, "--json", "--timeout", "60")
    first = json.loads(out)
    assert code == 1 and first["verdict"] == "blocked"   # OP-1 is still Dan's
    [observed] = _w12_observations(d.store)

    def snapshot():
        rows = {table: [tuple(r) for r in d.store.conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "task_runs", "attempts", "acceptance_decisions")}
        files = {rel: (d.repo / rel).read_bytes() for rel in (
            ".orchestrator/completed_tasks.json", "docs/ROADMAP.md", goal_store.STORE,
            goal_completion.REPORT_JSON, goal_completion.REPORT_MD)}
        art = d.repo / ".orchestrator" / "artifacts"
        stored = sorted((p.relative_to(art).as_posix(), p.read_bytes())
                        for p in art.rglob("*") if p.is_file())
        return rows, files, stored, demo.head(d.repo)

    before = snapshot()
    dan = _W12Dan()
    record = _w12_run(d, fx.iso(10), dan)
    assert (record["status"], record["asked"]) == ("done", ["IP-0001"]), record
    assert snapshot() == before
    dan.answers["dq-improve-IP-0001"] = "Approve"
    _w12_poll(d, dan, fx.iso(11))
    assert _w12_proposal(d)["state"] == "applied"
    assert "strength: strong" in d.config.read_text(encoding="utf-8")
    assert snapshot() == before
    imp.rollback(d.repo, _w12_proposal(d), at=fx.iso(12))
    assert _w12_proposal(d)["state"] == "rolled_back"
    assert d.config.read_text(encoding="utf-8") == _W12_CONFIG
    assert snapshot() == before
    # The observation row is the first row still; the rest are amendments of it.
    rows = _w12_observations(d.store)
    assert rows[0] == observed and len(rows) >= 2
    assert all(r["ledger_id"] == observed["ledger_id"] and "attribution_rule" in r
               for r in rows[1:])
    code, out, _ = _cli("goals", "finish", d.repo, "--json", "--timeout", "60")
    again = json.loads(out)
    assert (code, again["verdict"], again["fingerprint"], again["id"]) == (
        1, first["verdict"], first["fingerprint"], first["id"])


def test_a_historical_no_progress_row_reads_unattributed_and_a_declined_proposal_returns_only_on_new_evidence_on_the_demo(
        w12):
    """C32, R60, R62 on Recipe Box with AC-1 still RED (the seed's real baseline measured
    0): a W11a `task_did_not_serve_goal` row (mapping version 1, captured `planner`) reads
    `unattributed`; AC-1's real miss, with no establishing evidence, stays `unattributed`,
    never `planner`. Dan declines the reviewer proposal; the same pattern with only 2 rows
    recorded after his decision is suppressed, and with 4 new rows it is asked again,
    naming them."""
    from maestro import improvement as imp
    from maestro.usage_report import build_usage_report
    from maestro.workflows import telemetry_capture

    d = w12(_ALL_BUT_IMPORT)
    assert {r.id: r.state for r in acceptance.load(d.repo)}["AC-1"] == RED
    runs = {task: _w12_task(d, task, hour, miss_goal="G1" if task == demo.AC1_TASK else None)
            for hour, task in enumerate(_W12_TASKS, start=1)}
    # What W11a wrote for the harness before GDT-6 (09d2ad0).
    d.store.append_event(telemetry_capture.EXTRA_STEP_EVENT, "planner",
                         run_id=runs[demo.HARNESS], payload={
                             "ledger_id": f"goal:G3:{demo.HARNESS}",
                             "extra_step_cause": "task_did_not_serve_goal",
                             "observed_cause": "task_did_not_serve_goal", "fault": "planner",
                             "role": "planner", "node_id": "planner", "mapping_version": 1,
                             "flags": []})
    [legacy] = [r for t in build_usage_report(d.db)["tasks"] for r in t["blame_ledger"]
                if r["extra_step_cause"] == "task_did_not_serve_goal"]
    assert (legacy["fault"], legacy["captured_fault"], legacy["mapping_version"]) == (
        "unattributed", "planner", 1)

    dan = _W12Dan()
    assert _w12_run(d, fx.iso(10), dan)["asked"] == ["IP-0001"]
    [miss] = _w12_impacts(d)
    assert (miss["goal"], miss["attribution"]) == ("G1", "unattributed")
    assert "planner" not in {r.get("attribution") for r in _w12_observations(d.store)}
    dan.answers["dq-improve-IP-0001"] = "Decline"
    _w12_poll(d, dan, fx.iso(11))
    assert _w12_proposal(d)["state"] == "declined"

    # The same pattern: 4 rows in 2 tasks, only the third round's 2 recorded after the decline.
    _w12_task(d, "ac-1-retry", 10.5)
    _w12_task(d, "ac-1-third", 12)
    out = _w12_run(d, fx.iso(20), dan)
    [suppressed] = out["suppressed"]
    assert suppressed["of"] == "IP-0001" and "declined" in suppressed["reason"]
    assert [s["id"] for s in dan.asked] == ["dq-improve-IP-0001"]
    # 4 rows recorded after the decline are new evidence: asked again, naming them.
    _w12_task(d, "polish-1-a", 21)
    _w12_task(d, "polish-1-b", 22)
    out = _w12_run(d, fx.iso(30), dan)
    assert out["suppressed"] == [] and out["asked"] == ["IP-0002"]
    again = _w12_proposal(d, "IP-0002")
    assert again["new_evidence"] == {"since": "IP-0001", "at": fx.iso(11), "rows": 4}
    [spec] = [s for s in dan.asked if s["id"] == "dq-improve-IP-0002"]
    assert "new since you declined IP-0001" in spec["spec"]["question"]
    assert [(r["id"], r["state"]) for r in imp.read(d.repo)["proposals"]] == [
        ("IP-0001", "declined"), ("IP-0002", "asked")]


@pytest.mark.parametrize("after", ["cheaper", "cheaper_but_a_defect_escaped"])
def test_an_approved_lever_s_effect_is_measured_before_and_after_on_the_demo(w12, after):
    """C33, R59 on Recipe Box: Dan approves raising the reviewer's strength after 5
    accepted tasks; 5 accepted tasks later the next run measures the paired windows. The
    effect reports cost per pool, false holds, time to outcome and quality, and no metric
    counts green criteria. Cheaper with no defect is `improved`; cheaper while a defect
    escaped review is `worse`, and Dan is asked to roll back (a question that holds
    nothing), never rolled back by itself."""
    d = w12()
    before_tasks = (*_W12_TASKS, "ac-1-retry")
    for hour, task in enumerate(before_tasks, start=1):
        _w12_task(d, task, hour)
    dan, told = _W12Dan(), []
    assert _w12_run(d, fx.iso(6), dan)["asked"] == ["IP-0001"]
    dan.answers["dq-improve-IP-0001"] = "Approve"
    _w12_poll(d, dan, fx.iso(7))
    assert "strength: strong" in d.config.read_text(encoding="utf-8")
    after_tasks = [f"polish-1-{c}" for c in "abcde"]
    for hour, task in enumerate(after_tasks, start=8):
        escaped = after != "cheaper" and hour == 10
        _w12_after(d, task, hour, causes=[("escaped_defect", "reviewer")] if escaped else [])
    out = _w12_run(d, fx.iso(20), dan, told=told)

    row = _w12_proposal(d)
    effect = row["effect"]
    assert row["state"] == "measured"
    assert effect["windows"]["before"]["tasks"] == list(before_tasks)   # in finish order
    assert effect["windows"]["after"]["tasks"] == after_tasks
    metrics = effect["metrics"]
    for key in ("cost_units_per_task:codex-subscription", "false_hold_hours",
                "time_to_outcome_hours", "quality_defects_per_task",
                "extra_steps_per_task:reviewer/missed_earlier"):
        assert key in metrics, key
    cost = metrics["cost_units_per_task:codex-subscription"]
    assert cost["after"] < cost["before"]
    assert not [k for k in metrics if "green" in k.lower()]
    [line] = [m["line"] for m in out["measured"]]
    [digest] = [t for t in told if t.startswith("📈")]
    for part in ("cost units/task", "false holds", "time to outcome", "quality defects/task"):
        assert part in line and part in digest, part
    assert "green" not in digest.lower()
    rollback = [s for s in dan.asked if s["id"] == "dq-improve-rollback-IP-0001"]
    if after == "cheaper":
        assert effect["verdict"] == "improved" and rollback == []
    else:
        assert effect["verdict"] == "worse"
        assert metrics["quality_defects_per_task"]["after"] > \
            metrics["quality_defects_per_task"]["before"]
        [asked] = rollback
        assert asked["holds"] is False
        assert [o["label"] for o in asked["spec"]["options"]] == ["Roll back", "Keep"]
    assert "strength: strong" in d.config.read_text(encoding="utf-8")   # never by itself


def test_the_provenance_cross_checks_are_real(tmp_path):
    """GDT-14 review L4: the bundle's fixture hash is checked against the committed tree,
    not against itself recomputed; it names the Maestro the gate's interpreter imports
    (through the venv's editable install, the root checkout even in a worktree run); and
    a finish with nothing met is not "independent"."""
    tree = demo.git(demo.REPO_ROOT, "rev-parse", "HEAD:tests/fixtures/goals/demo")
    clean = not demo.git(demo.REPO_ROOT, "status", "--porcelain", "--",
                         "tests/fixtures/goals/demo")
    assert (demo.tree_sha256(tree) == demo.fixture_sha256()) == clean
    assert demo.tree_sha256(tree) != demo.tree_sha256(
        demo.git(demo.REPO_ROOT, "rev-parse", "HEAD:tests/fixtures/goals/demo/overlays"))

    gate = demo.gate_maestro(sys.executable, tmp_path)
    assert gate["interpreter"] == sys.executable
    assert gate["package"] == str(Path(gate["root"]) / "maestro")
    assert (Path(gate["package"]) / "acceptance.py").is_file()
    assert demo.git(Path(gate["root"]), "rev-parse", "HEAD") == gate["commit"]

    assert demo.independence([], [])["independent"] is False
    assert demo.independence(["finish", "operator"], [])["independent"] is True
    assert demo.independence(["finish", "gate"], [])["independent"] is False


def _param_ids(test: ast.FunctionDef) -> Optional[set[str]]:
    """A test's parametrize ids when they are literal strings (`ids=` or the values of
    one argument); `None` when they cannot be read without running pytest."""
    for deco in test.decorator_list:
        if not (isinstance(deco, ast.Call) and getattr(deco.func, "attr", "") == "parametrize"):
            continue
        ids = next((k.value for k in deco.keywords if k.arg == "ids"),
                   deco.args[1] if len(deco.args) > 1 else None)
        if isinstance(ids, (ast.List, ast.Tuple)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in ids.elts):
            return {e.value for e in ids.elts}
        return None
    return None


_TESTS: dict[Path, dict[str, ast.FunctionDef]] = {}


def _node_exists(node: str) -> bool:
    """A pytest node id names a module-level test in its file, and its `[id]` one of that
    test's literal parametrize ids (GDT-14 review L8)."""
    file, _, name = node.partition("::")
    path = demo.REPO_ROOT / file
    if not path.is_file():
        return False
    if path not in _TESTS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _TESTS[path] = {n.name: n for n in tree.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name.startswith("test")}
    base, bracket, param = name.partition("[")
    test = _TESTS[path].get(base)
    if test is None:
        return False
    if not bracket:
        return True
    ids = _param_ids(test)
    return ids is not None and param.endswith("]") and param[:-1] in ids


def test_the_c_map_check_rejects_a_typo_d_parametrize_id_or_a_nested_name():
    """GDT-14 review L8: the check stripped a node's `[id]` and matched any function in the
    file, nested ones included, so `[skil]` or a helper's name passed as a test."""
    node = _D_NODE + "test_the_demo_contract_is_the_grill_s_contract_plus_setup_s_bindings"
    assert _node_exists(node + "[skill]") and _node_exists(node + "[drive]")
    assert not _node_exists(node + "[skil]")
    assert not _node_exists(_D_NODE + "graduate")          # nested in a test
    assert not _node_exists(_D_NODE + "_node_exists")      # not a test
    assert _node_exists("tests/test_acceptance.py::"
                        "test_an_or_equal_oracle_passes_at_the_threshold_itself")


_TRACEABILITY = demo.REPO_ROOT / "docs" / "agent-plans" / "p13-goal-driven-tdd" / "TRACEABILITY.md"
_D_NODE = "tests/test_goals_demo.py::"


def _kind(entry) -> str:
    """`demo` (a GDT-14 test on Recipe Box), `reuse` (existing tests only), `partial` (a
    named limit with the tests of what is built), `limit` (not built)."""
    if isinstance(entry, dict):
        return "partial" if entry.get("tests") else "limit"
    ours = ("tests/test_goals_demo.py::", "tests/graph_engineering/test_goals_demo_lanes.py::")
    return "demo" if any(n.startswith(ours) for n in entry) else "reuse"


def test_traceability_s5_says_what_the_c_map_proves():
    """GDT-14 review M1: TRACEABILITY §5 is the C-map in prose; each row's kind, every test
    it names (L:A / L:B for the demo lanes) and the tally say exactly what `C_MAP` holds,
    so a row cannot claim a demo proof (or a full one) the map does not have."""
    text = _TRACEABILITY.read_text(encoding="utf-8")
    section = text.split("## 5. C1–C33 system scenarios → tests", 1)[1]
    rows = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
        r"^\| (C\d+) \| (\w+) \| (.*) \|$", section, re.M)}
    assert set(rows) == set(demo.C_MAP)
    tally: dict[str, list[str]] = {}
    for cid, entry in demo.C_MAP.items():
        kind, cell = rows[cid]
        assert kind == _kind(entry), f"{cid}: §5 says {kind}, C_MAP is {_kind(entry)}"
        tally.setdefault(kind, []).append(cid)
        nodes = entry if isinstance(entry, list) else list(entry.get("tests") or ())
        for node in nodes:
            name = node.partition("::")[2]
            token = {demo._A: "L:A", demo._B: "L:B"}.get(node, name)
            assert token in cell, f"{cid}: §5 does not name {token}"
        if isinstance(entry, dict):
            assert f"Owner: {entry['owner']}" in cell, cid
    found = re.search(r"Tally:\s(\d+)\sC-ids\swith\sdemo\stests\s.*?,\s(\d+)\sreuse-only\s.*?,"
                      r"\s(\d+)\spartial\s.*?,\s(\d+)\slimits?\s", section, re.S)
    assert found, "§5 has no tally line"
    assert [int(n) for n in found.groups()] == [
        len(tally.get(k, ())) for k in ("demo", "reuse", "partial", "limit")]


def test_every_c_scenario_maps_to_a_test_or_a_named_limit():
    """R64: C1-C33 each name the tests that prove them (on the demo or an existing system
    test) or a limit with its owner; every named test exists, so the map cannot rot the
    way a renamed test once did in TRACEABILITY."""
    assert set(demo.C_MAP) == {f"C{n}" for n in range(1, 34)}
    for cid, entry in demo.C_MAP.items():
        nodes = entry if isinstance(entry, list) else list(entry.get("tests") or ())
        if isinstance(entry, dict):
            assert entry.get("limit") and entry.get("owner"), cid
        assert nodes or isinstance(entry, dict), cid
        for node in nodes:
            assert _node_exists(node), f"{cid}: {node}"
