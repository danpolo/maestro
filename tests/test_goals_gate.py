"""GDT-2: the goals contract's two validation modes (R29, R31, R69; `02` C1.4).

Drafting is advisory: every problem is listed. Execution is strict for the work a problem
affects: a `gate` problem holds the tasks that touch its ids and nothing else, so a broken
G3 never stops G1 work or `serves: []` maintenance. Coverage (a goal no task serves, a goal
nothing runnable verifies) is surfaced before execution, as drafts until GDT-4's
feasibility gate.
"""
from __future__ import annotations

from maestro import acceptance
from maestro.docs import goals

from .test_goals import SKILL_STYLE, _parse, _project
from .test_goals_contract import V2

NO_G3_TARGET = V2.replace("- **Target:** under 10 seconds for a year of exports.\n", "")
NO_REVISION = V2.replace("Contract revision: 2 (approved 2026-10-17 by Dan)\n", "")
NO_G1_BASELINE = V2.replace("- **Baseline:** not_applicable\n", "", 1)


def _texts(problems, severity=None):
    return [p.text for p in problems if severity is None or p.severity == severity]


# --- task classification (R29) ------------------------------------------------------------


def test_missing_empty_unknown_and_malformed_serves_are_told_apart():
    doc = goals.parse(V2)
    problems = {
        tid: goals.task_problems(doc, task) for tid, task in (
            ("none", {"id": "none"}),
            ("polish", {"id": "polish", "serves": []}),
            ("unknown", {"id": "unknown", "serves": ["G9"]}),
            ("odd", {"id": "odd", "serves": ["G1", "five"]}),
            ("crit", {"id": "crit", "serves": [], "criteria": ["AC-09", "S-01"]}),
            ("fine", {"id": "fine", "serves": "G1, G2", "criteria": "AC-01"}),
        )
    }
    assert _texts(problems["none"]) == [
        "task none names no goal it serves (add `serves:`; `serves: []` for polish)"]
    assert problems["polish"] == () and problems["fine"] == ()
    assert _texts(problems["unknown"]) == ["task unknown serves unknown goal G9"]
    assert _texts(problems["odd"]) == ["task odd serves entry 'five' is not a goal id"]
    assert _texts(problems["crit"]) == ["task crit names unknown criterion AC-09"]
    flat = [p for ps in problems.values() for p in ps]
    assert {p.severity for p in flat} == {"gate"}
    assert all(p.task == tid for tid, ps in problems.items() for p in ps)


def test_task_links_name_the_served_goals_the_named_criteria_and_their_goals():
    doc = goals.parse(V2)
    assert goals.task_links(doc, {"serves": ["G1"]}) == {"G1"}
    assert goals.task_links(doc, {"serves": []}) == set()
    assert goals.task_links(doc, {"serves": [], "criteria": ["AC-04"]}) == {"AC-04", "G3"}
    assert goals.task_links(doc, {"criteria": ["S-01", "OP-01"]}) == {"S-01", "OP-01",
                                                                     "G1", "G2"}


# --- draft vs execution gate (R31) ---------------------------------------------------------


def test_one_invalid_contract_is_a_draft_listing_and_a_gate_on_the_affected_tasks_only():
    doc = goals.parse(NO_G3_TARGET)
    g3 = {"id": "speed", "serves": ["G3"]}
    g1 = {"id": "import", "serves": ["G1"]}
    polish = {"id": "polish", "serves": []}
    bench = {"id": "bench", "serves": [], "criteria": ["AC-04"]}
    listed = goals.diagnose(doc, [g3, g1, polish, bench])
    assert "G3: no target (ask Dan; never invented)" in _texts(listed, "gate")
    assert _texts(goals.task_gate(doc, g3)) == ["G3: no target (ask Dan; never invented)"]
    assert goals.task_gate(doc, g1) == ()
    assert goals.task_gate(doc, polish) == ()
    assert _texts(goals.task_gate(doc, bench)) == ["G3: no target (ask Dan; never invented)"]
    assert {p.task for p in goals.task_gate(doc, bench)} == {"bench"}


