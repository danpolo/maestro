"""P13 W8 lane 4: `maestro ctl retry` / `ctl cancel` reach a running graph loop.

Before W8 there was no operator verb for one graph node: a node left `uncertain` held its
run open until someone edited the control database by hand. The CLI is an external actor
(`[INV-01]`), so it checks the request, enqueues it, and the loop applies it on its next
poll. Each lane here calls the real `cmd_ctl` while the loop holds the controller lease.

Nothing is signalled: every stopped attempt is a claim whose worker never bound a pid.
The runner's side (which pid is signalled, the lease, the counters) is pinned in
`tests/workflows/test_operator_verbs.py`.
"""
from __future__ import annotations

import pytest

import maestro.state
from maestro import cli
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import enqueue_external_command
from maestro.hitl import verify as _verify

from .test_dispatch_wiring import _events, _journal_details, _node_phase
from .test_end_to_end import (
    _CommittingAgent,
    _drive_every_worker_of_any_run,
    _git,
    _git_sandbox,
    _happy_scenario,
    _patch_fixture_cli_driver,
    FEATURE,
    TARGET,
)
from .test_failure_ladder import _counters, _rows_for


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


def _runs() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT run_id, status FROM task_runs ORDER BY started_at, rowid"
    ).fetchall()


def _inbox() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT command_type, status, error, payload FROM command_inbox"
        " ORDER BY received_at, rowid"
    ).fetchall()


def _first_implementer_is_claimed() -> bool:
    rows = _rows_for("implementer")
    return len(rows) == 1 and rows[0]["status"] == "claimed"


def _telegram(monkeypatch) -> tuple:
    """`(before_run, sent)`: the sandbox, with the loop's operator replies collected."""
    sent: list = []

    def before_run(run) -> None:
        _git_sandbox(run)
        monkeypatch.setattr(run.orchestrator, "notify_telegram",
                            lambda text, *a, **k: sent.append(str(text)))

    return before_run, sent


# ── ctl retry <task> <node> ──────────────────────────────────────────────────────────


def test_a_retried_node_gets_a_fresh_attempt_from_the_same_start(run_lane, monkeypatch):
    """The stopped implementer had committed half a change and left a dirty file. The
    fresh attempt starts from the stopped one's `start_sha`, so neither reaches the
    target, and the operator's retry spends no quality budget."""
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    before_run, sent = _telegram(monkeypatch)
    asked: list = []

    def on_poll(run) -> None:
        if _first_implementer_is_claimed():
            if not asked:
                tree = run.repo / ".orchestrator" / "worktrees" / "TASK-001"
                (tree / "half.txt").write_text("half a change\n", encoding="utf-8")
                _git(tree, "add", "half.txt")
                _git(tree, "commit", "-q", "-m", "wip: half a change")
                (tree / "dirty.txt").write_text("never committed\n", encoding="utf-8")
                asked.append(cli.cmd_ctl(run.repo, "retry", ["TASK-001", "implementer"]))
            return
        if _runs()[0]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("w8_ctl_retry_node", before_run=before_run, on_poll=on_poll))

    assert run.returncode == 0 and asked == [0]
    assert [r["status"] for r in _runs()] == ["succeeded"], _journal_details("graph_run_settled")
    first, second = _rows_for("implementer")
    assert (first["status"], first["failure_kind"]) == ("failed", "operator_retry")
    assert second["status"] == "succeeded"
    assert _counters("implementer")["quality_failures"] == 0
    assert any(command.startswith("tmux kill-window") for command in run.commands)
    files = _git(run.repo, "ls-tree", "--name-only", TARGET).splitlines()
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "done"
    assert "half.txt" not in files and "dirty.txt" not in files
    [applied] = _journal_details("command_applied")
    assert applied.startswith("graph_retry ") and "implementer" in applied
    assert any("TASK-001" in text and "implementer" in text for text in sent)


# ── ctl cancel <task> <node>, then ctl retry <task> ──────────────────────────────────


def test_a_stuck_uncertain_node_is_cancelled_and_the_task_retried(run_lane, monkeypatch):
    """What a pre-W8 stuck run looks like: the implementer `uncertain`, nobody deciding it.
    `ctl cancel` fails the node, the run takes its failure path and settles, and `ctl
    retry <task>` then opens a fresh run that lands the task."""
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    monkeypatch.setattr(_verify, "send_via_bot", lambda text, markup=None: 1)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    before_run, sent = _telegram(monkeypatch)
    asked: dict = {}

    def on_poll(run) -> None:
        runs = _runs()
        if "cancel" not in asked:
            if _first_implementer_is_claimed():
                [attempt] = _rows_for("implementer")
                life = Lifecycle(maestro.state.control_store())
                life.finish_attempt(attempt["attempt_id"], run_id=runs[0]["run_id"],
                                    status="uncertain")
                life.set_node_phase(runs[0]["run_id"], "implementer", "uncertain",
                                    attempt_id=attempt["attempt_id"])
                asked["cancel"] = cli.cmd_ctl(run.repo, "cancel", ["TASK-001", "implementer"])
            return
        if "retry" not in asked:
            if runs[0]["status"] == "running":
                _drive_every_worker_of_any_run(run)
                return
            asked["first_run"] = runs[0]["status"]
            asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        if len(runs) == 2 and runs[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("w8_ctl_cancel_node", before_run=before_run, on_poll=on_poll))

    assert run.returncode == 0
    assert asked == {"cancel": 0, "first_run": "failed", "retry": 0}
    assert [r["status"] for r in _runs()] == ["failed", "succeeded"]
    first, second = _rows_for("implementer")
    assert (first["status"], first["failure_kind"]) == ("failed", "operator_canceled")
    assert second["status"] == "succeeded"
    [notified] = _journal_details("graph_run_failed_notified")
    assert "node=implementer" in notified
    assert len(_journal_details("graph_run_reopened")) == 1
    assert [line.split()[0] for line in _journal_details("command_applied")] == [
        "graph_cancel", "graph_retry"]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "done"
    assert any("a fresh run opens" in text for text in sent)


