"""GDT-4: ROADMAP contribution, the RED-baseline task proposal and the feasibility gate
(R20, R26, R27, R30, R32; `02` C1.5, C2.11, C3.16).

A task says how it contributes to each goal it serves: `direct` (it should move the goal's
criteria), `enabling` (a verifiable prerequisite, e.g. a benchmark harness) or `maintenance`.
A phase's `serves` inherited by its children is a suggestion: a child that does not say how
it contributes is unclassified, never assumed `direct`. Setup turns the stored acceptance
baseline into proposed tasks (an enabling harness task before direct work on a criterion
whose test cannot run yet), and the feasibility gate refuses a plan in which a mandatory
criterion that is not GREEN has no open task naming it.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from maestro import acceptance, goal_roadmap, taskgraph
from maestro.docs import goals

from .test_goals import SKILL_STYLE, _project
from .hang_bound import HANG_SECONDS
from .test_goals_contract import V2


def _texts(problems, severity=None):
    return [p.text for p in problems if severity is None or p.severity == severity]


def _results(revision=2):
    r = acceptance.Result
    return [
        r("AC-01", acceptance.GREEN, "passed", revision, ("G1",), ("S-01",)),
        r("AC-02", acceptance.RED, "test_refused: assertion failed: AssertionError", revision,
          ("G1",), ("S-02",)),
        r("AC-03", acceptance.BLOCKED, "ModuleNotFoundError: No module named 'ledger'",
          revision, ("G2",), ("S-01",)),
        r("AC-04", acceptance.UNKNOWN, "no test bound yet", revision, ("G3",), ("S-03",)),
        r("OP-01", acceptance.OPERATOR, "Dan: open the summary", revision, ("G2",),
          ("S-01",)),
    ]


# --- contribution (R27, R32) ---------------------------------------------------------------


def test_contribution_is_per_goal_and_one_value_applies_to_every_served_goal():
    assert goals.task_contribution({"serves": ["G1", "G2"], "contribution": "direct"}) == {
        "G1": "direct", "G2": "direct"}
    assert goals.task_contribution({"serves": "G1, G3",
                                    "contribution": {"G1": "direct", "G3": "enabling"}}) == {
        "G1": "direct", "G3": "enabling"}
    assert goals.task_contribution({"serves": [], "contribution": "maintenance"}) == {}
    assert goals.task_contribution({"serves": ["G1"]}) == {}


def test_a_malformed_contribution_or_tests_list_holds_the_task():
    doc = goals.parse(V2)
    cases = {
        "a": ({"serves": ["G1"], "contribution": "huge"},
              "task a contribution 'huge' is not direct, enabling or maintenance"),
        "b": ({"serves": ["G1"], "contribution": {"G1": "direct", "G3": "enabling"}},
              "task b contribution names G3, which it does not serve"),
        "c": ({"serves": [], "contribution": "direct"},
              "task c serves no goal, so its contribution can only be maintenance"),
        "d": ({"serves": ["G1"], "contribution": "direct", "tests": 5},
              "task d tests must be a list of test ids or commands"),
    }
    for tid, (task, text) in cases.items():
        held = goals.task_gate(doc, {"id": tid, **task})
        assert _texts(held) == [text]
        assert {p.severity for p in held} == {"gate"}


def test_an_unclassified_contribution_and_missing_evidence_are_drafts_not_holds():
    doc = goals.parse(V2)
    bare = {"id": "a", "serves": ["G1"]}
    direct = {"id": "b", "serves": ["G1"], "contribution": "direct", "evidence": "AC-01 green"}
    partial = {"id": "c", "serves": ["G1", "G2"], "contribution": {"G1": "enabling"},
               "acceptance_criteria": ["the fixtures load"]}
    assert goals.task_gate(doc, bare) == goals.task_gate(doc, direct) == ()
    drafts = _texts(goals.diagnose(doc, [bare, direct, partial]), "draft")
    assert "task a serves G1 but names no contribution (direct, enabling or maintenance)" in drafts
    assert "task a names no evidence of done (`evidence:` or `acceptance_criteria:`)" in drafts
    assert "task b is direct for G1 but names no criterion or test it should turn GREEN" in drafts
    assert "task c names no contribution for G2" in drafts
    assert not any(t.startswith("task c names no evidence") for t in drafts)
    # Format 1 projects are never asked for the format-2 task fields.
    old = goals.diagnose(goals.parse(SKILL_STYLE), [{"id": "a", "serves": ["G1"]}])
    assert not any("contribution" in t or "evidence" in t for t in _texts(old))


def test_a_phase_serving_g1_does_not_make_every_child_direct():
    """C5 / R26: children of a phase that serves G1 inherit `serves: [G1]` as a suggestion;
    each says how it contributes, and one that does not is unclassified, not `direct`."""
    doc = goals.parse(V2)
    children = [
        {"id": "fixtures", "serves": ["G1"], "contribution": "enabling", "criteria": ["AC-01"]},
        {"id": "deps", "serves": ["G1"], "contribution": "maintenance"},
        {"id": "inherited", "serves": ["G1"]},
    ]
    assert goals.contribution_map(doc, children) == {
        "G1": {"enabling": ["fixtures"], "maintenance": ["deps"], "unclassified": ["inherited"]},
        "G2": {}, "G3": {}}
    assert all(goals.task_gate(doc, c) == () for c in children)


def test_a_benchmark_harness_task_is_enabling_for_its_goal():
    """C16 / R27: building the benchmark is enabling work for G3; it moves no metric."""
    doc = goals.parse(V2)
    bench = {"id": "bench", "serves": ["G3"], "contribution": "enabling",
             "criteria": ["AC-04"], "evidence": "`python bench.py` prints a median"}
    assert goals.contribution_map(doc, [bench])["G3"] == {"enabling": ["bench"]}
    assert goals.task_gate(doc, bench) == ()
    assert not any(p.task == "bench" for p in goals.diagnose(doc, [bench]))


def test_completed_tasks_are_not_in_the_contribution_map():
    doc = goals.parse(V2)
    done = {"id": "x", "serves": ["G1"], "contribution": "direct", "status": "complete"}
    assert goals.contribution_map(doc, [done])["G1"] == {}


# --- the RED baseline becomes proposed tasks (R20, C11) ------------------------------------


def test_a_red_baseline_becomes_direct_tasks_after_an_enabling_harness_where_needed():
    doc = goals.parse(V2.replace("- **Evidence:** test\n\n### AC-03",
                                 "- **Evidence:** test\n- **Test:** `pytest tests/acceptance/"
                                 "test_import.py::test_refused`\n\n### AC-03"))
    proposed = goal_roadmap.propose_tasks(doc, _results())
    assert [t["id"] for t in proposed] == [
        "ac-02-broken-export-is-refused",
        "ac-03-harness", "ac-03-totals-match-the-statement",
        "ac-04-harness", "ac-04-a-year-imports-in-under-10-seconds"]
    by_id = {t["id"]: t for t in proposed}
    red = by_id["ac-02-broken-export-is-refused"]
    assert (red["serves"], red["contribution"], red["criteria"], red["deps"]) == (
        ["G1"], "direct", ["AC-02", "S-02"], [])
    assert red["tests"] == ["pytest tests/acceptance/test_import.py::test_refused"]
    assert "AC-02 green_already_satisfied at contract revision 2" in red["evidence"]
    harness = by_id["ac-03-harness"]
    assert (harness["serves"], harness["contribution"], harness["criteria"]) == (
        ["G2"], "enabling", ["AC-03"])
    assert "ModuleNotFoundError" in harness["short_desc"]
    assert by_id["ac-03-totals-match-the-statement"]["deps"] == ["ac-03-harness"]
    # Every proposal passes the execution gate and the drafting checks on tasks.
    assert all(goals.task_gate(doc, t) == () for t in proposed)
    assert not any(p.task for p in goals.diagnose(doc, proposed))
    # GREEN stays as regression protection and an operator check is Dan's: no task for either.
    assert not any("AC-01" in t["criteria"] or "OP-01" in t["criteria"] for t in proposed)


def test_proposed_blocks_parse_back_as_roadmap_tasks():
    proposed = goal_roadmap.propose_tasks(goals.parse(V2), _results())
    parsed = taskgraph.scan_tasks("# ROADMAP\n\n" + goal_roadmap.render_tasks(proposed))
    assert [(t["id"], t["serves"], t["contribution"]) for t in parsed] == [
        (t["id"], t["serves"], t["contribution"]) for t in proposed]


def test_a_proposed_block_without_scope_asks_for_one_and_still_parses():
    """GDT-14 review M2a: a proposal cannot know which files its task writes (its bound
    test is the one file it must not edit), and a task without `scope:` runs alone (D20).
    The block setup shows Dan says so on the block itself; a block with a scope does not."""
    proposed = goal_roadmap.propose_tasks(goals.parse(V2), _results())
    proposed[0] = {**proposed[0], "scope": ["app.py"]}
    text = goal_roadmap.render_tasks(proposed, scope_hint=True)
    blocks = text.split("```yaml\n")[1:]
    assert all(goal_roadmap.SCOPE_HINT in b for b in blocks[1:])
    assert goal_roadmap.SCOPE_HINT not in blocks[0]
    assert "runs alone" in goal_roadmap.SCOPE_HINT
    parsed = taskgraph.scan_tasks("# ROADMAP\n\n" + text)
    assert [t["id"] for t in parsed] == [t["id"] for t in proposed]
    assert "scope" not in parsed[1]
    assert goal_roadmap.SCOPE_HINT not in goal_roadmap.render_tasks(proposed[1:])


def test_a_proposal_skips_named_criteria_and_needs_a_current_baseline():
    doc = goals.parse(V2)
    open_task = {"id": "mine", "serves": ["G1"], "contribution": "direct",
                 "criteria": ["AC-02"]}
    ids = [t["id"] for t in goal_roadmap.propose_tasks(doc, _results(), [open_task])]
    assert not any(i.startswith("ac-02") for i in ids)
    with pytest.raises(ValueError, match=r"no acceptance baseline at contract revision 2 "
                                         r"\(run `maestro goals baseline`\)"):
        goal_roadmap.propose_tasks(doc, _results(revision=1))


def test_an_optional_criterion_gets_no_proposal():
    doc = goals.parse(V2.replace("- **Evidence:** metric\n",
                                 "- **Evidence:** metric\n- **Mandatory:** no\n"))
    assert not any("AC-04" in t["criteria"]
                   for t in goal_roadmap.propose_tasks(doc, _results()))


# --- the feasibility gate (R30, R69) -------------------------------------------------------


def test_feasibility_needs_an_open_task_naming_every_unmet_mandatory_criterion():
    doc = goals.parse(V2)
    problems = goal_roadmap.feasibility(doc, [], _results())
    assert _texts(problems) == [
        "AC-02 is red_expected_missing_behavior and no open task names it in `criteria:`",
        "AC-03 is blocked_environment and no open task names it in `criteria:`",
        "AC-04 is unknown and no open task names it in `criteria:`",
    ]
    assert {p.severity for p in problems} == {"gate"}
    assert problems[0].ids == ("AC-02", "G1")
    assert goal_roadmap.feasibility(doc, goal_roadmap.propose_tasks(doc, _results()),
                                    _results()) == []


def test_metadata_completed_or_direct_only_work_is_not_a_path():
    doc = goals.parse(V2)
    tasks = [
        {"id": "meta", "serves": ["G1"], "contribution": "direct"},          # no criteria
        {"id": "done", "serves": ["G1"], "contribution": "direct", "criteria": ["AC-02"],
         "status": "complete"},
        {"id": "direct-only", "serves": ["G2"], "contribution": "direct",
         "criteria": ["AC-03"]},
        {"id": "bench", "serves": ["G3"], "contribution": "enabling", "criteria": ["AC-04"]},
    ]
    assert _texts(goal_roadmap.feasibility(doc, tasks, _results())) == [
        "AC-02 is red_expected_missing_behavior and no open task names it in `criteria:`",
        "AC-03 is blocked_environment: no open enabling task makes its test runnable before "
        "the direct work",
    ]


def test_feasibility_without_a_current_baseline_and_for_format_one():
    doc = goals.parse(V2)
    assert _texts(goal_roadmap.feasibility(doc, [], _results(revision=1))) == [
        "no acceptance baseline at contract revision 2 (run `maestro goals baseline`)"]
    assert _texts(goal_roadmap.feasibility(doc, [], [])) == [
        "no acceptance baseline at contract revision 2 (run `maestro goals baseline`)"]
    assert goal_roadmap.feasibility(goals.parse(SKILL_STYLE), [], []) == []


def test_doctor_fails_an_infeasible_plan_and_warns_on_a_feasible_one(tmp_path):
    from maestro import cli

    doc = goals.parse(V2)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    infeasible = _project(tmp_path / "a", goals_md=V2, tasks=[{"id": "polish", "serves": []}])
    acceptance.store(infeasible, doc, _results())
    check = cli._check_goals(infeasible)
    assert (check.ok, check.required) == (False, True)
    assert check.detail.startswith(
        "not feasible: AC-02 is red_expected_missing_behavior and no open task names it")

    tasks = goal_roadmap.propose_tasks(doc, _results())
    feasible = _project(tmp_path / "b", goals_md=V2, tasks=tasks)
    acceptance.store(feasible, doc, _results())
    check = cli._check_goals(feasible)
    assert check.required is False and "not feasible" not in check.detail


def test_doctor_passes_once_the_gate_observed_the_named_criterion_green(tmp_path):
    """DESIGN §19.4's false FAIL (R30, R35): the direct task naming AC-02 graduated and the
    gate observed AC-02 GREEN, but the baseline still says RED and no open task names it.
    The doctor reads the baseline overlaid by the gate's observations (`goal_state`)."""
    from datetime import datetime, timedelta, timezone

    from maestro import cli, goal_state

    doc = goals.parse(V2)
    tasks = goal_roadmap.propose_tasks(doc, _results())
    done = [dict(t, status="complete") if "AC-02" in t["criteria"] else t for t in tasks]
    project = _project(tmp_path, goals_md=V2, tasks=done)
    acceptance.store(project, doc, _results())
    check = cli._check_goals(project)
    assert check.required is True
    assert "AC-02 is red_expected_missing_behavior and no open task names it" in check.detail
    after = (datetime.now(tz=timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
    goal_state.record_gate(project, doc, {"acceptance": {"AC-01": acceptance.GREEN,
                                                         "AC-02": acceptance.GREEN},
                                          "snapshot": "abc1234"},
                           task="ac-02-broken-export-is-refused", run_id="run-1", at=after)
    check = cli._check_goals(project)
    assert check.required is False and "not feasible" not in check.detail, check.detail


# --- `maestro goals tasks` -----------------------------------------------------------------


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "maestro.cli", "goals", *args],
                          capture_output=True, text=True, timeout=HANG_SECONDS)