def test_a_queued_followups_task_serves_no_goal_and_is_not_held():
    """GDT-11 S1: a `before_dependents` follow-up task is maintenance (`serves: []`); before,
    it named no goal, so a format-2 gate held it and its dependents waited forever."""
    from maestro.docs import followups

    task = followups.build_task({"id": "import", "serves": ["G1"], "criteria": ["AC-01"]},
                                [followups.FollowUp("rename the field", 1, "proof_review")])
    assert task["serves"] == []
    doc = goals.parse(V2)
    assert goals.task_problems(doc, task) == ()
    assert goals.task_gate(doc, task) == ()


def test_a_polish_task_naming_area_tests_is_never_held_by_a_contract_problem_on_them():
    """GDT-11 (R28): a polish task names its area's criteria in `tests:`, never
    `criteria:`, so a contract problem on them (G3 has no target; AC-04 verifies G3) holds
    the task that names AC-04 in `criteria:` but never the polish task rerunning it."""
    from maestro import backlog

    item = {"id": "BL-0001", "text": "tidy the benchmark", "scope": None, "checks": [],
            "source": {"task": "bench", "round": 1, "node": "proof_review", "commit": ""},
            "criteria": ["AC-04"]}
    polish = backlog.polish_task(1, "a", [item])
    assert polish["tests"] == ["AC-04"] and "criteria" not in polish
    doc = goals.parse(NO_G3_TARGET)
    assert goals.task_links(doc, polish) == set()
    assert goals.task_problems(doc, polish) == ()
    assert goals.task_gate(doc, polish) == ()
    assert _texts(goals.task_gate(doc, {"id": "bench", "serves": [], "criteria": ["AC-04"]})) \
        == ["G3: no target (ask Dan; never invented)"]


def test_a_contract_wide_gate_problem_holds_goal_work_but_not_maintenance():
    doc = goals.parse(NO_REVISION)
    assert _texts(goals.task_gate(doc, {"id": "a", "serves": ["G1"]})) == [
        "no contract revision"]
    assert _texts(goals.task_gate(doc, {"id": "b", "serves": [], "criteria": ["AC-01"]})) == [
        "no contract revision"]
    assert goals.task_gate(doc, {"id": "polish", "serves": []}) == ()


def test_draft_problems_never_hold_a_task():
    doc = goals.parse(NO_G1_BASELINE)
    task = {"id": "import", "serves": ["G1"]}
    assert "G1: no baseline (a value, or not_applicable)" in _texts(
        goals.diagnose(doc, [task]), "draft")
    assert goals.task_gate(doc, task) == ()


def test_an_unclassified_task_is_held_even_by_a_clean_contract():
    doc = goals.parse(V2)
    assert _texts(goals.task_gate(doc, {"id": "b"})) == [
        "task b names no goal it serves (add `serves:`; `serves: []` for polish)"]


def test_execution_holds_skip_completed_tasks_and_keep_document_order():
    doc = goals.parse(NO_G3_TARGET)
    tasks = [{"id": "z", "serves": ["G3"]}, {"id": "ok", "serves": ["G1"]},
             {"id": "done", "status": "complete"}, {"id": "a"}]
    holds = goals.execution_holds(doc, tasks)
    assert list(holds) == ["z", "a"]
    assert goals.describe_holds(holds) == (
        "z (G3: no target (ask Dan; never invented)); "
        "a (task a names no goal it serves (add `serves:`; `serves: []` for polish))")


# --- coverage before execution (R69) -------------------------------------------------------


def test_coverage_flags_a_goal_without_a_task_and_a_goal_without_a_runnable_test():
    doc = goals.parse(V2)
    listed = goals.diagnose(doc, [{"id": "a", "serves": ["G1"]}, {"id": "c", "serves": ["G3"]}])
    drafts = _texts(listed, "draft")
    assert "goal G2 is served by no open or completed task" in drafts
    # G3 is served, but AC-04 has no test and G3 has no operator check: metadata only.
    assert "G3 has no bound test or operator check" in drafts
    assert not any(t.startswith(("G1 has", "G2 has")) for t in drafts)  # AC-01 test; OP-01
    for ac in ("AC-02", "AC-03", "AC-04"):
        assert f"{ac} has no test bound yet (setup binds one, GDT-3)" in drafts
    assert not any(t.startswith("AC-01") for t in drafts)
    assert _texts(listed, "gate") == []

    served = goals.diagnose(doc, [{"id": "a", "serves": ["G1"]}],
                            completed=[{"id": "x", "serves": ["G2", "G3"]}])
    assert not any("served by no" in t for t in _texts(served))


