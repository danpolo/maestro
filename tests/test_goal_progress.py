"""The goals excerpt in every brief and the reviewer's `goal_progress` (P13 W11a, GDT-6).

`maestro.workflows.goal_progress`: what the runner adds to each agent's manifest, the
reviewer's rule, and how `review.json`'s `goal_progress` is read. The reviewer states an
impact and its evidence; the rule decides (R39). Six impacts (R38): `advanced`,
`enabled`, `unchanged_expected`, `no_progress_unexpected`, `regressed`, `unknown`; a
stored W11a `no_progress` reads as `no_progress_legacy`, never as an unexpected miss
(R62, C32). A diff shows an edit, not an outcome: `advanced` or `regressed` without a
test result, a measurement against its baseline, a trace or an operator observation is
`unknown` with a targeted follow-up (R40, R42). A mocked test is no evidence for an e2e or
integration criterion (R24). Task correctness is separate from goal impact: a correct
task that did not move its goal merges, and the miss is an unattributed observation, not
a planner fault (R37, R58). `serves: []` skips expected advancement only: a verified
regression on any goal blocks (R28, R41). Goal work the task did not ask for is a scope
finding through W9's out-of-scope path (R36). The lanes are in
`tests/graph_engineering/test_goals_lanes.py`.
"""
from __future__ import annotations

import pytest

from maestro import acceptance
from maestro.docs import goals
from maestro.workflows import goal_progress as gp
from maestro.workflows import scope_gate

from .test_goals import SKILL_STYLE
from .test_goals_contract import V2


def _repo(tmp_path, goals_md=SKILL_STYLE):
    (tmp_path / "docs").mkdir(exist_ok=True)
    if goals_md is not None:
        (tmp_path / "docs" / "GOALS.md").write_text(goals_md, encoding="utf-8")
    return tmp_path


# --- the manifest fields ------------------------------------------------------------------


def test_every_role_gets_the_excerpt_and_a_reviewer_gets_the_check(tmp_path):
    repo = _repo(tmp_path)
    fields, cut = gp.manifest_fields(repo, {"id": "t", "serves": ["G2"]}, role="implementer")
    assert cut == ""
    assert "This task serves: G2" in fields["goals_excerpt"]
    assert "goals_check" not in fields
    fields, _ = gp.manifest_fields(repo, {"id": "t", "serves": ["G2"]}, role="reviewer")
    from maestro import goal_conflicts

    # GDT-7: the conflict context `goal_conflicts.judge` reads, with Dan's decisions.
    assert fields["goals_check"] == {"mode": "served", "serves": ["G2"],
                                     "goal_ids": ["G1", "G2", "G3"], "contribution": {},
                                     "criteria": {}, "conflicts": goal_conflicts.context(
                                         goals.load(repo), "t", [])}


def test_the_reviewer_check_carries_contribution_criteria_and_baseline(tmp_path):
    repo = _repo(tmp_path, V2)
    doc = goals.load(repo)
    acceptance.store(repo, doc, [
        acceptance.Result(id="AC-01", state=acceptance.GREEN, reason="passed", revision=2),
        acceptance.Result(id="AC-02", state=acceptance.RED, reason="assert", revision=1),
    ])
    task = {"id": "t", "serves": ["G1"], "contribution": "direct"}
    check = gp.manifest_fields(repo, task, role="reviewer")[0]["goals_check"]
    assert check["contribution"] == {"G1": "direct"}
    assert check["criteria"]["AC-01"] == {
        "goals": ["G1"], "layer": "e2e", "evidence": "test",
        "test": "pytest tests/acceptance/test_import.py::test_valid",
        "baseline": acceptance.GREEN, "direction": None}
    # an older revision's result closes nothing (R67)
    assert check["criteria"]["AC-02"]["baseline"] is None
    assert check["criteria"]["AC-04"]["evidence"] == "metric"


def test_check_mode_follows_serves(tmp_path):
    repo = _repo(tmp_path)
    modes = {
        name: gp.manifest_fields(repo, task, role="reviewer")[0]["goals_check"]["mode"]
        for name, task in (("missing", {"id": "t"}), ("empty", {"id": "t", "serves": []}))
    }
    assert modes == {"missing": "regressions", "empty": "skip"}


def test_a_project_without_goals_md_adds_nothing(tmp_path):
    assert gp.manifest_fields(_repo(tmp_path, None), {"id": "t", "serves": ["G1"]},
                              role="reviewer") == ({}, "")


# --- the brief -----------------------------------------------------------------------------


def test_brief_section_names_the_excerpt_for_every_role():
    text = gp.render({"goals_excerpt": "Purpose: x\nThis task serves: G1"}, role="implementer")
    assert text.startswith("GOALS (docs/GOALS.md excerpt; why this project exists")
    assert "This task serves: G1" in text
    assert "goal_progress" not in text


