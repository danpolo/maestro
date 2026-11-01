"""Role briefs from the goals contract (P13 GDT-5: R33, R34, R36, R65, D23).

`goals.excerpt(doc, serves, role=..., task=..., results=...)` is the bounded, role-specific
excerpt every agent brief carries (`01` §5): the purpose and contract revision, the served
goals in full with their scenarios, criteria, bound test commands and baseline states, the
task's own tests, hard invariants and trade-off bounds, the priority order, the other goals
in one line each and the non-goals; then one rule for the role. Critical lines (served
goals and criteria, invariants, trade-offs, the role's rule) are never cut; the cut drops
lower-priority lines and names each with its source in `docs/GOALS.md`.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maestro import acceptance
from maestro.docs import goals
from maestro.workflows import goal_progress as gp

from .test_goals import SKILL_STYLE
from .test_goals_contract import V2
from .test_goals_roadmap import _results

ROLES = ("planner", "implementer", "reviewer", "diagnoser", "triage")
TASK = {"id": "t", "serves": ["G1"], "criteria": ["AC-02"], "contribution": "direct",
        "tests": ["tests/acceptance/test_import.py::test_broken"]}


def _doc():
    return goals.parse(V2)


def _excerpt(role, task=TASK, results=None, doc=None):
    task = dict(task)
    return goals.excerpt(doc or _doc(), goals.task_serves(task), role=role, task=task,
                         results=_results() if results is None else results)


# --- what every role sees (R33) ------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_every_role_sees_the_served_goal_in_full_and_the_rest_short(role):
    text, cut = _excerpt(role)
    assert cut == ""
    lines = text.splitlines()
    assert lines[0].startswith("Purpose: Ledger records household spending")
    assert "Contract revision: 2 (full contract: docs/GOALS.md)" in lines
    assert "This task serves: G1 (direct)" in lines
    assert "G1 (served) Imports every export" in lines
    assert "  target: every CSV export from the two banks imports with no manual edits." in lines
    assert "G2 Monthly summary is right" in lines and "G3 Fast enough" in lines
    assert "Priority when goals conflict: G2 > G1 > G3" in lines
    assert "Non-goals: No budgeting or alerts." in lines
    assert "INV-01: no entry is ever stored twice (checked by AC-01)" in lines
    assert any(line.startswith("TO-01: import may take up to 12 seconds") and
               "bound 12 seconds" in line for line in lines)
    # the served goal's criteria with their bound command and baseline state
    assert ("AC-01 Valid exports import (G1; S-01) — baseline: green_already_satisfied"
            in lines)
    assert "  test: `pytest tests/acceptance/test_import.py::test_valid`" in lines
    assert ("AC-02 Broken export is refused (G1; S-02; named by this task) — baseline: "
            "red_expected_missing_behavior" in lines)
    assert "  test: none bound yet" in lines
    assert any(line.startswith("S-02 A broken export (error): given an export") for line in lines)
    assert "Tests this task should affect: tests/acceptance/test_import.py::test_broken" in lines


def test_an_unserved_goal_criteria_reach_only_the_planner():
    """R65: other goals are one line each; only the planner, which plans every remaining
    gap (`01` §5), sees the criteria of goals the task does not serve."""
    for role in ROLES:
        text, _ = _excerpt(role)
        assert ("AC-03" in text) == (role == "planner"), role
        assert "AC-04" not in text or role == "planner", role


def test_each_role_gets_its_own_rule():
    texts = {role: _excerpt(role)[0] for role in (*ROLES, "test_designer", "")}
    # D23: a writer knows the expected task and may not widen it (R36)
    for role in ("implementer", "test_designer"):
        assert "SCOPE (D23): the goals explain why this task exists; they never widen it." \
            in texts[role], role
        assert "say so in your result for the planner" in texts[role], role
    # the reviewer judges the task and its goal impact separately; extra goal work is scope
    assert "Expected contribution: G1 direct." in texts["reviewer"]
    assert "a scope finding, not progress (D23)" in texts["reviewer"]
    # the planner sees every remaining gap at this revision
    assert ("Remaining gaps at contract revision 2 (mandatory criteria not GREEN): "
            "AC-02 red_expected_missing_behavior (G1); AC-03 blocked_environment (G2); "
            "AC-04 unknown (G3)" in texts["planner"])
    assert "Operator checks Dan signs off at completion: OP-01 (G2)" in texts["planner"]
    # the diagnoser sees the stop and retry policy, and what a repair may not change
    assert "Authority (retries): two, then a stronger diagnoser." in texts["diagnoser"]
    assert ("Authority (stop a failed approach when): two attempts miss the same "
            "criterion for the same cause." in texts["diagnoser"])
    assert "changing a goal, a criterion, its bound test or an invariant is Dan's" \
        in texts["diagnoser"]
    # triage: polish never adds goal work
    assert "TRIAGE: polish keeps every hard invariant and never adds goal work" \
        in texts["triage"]
    assert not any(marker in texts[""] for marker in
                   ("SCOPE (D23)", "REVIEW:", "PLANNING:", "DIAGNOSIS:", "TRIAGE:"))
    for role, text in texts.items():
        assert ("Remaining gaps" in text) == (role == "planner"), role
        assert ("Authority (" in text) == (role == "diagnoser"), role


def test_no_baseline_at_this_revision_is_said_not_hidden():
    text, _ = _excerpt("planner", results=[])
    assert ("No acceptance baseline at contract revision 2 (run `maestro goals baseline`)"
            in text)
    assert "AC-01 Valid exports import (G1; S-01) — baseline: none at revision 2" in text
    old = [replace(r, revision=1) for r in _results()]
    assert "baseline: none at revision 2" in _excerpt("implementer", results=old)[0]


def test_a_format_one_contract_keeps_its_excerpt_and_gains_the_role_rule():
    doc = goals.parse(SKILL_STYLE)
    plain, _ = goals.excerpt(doc, ("G2",))
    text, cut = goals.excerpt(doc, ("G2",), role="implementer", task={"id": "t",
                                                                     "serves": ["G2"]})
    assert cut == "" and text.startswith(plain)
    assert "SCOPE (D23)" in text and "Contract revision" not in text


# --- bounded, never cutting what is critical (R34) ---------------------------------------------


def _inflated(non_goals=200, extra_goals=30):
    doc = _doc()
    more = tuple(goals.Goal(f"G{n}", f"Extra goal {n} " + "y" * 60, target="t")
                 for n in range(4, 4 + extra_goals))
    return replace(doc, goals=doc.goals + more,
                   non_goals=tuple(f"Non-goal {i} " + "x" * 80 for i in range(non_goals)))


@pytest.mark.parametrize("role", ROLES)
def test_the_cut_drops_low_priority_lines_names_them_and_keeps_every_critical_one(role):
    # triage's non-goals are critical, so its contract has fewer of them to stay in bound
    text, cut = _excerpt(role, doc=_inflated(non_goals=10 if role == "triage" else 200))
    assert len(text) <= goals.MAX_GOALS_TOKENS * 4
    for kept in ("G1 (served) Imports every export", "INV-01: no entry is ever stored twice",
                 "TO-01: import may take up to 12 seconds", "AC-01 Valid exports import",
                 "AC-02 Broken export is refused", "Priority when goals conflict"):
        assert kept in text, (role, kept)
    assert cut.startswith("goals_excerpt: omitted to stay within 1000 tokens: ")
    assert "(docs/GOALS.md §Goals)" in cut
    if role == "triage":
        # triage's non-goals are its constraints: kept, so only goal summaries go
        assert "Non-goal 9 " in text and "§Non-goals" not in cut
    else:
        assert "non-goals" in cut and "(docs/GOALS.md §Non-goals)" in cut
        assert "Non-goal 0 " in text  # the first non-goals are kept as far as they fit
    # the highest-priority other goal is kept before the extra ones
    assert "G2 Monthly summary is right" in text


def test_critical_lines_over_the_bound_are_kept_in_full_and_said():
    doc = _doc()
    long = replace(doc.invariants[0], text="no entry is ever stored twice " + "z" * 5000)
    text, cut = _excerpt("implementer", doc=replace(doc, invariants=(long,)))
    assert long.text in text
    assert "AC-02 Broken export is refused" in text and "SCOPE (D23)" in text
    assert cut.startswith("goals_excerpt: critical lines exceed the 1000-token bound and "
                          "are kept in full")
    assert "docs/GOALS.md" in cut


# --- snapshot (R34) ------------------------------------------------------------------------

IMPLEMENTER_SNAPSHOT = """\
Purpose: Ledger records household spending from bank exports. It exists so Dan can see where the money went each month without a spreadsheet.
Contract revision: 2 (full contract: docs/GOALS.md)
This task serves: G1 (direct)
G1 (served) Imports every export
  outcome: every bank export Dan drops in the inbox becomes ledger entries.
  rationale: a missing import makes every total wrong.
  baseline: not_applicable
  target: every CSV export from the two banks imports with no manual edits.
  verified by: AC-01, AC-02
  constraints: INV-01
