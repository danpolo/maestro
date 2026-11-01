"""P13 W8 lane 5: the controller consumes the command inbox.

The watchdog enqueues `clear_pause`, `clear_blocked_on` and `reconcile_leases` once a
project has a control database (`[INV-01]`: it may not write `state.json` itself), and
nothing consumed them: they were dead letters, re-requested on every watchdog poll.
"""
from __future__ import annotations

import json

import pytest

from maestro import command_inbox
from maestro import state as state_module
from maestro.control.store import ControlStore

_PAST = "2020-01-01T00:00:00Z"
_FUTURE = "2999-01-01T00:00:00Z"


@pytest.fixture
def project(monkeypatch, repo, db_path):
    """`maestro.state` rebased onto a temporary project that has a control database."""
    orch = repo / ".orchestrator"
    monkeypatch.setattr(state_module, "STATE_JSON", orch / "state.json")
    monkeypatch.setattr(state_module, "JOURNAL", orch / "journal.ndjson")
    monkeypatch.setattr(state_module, "CONTROL_DB", db_path)
    ControlStore.open_controller(db_path).close()
    state_module._reset_control_store()
    state_module.write_state({"in_flight": []})
    yield repo
    state_module._reset_control_store()


def _set(**fields) -> None:
    state = state_module.read_state()
    state.update(fields)
    state_module.write_state(state)


def _enqueue(command_type: str, payload: dict, key: str):
    return state_module.control_store().enqueue(command_type, payload, idempotency_key=key)


def _drain(project, **extra):
    store = state_module.control_store()
    return command_inbox.drain(store, {**command_inbox.state_handlers(project), **extra})


def _status(command) -> tuple[str, str | None]:
    row = state_module.control_store().command(command.command_id)
    return row.status, row.error


def _journal(event: str) -> list[str]:
    lines = state_module.JOURNAL.read_text(encoding="utf-8").splitlines()
    return [r["detail"] for r in map(json.loads, lines) if r["event"] == event]


def test_an_expired_pause_the_watchdog_reported_is_cleared(project):
    _set(paused_until=_PAST)
    command = _enqueue("clear_pause", {"paused_until": _PAST}, f"clear_pause:{_PAST}")

    _drain(project)

    assert state_module.read_state()["paused_until"] is None
    assert _status(command) == ("committed", None)
    assert state_module.control_store().pending_commands() == []
    [applied] = _journal("command_applied")
    assert applied.startswith(f"clear_pause {command.command_id}: ")


def test_a_pause_set_since_the_request_is_never_cleared(project):
    """The request names the pause the watchdog saw. A later, live one stays."""
    _set(paused_until=_FUTURE)
    command = _enqueue("clear_pause", {"paused_until": _PAST}, f"clear_pause:{_PAST}")

    _drain(project)

    assert state_module.read_state()["paused_until"] == _FUTURE
    status, error = _status(command)
    assert status == "rejected" and _FUTURE in error
    assert _journal("command_rejected")


def test_a_pause_that_has_not_expired_is_not_cleared(project):
    _set(paused_until=_FUTURE)
    command = _enqueue("clear_pause", {"paused_until": _FUTURE}, f"clear_pause:{_FUTURE}")

    _drain(project)

    assert state_module.read_state()["paused_until"] == _FUTURE
    assert _status(command)[0] == "rejected"


def test_only_the_answered_requests_leave_blocked_on(project):
    _set(blocked_on=["R1", "R2", "R3"])
    questions = project / ".orchestrator" / "questions"
    questions.mkdir()
    (questions / "R1.answer").write_text("yes", encoding="utf-8")
    (questions / "R3.answer").write_text("no", encoding="utf-8")
    command = _enqueue("clear_blocked_on", {"blocked_on": ["R1", "R2", "R3"]}, "clear:R1,R2,R3")

    _drain(project)

    assert state_module.read_state()["blocked_on"] == ["R2"]
    assert _status(command)[0] == "committed"


def test_a_lease_reconciliation_request_runs_the_recovery_scan(project, monkeypatch):
    """The scan is the controller's (`[INV-01]`/`[INV-02]`), with the attempt inboxes, so a
    receipt on disk is ingested rather than adjudicated as a death."""
    from maestro.workflows import handlers

    seen = []
    monkeypatch.setattr(
        command_inbox, "reconcile",
        lambda store, *, attempts_root: seen.append((store, attempts_root)) or (),
    )
    command = _enqueue("reconcile_leases", {"reason": "watchdog_start"}, "reconcile:1")

    _drain(project)

    assert seen == [(state_module.control_store(), handlers.attempts_root(project))]
    assert _status(command)[0] == "committed"