def test_reviewer_rule_asks_impact_and_evidence_and_keeps_the_d23_bound():
    manifest = {"goals_excerpt": "Purpose: x",
                "goals_check": {"mode": "served", "serves": ["G1", "G3"],
                                "goal_ids": ["G1", "G2", "G3"]}}
    text = gp.render(manifest, role="reviewer")
    assert "GOAL PROGRESS" in text and '"goal_progress"' in text and "G1, G3" in text
    for impact in gp.IMPACTS:
        assert impact in text, impact
    for kind in gp.EVIDENCE_KINDS:
        assert f'"kind": "{kind}"' in text, kind
    assert "a diff shows an edit, not an outcome" in text
    assert '"mocked": true' in text
    assert '"unasked_goal_work"' in text
    assert "never widens this task's scope" in text
    assert "does not send the task back" in text
    assert "no_progress" not in text.replace("no_progress_unexpected", "")


def test_reviewer_brief_says_which_criteria_were_red_on_the_base_too():
    """GDT-9 verification L5: a reviewer who writes its own failing-test finding is not
    overruled, so its brief says the criterion was already RED on the base."""
    manifest = {"goals_excerpt": "Purpose: x",
                "goals_check": {"mode": "served", "serves": ["G1"], "goal_ids": ["G1"],
                                "criteria": {"AC-01": {"baseline": acceptance.GREEN},
                                             "AC-02": {"baseline": acceptance.GREEN,
                                                        "red_on_base": True}}}}
    text = gp.render(manifest, role="reviewer")
    assert "AC-02 was RED on the base too" in text and "AC-01 was RED" not in text
    plain = dict(manifest, goals_check=dict(manifest["goals_check"], criteria={}))
    assert "RED on the base" not in gp.render(plain, role="reviewer")


def test_reviewer_rule_for_a_task_without_serves_asks_only_for_regressions():
    manifest = {"goals_excerpt": "Purpose: x",
                "goals_check": {"mode": "regressions", "serves": [], "goal_ids": ["G1"]}}
    text = gp.render(manifest, role="reviewer")
    assert "only for a goal the change makes worse" in text


def test_serves_empty_still_asks_for_regressions():
    manifest = {"goals_excerpt": "Purpose: x",
                "goals_check": {"mode": "skip", "serves": [], "goal_ids": ["G1"]}}
    text = gp.render(manifest, role="reviewer")
    assert "serves no goal (serves: [])" in text
    assert "only for a goal the change makes worse" in text


# --- reading review.json: the six impacts (R38) ---------------------------------------------


CRITERIA = {
    "AC-01": {"goals": ["G1"], "layer": "e2e", "evidence": "test",
              "test": "`pytest tests/acceptance/test_import.py::test_valid`",
              "baseline": acceptance.GREEN},
    "AC-02": {"goals": ["G1"], "layer": "e2e", "evidence": "test",
              "test": "`pytest tests/acceptance/test_import.py::test_refused`",
              "baseline": acceptance.RED},
    "AC-03": {"goals": ["G2"], "layer": "unit", "evidence": "test",
              "test": "tests/test_totals.py::test_match", "baseline": acceptance.RED},
    "AC-04": {"goals": ["G3"], "layer": "e2e", "evidence": "metric",
              "test": "`python bench.py`", "baseline": acceptance.GREEN, "direction": "below"},
}
SERVED = {"mode": "served", "serves": ["G1", "G2"], "goal_ids": ["G1", "G2", "G3"],
          "contribution": {"G1": "direct", "G2": "enabling"}, "criteria": CRITERIA}
PASSED_AC02 = {"kind": "test", "id": "tests/acceptance/test_import.py::test_refused",
               "result": "passed"}
FAILED_AC02 = dict(PASSED_AC02, result="failed")
FAILED_AC01 = {"kind": "test", "criterion": "AC-01", "id": "test_valid", "result": "failed"}
BENCH = {"kind": "metric", "name": "import seconds", "value": 12.0, "baseline": 8.0,
         "criterion": "AC-04"}


def _impacts(read):
    return [(r["goal"], r["impact"]) for r in read.records]


def test_each_impact_with_its_evidence_is_kept():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "advanced", "evidence": [PASSED_AC02]},
        {"goal": "G2", "impact": "enabled",
         "evidence": [{"kind": "trace", "path": "logs/harness.txt"}]},
        {"goal": "G3", "impact": "regressed", "evidence": [BENCH]},
    ]}, SERVED)
    assert _impacts(read) == [("G1", "advanced"), ("G2", "enabled"), ("G3", "regressed")]
    assert [r["served"] for r in read.records] == [True, True, False]
    assert read.records[0]["evidence"] == [PASSED_AC02]
    assert read.shortfalls == [] and read.follow_ups == [] and read.flags == []
    [blocking] = read.blocking
    assert blocking.startswith("Goal G3 regressed: metric import seconds 12.0 against "
                               "baseline 8.0 (AC-04)")


def test_unchanged_expected_unknown_and_no_progress_unexpected_hold_nothing():
    check = dict(SERVED, serves=["G1", "G2", "G3"],
                 contribution={"G1": "direct", "G2": "maintenance", "G3": "direct"})
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "no_progress_unexpected", "evidence": [FAILED_AC02]},
        {"goal": "G2", "impact": "unchanged_expected", "evidence": "tidies the parser"},
        {"goal": "G3", "impact": "unknown", "evidence": "no benchmark was run"},
    ]}, check)
    assert _impacts(read) == [("G1", "no_progress_unexpected"), ("G2", "unchanged_expected"),
                              ("G3", "unknown")]
    # R37, C17: a correct task that did not move its goal merges; the miss is recorded
    assert read.blocking == [] and read.shortfalls == ["G1"]


