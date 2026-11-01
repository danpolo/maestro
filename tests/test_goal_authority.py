"""The human authority matrix: what agents decide and what only Dan decides (GDT-7).

Agents decide implementation details, retries, test selection, local debugging,
within-scope fixes and routine plan adjustments; goal and threshold changes, the final-state
definition, goal conflicts, scope expansion, hard-constraint relaxation, security exceptions,
paid operations, destructive actions and true product questions are Dan's (`01` §11). A
project's `Ask Dan:` can reserve a local action too; its `Autonomous:` can never grant a
reserved one, and trying is a draft problem. Requirements: R61, R09.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maestro import goal_authority as ga
from maestro.docs import goals

REPO_ROOT = Path(__file__).resolve().parents[1]
V2 = (REPO_ROOT / "tests" / "fixtures" / "goals" / "v2_GOALS.md").read_text(encoding="utf-8")
AUTONOMOUS = "- **Autonomous:** implementation, tests, retries, task order within a phase."
ASK_DAN = "- **Ask Dan:** new or changed goals, thresholds, any goal conflict, paid services."


def test_the_matrix_names_every_action_once():
    assert set(ga.LOCAL) == {"implementation_detail", "retry", "test_selection",
                             "local_debugging", "within_scope_fix", "routine_plan_adjustment"}
    assert set(ga.RESERVED) == {
        "goal_change", "acceptance_threshold", "final_state_definition", "goal_conflict",
        "scope_expansion", "hard_constraint_relaxation", "security_exception",
        "paid_operation", "destructive_action", "product_question"}
    assert not set(ga.LOCAL) & set(ga.RESERVED)


@pytest.mark.parametrize("doc", [None, goals.parse(V2)], ids=["no contract", "fixture"])
def test_local_actions_are_the_agents_and_reserved_ones_are_dans(doc):
    for action in ga.LOCAL:
        needed, reason = ga.requires_dan(action, doc)
        assert needed is False and reason
    for action in ga.RESERVED:
        needed, reason = ga.requires_dan(action, doc)
        assert needed is True and reason


def test_ask_dan_can_reserve_a_local_action():
    doc = goals.parse(V2.replace(ASK_DAN, ASK_DAN[:-1] + ", any retry."))
    needed, reason = ga.requires_dan("retry", doc)
    assert needed is True and "Ask Dan" in reason
    assert ga.requires_dan("implementation_detail", doc)[0] is False
    assert [p for p in doc.issues if "authority" in p.text.lower()] == []


def test_autonomous_can_never_grant_a_reserved_action():
    doc = goals.parse(V2.replace(AUTONOMOUS, AUTONOMOUS[:-1] + ", paid services under $5."))
    needed, reason = ga.requires_dan("paid_operation", doc)
    assert needed is True and "Autonomous" in reason
    (problem,) = [p for p in doc.issues if "paid_operation" in p.text]
    assert problem.severity == "draft"
    assert doc.gate == ()
    assert ga.contract_problems(doc) == (problem,)


@pytest.mark.parametrize("line", [
    "resolve merge conflicts, delete temp files",
    "relax lint rules, retry flaky tests, spend extra minutes on profiling",
])
def test_ordinary_work_in_autonomous_is_no_problem(line):
    """GDT-7 review L6: a bare keyword (`conflicts`, `delete`) is ordinary work."""
    doc = goals.parse(V2.replace(AUTONOMOUS, f"- **Autonomous:** {line}."))
    assert ga.contract_problems(doc) == ()


@pytest.mark.parametrize("line, action", [
    ("any goal conflict", "goal_conflict"),
    ("delete production data", "destructive_action"),
    ("destructive migrations", "destructive_action"),
    ("security exceptions", "security_exception"),
    ("relax a hard constraint", "hard_constraint_relaxation"),
    ("paid services", "paid_operation"),
    ("spend money on cloud runs", "paid_operation"),
])
def test_reserved_phrasings_in_autonomous_are_still_draft_problems(line, action):
    doc = goals.parse(V2.replace(AUTONOMOUS, f"- **Autonomous:** implementation, {line}."))
    assert [action in p.text for p in ga.contract_problems(doc)] == [True]


def test_the_fixtures_authority_has_no_problem():
    assert ga.contract_problems(goals.parse(V2)) == ()


def test_an_unknown_action_raises():
    with pytest.raises(ValueError):
        ga.requires_dan("make_coffee")


@pytest.mark.parametrize("line", [
    "relax the threshold", "edit the thresholds in GOALS.md", "loosen tolerance",
    "drop the tolerance", "lower the thresholds", "raise the tolerances",
    "change the threshold",
])
def test_threshold_and_tolerance_phrasings_are_reserved(line):
    """GDT-7 verification low: verb + threshold/tolerance is the reserved action."""
    doc = goals.parse(V2.replace(AUTONOMOUS, f"- **Autonomous:** {line}."))
    (problem,) = [p for p in ga.contract_problems(doc) if "acceptance_threshold" in p.text]
    assert problem.severity == "draft"
