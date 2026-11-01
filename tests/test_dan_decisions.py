"""Dan's decisions on agents' spec questions (P12 pilot finding D26): the question schema,
its Telegram rendering, and the ROADMAP/ADR record of his answer."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from maestro import taskgraph
from maestro.docs import decisions
from maestro.hitl import verify
from maestro.workflows import dan_questions

QUESTION = {
    "question": "At cold start, which half owns a track in both pools?",
    "why_undefined": "Criteria and ADR-0003 are silent; the split changes the selector.",
    "options": [
        {"label": "Shared counts for B", "consequence": "The Inferred Half takes it."},
        {"label": "Shared counts for A", "consequence": "The Sampled Half takes it."},
    ],
    "recommended": 0,
}


# --- the schema ---------------------------------------------------------------------


def test_a_round_parses_from_the_wrapper_a_list_or_one_bare_question():
    for raw in ({"questions": [QUESTION]}, [QUESTION], QUESTION, json.dumps(QUESTION)):
        questions, error = dan_questions.parse(raw)
        assert error == "" and questions[0]["question"] == QUESTION["question"]
        assert questions[0]["options"][0] == QUESTION["options"][0]


@pytest.mark.parametrize("broken, reason", [
    ({**QUESTION, "options": QUESTION["options"][:1]}, "2-4 options"),
    ({**QUESTION, "options": QUESTION["options"] * 3}, "2-4 options"),
    ({**QUESTION, "recommended": 2}, "recommended"),
    ({**QUESTION, "recommended": True}, "recommended"),
    ({**QUESTION, "why_undefined": ""}, "why_undefined"),
    ({**QUESTION, "question": " "}, "no question"),
])
def test_a_malformed_question_refuses_the_whole_round(broken, reason):
    questions, error = dan_questions.parse({"questions": [QUESTION, broken]})
    assert questions == [] and reason in error and error.startswith("question 2")


def test_a_round_is_capped_at_three_questions():
    questions, error = dan_questions.parse([QUESTION] * 4)
    assert questions == [] and "at most 3" in error


def test_not_json_is_a_parse_error():
    assert dan_questions.parse("{nope")[1].startswith("not JSON")


def test_decisions_render_as_a_binding_brief_section():
    text = dan_questions.render_decisions([{"question": "Q1?", "answer": "A1"}])
    assert text.startswith("DAN'S DECISIONS (binding") and "- Q: Q1?\n  A: A1" in text
    assert dan_questions.render_decisions([]) == ""


def test_the_re_ask_guard_folds_case_and_punctuation():
    assert dan_questions.normalise("Which HALF, owns it?") == \
        dan_questions.normalise("which half owns it")


# --- Telegram -----------------------------------------------------------------------


def test_the_question_message_numbers_the_options_and_stars_the_recommended_one():
    text, markup = verify.render_question("T-1", "Writer first light", "dq-T-1-abc", QUESTION)
    assert text.startswith("❓ <b>Decision needed — Writer first light</b>")
    assert QUESTION["question"] in text and "ADR-0003 are silent" in text
    assert "1 ⭐ <b>Shared counts for B</b> — The Inferred Half takes it." in text
    assert "2 <b>Shared counts for A</b>" in text
    assert "✍️ Other — reply to this message" in text
    buttons = markup["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["1 ⭐", "2"]
    assert [b["callback_data"] for b in buttons] == ["danreq:dq-T-1-abc:0",
                                                      "danreq:dq-T-1-abc:1"]


def test_the_outcome_line_names_the_chosen_option_or_the_free_text():
    record = {"type": verify.QUESTION_TYPE, "options": ["Shared counts for B", "Other"]}
    assert verify.outcome_line(record, "Shared counts for B") == \
        "<b>✅ Chosen: Shared counts for B</b>"
    assert verify.outcome_line(record, "neither: drop it") == \
        "<b>✅ Chosen: neither: drop it</b>"


# --- the ROADMAP edit ---------------------------------------------------------------

ROADMAP = """# Roadmap

### T-1 — Writer

```yaml
id: T-1
title: Writer   # Dan's comment
deps: []
notes: |
  keep me
verifications:
  - id: V1
    kind: auto
    check: "tests pass"
```

### T-2 — Other