def test_a_stored_w11a_no_progress_reads_as_legacy_never_as_unexpected():
    assert gp.stored_impact({"goal": "G1", "progress": "no_progress"}) == "no_progress_legacy"
    assert gp.stored_impact({"goal": "G1", "progress": "advanced"}) == "advanced"
    assert gp.stored_impact({"goal": "G1", "impact": "enabled"}) == "enabled"
    assert gp.LEGACY_NO_PROGRESS not in gp.IMPACTS


def test_a_reviewer_writing_the_old_no_progress_is_unknown_and_flagged():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "progress": "no_progress", "evidence": [FAILED_AC02]}]}, SERVED)
    assert _impacts(read) == [("G1", "unknown")]
    assert read.shortfalls == []
    assert ("goal_progress G1: 'no_progress' is no longer an impact (it cannot tell "
            "enabling work from an unexpected miss); recorded as unknown") in read.flags


# --- evidence (R24, R39, R40, R42) ----------------------------------------------------------


def test_a_diff_alone_is_never_advanced():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "advanced", "evidence": "adds the refusal branch"},
        {"goal": "G2", "impact": "advanced",
         "evidence": [{"kind": "diff", "text": "totals.py sums per month"}]},
    ]}, SERVED)
    assert _impacts(read) == [("G1", "unknown"), ("G2", "unknown")]
    assert [r["claimed"] for r in read.records] == ["advanced", "advanced"]
    assert read.records[0]["reason"] == ("advanced needs a test result, a measurement "
                                         "against its baseline, a trace or an operator "
                                         "observation; a diff shows an edit, not an outcome")
    assert read.follow_ups == [
        "Goal G1 impact unknown: gather evidence: run AC-01 "
        "(`pytest tests/acceptance/test_import.py::test_valid`), AC-02 "
        "(`pytest tests/acceptance/test_import.py::test_refused`)",
        "Goal G2 impact unknown: gather evidence: run AC-03 "
        "(tests/test_totals.py::test_match)"]


def test_a_mocked_test_is_no_evidence_for_an_e2e_or_integration_criterion():
    mocked = dict(PASSED_AC02, mocked=True)
    unit = {"kind": "test", "criterion": "AC-03", "id": "t", "result": "passed",
            "mocked": True}
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "advanced", "evidence": [mocked]},
        {"goal": "G2", "impact": "advanced", "evidence": [unit]},
    ]}, SERVED)
    assert _impacts(read) == [("G1", "unknown"), ("G2", "advanced")]
    assert "a mocked test is no evidence for AC-02 (e2e)" in read.records[0]["reason"]


def test_a_metric_with_no_number_or_no_baseline_is_unknown_with_a_targeted_follow_up():
    check = dict(SERVED, serves=["G3"], contribution={"G3": "direct"})
    for metric in ({"kind": "metric", "name": "import seconds", "value": 7.5},
                   {"kind": "metric", "name": "import seconds", "baseline": 8.0},
                   {"kind": "metric", "name": "import seconds", "value": "fast",
                    "baseline": 8.0}):
        read = gp.read({"goal_progress": [
            {"goal": "G3", "impact": "advanced", "evidence": [dict(metric, criterion="AC-04")]},
        ]}, check)
        assert _impacts(read) == [("G3", "unknown")], metric
        assert read.follow_ups == [
            "Goal G3 impact unknown: gather evidence: measure import seconds against its "
            "baseline (AC-04: `python bench.py`)"], metric
    read = gp.read({"goal_progress": [
        {"goal": "G3", "impact": "advanced",
         "evidence": [dict(BENCH, value=6.0)]}]}, check)
    assert _impacts(read) == [("G3", "advanced")]


def test_a_claim_over_a_failing_check_is_decided_by_rule():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "advanced", "evidence": [PASSED_AC02, FAILED_AC01]},
    ]}, SERVED)
    # AC-01 was green at the baseline and is failing now: a verified regression
    assert _impacts(read) == [("G1", "regressed")]
    assert read.records[0]["claimed"] == "advanced"
    assert read.blocking and read.blocking[0].startswith("Goal G1 regressed: test "
                                                         "test_valid failed (AC-01)")
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "advanced", "evidence": [FAILED_AC02]},
    ]}, SERVED)
    assert _impacts(read) == [("G1", "unknown")] and read.blocking == []
    assert read.records[0]["reason"] == ("advanced contradicts a failing check: test "
                                         "tests/acceptance/test_import.py::test_refused "
                                         "failed (AC-02)")