# ── ctl cancel <task> ────────────────────────────────────────────────────────────────


def test_a_cancelled_run_is_closed_without_its_failure_path(run_lane, monkeypatch):
    """A run cancel is not a failure: the node is stopped, the run is `canceled`, its tree
    is released, `diagnose` is never dispatched, Dan gets no failed-run notice and no
    fresh run opens by itself."""
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    before_run, sent = _telegram(monkeypatch)
    asked: list = []

    def on_poll(run) -> None:
        if not asked:
            if _first_implementer_is_claimed():
                asked.append(cli.cmd_ctl(run.repo, "cancel", ["TASK-001"]))
            return
        asked.append("poll")
        if len(asked) > 3:
            run.halt()

    run = run_lane(_happy_scenario("w8_ctl_cancel_run", before_run=before_run, on_poll=on_poll))

    assert run.returncode == 0 and asked[0] == 0
    assert [r["status"] for r in _runs()] == ["canceled"]
    [settled] = _journal_details("graph_run_settled")
    assert "TASK-001" in settled and settled.endswith("status=canceled")
    [implementer] = _rows_for("implementer")
    assert (implementer["status"], implementer["failure_kind"]) == ("failed", "operator_canceled")
    assert _node_phase("implementer") == "failed"
    assert _rows_for("diagnose") == []
    assert len(_events("TaskTreeReleased")) == 1
    assert _journal_details("graph_run_failed_notified") == []
    assert any("TASK-001" in text and "canceled" in text for text in sent)


# ── refusals ─────────────────────────────────────────────────────────────────────────

_REFUSED = (
    ("cancel", ["TASK-404"], "TASK-404 has no graph run"),
    ("retry", ["TASK-001"], "name the node"),
    ("retry", ["TASK-001", "accept"], "accept is pending"),
)


def test_a_request_the_run_does_not_allow_is_refused_at_the_cli_and_by_the_loop(
    run_lane, monkeypatch, capsys
):
    """The CLI refuses before it enqueues. The loop checks again when it applies, because
    the run may have moved since: the same requests put straight into the inbox are
    settled `rejected` with the reason, and the node is left alone."""
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    before_run, sent = _telegram(monkeypatch)
    seen: dict = {}

    def on_poll(run) -> None:
        if "cli" not in seen:
            if not _first_implementer_is_claimed():
                return
            capsys.readouterr()
            seen["cli"] = [cli.cmd_ctl(run.repo, verb, args) for verb, args, _ in _REFUSED]
            seen["cli"].append(cli.cmd_ctl(run.repo, "retry", []))
            seen["printed"] = capsys.readouterr().out
            seen["queued_by_cli"] = len(_inbox())
            for index, (verb, args, _) in enumerate(_REFUSED):
                enqueue_external_command(
                    f"graph_{verb}",
                    {"task_id": args[0], "node_id": args[1] if len(args) > 1 else None},
                    idempotency_key=f"test:{index}", repo=run.repo,
                )
            return
        run.halt()

    run = run_lane(_happy_scenario("w8_ctl_refusals", before_run=before_run, on_poll=on_poll))

    assert run.returncode == 0
    assert seen["cli"] == [1, 1, 1, 1] and seen["queued_by_cli"] == 0
    assert "usage: maestro ctl retry <task_id> [<node_id>]" in seen["printed"]
    rows = _inbox()
    assert [r["status"] for r in rows] == ["rejected"] * 3
    rejected = _journal_details("command_rejected")
    assert len(rejected) == 3
    for _, _, reason in _REFUSED:
        assert reason in seen["printed"]
        assert any(reason in row["error"] for row in rows)
        assert any(reason in line for line in rejected)
        assert any(reason in text for text in sent)
    assert _first_implementer_is_claimed() and _node_phase("implementer") == "claimed"
    assert [r["status"] for r in _runs()] == ["running"]


def test_ctl_cancel_outside_a_graph_project_is_refused(tmp_path, capsys):
    assert cli.cmd_ctl(tmp_path, "cancel", ["TASK-001"]) == 1

    assert "not a graph project" in capsys.readouterr().out