def test_a_telegram_verb_queued_while_halted_is_left_to_its_own_consumer(project):
    """`hitl.commands` claims these by their update id when Telegram redelivers them."""
    command = _enqueue("telegram_control", {"update_id": 7, "text": "/status"}, "telegram:update:7")

    _drain(project)

    assert _status(command) == ("received", None)


def test_a_command_type_nobody_consumes_is_rejected_by_name(project):
    command = _enqueue("defragment", {}, "defragment:1")

    _drain(project)

    status, error = _status(command)
    assert status == "rejected" and "defragment" in error


def test_a_handler_that_raises_rejects_its_command_and_the_next_one_still_runs(project):
    _set(paused_until=_PAST)

    def boom(command):
        raise RuntimeError("disk on fire")

    first = _enqueue("explode", {}, "explode:1")
    second = _enqueue("clear_pause", {"paused_until": _PAST}, f"clear_pause:{_PAST}")

    _drain(project, explode=boom)

    status, error = _status(first)
    assert status == "rejected" and "RuntimeError: disk on fire" in error
    assert _status(second)[0] == "committed"
    assert state_module.read_state()["paused_until"] is None


def test_a_settled_command_is_not_applied_twice(project):
    calls = []
    _enqueue("count", {}, "count:1")

    _drain(project, count=lambda command: calls.append(command.command_id) or "counted")
    _drain(project, count=lambda command: calls.append(command.command_id) or "counted")

    assert len(calls) == 1


@pytest.mark.parametrize("command_type", ["graph_cancel", "graph_retry"])
def test_a_graph_verb_waits_for_the_graph_loop(project, command_type):
    """The paused-by-user wait drains with the state handlers only (W8 review F1)."""
    command = _enqueue(command_type, {"task_id": "T-001", "node_id": None}, f"{command_type}:1")

    assert _drain(project) == []
    assert _status(command) == ("received", None)

    _drain(project, **{command_type: lambda command: "applied by the graph loop"})
    assert _status(command) == ("committed", None)


def _claimed_by_a_controller_that_died(command_type: str, payload: dict, key: str):
    command = _enqueue(command_type, payload, key)
    assert state_module.control_store().claim_command(command.command_id) is not None
    return command


def test_a_state_command_a_dead_controller_left_claimed_is_applied(project):
    """Claim and settle are two transactions; these handlers are safe to run again (F2)."""
    _set(paused_until=_PAST)
    command = _claimed_by_a_controller_that_died(
        "clear_pause", {"paused_until": _PAST}, f"clear_pause:{_PAST}")

    _drain(project)

    assert _status(command) == ("committed", None)
    assert state_module.read_state()["paused_until"] is None


def test_an_operator_verb_a_dead_controller_left_claimed_is_rejected_not_replayed(project):
    calls = []
    command = _claimed_by_a_controller_that_died(
        "graph_retry", {"task_id": "T-001", "node_id": "implementer"}, "graph_retry:1")

    _drain(project, graph_retry=lambda command: calls.append(command) or "retried")

    status, error = _status(command)
    assert status == "rejected" and "controller stopped" in error
    assert calls == []
    assert any("graph_retry" in detail for detail in _journal("command_rejected"))


def test_a_claimed_telegram_verb_is_left_to_its_own_consumer(project):
    command = _claimed_by_a_controller_that_died(
        "telegram_control", {"update_id": 8, "text": "/status"}, "telegram:update:8")

    _drain(project)

    assert _status(command) == ("processing", None)


# ── P13 W8 lane 4: what `ctl cancel` / `ctl retry` may ask for ────────────────────────
#
# One check, used twice: by the CLI before it enqueues (so the operator is told at the
# terminal) and by the controller when it applies (the state may have moved since).


def _run(task_id: str = "T-001", run_id: str = "run_1", *, status: str = "running",
         phases: dict | None = None, at: str = "2026-10-13T08:00:00Z", stored: bool = True):
    """A graph run of the default workflow, with the given node phases."""
    from maestro.taskgraph import normalize_spec
    from maestro.workflows import compiler, runner

    store = state_module.control_store()
    revision = compiler.compile_workflow(
        normalize_spec({"id": task_id, "title": "t", "status": "pending", "deps": []}),
        workflow_id=f"wf_{task_id}", code_base_sha="deadbeef",
    )
    life = runner.start_run(
        store, revision, repo=state_module.STATE_JSON.parent.parent, run_id=run_id,
        persist=stored, at=at,
    ).lifecycle
    for node_id, phase in (phases or {}).items():
        for step in ("ready", "claimed", "running", "uncertain")[
                :("ready", "claimed", "running", "uncertain").index(phase) + 1]:
            if step == "uncertain":
                # `running -> uncertain` is the reconciler's move, written as it writes it.
                store.conn.execute(
                    "UPDATE node_states SET phase = 'uncertain' WHERE run_id = ? AND node_id = ?",
                    (run_id, node_id))
                continue
            life.set_node_phase(run_id, node_id, step, at=at)
    if status != "running":
        life.finish_run(task_id, run_id, status, at=at)
    return revision