@pytest.mark.parametrize("evidence, impact, reason", [
    ([PASSED_AC02], "unknown", "a passed test shows no degradation"),
    ([FAILED_AC02], "unknown", "AC-02 was red_expected_missing_behavior at the baseline: "
                               "still failing is not a regression"),
    ([dict(BENCH, value=6.0)], "unknown", "metric import seconds 6.0 is not worse than its "
                                          "baseline 8.0"),
    ([dict(BENCH, criterion=None)], "unknown", "metric import seconds names no criterion "
                                               "whose threshold says which way is better"),
    ([BENCH], "regressed", ""),
    ([FAILED_AC01], "regressed", ""),
    ([{"kind": "test", "id": "tests/test_other.py::t", "result": "failed"}], "regressed", ""),
])
def test_regressed_needs_evidence_that_points_down(evidence, impact, reason):
    """Stage-1 review (R39, R22, C12, C20): a `regressed` claim stands only on evidence of
    a degradation; a passing test, a criterion that was never GREEN, or a better metric is
    the reviewer's word against the evidence, and the rule decides."""
    read = gp.read({"goal_progress": [
        {"goal": "G3", "impact": "regressed", "evidence": evidence}]}, SERVED)
    assert _impacts(read) == [("G3", impact)]
    assert bool(read.blocking) == (impact == "regressed")
    if reason:
        assert reason in read.records[0]["reason"]


def test_advanced_needs_a_metric_that_points_up():
    check = dict(SERVED, serves=["G3"], contribution={"G3": "direct"})
    read = gp.read({"goal_progress": [
        {"goal": "G3", "impact": "advanced", "evidence": [BENCH]}]}, check)
    assert _impacts(read) == [("G3", "unknown")]
    assert "metric import seconds 12.0 is not better than its baseline 8.0" in \
        read.records[0]["reason"]


def test_the_reviewer_check_says_which_way_a_metric_criterion_is_better(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GOALS.md").write_text(V2)
    fields, _ = gp.manifest_fields(tmp_path, {"id": "T", "serves": ["G3"]}, role="reviewer")
    criteria = fields["goals_check"]["criteria"]
    assert criteria["AC-04"]["direction"] == "below"
    assert criteria["AC-01"]["direction"] is None


def test_regressed_without_runtime_evidence_is_unknown_not_blocking():
    read = gp.read({"goal_progress": [
        {"goal": "G3", "impact": "regressed", "evidence": "import loop is now O(n^2)"},
    ]}, SERVED)
    assert _impacts(read) == [("G3", "unknown")]
    assert read.blocking == []
    assert read.follow_ups == ["Goal G3 impact unknown: gather evidence: measure AC-04 "
                               "against its baseline (`python bench.py`)"]


def test_an_operator_observation_is_evidence():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "advanced",
         "evidence": [{"kind": "operator", "observation": "Dan imported his March export",
                       "by": "Dan"}]}]}, SERVED)
    assert _impacts(read) == [("G1", "advanced")]


# --- task correctness vs goal impact (R37, C16, C17) ----------------------------------------


def test_an_enabling_or_maintenance_task_is_never_a_shortfall():
    check = dict(SERVED, contribution={"G1": "maintenance", "G2": "enabling"})
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "no_progress_unexpected", "evidence": [FAILED_AC02]},
        {"goal": "G2", "impact": "no_progress_unexpected", "evidence": [FAILED_AC02]},
    ]}, check)
    assert _impacts(read) == [("G1", "unchanged_expected"), ("G2", "unchanged_expected")]
    assert read.shortfalls == [] and read.blocking == []
    assert read.records[1]["reason"] == "no direct progress was promised (enabling)"


def test_no_progress_unexpected_needs_evidence_too():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "no_progress_unexpected", "evidence": "nothing changed"},
    ]}, SERVED)
    assert _impacts(read) == [("G1", "unknown")] and read.shortfalls == []


# --- regression outside serves (R28, R41, C18) ----------------------------------------------


def test_serves_empty_still_blocks_a_verified_regression_on_any_goal():
    check = {"mode": "skip", "serves": [], "goal_ids": ["G1", "G2", "G3"],
             "contribution": {}, "criteria": CRITERIA}
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "regressed", "evidence": [FAILED_AC01]},
        {"goal": "G2", "impact": "advanced", "evidence": [PASSED_AC02]},
    ]}, check)
    assert _impacts(read) == [("G1", "regressed")]
    assert len(read.blocking) == 1 and read.blocking[0].startswith("Goal G1 regressed")
    assert read.flags == []


def test_a_task_without_serves_counts_only_regressions():
    check = {"mode": "regressions", "serves": [], "goal_ids": ["G1", "G2"],
             "contribution": {}, "criteria": CRITERIA}
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "no_progress_unexpected", "evidence": [FAILED_AC02]},
        # A failed test of no criterion: AC-03 was RED at the baseline, so a failure of
        # it would be no regression (stage-1 review).
        {"goal": "G2", "impact": "regressed", "evidence": [
            {"kind": "test", "id": "tests/test_other.py::t", "result": "failed"}]},
    ]}, check)
    assert len(read.blocking) == 1 and read.shortfalls == []
    assert [r["goal"] for r in read.records] == ["G2"]


# --- scope (R36 part 2) ---------------------------------------------------------------------