```yaml
id: T-2
deps: [T-1]
```
"""

ITEM = {"at": "2026-09-28T10:00:00Z", "asked_by": "reviewer/review#1",
        "question": "Which half?", "answer": "Shared counts for B: the Inferred Half"}


def _task(content, task_id="T-1"):
    return next(t for t in taskgraph.scan_tasks(content) if t["id"] == task_id)


def test_a_first_decision_adds_the_key_and_keeps_comments_and_other_blocks():
    new = decisions.add_decision(ROADMAP, "T-1", ITEM)
    assert _task(new)["decisions"] == [ITEM]
    assert "title: Writer   # Dan's comment" in new
    assert new.split("### T-2")[1] == ROADMAP.split("### T-2")[1]
    assert _task(new)["notes"] == "keep me\n"


def test_a_later_decision_appends_after_the_existing_list():
    second = {**ITEM, "question": "Which order?", "answer": "Newest first"}
    once = decisions.add_decision(ROADMAP, "T-1", ITEM)
    twice = decisions.add_decision(once, "T-1", second)
    assert _task(twice)["decisions"] == [ITEM, second]


def test_a_list_written_at_column_zero_keeps_its_style():
    content = ROADMAP.replace(
        "deps: []\n", "deps: []\ndecisions:\n- question: Old?\n  answer: Kept\n")
    new = decisions.add_decision(content, "T-1", ITEM)
    assert _task(new)["decisions"] == [{"question": "Old?", "answer": "Kept"}, ITEM]


def test_an_unknown_task_is_refused():
    with pytest.raises(decisions.DecisionError):
        decisions.add_decision(ROADMAP, "T-9", ITEM)


# --- the commit and the ADR ---------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "proj"
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
    (repo / "docs" / "adr" / "0001-halves.md").write_text(
        "# The halves\n\nTwo halves.\n", encoding="utf-8")
    (repo / "docs" / "adr" / "0002-refresh.md").write_text(
        "# Refresh\n\nBounded.\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    cas = taskgraph.parse_document(ROADMAP)
    taskgraph._atomic_write(taskgraph.cas_path(repo), json.dumps({
        "document_hash": cas.document_hash, "tasks": cas.block_hashes(),
        "recorded_at": "x", "schema_version": 1,
    }))
    return repo


def test_the_decision_and_an_appended_adr_paragraph_land_in_one_docs_commit(
        project, monkeypatch):
    prompts = []

    def agent(prompt, cwd):
        prompts.append(prompt)
        return '```json\n{"action": "append", "path": "docs/adr/0001-halves.md", ' \
               '"paragraph": "A shared track counts for B (Dan, 2026-09-28)."}\n```'

    monkeypatch.setattr(decisions, "_adr_agent", agent)
    sha = decisions.record(project, "T-1", ITEM)
    assert sha == _git(project, "rev-parse", "HEAD")
    changed = _git(project, "show", "--name-only", "--format=%s", "HEAD").splitlines()
    assert changed[0] == "roadmap(T-1): record Dan's decision (reviewer/review#1)"
    assert sorted(changed[2:]) == ["docs/ROADMAP.md", "docs/adr/0001-halves.md"]
    assert (project / "docs/adr/0001-halves.md").read_text().endswith(
        "Two halves.\n\nA shared track counts for B (Dan, 2026-09-28).\n")
    assert "# The halves" in prompts[0] and "# Refresh" in prompts[0]
    # the CAS records the new block, so drift does not read it as a hand edit
    record = taskgraph.read_cas(project)
    now = taskgraph.parse_document((project / "docs/ROADMAP.md").read_text())
    assert record.tasks["T-1"] == now.block_hashes()["T-1"]
    assert record.document_hash == now.document_hash


def test_no_fitting_adr_writes_the_next_numbered_one(project, monkeypatch):
    monkeypatch.setattr(decisions, "_adr_agent", lambda prompt, cwd: json.dumps({
        "action": "new", "slug": "Shared Tracks", "title": "Shared tracks",
        "body": "# Shared tracks\n\nThey count for B (Dan, 2026-09-28)."}))
    decisions.record(project, "T-1", ITEM)
    assert (project / "docs/adr/0003-shared-tracks.md").read_text().startswith(
        "# Shared tracks")
    assert "docs/adr/0003-shared-tracks.md" in _git(project, "show", "--name-only", "HEAD")


def test_an_agent_path_outside_the_adrs_is_never_written(project, monkeypatch):
    calls = []
    journal = []

    def agent(prompt, cwd):
        calls.append(1)
        return json.dumps({"action": "append", "path": "../../etc/passwd", "paragraph": "x"})

    monkeypatch.setattr(decisions, "_adr_agent", agent)
    decisions.record(project, "T-1", ITEM, journal=lambda e, d: journal.append(e))
    assert len(calls) == 2 and journal == ["dan_decision_adr_failed"]
    assert _git(project, "show", "--name-only", "--format=", "HEAD") == "docs/ROADMAP.md"


def test_a_project_without_adrs_skips_the_adr_and_says_why(project, monkeypatch):
    subprocess.run(["git", "rm", "-rqf", "docs/adr"], cwd=project, check=True)
    _git(project, "commit", "-qm", "no adrs")
    monkeypatch.setattr(decisions, "_adr_agent", lambda *a: pytest.fail("no agent call"))
    journal = []
    sha = decisions.record(project, "T-1", ITEM, journal=lambda e, d: journal.append((e, d)))
    assert sha and journal == [("dan_decision_adr_skipped", "T-1: the project has no docs/adr/")]


def test_a_hand_edited_block_is_not_laundered_into_the_cas(project, monkeypatch):
    monkeypatch.setattr(decisions, "_adr_agent", lambda *a: "")
    edited = ROADMAP.replace("title: Writer", "title: Writer v2")
    (project / "docs/ROADMAP.md").write_text(edited)
    _git(project, "commit", "-qam", "Dan's edit")
    old = taskgraph.read_cas(project)
    decisions.record(project, "T-1", ITEM, adr=False)
    assert taskgraph.read_cas(project).tasks["T-1"] == old.tasks["T-1"]


def test_an_uncommitted_roadmap_edit_is_written_but_left_uncommitted(project):
    (project / "docs/ROADMAP.md").write_text(ROADMAP + "\nDan is typing\n")
    journal = []
    sha = decisions.record(project, "T-1", ITEM, adr=False,
                           journal=lambda e, d: journal.append(e))
    assert sha is None and journal == ["dan_decision_uncommitted"]
    assert _task((project / "docs/ROADMAP.md").read_text())["decisions"] == [ITEM]
