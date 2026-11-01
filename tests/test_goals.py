"""`docs/GOALS.md`, the goals contract (P13 W11a, B16).

grill-me writes it (`GOALS-FORMAT.md`), Dan may write it in ChatGPT web instead, and
maestro-setup validates it with `maestro goals check`, asking Dan only for what is missing.
`maestro.docs.goals` parses either style, lists what is missing, normalises a task's
`serves:` and builds the bounded excerpt every agent role's brief carries.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from maestro.docs import goals
from tests.hang_bound import HANG_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[1]
CHATGPT = REPO_ROOT / "tests" / "fixtures" / "goals" / "chatgpt_GOALS.md"

#: The skill's own style (`GOALS-FORMAT.md`).
SKILL_STYLE = """\
# Goals: Ledger

## Purpose

Ledger records household spending from bank exports. It exists so Dan can see where the
money went each month without a spreadsheet.

## Users

- **Dan**: imports exports and reads the monthly summary.

## Goals

### G1: Imports every export

- **Target:** every CSV export from the two banks imports with no manual edits.
- **Verified by:** command: `pytest tests/acceptance/test_import.py`

### G2: Monthly summary is right

- **Target:** the monthly totals match the bank statements to the shekel.
- **Verified by:** observation: Dan compares one month's summary with the statement.

### G3: Fast enough

- **Target:** a year of exports imports in under 10 seconds.
- **Verified by:** metric: `import_seconds` in the import log, below 10.

## Final product

1. Dan drops the month's exports into the inbox folder.
2. He opens the summary page and sees the month's totals by category.

## Non-goals

- No budgeting or alerts.

## Priority

1. G2
2. G1
3. G3
"""


def _parse(text: str) -> goals.Goals:
    return goals.parse(text)


# --- parsing both styles ----------------------------------------------------------------


def test_skill_style_parses_complete():
    doc = _parse(SKILL_STYLE)
    assert doc.problems == ()
    assert [g.id for g in doc.goals] == ["G1", "G2", "G3"]
    g1 = doc.by_id["G1"]
    assert g1.name == "Imports every export"
    assert g1.target.startswith("every CSV export")
    assert (g1.verified_kind, g1.verified_by) == ("command", "`pytest tests/acceptance/test_import.py`")
    assert doc.by_id["G2"].verified_kind == "observation"
    assert doc.by_id["G3"].verified_kind == "metric"
    assert doc.priority == ("G2", "G1", "G3")
    assert doc.purpose.startswith("Ledger records")
    assert doc.users == ("Dan: imports exports and reads the monthly summary.",)
    assert doc.non_goals == ("No budgeting or alerts.",)
    assert "inbox folder" in doc.final_product


def test_a_goals_md_written_in_chatgpt_web_passes_validation():
    """The Proof: a fixture written outside the skill passes setup's validation."""
    doc = goals.parse(CHATGPT.read_text(encoding="utf-8"))
    assert doc.problems == ()
    assert [g.id for g in doc.goals] == ["G1", "G2", "G3"]
    assert doc.by_id["G1"].name == "Every saved post lands in the cookbook"
    assert doc.by_id["G1"].verified_kind == "metric"
    assert doc.by_id["G2"].verified_kind == "command"
    g3 = doc.by_id["G3"]
    assert g3.verified_kind == "observation"
    assert "reads each one without zooming" in g3.verified_by  # continuation line kept
    assert doc.priority == ("G3", "G1", "G2")
    assert len(doc.users) == 2


# --- what is missing ---------------------------------------------------------------------


def _without(text: str, start: str, end: str) -> str:
    head, rest = text.split(start, 1)
    return head + rest[rest.index(end):]


def test_missing_sections_are_named():
    text = _without(SKILL_STYLE, "## Non-goals", "## Priority")
    text = _without(text, "## Users", "## Goals")
    problems = _parse(text).problems
    assert "missing section: Users" in problems
    assert "missing section: Non-goals" in problems


def test_goal_count_outside_three_to_seven_is_only_a_note():
    """R03 (GDT-1): 3-7 is guidance, never a validity limit."""
    two = _parse(_without(SKILL_STYLE, "### G3", "## Final product").replace("3. G3\n", ""))
    assert two.problems == ()
    assert any("3-7" in n for n in two.notes)