def test_unasked_goal_work_is_a_scope_finding_through_the_w9_path():
    read = gp.read({"unasked_goal_work": [
        {"path": "app/summary.py", "goal": "G2", "what": "adds a summary page"},
        {"goal": "G2"},
    ]}, SERVED)
    assert read.scope_findings == [scope_gate.out_of_scope_finding(
        "app/summary.py", "goal work the task does not ask for (G2: adds a summary page)")]
    assert read.scope_findings[0].startswith("out_of_scope: app/summary.py; ")
    # (the two `no entry for served goal` flags come first: this review has no goal_progress)
    assert read.flags[-1:] == ["unasked_goal_work entry 2 names no path"]


def test_the_scope_finding_text_is_w9s():
    assert scope_gate.out_of_scope_finding("a.py") == (
        "out_of_scope: a.py; remove this change or have the task author declare a "
        "reasoned scope_exceptions entry for a legitimately needed file")


# --- flags ----------------------------------------------------------------------------------


def test_bad_entries_are_flagged_never_failed():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "great", "evidence": "x"},
        {"goal": "G9", "impact": "advanced", "evidence": "x"},
        "nonsense",
        {"goal": "G2", "impact": "enabled", "evidence": [{"kind": "video"}]},
    ]}, SERVED)
    assert read.blocking == []
    assert read.flags == [
        "goal_progress G1: impact 'great' is not one of advanced, enabled, "
        "unchanged_expected, no_progress_unexpected, regressed, unknown",
        "goal_progress G9: not a goal in docs/GOALS.md",
        "goal_progress entry 3 is not an object with goal and impact",
        "goal_progress G2: evidence item 1 is not a test, metric, trace, operator or diff "
        "record",
    ]


def test_missing_goal_progress_on_a_served_task_is_flagged():
    read = gp.read({}, SERVED)
    assert read.flags == ["goal_progress: no entry for served goal G1",
                          "goal_progress: no entry for served goal G2"]
    assert read.records == [] and read.blocking == []


def test_infeasible_needs_proof():
    """GDT-8 (R44): `infeasible` on a direct `no_progress_unexpected` with runtime proof for
    a criterion is a contradiction (its facts: criterion and evidence, never the text);
    otherwise it is flagged and becomes a targeted follow-up, never a replan."""
    check = dict(SERVED, serves=["G1", "G2", "G3"],
                 contribution={"G1": "direct", "G2": "enabling", "G3": "direct"})
    slow = dict(BENCH, value=11.0, baseline=12.0)
    read = gp.read({"goal_progress": [
        {"goal": "G3", "impact": "no_progress_unexpected", "evidence": [slow],
         "infeasible": "parsing alone takes 11 s"},
        {"goal": "G1", "impact": "no_progress_unexpected", "evidence": "rewrote the parser",
         "infeasible": "the bank format cannot be parsed"},
        {"goal": "G2", "impact": "no_progress_unexpected", "evidence": [FAILED_AC02],
         "infeasible": "the totals never match"},
    ]}, check)
    g3, g1, g2 = read.records
    assert g3["impact"] == "no_progress_unexpected"
    assert g3["contradiction"]["criterion"] == "AC-04"
    assert g3["contradiction"]["why"] == "parsing alone takes 11 s"
    again = gp.read({"goal_progress": [{"goal": "G3", "impact": "no_progress_unexpected",
                                        "evidence": [slow], "infeasible": "other words"}]},
                    check).records[0]["contradiction"]
    assert again["facts"] == g3["contradiction"]["facts"]
    assert (g1["impact"], g2["impact"]) == ("unknown", "unchanged_expected")
    assert "contradiction" not in g1 and "contradiction" not in g2
    unproven = [f for f in read.follow_ups if "infeasibility unproven" in f]
    assert [f.split(":")[0] for f in unproven] == ["Goal G1 infeasibility unproven",
                                                    "Goal G2 infeasibility unproven"]
    assert sum("infeasible" in flag for flag in read.flags) == 2
    assert read.blocking == []


def test_the_reviewer_rule_says_when_infeasible_counts():
    manifest = {"goals_excerpt": "Purpose: x",
                "goals_check": {"mode": "served", "serves": ["G1"], "goal_ids": ["G1"]}}
    text = gp.render(manifest, role="reviewer")
    assert '"infeasible": "<why>"' in text


def test_replan_decisions_reach_served_briefs(tmp_path):
    """GDT-8 (R46): Dan's answers to a replan of a goal reach every brief of a task that
    serves it, binding; a task serving another goal does not carry them."""
    from maestro import goal_store

    repo = _repo(tmp_path, V2)
    decided = {"id": "RP-1234abcd", "goal": "G2", "status": "decided",
               "decision": {"resolution": "notes", "alternative": "",
                            "answer": "try the streaming parser first",
                            "rationale": "try the streaming parser first",
                            "at": "2026-10-18T09:00:00+00:00"}}
    goal_store.update(repo, "replans", lambda rows: [*rows, decided,
                                                     {"id": "RP-0000aaaa", "goal": "G2",
                                                      "status": "asked"}])
    for role in ("implementer", "reviewer", "planner"):
        fields, _ = gp.manifest_fields(repo, {"id": "t", "serves": ["G2"]}, role=role)
        [entry] = fields["replan_decisions"]
        assert (entry["id"], entry["goal"], entry["resolution"]) == ("RP-1234abcd", "G2",
                                                                      "notes")
        text = gp.render(fields, role=role)
        assert "DAN'S REPLAN DECISIONS" in text and "RP-1234abcd" in text
        assert "try the streaming parser first" in text
    fields, _ = gp.manifest_fields(repo, {"id": "t", "serves": ["G1"]}, role="implementer")
    assert "replan_decisions" not in fields
    assert "DAN'S REPLAN DECISIONS" not in gp.render(fields, role="implementer")