def test_a_scenario_proved_only_by_unit_criteria_is_a_draft_diagnostic():
    """GDT-10 (R25): the completion gate never takes unit-level proof alone for a scenario
    (it needs an e2e or integration criterion, or an operator check), so setup flags it
    early as a draft. A draft never holds a task."""
    unit = V2.replace("- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
                      "- **Layer:** unit\n- **Evidence:** test\n\n### AC-03")
    tasks = [{"id": "a", "serves": ["G1"], "contribution": "direct", "criteria": ["AC-02"]}]
    listed = goals.diagnose(goals.parse(unit), tasks)
    found = [p for p in listed if "only by unit-level criteria" in p.text]
    assert [(p.severity, p.text) for p in found] == [
        ("draft", "S-02 is proved only by unit-level criteria (no e2e/integration criterion "
                  "or operator check)")]
    assert "S-02" in found[0].ids
    assert goals.execution_holds(goals.parse(unit), tasks) == goals.execution_holds(
        goals.parse(V2), tasks)
    assert not any("unit-level" in t for t in _texts(goals.diagnose(goals.parse(V2), tasks)))


def test_format_one_coverage_is_unchanged():
    doc = _parse(SKILL_STYLE)
    tasks = [{"id": "a", "serves": ["G1"]}, {"id": "b"}, {"id": "polish", "serves": []},
             {"id": "c", "serves": ["G9"]}]
    completed = [{"id": "done", "serves": ["G2"]}]
    listed = goals.diagnose(doc, tasks, completed)
    assert _texts(listed) == goals.coverage(doc, tasks, completed)
    assert _texts(listed, "gate") == [
        "task b names no goal it serves (add `serves:`; `serves: []` for polish)",
        "task c serves unknown goal G9"]


# --- doctor: WARN while drafting, FAIL for execution-invalid state (R31) -------------------


def test_doctor_fails_when_a_task_would_be_held_and_warns_on_drafts_only(tmp_path):
    from maestro import cli

    held = cli._check_goals(_project(
        tmp_path, goals_md=NO_G3_TARGET,
        tasks=[{"id": "speed", "serves": ["G3"]}, {"id": "import", "serves": ["G1", "G2"]}]))
    assert (held.ok, held.required) == (False, True)
    assert held.detail.startswith(
        "blocks at execution: speed (G3: no target (ask Dan; never invented)); not feasible: "
        "no acceptance baseline at contract revision 2")
    assert "AC-02 has no test bound yet" in held.detail

    # GDT-4: a plan is only feasible with a current baseline; an all-GREEN one leaves drafts.
    feasible = _project(
        tmp_path, goals_md=V2,
        tasks=[{"id": "speed", "serves": ["G3"]}, {"id": "import", "serves": ["G1", "G2"]},
               {"id": "polish", "serves": []}])
    acceptance.store(feasible, goals.parse(V2), [
        acceptance.Result(c, acceptance.GREEN, "passed", 2)
        for c in ("AC-01", "AC-02", "AC-03", "AC-04")])
    drafts = cli._check_goals(feasible)
    assert (drafts.ok, drafts.required) == (False, False)
    assert "blocks at execution" not in drafts.detail
    assert "G3 has no bound test or operator check" in drafts.detail

    maintenance = cli._check_goals(_project(
        tmp_path, goals_md=NO_REVISION, tasks=[{"id": "polish", "serves": []}],
        completed=[{"id": "x", "serves": ["G1", "G2", "G3"]}]))
    # Maintenance is never held; the plan is still infeasible without a baseline (GDT-4).
    assert "blocks at execution" not in maintenance.detail
    assert maintenance.detail.startswith("not feasible: no acceptance baseline")
    assert "no contract revision" in maintenance.detail
