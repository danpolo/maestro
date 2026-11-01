"""`docs/GOALS.md` format 2, the product completion contract (P13 W11, GDT-1).

Format 2 adds to the six W11a sections: a format line and contract revision, scenarios
(`S-`), acceptance criteria (`AC-`), operator checks (`OP-`), hard invariants (`INV-`),
trade-offs (`TO-`), an authority policy, a change history and retired ids. Every problem
has a severity: `note` (guidance only), `draft` (asked while drafting) or `gate` (blocks
the affected work at execution). A format-1 file still parses and still passes.
Requirements: R02-R07, R09, R11, R12, R14, R62, R67
(`docs/agent-plans/p13-goal-driven-tdd/`).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from maestro.docs import goals

from .hang_bound import HANG_SECONDS
from .test_goals import CHATGPT, SKILL_STYLE, _without

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "goals"
V2 = (FIXTURES / "v2_GOALS.md").read_text(encoding="utf-8")
CHATGPT_V2 = (FIXTURES / "chatgpt_v2_GOALS.md").read_text(encoding="utf-8")


def _issues(doc: goals.Goals, severity: str) -> list[str]:
    return [p.text for p in doc.issues if p.severity == severity]


# --- format detection and the full v2 parse (R02, R06, R07, R09) ---------------------------


def test_format_one_is_detected_and_still_passes():
    doc = goals.parse(SKILL_STYLE)
    assert doc.format == 1
    assert doc.problems == ()
    assert _issues(doc, "gate") == [] and _issues(doc, "draft") == []
    assert any("maestro goals migrate" in n for n in doc.notes)


def test_format_two_parses_every_part():
    doc = goals.parse(V2)
    assert doc.format == 2
    assert (doc.revision, doc.approval) == (2, "approved 2026-10-17 by Dan")
    assert [p.text for p in doc.issues] == []
    g1 = doc.by_id["G1"]
    assert g1.outcome.startswith("every bank export")
    assert g1.rationale == "a missing import makes every total wrong."
    assert g1.baseline == "not_applicable"
    assert g1.verified_ids == ("AC-01", "AC-02")
    assert g1.constraints == ("INV-01",)
    assert doc.by_id["G2"].depends_on == ("G1",)
    assert doc.by_id["G3"].baseline.startswith("42 seconds")

    assert [s.id for s in doc.scenarios] == ["S-01", "S-02", "S-03"]
    assert doc.scenarios[2].kind == "boundary"
    s1 = doc.scenarios[0]
    assert (s1.goals, s1.kind, s1.criteria, s1.operator_checks) == (
        ("G1", "G2"), "normal", ("AC-01", "AC-03"), ("OP-01",))
    assert s1.given == "two valid exports in the inbox" and s1.then.startswith("the month's")
    assert doc.scenarios[1].kind == "error"
    assert "inbox folder" in doc.final_product and "### S-01" not in doc.final_product

    ac = {c.id: c for c in doc.criteria}
    assert list(ac) == ["AC-01", "AC-02", "AC-03", "AC-04"]
    assert ac["AC-01"].test == "pytest tests/acceptance/test_import.py::test_valid"
    assert ac["AC-01"].layer == "e2e" and ac["AC-01"].evidence == "test"
    assert ac["AC-02"].test == ""                     # setup fills it later
    assert (ac["AC-04"].threshold, ac["AC-04"].evidence) == ("10 seconds", "metric")
    assert ac["AC-04"].mandatory

    (op,) = doc.operator_checks
    assert (op.id, op.goals, op.sign_off) == ("OP-01", ("G2",), "Dan")
    assert op.expected == "every category total is the same."

    (inv,) = doc.invariants
    assert (inv.id, inv.checked_by) == ("INV-01", ("AC-01",))
    assert inv.text == "no entry is ever stored twice"
    (to,) = doc.trade_offs
    assert (to.id, to.decision, to.bound) == ("TO-01", "D-7", "12 seconds")
    assert to.expires == "when the bank adapter lands"

    assert doc.authority["channel"] == "telegram"
    assert doc.authority["ask dan"].startswith("new or changed goals")
    assert doc.authority["stop a failed approach when"].startswith("two attempts")
    assert [h.rev for h in doc.history] == [1, 2]
    assert doc.history[1].approved_by == "Dan" and doc.history[1].impact == "AC-04"
    assert doc.retired == {"G4": goals.Retired("G4", 2, "CSV export of the ledger is a "
                                                         "non-goal now.")}


def test_a_chatgpt_written_v2_contract_parses_like_the_skill_one():
    """R12: the ChatGPT path (em-dashes, `*` lists, numbered headings, ids without the
    hyphen, `·`-joined fields) gives the same structure."""
    doc = goals.parse(CHATGPT_V2)
    assert doc.format == 2 and doc.revision == 1
    assert [p.text for p in doc.issues if p.severity != "note"] == []
    assert doc.by_id["G1"].verified_ids == ("AC-1",)
    assert doc.by_id["G1"].constraints == ("INV-1",)
    (s1,) = doc.scenarios
    assert (s1.id, s1.criteria, s1.operator_checks) == ("S-1", ("AC-1",), ("OP-1",))
    (ac,) = doc.criteria
    assert (ac.goals, ac.scenarios, ac.layer, ac.evidence) == (("G1",), ("S-1",), "e2e", "metric")
    assert doc.trade_offs == ()                       # "None." is an answer
    assert doc.priority == ("G2", "G1")
    assert doc.authority["channel"] == "telegram"


# --- severities (R03, R31 groundwork) ------------------------------------------------------


@pytest.mark.parametrize("count", [2, 5, 9])
def test_any_goal_count_is_valid_with_at_most_a_note(count):
    """R03: 3-7 is guidance, never a validity limit."""
    blocks = "".join(f"### G{i}: Goal {i}\n\n- **Target:** t{i}\n"
                     f"- **Verified by:** command: `pytest -k g{i}`\n\n"
                     for i in range(1, count + 1))
    text = SKILL_STYLE.split("## Goals")[0] + "## Goals\n\n" + blocks + \
        "## Final product" + SKILL_STYLE.split("## Final product")[1].split("## Priority")[0] + \
        "## Priority\n\n" + " > ".join(f"G{i}" for i in range(1, count + 1)) + "\n"
    doc = goals.parse(text)
    assert doc.problems == ()
    count_notes = [n for n in doc.notes if "3-7" in n]
    assert bool(count_notes) == (count not in range(3, 8))


def test_a_long_purpose_is_a_note_not_a_problem():
    text = V2.replace("without a spreadsheet.", "without a spreadsheet. It is local. Free.")
    doc = goals.parse(text)
    assert doc.problems == ()
    assert any(n.startswith("purpose has 4 sentences") for n in doc.notes)


def test_problems_carry_the_ids_they_affect():
    doc = goals.parse(V2.replace("- **Target:** under 10 seconds for a year of exports.\n", ""))
    (problem,) = [p for p in doc.issues if p.severity == "gate"]
    assert problem.ids == ("G3",)


# --- ids (R04) ---------------------------------------------------------------------------


def test_a_retired_id_reused_is_a_gate_problem():
    text = V2.replace("### G3: Fast enough", "### G4: Fast enough").replace(
        "- **Goals:** G3\n", "- **Goals:** G4\n").replace("3. G3", "3. G4").replace(
        "- **Depends on:** G1\n\n## Final", "- **Depends on:** G1\n\n## Final")
    doc = goals.parse(text)
    assert "G4 is retired (rev 2) and cannot be reused" in _issues(doc, "gate")


@pytest.mark.parametrize("old, new, message", [
    ("### S-02: A broken export", "### S-01: A broken export", "duplicate id S-01"),
    ("### AC-03: Totals", "### AC-02: Totals", "duplicate id AC-02"),
    ("### OP-01: Summary reads right", "### OP: Summary reads right",
     "heading '### OP: Summary reads right' has no id"),
])
def test_duplicate_and_missing_ids_are_gate_problems(old, new, message):
    doc = goals.parse(V2.replace(old, new))
    assert any(p.startswith(message) for p in _issues(doc, "gate")), doc.issues


def test_unknown_references_are_gate_problems_naming_the_referrer():
    doc = goals.parse(V2.replace("- **Criteria:** AC-02", "- **Criteria:** AC-09")
                      .replace("- **Constraints:** INV-01\n- **Depends on:** G1",
                               "- **Constraints:** INV-07\n- **Depends on:** G1"))
    gate = _issues(doc, "gate")
    assert "S-02 names unknown id AC-09" in gate
    assert "G2 names unknown id INV-07" in gate


# --- links both ways (R06, R14, R69 groundwork) ----------------------------------------------


def test_every_goal_needs_a_criterion_or_operator_check():
    doc = goals.parse(V2.replace("- **Verified by:** AC-04\n", "")
                      .replace("- **Goals:** G3\n", "- **Goals:** G1\n"))
    assert "G3 has no acceptance criterion or operator check" in _issues(doc, "gate")


def test_a_link_named_on_one_side_counts_and_the_mismatch_is_a_draft_problem():
    doc = goals.parse(V2.replace("- **Verified by:** AC-04\n", ""))
    # AC-04 still names G3, so G3 is verified; the goal just does not say so.
    assert "G3 has no acceptance criterion or operator check" not in _issues(doc, "gate")
    assert "AC-04 names G3 but G3 does not list AC-04" in _issues(doc, "draft")
    assert doc.verifiers("G3") == ("AC-04",)


def test_orphan_scenarios_and_criteria_are_gate_problems():
    text = V2.replace("""- **Goals:** G1