def test_no_check_reads_nothing():
    assert gp.read({"goal_progress": [{"goal": "G1", "impact": "regressed"}]}, None) == gp.Read()


# --- GDT-7: decided advanced vs regressed records go through `goal_conflicts.judge` ---------


def _conflicts(decisions=()):
    from maestro import goal_conflicts

    return dict(SERVED, conflicts=goal_conflicts.context(goals.parse(V2), "TASK-001",
                                                         list(decisions)))


#: G1 advanced (AC-02 passes) while G3 regressed (AC-04 12 s against 8 s: 50% worse).
TRADE = {"goal_progress": [
    {"goal": "G1", "impact": "advanced", "evidence": [PASSED_AC02]},
    {"goal": "G2", "impact": "enabled", "evidence": []},
    {"goal": "G3", "impact": "regressed", "evidence": [BENCH]},
]}


def test_a_regressed_goal_while_another_advanced_is_a_question_not_a_finding():
    read = gp.read(TRADE, _conflicts())
    assert read.blocking == [] and read.conflict_findings == [] and read.accepted == []
    [question] = read.questions
    conflict = question["conflict"]
    assert (conflict["advanced"], conflict["regressed"], conflict["criteria"]) == (
        ["G1"], "G3", ["AC-04"])
    assert conflict["stage"] == "review"
    assert [o["label"] for o in question["options"]] == [
        "Keep G3", "Accept G3 worse by up to +50%"]
    [g3] = [r for r in read.records if r["goal"] == "G3"]
    assert g3["impact"] == "regressed" and g3["conflict"] == conflict["id"]


def test_a_conflict_dan_accepted_is_not_asked_again_and_does_not_block():
    from maestro import goal_conflicts

    [question] = gp.read(TRADE, _conflicts()).questions
    decision = goal_conflicts.decide(question, question["options"][1]["label"],
                                     "2026-10-18T10:00:00+03:00")
    read = gp.read(TRADE, _conflicts([decision]))
    assert read.questions == [] and read.blocking == [] and read.conflict_findings == []
    [accepted] = read.accepted
    assert accepted["conflict"] == question["conflict"]["id"] and accepted["bound"] == 50.0


def test_dan_keeping_the_regressed_goal_is_a_conflict_finding():
    from maestro import goal_conflicts

    [question] = gp.read(TRADE, _conflicts()).questions
    decision = goal_conflicts.decide(question, "Keep G3", "2026-10-18T10:00:00+03:00")
    read = gp.read(TRADE, _conflicts([decision]))
    assert read.questions == [] and read.blocking == []
    [finding] = read.conflict_findings
    assert finding.startswith(f"Dan decided {question['conflict']['id']}")


def test_a_hard_invariant_regression_blocks_despite_progress_in_review():
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "regressed", "evidence": [FAILED_AC01]},
        {"goal": "G2", "impact": "advanced", "evidence": [
            {"kind": "test", "id": "tests/test_totals.py::test_match", "result": "passed"}]},
    ]}, _conflicts())
    assert read.questions == []
    assert len(read.blocking) == 1 and read.blocking[0].startswith("Goal G1 regressed")
    [finding] = read.conflict_findings
    assert finding.startswith("hard_invariant_regression: G1 regressed on AC-01")


def test_a_regression_with_nothing_advanced_keeps_the_gdt6_finding_under_conflicts():
    read = gp.read({"goal_progress": [TRADE["goal_progress"][2]]}, _conflicts())
    assert read.questions == [] and read.conflict_findings == []
    assert len(read.blocking) == 1 and read.blocking[0].startswith("Goal G3 regressed")
    assert "conflict" not in read.records[0]


def test_a_regression_dan_accepted_is_covered_at_review_when_nothing_advanced():
    """GDT-7 review H1: his accept on (G3, AC-04) covers a later review whatever advanced."""
    from maestro import goal_conflicts

    [question] = gp.read(TRADE, _conflicts()).questions
    decision = goal_conflicts.decide(question, question["options"][1]["label"],
                                     "2026-10-18T10:00:00+03:00")
    read = gp.read({"goal_progress": [TRADE["goal_progress"][2]]}, _conflicts([decision]))
    assert read.questions == [] and read.blocking == [] and read.conflict_findings == []
    [accepted] = read.accepted
    assert accepted["conflict"] == question["conflict"]["id"]