def test_goal_without_target_or_verification_is_named():
    text = SKILL_STYLE.replace(
        "- **Target:** a year of exports imports in under 10 seconds.\n", ""
    ).replace("- **Verified by:** observation: Dan compares", "- **Verified by:** Dan compares")
    problems = _parse(text).problems
    assert "G3: no measurable target" in problems
    assert any(p.startswith("G2: verification must say") for p in problems)


def test_command_verification_needs_a_command():
    text = SKILL_STYLE.replace(
        "command: `pytest tests/acceptance/test_import.py`", "command:"
    )
    assert "G1: verified by command, but names no command" in _parse(text).problems


def test_duplicate_ids_and_bad_priority_are_problems():
    text = SKILL_STYLE.replace("### G3: Fast enough", "### G2: Fast enough")
    problems = _parse(text).problems
    assert "duplicate goal id G2" in problems
    text = SKILL_STYLE.replace("3. G3\n", "3. G9\n")
    problems = _parse(text).problems
    assert "priority names unknown goal G9" in problems
    assert "priority does not rank G3" in problems


def test_purpose_longer_than_two_sentences_is_a_note():
    """`01` §2.1: Purpose is normally two sentences at most, guidance not validity."""
    text = SKILL_STYLE.replace(
        "without a spreadsheet.", "without a spreadsheet. It is local. It is free."
    )
    assert any(n.startswith("purpose has 4 sentences") for n in _parse(text).notes)


def test_empty_file_lists_every_section():
    problems = _parse("").problems
    for section in ("Purpose", "Users", "Goals", "Final product", "Non-goals", "Priority"):
        assert f"missing section: {section}" in problems


# --- load ------------------------------------------------------------------------------------


def test_load_reads_docs_goals_md_or_returns_none(tmp_path):
    assert goals.load(tmp_path) is None
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GOALS.md").write_text(SKILL_STYLE, encoding="utf-8")
    assert [g.id for g in goals.load(tmp_path).goals] == ["G1", "G2", "G3"]


# --- serves --------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, ids", [
    (["G1", "G3"], ("G1", "G3")),
    ("G2", ("G2",)),
    ("G1, G2", ("G1", "G2")),
    ([], ()),
    (None, ()),
    (["g1", " G2 "], ("G1", "G2")),
])
def test_serves_normalises(raw, ids):
    assert goals.normalise_serves(raw) == (ids, [])


def test_serves_flags_what_is_not_a_goal_id():
    ids, problems = goals.normalise_serves(["G1", "polish", 3])
    assert ids == ("G1",)
    assert problems == ["serves entry 'polish' is not a goal id", "serves entry 3 is not a goal id"]


def test_task_serves_tells_missing_from_empty():
    assert goals.task_serves({"id": "t"}) is None
    assert goals.task_serves({"id": "t", "serves": []}) == ()
    assert goals.task_serves({"id": "t", "serves": ["G2"]}) == ("G2",)


# --- the bounded excerpt -------------------------------------------------------------------


def test_excerpt_carries_served_goals_in_full_and_the_others_as_one_line():
    text, cut = goals.excerpt(_parse(SKILL_STYLE), ("G2",))
    assert cut == ""
    assert "Purpose: Ledger records household spending" in text
    assert "This task serves: G2" in text
    served = next(line for line in text.splitlines() if line.startswith("G2 "))
    assert "the monthly totals match" in served and "observation" in served
    other = next(line for line in text.splitlines() if line.startswith("G1 "))
    assert other == "G1 Imports every export"
    assert "Priority when goals conflict: G2 > G1 > G3" in text
    assert "Non-goals: No budgeting or alerts." in text


def test_excerpt_says_when_a_task_serves_no_goal_or_does_not_say():
    doc = _parse(SKILL_STYLE)
    assert "serves no goal (serves: [])" in goals.excerpt(doc, ())[0]
    assert "does not name the goals it serves" in goals.excerpt(doc, None)[0]


def test_excerpt_is_bounded_and_the_cut_is_named():
    doc = _parse(SKILL_STYLE.replace(
        "- No budgeting or alerts.", "\n".join(f"- Non-goal {i} " + "x" * 80 for i in range(200))
    ))
    text, cut = goals.excerpt(doc, ("G1",))
    assert len(text) <= goals.MAX_GOALS_TOKENS * 4
    assert "This task serves: G1" in text and "every CSV export" in text  # served goal kept
    # GDT-5 (R34): lower-priority lines are dropped whole and named with their source
    assert cut.startswith("goals_excerpt: omitted to stay within 1000 tokens: non-goals ")
    assert "(docs/GOALS.md §Non-goals)" in cut