def _refusal(command_type: str, task_id, node_id=None, *, store=None) -> str:
    with pytest.raises(command_inbox.CommandRejected) as refused:
        command_inbox.check_graph_request(
            store or state_module.control_store(), command_type, task_id, node_id)
    return str(refused.value)


@pytest.mark.parametrize("command_type", ["graph_cancel", "graph_retry"])
def test_a_task_with_no_graph_run_is_refused(project, command_type):
    _run()

    assert "T-404 has no graph run" in _refusal(command_type, "T-404")
    assert "has no graph run" in _refusal(command_type, "")


@pytest.mark.parametrize("command_type", ["graph_cancel", "graph_retry"])
@pytest.mark.parametrize("phase", ["claimed", "running", "uncertain"])
def test_a_live_node_of_a_running_run_may_be_stopped(project, command_type, phase):
    _run(phases={"implementer": phase})

    row = command_inbox.check_graph_request(
        state_module.control_store(), command_type, "T-001", "implementer")

    assert (row["run_id"], row["status"]) == ("run_1", "running")


def test_a_node_that_is_not_live_is_refused_and_the_live_ones_are_named(project):
    _run(phases={"implementer": "claimed"})

    reason = _refusal("graph_retry", "T-001", "gate")

    assert "gate is pending" in reason and "implementer" in reason


def test_a_node_the_stored_graph_does_not_have_is_refused(project):
    revision = _run(phases={"implementer": "claimed"})

    reason = _refusal("graph_cancel", "T-001", "implementor")

    assert "'implementor' is not a node" in reason
    assert all(node_id in reason for node_id in revision.node_ids)


def test_a_node_of_a_settled_run_is_refused(project):
    _run(status="failed")

    assert "run_1 is failed" in _refusal("graph_cancel", "T-001", "implementer")
    assert "run_1 is failed" in _refusal("graph_retry", "T-001", "implementer")


def test_a_run_is_cancelled_only_while_it_runs(project):
    _run("T-001", "run_1")
    _run("T-002", "run_2", status="failed")
    store = state_module.control_store()

    assert command_inbox.check_graph_request(store, "graph_cancel", "T-001")["run_id"] == "run_1"
    assert "run_2 is failed" in _refusal("graph_cancel", "T-002")


def test_a_task_retry_is_for_a_settled_run_and_a_live_one_must_name_its_node(project):
    _run("T-001", "run_1", phases={"implementer": "running"})
    _run("T-002", "run_2", status="failed")
    _run("T-003", "run_3", status="canceled")
    _run("T-004", "run_4", status="succeeded")
    store = state_module.control_store()

    live = _refusal("graph_retry", "T-001")
    assert "name the node" in live and "implementer" in live
    assert command_inbox.check_graph_request(store, "graph_retry", "T-002")["run_id"] == "run_2"
    assert command_inbox.check_graph_request(store, "graph_retry", "T-003")["run_id"] == "run_3"
    assert "succeeded" in _refusal("graph_retry", "T-004")


def test_the_latest_run_of_a_task_is_the_one_judged(project):
    _run("T-001", "run_old", status="failed", at="2026-10-13T08:00:00Z")
    _run("T-001", "run_new", phases={"implementer": "claimed"}, at="2026-10-13T09:00:00Z")

    assert "name the node" in _refusal("graph_retry", "T-001")
    assert command_inbox.check_graph_request(
        state_module.control_store(), "graph_cancel", "T-001")["run_id"] == "run_new"


def test_the_check_reads_through_a_client_store(project, db_path):
    """The CLI holds no controller lease: it checks on the store an external actor opens."""
    _run(phases={"implementer": "claimed"})
    client = ControlStore.open_client(db_path)
    try:
        row = command_inbox.check_graph_request(client, "graph_cancel", "T-001", "implementer")
        assert row["run_id"] == "run_1"
        assert "gate is pending" in _refusal("graph_retry", "T-001", "gate", store=client)
    finally:
        client.close()


def test_a_run_whose_graph_is_not_stored_still_checks_the_phase(project):
    _run(phases={"implementer": "claimed"}, stored=False)
    store = state_module.control_store()

    assert command_inbox.check_graph_request(store, "graph_cancel", "T-001", "implementer")
    assert "ghost is pending" in _refusal("graph_cancel", "T-001", "ghost")
