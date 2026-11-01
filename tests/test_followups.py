"""Review follow-ups become one follow-up task at graduation (Dan, 2026-09-26).

`maestro.docs.followups`: collected across every run of the accepted workflow, queued as
`<id>-followups` in the task's place, never chained, and briefed to a different bar. The
graduation commit itself is covered end to end in
`tests/graph_engineering/test_end_to_end.py`.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest
import yaml

from maestro import taskgraph
from maestro.docs import complete, followups
from maestro.workflows import worker


# --- a DuetFlow-shaped ROADMAP: bare consecutive yaml blocks (P12 finding D18) -------


def _block(**fields) -> str:
    return "```yaml\n" + yaml.safe_dump(fields, sort_keys=False) + "```\n"


PARENT = {
    "id": "02-collector", "status": "open", "mode": "autonomous", "deps": ["01-auth"],
    "self_modifying": False, "hold": False, "title": "Collector",
    "scope": ["duetflow/collector.py"], "resources": {"cpu": 1}, "resource": "spotify",
    "verifications": [
        {"id": "V1", "kind": "auto", "check": "collects", "cmd": "pytest -k collect"},
        {"id": "V2", "kind": "dan", "check": "Dan looks at it"},
        {"id": "V3", "kind": "auto", "await": True, "cmd": "wait-for-a-day"},
    ],
    "questions": [{"id": "q", "prompt": "?"}],
}
LATER = {"id": "03-reader", "status": "open", "mode": "autonomous",
         "deps": ["02-collector"], "title": "Reader"}


def _roadmap(*, headings: bool = False) -> str:
    parts = ["# Roadmap\n\nProse.\n"]
    for task in (PARENT, LATER):
        if headings:
            parts.append(f"### {task['id']} — {task['title']}\n\nHand-written prose.\n")
        parts.append(_block(**task))
    return "\n".join(parts)


ITEMS = [
    followups.FollowUp("guard JSON decoding on the data path", 1, "proof_review", "abc123"),
    followups.FollowUp("clear_access() is dead code", 2, "proof_review"),
]


def _ids(text: str) -> list[str]:
    return [str(t["id"]) for t in taskgraph.scan_tasks(text)]


# --- collect ---------------------------------------------------------------------------


class _Artifacts:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def put(self, key: str, doc) -> str:
        self.blobs[key] = json.dumps(doc).encode("utf-8")
        return key

    def read(self, key: str) -> bytes:
        return self.blobs[key]


class _Store:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE task_runs (run_id TEXT, workflow_id TEXT);"
            "CREATE TABLE attempts (run_id TEXT, node_id TEXT, outcome_ref TEXT,"
            " manifest_artifact_id TEXT);"
        )


def _review(follow_ups, verdict="approved", before_dependents=None):
    review = {"verdict": verdict, "findings": [], "follow_ups": follow_ups}
    if before_dependents is not None:
        review["before_dependents"] = before_dependents
    return {"status": "succeeded", "outputs": {"review": review}}


def test_collect_reads_every_run_of_the_workflow_deduplicated_with_its_round():
    store, artifacts = _Store(), _Artifacts()
    store.conn.executemany("INSERT INTO task_runs VALUES (?, ?)", [
        ("run_a", "wf_02"), ("run_b", "wf_02"), ("run_x", "wf_other")])
    rows = [
        ("run_a", "implementer", artifacts.put("o1", {"status": "succeeded"}), None),
        ("run_a", "proof_review", artifacts.put("o2", _review(["SQL outside db.py"])),
         artifacts.put("m2", {"reviewed_commit": "0123456789abcdef"})),
        # A repair's successor run: its review is round 2 of the same review.
        ("run_b", "proof_review", artifacts.put("o3", _review([
            "SQL  outside db.py",     # the same follow-up again: kept once
            {"finding": "no runs row on failure", "severity": "follow_up",
             "rule": "every run is logged", "verify": "a test"},
        ])), None),
        ("run_x", "proof_review", artifacts.put("o4", _review(["another task's"])), None),
        ("run_b", "proof_review", "missing", None),   # unreadable: says nothing
    ]
    store.conn.executemany("INSERT INTO attempts VALUES (?, ?, ?, ?)", rows)

    items = followups.collect(store, artifacts, "wf_02")

    assert [(i.text, i.round, i.commit) for i in items] == [
        ("SQL outside db.py", 1, "0123456789ab"),
        ("no runs row on failure Rule: every run is logged. Verify: a test.", 2, ""),
    ]
    assert followups.collect(store, None, "wf_02") == []


def test_collect_marks_an_item_before_dependents_when_any_round_flagged_it():
    """GDT-11 S1: round 1 raised it plain, round 2 flagged it (worded with other
    whitespace); the item stays round 1's and carries the mark. A text no round flagged,
    or one flagged only in another workflow, carries none."""
    store, artifacts = _Store(), _Artifacts()
    store.conn.executemany("INSERT INTO task_runs VALUES (?, ?)", [
        ("run_a", "wf_02"), ("run_x", "wf_other")])
    rows = [
        ("run_a", "proof_review", artifacts.put("o1", _review(
            ["the schema field is misnamed", "SQL outside db.py"])), None),
        ("run_a", "proof_review", artifacts.put("o2", _review(
            ["the  schema field is misnamed"],
            before_dependents=["the  schema field is misnamed"])), None),
        ("run_x", "proof_review", artifacts.put("o3", _review(
            ["SQL outside db.py"], before_dependents=["SQL outside db.py"])), None),
    ]
    store.conn.executemany("INSERT INTO attempts VALUES (?, ?, ?, ?)", rows)

    items = followups.collect(store, artifacts, "wf_02")

    assert [(i.text, i.round, i.before_dependents) for i in items] == [
        ("the schema field is misnamed", 1, True), ("SQL outside db.py", 1, False)]
    assert followups.FollowUp("x", 1, "proof_review").before_dependents is False


# --- queue the follow-up task --------------------------------------------------------


@pytest.mark.parametrize("headings", [False, True])
def test_the_follow_up_task_takes_the_parents_place_and_survives_its_strip(headings):
    content, task_id = followups.queue_task(_roadmap(headings=headings), PARENT, ITEMS)
    assert task_id == "02-collector-followups"
    assert _ids(content) == ["02-collector", "02-collector-followups", "03-reader"]

    stripped, title = complete.remove_task_section(content, "02-collector")
    assert title == "Collector"
    assert _ids(stripped) == ["02-collector-followups", "03-reader"]
    if headings:
        assert "### 02-collector-followups — Review follow-ups of 02-collector" in stripped
        assert "### 03-reader" in stripped and "### 02-collector —" not in stripped

    # Schedulable: valid with the parent graduated, and its dependents still wait on the
    # parent only (nothing rewired).
    document = taskgraph.parse_document(stripped)
    assert taskgraph.validate(document, known_ids={"01-auth", "02-collector"}) == []
    tasks = {str(t["id"]): t for t in taskgraph.scan_tasks(stripped)}
    assert tasks["03-reader"]["deps"] == ["02-collector"]
    spec = taskgraph.normalize_spec(tasks["02-collector-followups"])
    assert spec.depends_on == ("02-collector",)
    assert spec.execution_kind == "ordinary"

    # And the follow-up task's own strip leaves the rest alone.
    again, _ = complete.remove_task_section(stripped, "02-collector-followups")
    assert _ids(again) == ["03-reader"]


def test_the_follow_up_task_carries_the_parents_scope_and_one_criterion_per_item():
    task = followups.build_task(PARENT, ITEMS)

    assert task["mode"] == "autonomous" and "dispatch" not in task
    assert task["deps"] == ["02-collector"]
    assert task[followups.MARKER] == "02-collector"
    assert (task["scope"], task["resources"], task["resource"]) == (
        ["duetflow/collector.py"], {"cpu": 1}, "spotify")
    assert "questions" not in task
    assert task["acceptance_criteria"] == [
        "(review round 1, proof_review @ abc123) guard JSON decoding on the data path",
        "(review round 2, proof_review) clear_access() is dead code",
    ]
    # The gate re-runs the parent's automatic checks, never Dan's or an awaited one.
    assert [v["id"] for v in task["verifications"]] == ["V1"]
    spec = taskgraph.normalize_spec(task)
    assert spec.acceptance_criteria == tuple(task["acceptance_criteria"])


def test_a_retried_graduation_queues_the_follow_up_task_once():
    content, _ = followups.queue_task(_roadmap(), PARENT, ITEMS)
    assert followups.queue_task(content, PARENT, ITEMS) is None


def test_record_writes_the_roadmap_and_registers_the_block_in_the_cas(tmp_path):
    repo = tmp_path
    roadmap = taskgraph.roadmap_path(repo)
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text(_roadmap(), encoding="utf-8")
    (repo / ".orchestrator").mkdir()
    taskgraph.registry_path(repo).write_text(json.dumps([{"id": "01-auth"}]))
    taskgraph.record_applied(repo, taskgraph.load_document(roadmap))

    # GDT-11 S1: only items the reviewer marked `before_dependents` queue a task, and
    # only while a task depends on the parent (03-reader does); the rest wait in the
    # backlog (`tests/test_backlog.py`).
    flagged = [dataclasses.replace(i, before_dependents=True) for i in ITEMS]
    recorded = followups.record(repo, roadmap, dict(PARENT), flagged)

    assert recorded == followups.Recorded(count=2, queued_as="02-collector-followups",
                                          wired=("03-reader",))
    assert recorded.summary() == (
        "2 review follow-up(s): 2 queued as 02-collector-followups (before 03-reader)")
    assert "02-collector-followups" in _ids(roadmap.read_text(encoding="utf-8"))
    # Not a hand edit: the drift guard does not withhold it from dispatch.
    assert not {"02-collector-followups", "03-reader"} & taskgraph.blocked_task_ids(repo)
    assert followups.record(repo, roadmap, dict(PARENT), []) is None


# --- the bar: briefs ---------------------------------------------------------------


def test_only_a_follow_up_task_is_briefed_to_the_follow_up_bar():
    task = followups.build_task(PARENT, ITEMS)
    writer = followups.contract(task, reviewer=False)
    reviewer = followups.contract(task, reviewer=True)

    assert followups.contract(PARENT, reviewer=False) == ""
    assert "still relevant" in writer and "declined" in writer and "no new scope" in writer
    assert "declined with a reason" in reviewer and "new scope" in reviewer
    assert "blocking" in reviewer

    brief = worker.agent_brief({"acceptance_criteria": ["x"], "followup_contract": writer})
    assert "FOLLOW-UP TASK" in brief and "- x" in brief
    assert "FOLLOW-UP TASK" not in worker.agent_brief({"acceptance_criteria": ["x"]})


# --- graduation wiring: the accept report --------------------------------------------


def test_graduation_hands_the_accept_report_where_the_follow_ups_went(monkeypatch):
    from maestro import orchestrator

    recorded = followups.Recorded(count=3, queued_as="T1-followups")
    calls: dict = {}
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: {"id": tid})
    monkeypatch.setattr(orchestrator, "_graph_accepted_run", lambda store, tid: None)

    def mark(task_id, follow_ups=()):
        calls["mark"] = follow_ups
        return recorded

    monkeypatch.setattr(orchestrator, "mark_roadmap_complete", mark)
    monkeypatch.setattr(orchestrator, "read_state", lambda: {})
    monkeypatch.setattr(orchestrator, "_graph_push_target", lambda: None)
    monkeypatch.setattr(orchestrator, "append_journal", lambda *a: None)
    monkeypatch.setattr(orchestrator, "phase_report",
                        lambda tid, td, smoke, follow_ups="": calls.update(report=follow_ups))

    orchestrator._graph_graduate(object(), "T1")

    assert calls == {"mark": [],
                     "report": "3 review follow-up(s): 3 queued as T1-followups"}
