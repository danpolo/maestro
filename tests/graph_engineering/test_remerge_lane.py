"""`maestro ctl remerge`, end to end through the real event loop.

A run whose merge failed after an approving review is recovered by the operator fixing the
branch by hand; the loop then resumes the successor run, and only merge and accept run —
no implementer, no gate, no reviewer, no model (Dan, 2026-09-26). The merge failure here
is the deny-list guard rather than a conflict, because the lane harness cannot move the
target between gate and merge; to the verb both are the same thing: a failed merge node.
The lane takes away the gate's deny-list ask (D29) so the hit reaches the merge guard.
"""
from __future__ import annotations

import pytest

import maestro.state
from maestro.workflows import remerge

from .test_dispatch_wiring import _journal_details
from .test_end_to_end import TARGET, _drive_every_worker_of_any_run, _git, _happy_scenario
from .test_failure_ladder import _patch_fixture_cli_driver
from .test_merge_guard import _project, _WritingAgent, no_deny_ask  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


def _runs() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT run_id, status FROM task_runs ORDER BY rowid"
    ).fetchall()


def _drive_until_the_latest_run_settles(run) -> None:
    runs = _runs()
    if runs and runs[-1]["status"] != "running":
        run.halt()
        return
    _drive_every_worker_of_any_run(run)


def test_a_remerged_run_lands_after_a_hand_fix_without_any_agent(
    run_lane, monkeypatch, tmp_path, no_deny_ask
):
    agent = _WritingAgent({"db/reset.sql": "DROP TABLE users;\n"})
    _patch_fixture_cli_driver(monkeypatch, agent)
    run = run_lane(_happy_scenario(
        "remerge", project_yaml=_project(), on_poll=_drive_until_the_latest_run_settles,
    ))
    [old] = _runs()
    assert old["status"] == "failed"
    store = maestro.state.control_store()
    agent_attempts = store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE node_id IN ('implementer', 'proof_review')"
    ).fetchone()[0]
    briefs = len(agent.specs)

    # The operator's hand fix, on the task branch the failed run left behind (its task
    # tree is gone once the run settled): the offending file goes, committed.
    fix = tmp_path / "operator-fix"
    _git(run.repo, "worktree", "add", "-q", str(fix), "impl/TASK-001")
    _git(fix, "rm", "-q", "db/reset.sql")
    _git(fix, "commit", "-q", "-m", "fix: drop the reset script")
    _git(run.repo, "worktree", "remove", str(fix))

    result = remerge.remerge(store, "TASK-001")
    assert result.old_run_id == old["run_id"]
    assert result.carried == ("gate", "implementer", "proof_review")

    run.orchestrator.HALT_FILE.unlink()
    run.run()

    runs = _runs()
    assert [r["status"] for r in runs] == ["failed", "succeeded"], _journal_details(
        "graph_run_settled")
    assert runs[-1]["run_id"] == result.new_run_id
    # Only merge and accept ran in the successor: no agent, no gate, no new brief.
    ran = [r["node_id"] for r in store.conn.execute(
        "SELECT node_id FROM attempts WHERE run_id = ? ORDER BY rowid", (result.new_run_id,))]
    assert ran == ["merge", "accept"]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE node_id IN ('implementer', 'proof_review')"
    ).fetchone()[0] == agent_attempts
    assert len(agent.specs) == briefs
    assert "fix: drop the reset script" in _git(run.repo, "log", "--format=%s", TARGET)
    assert f"{result.new_run_id}" in " ".join(_journal_details("graph_run_resumed"))
    # The successor was resumed, not a fresh run opened from the implementer.
    assert len(_journal_details("graph_run_opened")) == 1
