"""`maestro ctl remerge <task>`: an operator's merge-conflict recovery that spends no model.

A graph run whose `merge` failed after implementer, gate and proof review succeeded (a
conflict with a task that merged first) is recovered by the operator fixing the branch by
hand. Dan's rule: no new review and no model usage — only the merge node (which re-runs
the task's deterministic checks on the merged tree) and acceptance may run again. The
verb opens revision `N + 1` through the ordinary plan-repair machinery, with the upstream
nodes carried forward as `succeeded`.
"""
from __future__ import annotations

import json

import pytest

from maestro.workflows import compiler, runner
from maestro.workflows.compiler import compile_workflow, repair_counters
from maestro.workflows.remerge import RemergeRefused, remerge

from .conftest import task_spec


@pytest.fixture(autouse=True)
def _journal(tmp_path, monkeypatch):
    import maestro.state

    monkeypatch.setattr(maestro.state, "JOURNAL", tmp_path / "journal.ndjson")
    monkeypatch.setattr(maestro.state, "CONTROL_DB", tmp_path / "no-control.sqlite3")


@pytest.fixture
def graph(store, lifecycle, repo, artifacts):
    """The escalated graph the loop compiles, stored, with a helper to settle its nodes."""
    revision = compile_workflow(
        task_spec("T-001"), workflow_id="wf_T-001", code_base_sha="deadbeef", escalation=True,
    )
    runner.start_run(store, revision, repo=repo, run_id="run_old", artifacts=artifacts)

    def settle(node_id: str, phase: str, *, run_id: str = "run_old", index: int = 1,
               outputs: dict | None = None) -> str:
        attempt_id = f"att_{run_id}_{node_id}_{index:02d}"
        lifecycle.claim_attempt(attempt_id, run_id=run_id, node_id=node_id, lease_epoch=1)
        for step in ("ready", "claimed", "running"):
            lifecycle.set_node_phase(run_id, node_id, step, attempt_id=attempt_id,
                                     attempt_index=index)
        if phase in ("claimed", "running"):
            return attempt_id
        result = {"run_id": run_id, "node_id": node_id, "attempt_id": attempt_id,
                  "status": phase, "outputs": outputs or {}}
        ref = artifacts.put_bytes(json.dumps(result).encode(), type="node_result")
        lifecycle.finish_attempt(attempt_id, run_id=run_id, status=phase,
                                 outcome_ref=ref.artifact_id)
        lifecycle.set_node_phase(run_id, node_id, phase, attempt_id=attempt_id)
        return attempt_id

    def skip(node_id: str, run_id: str = "run_old") -> None:
        lifecycle.set_node_phase(run_id, node_id, "skipped")

    return revision, settle, skip


def _merge_failed(graph, lifecycle, *, run_status="failed"):
    """DuetFlow 04-scoring's shape: reviewed and approved, then merge failed on a conflict."""
    revision, settle, skip = graph
    settle("implementer", "succeeded", index=2)
    settle("gate", "succeeded", index=2, outputs={"snapshot": "abc123"})
    settle("proof_review", "succeeded", index=2,
           outputs={"review": {"verdict": "approved", "check_id": "proof_review"}})
    settle("merge", "failed")
    for node_id in ("accept", "diagnose", "park"):
        skip(node_id)
    if run_status != "running":
        lifecycle.finish_run("T-001", "run_old", run_status)
    return revision


def _run(store, run_id):
    return store.conn.execute("SELECT * FROM task_runs WHERE run_id = ?", (run_id,)).fetchone()


def _states(store, run_id):
    return {
        r["node_id"]: r
        for r in store.conn.execute("SELECT * FROM node_states WHERE run_id = ?", (run_id,))
    }


def test_remerge_opens_a_successor_run_that_reruns_only_merge_and_accept(
    graph, store, lifecycle, repo, artifacts
):
    revision = _merge_failed(graph, lifecycle)

    result = remerge(store, "T-001", run_id="run_new")

    assert (result.task_id, result.old_run_id, result.new_run_id) == ("T-001", "run_old", "run_new")
    assert result.revision == 2
    # History stays honest: the old run failed, and it still says so.
    assert _run(store, "run_old")["status"] == "failed"
    new = _run(store, "run_new")
    assert (new["status"], new["workflow_revision"]) == ("running", 2)
    stored = compiler.latest_revision(store, revision.workflow_id)
    assert (stored.revision, stored.parent_revision) == (2, 1)
    assert compiler.OPERATOR_REMERGE_REASON in stored.mutation_reason

    after = runner.WorkflowRunner(store, stored, run_id="run_new", repo=repo,
                                  artifacts=artifacts)
    phases = after.phases()
    assert {n: phases[n] for n in ("implementer", "gate", "proof_review")} == {
        "implementer": "succeeded", "gate": "succeeded", "proof_review": "succeeded"}
    assert phases["merge"] == "pending" and phases["accept"] == "pending"
    # Carried attempt counters keep counting; merge's budget restarts (not a retry).
    states = _states(store, "run_new")
    assert states["implementer"]["attempt_index"] == 2
    assert "merge" not in states
    assert after.attempt_index("merge") == 0
    after.pre_validate(stored.node("merge"))  # a fresh budget: not refused