def test_a_criterion_the_gate_judged_is_not_asked_again_at_review():
    """GDT-7 review H2: Dan accepted the gate's measured AC-04; the reviewer reports it as a
    failing test. The gate reran AC-04 (GREEN at the baseline) on this tree, so the review
    is covered instead of asking him a second time."""
    from maestro import goal_conflicts

    [question] = gp.read(TRADE, _conflicts()).questions
    decision = goal_conflicts.decide(question, question["options"][1]["label"],
                                     "2026-10-18T10:00:00+03:00")
    failing = {"kind": "test", "criterion": "AC-04", "id": "bench.py", "result": "failed"}
    review = {"goal_progress": [TRADE["goal_progress"][0],
                                {"goal": "G3", "impact": "regressed", "evidence": [failing]}]}
    check = dict(SERVED, conflicts=goal_conflicts.context(
        goals.parse(V2), "TASK-001", [decision], judged=["AC-01", "AC-04"]))
    read = gp.read(review, check)
    assert read.questions == [] and read.blocking == [] and read.conflict_findings == []
    assert [a["conflict"] for a in read.accepted] == [question["conflict"]["id"]]


def test_the_reviewer_check_names_what_the_gate_judged_and_takes_the_runner_s_decisions(
        tmp_path):
    repo = _repo(tmp_path, V2)
    doc = goals.load(repo)
    acceptance.store(repo, doc, [
        acceptance.Result(id="AC-01", state=acceptance.GREEN, reason="passed", revision=2),
        acceptance.Result(id="AC-04", state=acceptance.GREEN, reason="8 s", revision=2,
                          value=8.0)])
    task = {"id": "t", "serves": ["G1"], "tests": ["AC-02"]}
    decision = {"id": "GC-00000000", "resolution": "preserve"}
    check = gp.manifest_fields(repo, task, role="reviewer",
                               decisions=[decision])[0]["goals_check"]
    # the gate reruns the GREEN criteria and the task-named ones before the review
    assert check["conflicts"]["judged"] == ["AC-01", "AC-02", "AC-04"]
    assert check["conflicts"]["decisions"] == [decision]


def test_the_reviewer_reads_an_observed_green_criterion_as_protected(tmp_path):
    """GDT-9 (C12): AC-02 is RED at the baseline and an earlier task's gate observed it
    GREEN. The reviewer's check reads it as GREEN (a failing test of it is a regression),
    and every brief shows where that state was observed; the baseline lines stay as they
    were."""
    from maestro import goal_state

    repo = _repo(tmp_path, V2)
    doc = goals.load(repo)
    acceptance.store(repo, doc, [
        acceptance.Result(id="AC-01", state=acceptance.GREEN, reason="passed", revision=2),
        acceptance.Result(id="AC-02", state=acceptance.RED, reason="assert", revision=2)])
    task = {"id": "t", "serves": ["G1"], "contribution": "direct"}
    before, _ = gp.manifest_fields(repo, task, role="implementer")
    goal_state.record_gate(repo, doc, {"acceptance": {"AC-02": acceptance.GREEN},
                                       "snapshot": "abc1234def"},
                           task="T1", run_id="run-T1", at="2099-01-01T00:00:00+00:00")
    check = gp.manifest_fields(repo, task, role="reviewer")[0]["goals_check"]
    assert check["criteria"]["AC-02"]["baseline"] == acceptance.GREEN
    assert check["criteria"]["AC-01"]["baseline"] == acceptance.GREEN
    excerpt = gp.manifest_fields(repo, task, role="implementer")[0]["goals_excerpt"]
    [line] = [x for x in excerpt.splitlines() if x.startswith("AC-02 ")]
    assert "observed by the gate after T1" in line and line.endswith(acceptance.GREEN)
    [ac01] = [x for x in excerpt.splitlines() if x.startswith("AC-01 ")]
    assert ac01 in before["goals_excerpt"].splitlines()


# --- GDT-9 review follow-up: the reviewer honours the gate's red-on-base note ---------------

#: AC-02 GREEN only by an observation since the baseline (the gate's `observed_only`).
_OBSERVED = dict(SERVED, criteria={
    **CRITERIA, "AC-02": dict(CRITERIA["AC-02"], baseline=acceptance.GREEN)})
_ON_BASE = dict(_OBSERVED, criteria={
    **_OBSERVED["criteria"], "AC-02": dict(_OBSERVED["criteria"]["AC-02"], red_on_base=True)})


def test_a_reviewer_regression_on_a_criterion_red_on_base_is_a_note():
    """M4 follow-up: the gate found AC-02 (observed GREEN, not at the baseline) RED on the
    task's tree and on the base, a `goal_protected_red_on_base` note. A reviewer who cites
    the failing test claims no blocking regression: it is a note, like the gate's."""
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "regressed", "evidence": [FAILED_AC02]}]}, _ON_BASE)
    assert read.blocking == [] and read.conflict_findings == [] and read.follow_ups == []
    assert _impacts(read) == [("G1", "unchanged_expected")]
    assert read.records[0]["reason"].startswith("goal_protected_red_on_base: AC-02 ")


