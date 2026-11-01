"""P13 W8 lane 3: a node whose worker or controller died with no receipt is decided.

Only `merge` had a postcondition probe, and only a node holding a writer lease was ever
noticed dead (the reconciler scans leases). An async launch, a test designer and a manual
step hold none, so their death left the node `claimed`/`running` and the run open forever.
"""
from __future__ import annotations

import json

import pytest

import maestro.state
from maestro.hitl import verify as _verify
from maestro.workflows import handlers
from maestro.workflows import worker as wf_worker

from .conftest import HaltRequested
from .test_dispatch_wiring import (
    _free_quality,
    _graph_scenario,
    _node_phase,
    _strong_routing,
    _window_for,
    _windows,
    _worker_argv,
)
from .test_end_to_end import (
    _git,
    _happy_scenario,
    _journal_details,
    _patch_fixture_cli_driver,
    _run_status,
    _task,
    FEATURE,
    TARGET,
)
from .test_failure_ladder import _counters, _kill_first_attempt_of, _rows_for
from .test_g7_parity import _ActionDan, _manual_lane, _PrepAgent

_DEAD_PID = 4_000_000


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


def _failure(attempt_id: str) -> dict:
    row = maestro.state.control_store().conn.execute(
        "SELECT failure_json FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return json.loads(row["failure_json"])


def _kill_the_launch_then_drive(run) -> None:
    """`on_poll`: the async launch worker dies with no receipt; nothing else is touched."""
    if _run_status() not in (None, "running"):
        run.halt()
        return
    for row in maestro.state.control_store().conn.execute(
        "SELECT attempt_id, node_id FROM attempts WHERE status IN ('claimed', 'running')"
        " ORDER BY rowid"
    ).fetchall():
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
        if inbox.result() is not None:
            continue
        if row["node_id"] == "async_launch":
            inbox.bind_pid(_DEAD_PID)
            continue
        if any(f"--attempt-id {row['attempt_id']} " in w for w in _windows(run)):
            wf_worker.main(_worker_argv(_window_for(run, row["attempt_id"])))


def _async_task(artifact) -> dict:
    check = {"id": "V-AWAIT", "kind": "auto", "cmd": f"cat {artifact}", "expect": "ok",
             "await": True, "await_artifact": str(artifact)}
    return _task("TASK-001", dispatch="async-job", launch_cmd="true",
                 short_desc="evaluate the model", verifications=[check])


# ── async_job_probe ──────────────────────────────────────────────────────────────────


def test_a_dead_async_launch_is_failed_with_the_probes_answer_and_the_run_settles(
    run_lane, monkeypatch, tmp_path
):
    """Nothing proves the job never started, so the launch is not replayed (A05): the node
    fails, the run parks and Dan is told, instead of the run staying open forever."""
    monkeypatch.setattr(_verify, "send_via_bot", lambda text, markup=None: 1)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    artifact = tmp_path / "eval-row.txt"

    run = run_lane(_happy_scenario(
        "w8_async_launch_killed", tasks=[_async_task(artifact)],
        on_poll=_kill_the_launch_then_drive,
    ))

    assert run.returncode == 0
    assert _node_phase("async_launch") == "failed"
    assert _run_status() == "failed", _journal_details("graph_run_settled")
    [launch] = _rows_for("async_launch")
    assert (launch["status"], launch["failure_kind"]) == ("uncertain", "worker_crashed")
    evidence = _failure(launch["attempt_id"])["evidence"]
    assert "no receipt" in evidence
    assert "probe: " in evidence and str(artifact) in evidence and "cannot tell" in evidence
    [notified] = _journal_details("graph_run_failed_notified")
    assert "node=async_launch" in notified


def test_the_async_job_probe_reads_the_awaited_artifact(tmp_path, monkeypatch):
    """Unit: the awaited artifact existing proves the job ran; its absence proves nothing."""
    from maestro.workflows import runner as wf_runner

    artifact = tmp_path / "eval-row.txt"
    task = _async_task(artifact)
    monkeypatch.setattr(wf_runner, "_roadmap_task", lambda task_id: task)
    probe = wf_runner._POSTCONDITION_PROBES["maestro.reconcile.async_job_probe"]

    class _Runner:
        repo = tmp_path

        class revision:
            task_id = "TASK-001"

    class _Node:
        params = {"task_id": "TASK-001"}

    landed, evidence = probe(_Runner(), _Node())
    assert landed is None and "cannot tell" in evidence

    artifact.write_text("ok\n", encoding="utf-8")
    landed, evidence = probe(_Runner(), _Node())
    assert landed is True and str(artifact) in evidence


# ── worktree_probe ───────────────────────────────────────────────────────────────────


def test_a_dead_test_designer_is_retried_like_any_dead_writer(run_lane, monkeypatch):
    """The test designer holds no writer lease, so the reconciler never saw it die."""
    _free_quality(monkeypatch)
    settled = lambda: [  # noqa: E731
        r for r in _rows_for("test_designer") if r["status"] not in ("claimed", "running")]

    run = run_lane(_graph_scenario(
        "w8_test_designer_killed", routing=_strong_routing,
        on_poll=_kill_first_attempt_of("test_designer", halt_when=lambda: len(settled()) >= 2),
    ))

    assert run.returncode == 0
    first, second = _rows_for("test_designer")
    assert (first["status"], first["failure_kind"]) == ("failed", "worker_crashed")
    assert second["status"] == "succeeded"
    assert _counters("test_designer")["quality_failures"] == 1
    assert "probe: " in _failure(first["attempt_id"])["evidence"]


def test_a_dead_implementers_evidence_says_what_it_left_in_the_tree(run_lane, monkeypatch):
    """The worktree probe's answer rides on the crash record; the retry is F6's, unchanged."""
    from .test_end_to_end import _CommittingAgent, _crash_the_first_implementer

    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())

    run = run_lane(_happy_scenario("w8_worktree_probe", on_poll=_crash_the_first_implementer))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    first, second = _rows_for("implementer")
    assert (first["failure_kind"], second["status"]) == ("worker_crashed", "succeeded")
    evidence = _failure(first["attempt_id"])["evidence"]
    assert "probe: " in evidence and "1 commit" in evidence and "uncommitted" in evidence
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "done"