def test_the_successor_merge_is_the_only_node_ready_and_no_agent_is_dispatched(
    graph, store, lifecycle, repo, artifacts
):
    _merge_failed(graph, lifecycle)
    remerge(store, "T-001", run_id="run_new")
    stored = compiler.latest_revision(store, "wf_T-001")
    after = runner.WorkflowRunner(store, stored, run_id="run_new", repo=repo,
                                  artifacts=artifacts)
    assert runner.ready_frontier(stored, after.phases()) == ("merge",)


def test_merge_inputs_see_the_carried_proof_review_and_gate(
    graph, store, lifecycle, repo, artifacts
):
    """The merge node's `proof_reviews` (D10's risky-set guard) and the gate's snapshot are
    read from committed results; in the successor those nodes ran in the old run."""
    _merge_failed(graph, lifecycle)
    remerge(store, "T-001", run_id="run_new")
    stored = compiler.latest_revision(store, "wf_T-001")
    after = runner.WorkflowRunner(store, stored, run_id="run_new", repo=repo,
                                  artifacts=artifacts)
    assert [r["verdict"] for r in after.proof_reviews()] == ["approved"]
    assert after.verified_snapshot() == "abc123"


def test_a_rerun_node_does_not_see_its_old_runs_result(graph, store, lifecycle, repo, artifacts):
    """D06: only a node carried as `succeeded` reads an earlier run's result; the merge
    being re-run must not report the old run's failed outcome as its own."""
    _merge_failed(graph, lifecycle)
    remerge(store, "T-001", run_id="run_new")
    stored = compiler.latest_revision(store, "wf_T-001")
    after = runner.WorkflowRunner(store, stored, run_id="run_new", repo=repo,
                                  artifacts=artifacts)
    assert after._committed_result("merge") == {}


def test_remerge_does_not_spend_the_diagnosers_repair_budget(graph, store, lifecycle):
    _merge_failed(graph, lifecycle)
    remerge(store, "T-001", run_id="run_new")
    assert repair_counters(store, "wf_T-001").repairs_used == 0


def test_remerge_journals_and_records_the_repair_event(graph, store, lifecycle):
    import maestro.state

    _merge_failed(graph, lifecycle)
    remerge(store, "T-001", run_id="run_new")
    lines = [json.loads(line) for line in maestro.state.JOURNAL.read_text().splitlines()
             if line.strip()]
    [entry] = [line for line in lines if line["event"] == "graph_run_remerge"]
    assert "T-001" in entry["detail"] and "run_old" in entry["detail"]
    assert "run_new" in entry["detail"]
    [event] = store.conn.execute(
        "SELECT payload FROM events WHERE type = ?", (compiler.WORKFLOW_REPAIRED,)
    ).fetchall()
    payload = json.loads(event["payload"])
    assert payload["carried"] == ["gate", "implementer", "proof_review"]
    assert (payload["old_run_id"], payload["new_run_id"]) == ("run_old", "run_new")


def test_remerge_refuses_a_task_with_no_run(store):
    with pytest.raises(RemergeRefused, match="no graph run"):
        remerge(store, "T-404")


def test_remerge_refuses_a_run_that_is_still_running(graph, store, lifecycle):
    _merge_failed(graph, lifecycle, run_status="running")
    with pytest.raises(RemergeRefused, match="running"):
        remerge(store, "T-001")
    assert _run(store, "run_old")["status"] == "running"


@pytest.mark.parametrize("status", ["canceled", "succeeded"])
def test_remerge_refuses_a_run_that_did_not_fail(graph, store, lifecycle, status):
    _merge_failed(graph, lifecycle, run_status=status)
    with pytest.raises(RemergeRefused, match=status):
        remerge(store, "T-001")


def test_remerge_refuses_when_a_node_other_than_merge_failed(graph, store, lifecycle):
    _, settle, skip = graph
    settle("implementer", "succeeded")
    settle("gate", "failed")
    lifecycle.finish_run("T-001", "run_old", "failed")
    with pytest.raises(RemergeRefused, match="merge did not fail|gate"):
        remerge(store, "T-001")
    assert compiler.latest_revision(store, "wf_T-001").revision == 1


def test_remerge_refuses_when_merge_did_not_fail(graph, store, lifecycle):
    _, settle, skip = graph
    for node_id in ("implementer", "gate"):
        settle(node_id, "succeeded")
    settle("proof_review", "failed")
    lifecycle.finish_run("T-001", "run_old", "failed")
    with pytest.raises(RemergeRefused, match="merge"):
        remerge(store, "T-001")


def test_remerge_refuses_an_uncertain_merge(graph, store, lifecycle):
    """An uncertain merge may have moved the target; it is reconciled, never re-run blind."""
    _, settle, skip = graph
    for node_id in ("implementer", "gate", "proof_review"):
        settle(node_id, "succeeded")
    settle("merge", "uncertain")
    lifecycle.finish_run("T-001", "run_old", "failed")
    with pytest.raises(RemergeRefused, match="uncertain"):
        remerge(store, "T-001")


def test_remerge_refuses_a_second_time_once_the_successor_is_open(graph, store, lifecycle):
    _merge_failed(graph, lifecycle)
    remerge(store, "T-001", run_id="run_new")
    with pytest.raises(RemergeRefused, match="running"):
        remerge(store, "T-001")
