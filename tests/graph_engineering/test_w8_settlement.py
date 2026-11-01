"""P13 W8: a node whose outcome nobody recorded is decided, never left `uncertain`.

Two ways a node used to stop a run forever, both proven under the real `orchestrator.main`:

* an agent that exits cleanly and writes no sentinel (pilot finding D4);
* a controller-executed node whose handler raises (`execute_inline`).

`uncertain` is not a terminal phase, so neither run ever settled and nothing told Dan.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import maestro.state
from maestro.workflows import failures, handlers

from .test_dispatch_wiring import _events, _graph_scenario, _node_phase, _task
from .test_end_to_end import (
    _attempts,
    _CommittingAgent,
    _drive_every_worker,
    _happy_scenario,
    _journal_details,
    _patch_fixture_cli_driver,
    _run_status,
)
from .test_failure_ladder import (
    _attempts_by_node,
    _committed_repo,
    _counters,
    _drive_implementer,
    _GOOD,
    _ONE_CHECK,
    _retry_rows,
    _ScriptedCliDriver,
    _settled_implementer,
)
from .test_failure_ladder import _patch_fixture_cli_driver as _patch_scripted_driver


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


# ── an agent that wrote no sentinel ──────────────────────────────────────────────────


def test_an_agent_that_writes_no_sentinel_is_retried_not_left_uncertain(
    run_lane, monkeypatch, tmp_path
):
    """The agent answered and exited 0, but reported nothing: bad output, run again."""
    _patch_scripted_driver(monkeypatch, _ScriptedCliDriver(_GOOD))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "w8_no_sentinel_retry", tasks=[_task("TASK-001", verifications=_ONE_CHECK)],
        on_poll=_drive_implementer(
            lambda: bool(_attempts_by_node("gate")), worktree=worktree, succeed_from=2,
        ),
    ))
    assert run.returncode == 0

    rows = _retry_rows()
    assert [r["status"] for r in rows] == ["failed", "succeeded"]
    assert [r["failure_kind"] for r in rows] == ["no_sentinel", None]
    assert _node_phase("implementer") == "succeeded"
    assert _counters()["quality_failures"] == 1


def test_an_agent_that_never_writes_a_sentinel_fails_its_node_and_the_run_settles(
    run_lane, monkeypatch, tmp_path
):
    _patch_scripted_driver(monkeypatch, _ScriptedCliDriver(_GOOD))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "w8_no_sentinel_exhausted", tasks=[_task("TASK-001", verifications=_ONE_CHECK)],
        on_poll=_drive_implementer(
            lambda: len(_settled_implementer()) >= failures.QUALITY_ATTEMPTS,
            worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    rows = _retry_rows()
    assert [r["failure_kind"] for r in rows] == ["no_sentinel"] * failures.QUALITY_ATTEMPTS
    assert _node_phase("implementer") == "failed", "decided, so its failure edge can open"


# ── a controller-executed node whose handler raises ──────────────────────────────────


def test_an_inline_handler_that_raises_fails_its_node_and_the_run_settles(
    run_lane, monkeypatch
):
    """Merge fast-forwards, then the acceptance commit fails. The node is `failed` with the
    probe's answer in its evidence, a `ControllerError` event records it, the run settles
    and the settle notice (which is how Dan hears) names the error."""
    from maestro.control.lifecycle import Lifecycle

    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    real_set_task_status = Lifecycle.set_task_status
    failed: list = []

    def disk_full_on_accept(self, task_id, status, **kwargs):
        if status == "accepted" and not failed:
            failed.append(task_id)
            raise sqlite3.OperationalError("database or disk is full")
        return real_set_task_status(self, task_id, status, **kwargs)

    def drive_until_settled(run) -> None:
        if _run_status() not in (None, "running"):
            run.halt()
            return
        _drive_every_worker(run)

    monkeypatch.setattr(Lifecycle, "set_task_status", disk_full_on_accept)
    run = run_lane(_happy_scenario("w8_inline_handler_raises", on_poll=drive_until_settled))

    assert run.returncode == 0 and failed == ["TASK-001"]
    assert _node_phase("merge") == "failed"
    assert _run_status() == "failed", _journal_details("graph_run_settled")
    [merge] = [a for a in _attempts() if a["node_id"] == "merge"]
    row = maestro.state.control_store().conn.execute(
        "SELECT status, failure_kind, failure_json FROM attempts WHERE attempt_id = ?",
        (merge["attempt_id"],),
    ).fetchone()
    assert (row["status"], row["failure_kind"]) == ("failed", "controller_error")
    evidence = json.loads(row["failure_json"])["evidence"]
    assert "disk is full" in evidence
    assert "already contains impl/TASK-001" in evidence, "the probe says the merge landed"
    payloads = [json.loads(e["payload"]) for e in _events("ControllerError")]
    [event] = [p for p in payloads if p["node_id"] == "merge"]
    assert "disk is full" in event["error"] and "set_task_status" in event["traceback"]
    result = handlers.AttemptInbox(handlers.attempts_root(run.repo), merge["attempt_id"]).result()
    assert result.status == "uncertain", "the handler's own account is kept as written"