S-01 Monthly import and summary (normal): given two valid exports in the inbox; when Dan opens the summary page; then the month's totals by category appear
S-02 A broken export (error): given an export with a truncated last line; when the import runs; then the page names the file and no entry from it is stored
AC-01 Valid exports import (G1; S-01) — baseline: green_already_satisfied
  given the two sample exports from each bank; when the import runs; then every row is a ledger entry
  oracle: entry count equals row count; amounts equal to the agora
  test: `pytest tests/acceptance/test_import.py::test_valid`
AC-02 Broken export is refused (G1; S-02; named by this task) — baseline: red_expected_missing_behavior
  given an export with a truncated last line; when the import runs; then an error names the file and nothing is stored
  oracle: zero entries from that file; the error names it
  test: none bound yet
Tests this task should affect: tests/acceptance/test_import.py::test_broken
Hard invariants (never traded for progress):
INV-01: no entry is ever stored twice (checked by AC-01)
Approved trade-offs (only within their bound):
TO-01: import may take up to 12 seconds while G1's new bank is added (decision D-7; bound 12 seconds; expires when the bank adapter lands)
Priority when goals conflict: G2 > G1 > G3
G2 Monthly summary is right
G3 Fast enough
Non-goals: No budgeting or alerts.
SCOPE (D23): the goals explain why this task exists; they never widen it. Build only what this task's acceptance criteria and notes ask; if a goal needs more, say so in your result for the planner instead of building it."""


def test_the_implementer_excerpt_snapshot():
    text, cut = _excerpt("implementer")
    assert cut == ""
    assert text == IMPLEMENTER_SNAPSHOT


# --- prompt size (R65) ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_every_role_excerpt_of_the_fixture_fits_the_bound(role):
    text, cut = _excerpt(role)
    assert cut == "" and len(text) <= goals.MAX_GOALS_TOKENS * 4


# --- the manifest carries the role's excerpt and the stored baseline --------------------------


def test_manifest_fields_build_the_role_excerpt_from_the_stored_baseline(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GOALS.md").write_text(V2, encoding="utf-8")
    acceptance.store(tmp_path, _doc(), _results())
    fields, cut = gp.manifest_fields(tmp_path, TASK, role="planner")
    assert cut == "" and "goals_check" not in fields
    assert "AC-03 blocked_environment (G2)" in fields["goals_excerpt"]
    fields, _ = gp.manifest_fields(tmp_path, TASK, role="reviewer")
    assert fields["goals_check"]["mode"] == "served"
    assert "Expected contribution: G1 direct." in fields["goals_excerpt"]
    assert "SCOPE (D23)" in gp.brief_section(tmp_path, TASK, role="implementer")