def test_a_reviewer_regression_on_an_observed_green_criterion_green_on_base_still_blocks():
    """No red-on-base note (the base was GREEN): the observed GREEN is protected and the
    failing test is a verified regression."""
    read = gp.read({"goal_progress": [
        {"goal": "G1", "impact": "regressed", "evidence": [FAILED_AC02]}]}, _OBSERVED)
    assert _impacts(read) == [("G1", "regressed")]
    [blocking] = read.blocking
    assert blocking.startswith("Goal G1 regressed")


def test_the_reviewer_check_marks_the_criteria_the_gate_found_red_on_base(tmp_path):
    from maestro import goal_state

    repo = _repo(tmp_path, V2)
    doc = goals.load(repo)
    acceptance.store(repo, doc, [
        acceptance.Result(id="AC-01", state=acceptance.GREEN, reason="passed", revision=2),
        acceptance.Result(id="AC-02", state=acceptance.RED, reason="assert", revision=2)])
    goal_state.record_gate(repo, doc, {"acceptance": {"AC-02": acceptance.GREEN},
                                       "snapshot": "abc1234def"},
                           task="T1", run_id="run-T1", at="2099-01-01T00:00:00+00:00")
    task = {"id": "t", "serves": ["G1"], "contribution": "direct"}
    check = gp.manifest_fields(repo, task, role="reviewer",
                               red_on_base=["AC-02"])[0]["goals_check"]
    assert check["criteria"]["AC-02"]["baseline"] == acceptance.GREEN
    assert check["criteria"]["AC-02"]["red_on_base"] is True
    assert "red_on_base" not in check["criteria"]["AC-01"]


def test_the_runner_reads_the_red_on_base_criteria_from_its_last_gate():
    from types import SimpleNamespace

    from maestro.workflows import runner as runner_mod

    note = ("goal_protected_red_on_base: AC-02 was observed green_already_satisfied since "
            "the baseline at contract revision 2 (not at it), but is red ...")
    other = "AC-01 was green at contract revision 2 but is blocked on the task's tree ..."
    gate = SimpleNamespace(node_id="gate", handler_ref=runner_mod.VERIFICATION_HANDLER)
    review = SimpleNamespace(node_id="review", handler_ref="maestro.handlers.agent")
    outputs = {"gate": {"goals_guard_notes": [other, note]}}
    stub = SimpleNamespace(revision=SimpleNamespace(nodes=[gate, review]),
                           _committed_outputs=lambda node_id: outputs.get(node_id, {}))
    assert runner_mod.WorkflowRunner.gate_red_on_base(stub) == ["AC-02"]
    outputs["gate"] = {}
    assert runner_mod.WorkflowRunner.gate_red_on_base(stub) == []


def _review_context(tmp_path, **inputs):
    from maestro.workflows import handlers
    from maestro.workflows.models import NodeSpec

    node = NodeSpec(node_id="proof_review", kind="agent", handler_ref="maestro.handlers.agent",
                    agent_definition_ref="reviewer")
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    return handlers.HandlerContext(
        run_id="r", task_id="TASK-001", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(tmp_path / "attempts", "a"), workspace=workspace,
        inputs=inputs)


def _adopt(ctx, extra):
    import json

    from maestro.workflows import handlers

    path = handlers.write_review(ctx.workspace, "approved", [])
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(extra)
    path.write_text(json.dumps(doc), encoding="utf-8")
    handlers.write_sentinel(ctx.workspace, "DONE")
    return handlers.agent_handler(ctx)


OWN_QUESTION = {"question": "Does a shared track count for B?", "why_undefined": "silent",
                "options": [{"label": "Yes", "consequence": "a"},
                            {"label": "No", "consequence": "b"}], "recommended": 0}


def test_the_review_handler_joins_conflict_questions_to_its_own(tmp_path):
    result = _adopt(_review_context(tmp_path, goals_check=_conflicts()),
                    {**TRADE, "questions": [OWN_QUESTION]})
    assert result.status == "failed"
    own, conflict = result.outputs["dan_questions"]
    assert own["question"] == OWN_QUESTION["question"]
    assert conflict["conflict"]["regressed"] == "G3"
    assert result.outputs["review"]["findings"] == []


def test_the_review_handler_blocks_on_dan_s_keep_and_records_an_accept(tmp_path):
    from maestro import goal_conflicts

    [question] = gp.read(TRADE, _conflicts()).questions
    keep = goal_conflicts.decide(question, "Keep G3", "2026-10-18T10:00:00+03:00")
    result = _adopt(_review_context(tmp_path / "keep", goals_check=_conflicts([keep])), TRADE)
    assert result.status == "failed" and "dan_questions" not in result.outputs
    review = result.outputs["review"]
    assert review["verdict"] == "changes_requested"
    [finding] = review["findings"]
    assert finding.startswith(f"Dan decided {question['conflict']['id']}")
    assert review["finding_records"][0]["kind"] == "goal_conflict"

    accept = goal_conflicts.decide(question, question["options"][1]["label"],
                                   "2026-10-18T10:00:00+03:00")
    result = _adopt(_review_context(tmp_path / "accept", goals_check=_conflicts([accept])),
                    TRADE)
    assert result.status == "succeeded"
    [accepted] = result.outputs["review"]["goal_conflicts_accepted"]
    assert accepted["conflict"] == question["conflict"]["id"]