# ── human_receipt_probe ──────────────────────────────────────────────────────────────


def test_a_controller_lost_while_asking_dan_resumes_the_same_request(run_lane, monkeypatch):
    """The controller dies after sending the manual step and before recording anything. A
    new controller re-reads the same request (asking is idempotent per attempt), so Dan is
    asked once, his answer is honoured and the step is never claimed a second time."""
    agent = _PrepAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _ActionDan(["done"])
    real_mark = _verify.DanVerificationChannel._mark_awaiting
    died: list = []

    def die_after_sending(self, *args, **kwargs):
        if not died:
            died.append(args[0])
            raise HaltRequested()
        return real_mark(self, *args, **kwargs)

    monkeypatch.setattr(_verify.DanVerificationChannel, "_mark_awaiting", die_after_sending)
    run = run_lane(_manual_lane("w8_human_lost_controller", monkeypatch, dan))

    assert died == ["TASK-001"] and _run_status() == "running"
    [human] = _rows_for("human_action")
    inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), human["attempt_id"])
    assert inbox.result() is None, "the controller died before recording the step"
    inbox.bind_pid(_DEAD_PID)
    run.orchestrator.HALT_FILE.unlink(missing_ok=True)
    maestro.state._reset_control_store()
    run.polls = 0
    run.run()

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert len(dan.requests()) == 1, "Dan was asked twice for one step"
    assert [r["attempt_id"] for r in _rows_for("human_action")] == [human["attempt_id"]]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "prep 1"


def test_the_human_receipt_probe_reads_dans_recorded_answer(run_lane, monkeypatch):
    """Unit, on a finished manual run: the probe names the request and Dan's answer."""
    from maestro.workflows import runner as wf_runner

    _patch_fixture_cli_driver(monkeypatch, _PrepAgent())
    run_lane(_manual_lane("w8_human_probe", monkeypatch, _ActionDan(["done"])))
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    [human] = _rows_for("human_action")
    probe = wf_runner._POSTCONDITION_PROBES["maestro.reconcile.human_receipt_probe"]

    class _Runner:
        store = maestro.state.control_store()
        run_id = store.conn.execute("SELECT run_id FROM task_runs").fetchone()["run_id"]

        class revision:
            task_id = "TASK-001"

    class _Node:
        node_id = "human_action"
        params = {"task_id": "TASK-001"}

    landed, evidence = probe(_Runner(), _Node())
    assert landed is True and "Done" in evidence
