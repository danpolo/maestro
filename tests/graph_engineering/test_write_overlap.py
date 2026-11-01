"""P12 pilot finding D20: tasks whose write sets overlap never run in parallel.

DuetFlow 03 and 04 ran at once, both edited one file, and 04's approved work could not
merge. Parallel tasks must merge trivially, so the graph loop opens a ready task only when
its declared `scope:` is disjoint from every live run's scope and changed files; a task
with no `scope:` declares nothing and runs alone.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import maestro.state
from maestro import goal_replan
from maestro.backends.base import Completion

from .test_dispatch_wiring import _journal_details, _task
from .test_end_to_end import (_PerTaskAgent, _drive_until_runs_settle, _git, _happy_scenario,
                              _report_done)
from .test_failure_ladder import _patch_fixture_cli_driver


def _journal() -> list[dict]:
    lines = maestro.state.JOURNAL.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _tasks(first: dict, second: dict) -> list[dict]:
    return [
        _task("TASK-001", **first,
              verifications=[{"id": "V1", "kind": "auto", "cmd": "test -f TASK-001.txt"}]),
        _task("TASK-002", **second,
              verifications=[{"id": "V1", "kind": "auto", "cmd": "test -f TASK-002.txt"}]),
    ]


def _runs(run_lane, name: str, tasks: list[dict]) -> list:
    run_lane(_happy_scenario(name, tasks=tasks, on_poll=_drive_until_runs_settle(2)))
    return maestro.state.control_store().conn.execute(
        "SELECT task_id, status, started_at, finished_at FROM task_runs ORDER BY rowid"
    ).fetchall()


def test_overlapping_scopes_run_one_after_the_other(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _PerTaskAgent())
    runs = _runs(run_lane, "write_overlap_serial", _tasks(
        {"scope": ["src/shared.py", "TASK-001.txt"]},
        {"scope": ["src", "TASK-002.txt"]},
    ))

    assert [(r["task_id"], r["status"]) for r in runs] == [
        ("TASK-001", "succeeded"), ("TASK-002", "succeeded")]
    assert runs[1]["started_at"] >= runs[0]["finished_at"], "they overlapped in time"
    [detail] = _journal_details("launch_skipped_write_overlap")
    assert detail == "TASK-002 waiting — overlaps TASK-001: src/shared.py"


def test_disjoint_scopes_run_in_parallel(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _PerTaskAgent())
    runs = _runs(run_lane, "write_overlap_parallel", _tasks(
        {"scope": ["TASK-001.txt"]}, {"scope": ["TASK-002.txt"]},
    ))

    assert [(r["task_id"], r["status"]) for r in runs] == [
        ("TASK-001", "succeeded"), ("TASK-002", "succeeded")]
    opened = [d.split()[0] for d in _journal_details("graph_run_opened")]
    assert opened == ["TASK-001", "TASK-002"]
    # Both opened in the first poll, before either run settled.
    events = [(r["event"], r["detail"].split()[0]) for r in _journal()
              if r["event"] in ("graph_run_opened", "graph_run_settled")]
    assert events[:2] == [("graph_run_opened", "TASK-001"), ("graph_run_opened", "TASK-002")]
    assert _journal_details("launch_skipped_write_overlap") == []


class _DiagnosingAgent(_PerTaskAgent):
    """Writes a diagnosis task's cause file (its gate check), other tasks' own files."""

    def complete(self, spec):
        task = re.match(r"You are the implementer for task (\S+?)[:.]", spec.prompt)
        if task and "-diagnose-" in task.group(1):
            cause = spec.cwd / goal_replan.cause_file(task.group(1))
            cause.parent.mkdir(parents=True, exist_ok=True)
            cause.write_text("Cause: harness\n", encoding="utf-8")
            _git(spec.cwd, "add", "-A")
            _git(spec.cwd, "commit", "-q", "-m", f"docs: {task.group(1)}")
            match = re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE)
            self.specs.append(spec)
            _report_done(spec, Path(match.group(1)), "done")
            return Completion(text="done", returncode=0, timed_out=False)
        return super().complete(spec)


def test_a_replanned_diagnosis_task_runs_beside_independent_work(run_lane, monkeypatch):
    """R66 limit (GDT-8 replan path): the diagnosis task a replan appends declares its cause
    file as its scope, so it opens in the same poll as an independent scoped task."""
    diag = goal_replan._diagnosis("G2", "RP-1234abcd", [], SimpleNamespace(criteria=[]),
                                  "AC-03 does not move.")
    _patch_fixture_cli_driver(monkeypatch, _DiagnosingAgent())
    runs = _runs(run_lane, "write_overlap_replan", [
        _task("TASK-001", scope=["TASK-001.txt"],
              verifications=[{"id": "V1", "kind": "auto", "cmd": "test -f TASK-001.txt"}]),
        _task(diag["id"], scope=diag["scope"], verifications=diag["verifications"]),
    ])

    assert [(r["task_id"], r["status"]) for r in runs] == [
        ("TASK-001", "succeeded"), (diag["id"], "succeeded")]
    events = [(r["event"], r["detail"].split()[0]) for r in _journal()
              if r["event"] in ("graph_run_opened", "graph_run_settled")]
    assert events[:2] == [("graph_run_opened", "TASK-001"), ("graph_run_opened", diag["id"])]
    assert _journal_details("launch_skipped_write_overlap") == []


def test_a_task_with_no_scope_is_serialized(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _PerTaskAgent())
    runs = _runs(run_lane, "write_overlap_undeclared", _tasks(
        {"scope": ["TASK-001.txt"]}, {},
    ))

    assert runs[1]["started_at"] >= runs[0]["finished_at"]
    [detail] = _journal_details("launch_skipped_write_overlap")
    assert detail == "TASK-002 waiting — overlaps TASK-001: undeclared scope"