# --- `maestro goals check` --------------------------------------------------------------------


def _check(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "maestro.cli", "goals", "check", *args],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=HANG_SECONDS,
    )


def test_goals_check_passes_the_chatgpt_fixture():
    run = _check(str(CHATGPT))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK" in run.stdout and "G1, G2, G3" in run.stdout


def test_goals_check_on_a_repo_lists_what_is_missing(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GOALS.md").write_text(
        _without(SKILL_STYLE, "## Non-goals", "## Priority"), encoding="utf-8")
    run = _check(str(tmp_path))
    assert run.returncode == 1
    assert "missing section: Non-goals" in run.stdout


def _project(tmp_path, *, goals_md=SKILL_STYLE, tasks=(), completed=None):
    (tmp_path / "docs").mkdir(exist_ok=True)
    if goals_md is not None:
        (tmp_path / "docs" / "GOALS.md").write_text(goals_md, encoding="utf-8")
    blocks = "".join(
        "```yaml\n" + "".join(f"{k}: {json.dumps(v)}\n" for k, v in t.items()) + "```\n\n"
        for t in tasks)
    (tmp_path / "docs" / "ROADMAP.md").write_text("# ROADMAP\n\n" + blocks, encoding="utf-8")
    if completed is not None:
        (tmp_path / ".orchestrator").mkdir(exist_ok=True)
        (tmp_path / ".orchestrator" / "completed_tasks.json").write_text(json.dumps(completed))
    return tmp_path


def test_coverage_warns_on_a_task_with_no_goal_and_a_goal_no_task_serves():
    doc = _parse(SKILL_STYLE)
    warnings = goals.coverage(
        doc,
        [{"id": "a", "serves": ["G1"]}, {"id": "b"}, {"id": "polish", "serves": []},
         {"id": "c", "serves": ["G9"]}],
        [{"id": "done", "serves": ["G2"]}],
    )
    assert "task b names no goal it serves (add `serves:`; `serves: []` for polish)" in warnings
    assert "task c serves unknown goal G9" in warnings
    assert "goal G3 is served by no open or completed task" in warnings
    assert not any("task polish" in w or "G1 is served" in w or "G2 is served" in w
                   for w in warnings)
    assert len(warnings) == 3


def test_doctor_goals_check_fails_an_unclassified_task_and_passes_a_whole_contract(tmp_path):
    """GDT-2 (R29, R31) supersedes W11a's "a warning, never a failure": a task with no
    `serves` is unclassified and would be held at execution, so doctor fails it."""
    from maestro import cli

    project = _project(tmp_path, tasks=[{"id": "a", "serves": ["G1"]}, {"id": "b"}],
                       completed=[{"id": "x", "serves": ["G2", "G3"]}])
    check = cli._check_goals(project)
    assert (check.name, check.ok, check.required) == ("goals", False, True)
    assert check.detail == ("blocks at execution: b (task b names no goal it serves "
                            "(add `serves:`; `serves: []` for polish))")

    whole = _project(tmp_path, tasks=[{"id": "a", "serves": ["G1"]}, {"id": "b", "serves": []}],
                     completed=[{"id": "x", "serves": ["G2", "G3"]}])
    check = cli._check_goals(whole)
    assert check.ok and "3 goals" in check.detail


def test_doctor_goals_check_without_goals_md_and_with_a_malformed_one(tmp_path):
    from maestro import cli

    check = cli._check_goals(_project(tmp_path, goals_md=None, tasks=[{"id": "a"}]))
    assert not check.ok and not check.required
    assert "no docs/GOALS.md" in check.detail
    assert "task a" not in check.detail      # one line, not one per task

    broken = _without(SKILL_STYLE, "## Non-goals", "## Priority")
    check = cli._check_goals(_project(tmp_path, goals_md=broken, tasks=[{"id": "a", "serves": []}]))
    assert not check.ok and "missing section: Non-goals" in check.detail


def test_goals_check_json_and_absent_file(tmp_path):
    run = _check(str(tmp_path), "--json")
    assert run.returncode == 1
    doc = json.loads(run.stdout)
    assert doc["exists"] is False and doc["problems"]
    run = _check(str(CHATGPT), "--json")
    doc = json.loads(run.stdout)
    assert doc["exists"] is True and doc["problems"] == []
    assert doc["goals"] == ["G1", "G2", "G3"]