- **Kind:** error""", "- **Kind:** error").replace("""- **Goals:** G3
- **Scenarios:** S-03
- **Given:** a year""", "- **Given:** a year").replace("- **Verified by:** AC-04\n", "").replace(
        "- **Criteria:** AC-04\n", "")
    gate = _issues(goals.parse(text), "gate")
    assert "S-02 names no goal" in gate
    assert "AC-04 names no goal or scenario" in gate


def test_operator_checks_need_instructions_and_an_expected_observation():
    doc = goals.parse(V2.replace("- **Expected:** every category total is the same.\n", ""))
    assert "OP-01 has no expected observation" in _issues(doc, "gate")
    assert all(c.id != "OP-01" for c in doc.criteria)   # never an automated criterion (R14)


def test_a_v2_contract_with_no_scenario_is_a_gate_problem():
    start = V2.index("### S-01")
    text = V2[:start] + V2[V2.index("## Acceptance criteria"):]
    for sid in ("S-01", "S-02", "S-03"):
        text = text.replace(f"- **Scenarios:** {sid}\n", "")
    assert "final product names no scenario" in _issues(goals.parse(text), "gate")


# --- no invented values (R05, C10) ---------------------------------------------------------


def test_a_missing_target_or_oracle_is_an_open_question_never_a_default():
    doc = goals.parse(V2.replace("- **Target:** under 10 seconds for a year of exports.\n", "")
                      .replace("- **Oracle:** exact equality per category\n", ""))
    gate = _issues(doc, "gate")
    assert "G3: no target (ask Dan; never invented)" in gate
    assert "AC-03: no oracle (ask Dan; never invented)" in gate
    assert doc.by_id["G3"].target == ""


def test_an_open_question_blocks_the_affected_ids_only():
    doc = goals.parse(V2.replace("- **Threshold:** 10 seconds",
                                 "- **Open question:** median or p95?"))
    (problem,) = [p for p in doc.issues if p.severity == "gate"]
    assert problem.text == "AC-04: open question for Dan: median or p95?"
    assert problem.ids == ("AC-04", "G3")


# --- revisions and change history (R11, R67) -----------------------------------------------


def test_a_v2_contract_without_a_revision_is_a_gate_problem():
    doc = goals.parse(V2.replace("Contract revision: 2 (approved 2026-10-17 by Dan)\n", ""))
    assert doc.revision is None
    assert "no contract revision" in _issues(doc, "gate")


def test_a_revision_without_its_history_row_is_a_gate_problem():
    doc = goals.parse(V2.replace("Contract revision: 2", "Contract revision: 3"))
    assert "contract revision 3 has no change-history row" in _issues(doc, "gate")


def test_changes_between_revisions_name_what_turned_stale():
    old = goals.parse(V2)
    new = goals.parse(
        V2.replace("Contract revision: 2", "Contract revision: 3")
        .replace("- **Threshold:** 10 seconds", "- **Threshold:** 8 seconds")
        .replace("| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |",
                 "| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |\n"
                 "| 3 | 2026-10-18 | AC-04 8 s | faster disks | AC-04 | Dan | rev 2 AC-04 |"))
    diff = goals.changes(old, new)
    assert diff.changed == ("AC-04",)
    assert set(diff.stale) == {"AC-04", "G3", "S-03"}    # the criterion and what it proves
    assert diff.problems == ()


def test_a_silent_change_or_removal_is_an_illegal_revision():
    old = goals.parse(V2)
    silent = goals.parse(V2.replace("- **Threshold:** 10 seconds", "- **Threshold:** 20 seconds"))
    diff = goals.changes(old, silent)
    assert [p.text for p in diff.problems] == [
        "AC-04 changed without a new contract revision"]
    assert diff.problems[0].severity == "gate"

    start = V2.index("### AC-02")
    removed = V2[:start] + V2[V2.index("### AC-03"):]
    removed = removed.replace("AC-01, AC-02", "AC-01").replace("- **Criteria:** AC-02",
                                                               "- **Criteria:** AC-01")
    diff = goals.changes(old, goals.parse(removed))
    assert "AC-02 removed without being retired" in [p.text for p in diff.problems]

    lower = goals.parse(V2.replace("Contract revision: 2", "Contract revision: 1"))
    assert "contract revision went back from 2 to 1" in [
        p.text for p in goals.changes(old, lower).problems]


# --- render and migrate (R02, R05, R62) ----------------------------------------------------


@pytest.mark.parametrize("text", [V2, CHATGPT_V2], ids=["skill", "chatgpt"])
def test_render_round_trips(text):
    doc = goals.parse(text)
    again = goals.parse(goals.render(doc))
    for field in ("format", "revision", "approval", "purpose", "users", "goals", "scenarios",
                  "criteria", "operator_checks", "non_goals", "invariants", "trade_offs",
                  "priority", "authority", "history", "retired", "final_product"):
        assert getattr(again, field) == getattr(doc, field), field


def test_migrate_keeps_every_id_and_text_and_invents_nothing():
    v1 = goals.parse(CHATGPT.read_text(encoding="utf-8"))
    skeleton = goals.migrate(v1)
    doc = goals.parse(skeleton)
    assert doc.format == 2 and doc.revision == 1
    assert doc.ids == v1.ids
    for old in v1.goals:
        new = doc.by_id[old.id]
        assert (new.name, new.target) == (old.name, old.target)
    assert doc.priority == v1.priority and doc.non_goals == v1.non_goals
    assert doc.users == v1.users and doc.purpose == v1.purpose
    # verification moves into criteria / operator checks, with the v1 words kept
    g2 = doc.by_id["G2"]                              # command
    (ac,) = [c for c in doc.criteria if c.id in g2.verified_ids]
    assert ac.test == "pytest tests/acceptance/test_search.py"
    g3 = doc.by_id["G3"]                              # observation
    (op,) = [o for o in doc.operator_checks if o.id in g3.verified_ids]
    assert "reads each one without zooming" in op.instructions
    # what v1 cannot say is asked, not invented
    gate = _issues(doc, "gate")
    assert any(p.startswith("Final product: open question for Dan") for p in gate)
    assert any(p.startswith("Hard invariants: open question for Dan") for p in gate)
    assert any(p.startswith("Authority: open question for Dan") for p in gate)
    assert "contract revision 1 is not approved (no approver in its change-history row)" in gate
    assert "Open question" in skeleton and "ask Dan" in skeleton


def test_migrate_of_a_v2_file_is_refused():
    with pytest.raises(ValueError, match="already format 2"):
        goals.migrate(goals.parse(V2))


# --- the CLI -------------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "maestro.cli", "goals", *args],
                          capture_output=True, text=True, cwd=REPO_ROOT,
                          timeout=HANG_SECONDS)


def test_check_prints_severities_and_gate_mode_fails_only_on_gate(tmp_path):
    path = tmp_path / "GOALS.md"
    path.write_text(V2.replace("## Non-goals\n\n- No budgeting or alerts.\n\n", "")
                    .replace("- **Verified by:** AC-04\n", "")
                    .replace("- **Goals:** G3\n", "- **Goals:** G1\n"), encoding="utf-8")
    run = _cli("check", str(path))
    assert run.returncode == 1
    assert "[gate] G3 has no acceptance criterion or operator check" in run.stdout
    assert "[draft] missing section: Non-goals" in run.stdout
    assert _cli("check", str(path), "--gate").returncode == 1

    path.write_text(V2.replace("## Non-goals\n\n- No budgeting or alerts.\n\n", ""),
                    encoding="utf-8")
    assert _cli("check", str(path)).returncode == 1          # draft: setup asks Dan
    assert _cli("check", str(path), "--gate").returncode == 0


def test_check_json_reports_format_revision_and_issues():
    doc = json.loads(_cli("check", str(FIXTURES / "v2_GOALS.md"), "--json").stdout)
    assert (doc["format"], doc["revision"], doc["problems"]) == (2, 2, [])
    assert doc["goals"] == ["G1", "G2", "G3"] and doc["issues"] == []
    doc = json.loads(_cli("check", str(CHATGPT), "--json").stdout)
    assert doc["format"] == 1 and doc["problems"] == []
    assert [i["severity"] for i in doc["issues"]] == ["note"]


def test_migrate_cli_prints_the_skeleton_and_never_writes(tmp_path):
    path = tmp_path / "GOALS.md"
    original = CHATGPT.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    run = _cli("migrate", str(path))
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("# Goals") and "Format: 2" in run.stdout
    assert path.read_text(encoding="utf-8") == original
    v2 = tmp_path / "v2.md"
    v2.write_text(V2, encoding="utf-8")
    run = _cli("migrate", str(v2))
    assert run.returncode == 1 and "already format 2" in run.stderr


def test_check_json_marks_only_dan_s_gaps_as_questions(tmp_path):
    """R12 (GDT-13): setup asks Dan only what he must decide. A missing target or oracle is
    his (`ask: true`); a link named on one side and an unbound criterion are fixed without
    him (`ask: false`, and the unbound one is not a contract problem at all)."""
    parity = (FIXTURES / "parity" / "skill_GOALS.md").read_text(encoding="utf-8")
    text = (parity
            .replace("- **Target:** Dan reads three recipes without zooming.\n", "")
            .replace("- **Oracle:** the right recipe is in the top 3 for at least 9 of the 10 "
                     "searches\n", "")
            .replace("- **Verified by:** AC-3, AC-4", "- **Verified by:** AC-3")
            .replace("- **Test:** `pytest tests/acceptance/test_search.py`\n", ""))
    path = tmp_path / "GOALS.md"
    path.write_text(text, encoding="utf-8")
    issues = json.loads(_cli("check", str(path), "--json").stdout)["issues"]
    assert all(isinstance(i.get("ask"), bool) for i in issues), issues
    assert [i["text"] for i in issues if i["ask"]] == [
        "G2: no target (ask Dan; never invented)", "AC-3: no oracle (ask Dan; never invented)"]
    assert {"severity": "draft", "text": "AC-4 names G3 but G3 does not list AC-4",
            "ids": ["G3", "AC-4"], "ask": False} in issues