def test_goals_tasks_cli_prints_proposals_from_the_current_baseline_only(tmp_path):
    project = _project(tmp_path, goals_md=V2, tasks=[{"id": "polish", "serves": []}])
    roadmap = (project / "docs" / "ROADMAP.md").read_text()
    run = _cli("tasks", str(project))
    assert run.returncode == 1 and "run `maestro goals baseline`" in run.stderr

    acceptance.store(project, goals.parse(V2), _results())
    run = _cli("tasks", str(project))
    assert run.returncode == 0, run.stderr
    assert "```yaml\nid: ac-02-broken-export-is-refused\n" in run.stdout
    as_json = json.loads(_cli("tasks", str(project), "--json").stdout)
    assert as_json[0]["id"] == "ac-02-broken-export-is-refused"
    assert (project / "docs" / "ROADMAP.md").read_text() == roadmap     # never written


def test_goals_tasks_prints_every_block_with_the_check_the_graph_runs(tmp_path):
    """GDT-10 follow-up M-A: a pasted block without `verifications` is refused by the graph
    (INV-04) and wedges the run, so each printed task carries `goal_roadmap.verifications`
    (here AC-02 is unbound: Dan's `kind: dan` check; AC-01's node would be a pytest run)."""
    project = _project(tmp_path, goals_md=V2, tasks=[{"id": "polish", "serves": []}])
    acceptance.store(project, goals.parse(V2), _results())
    doc = goals.parse(V2)
    as_json = json.loads(_cli("tasks", str(project), "--json").stdout)
    assert as_json
    for task in as_json:
        bare = {k: v for k, v in task.items() if k != "verifications"}
        assert task.get("verifications") == goal_roadmap.verifications(bare, doc, project)
    printed = _cli("tasks", str(project)).stdout
    assert printed.count("verifications:") == printed.count("```yaml\n") == len(as_json)
    assert [t["id"] for t in taskgraph.scan_tasks(printed)] == [t["id"] for t in as_json]
