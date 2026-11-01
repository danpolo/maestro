"""P13 W8 lane 5: the loop drains the command inbox, and the watchdog can ask again.

The handlers themselves are pinned in `tests/control/test_command_inbox.py`; these pin
that a running loop actually applies what an external actor queued.
"""
from __future__ import annotations

import pytest

import maestro.state
from maestro import watchdog
from maestro.control.store import ControlStore, enqueue_external_command

from .test_dispatch_wiring import _create_control_db, _graph_scenario, _journal_details

_PAST = "2020-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


def _queue_the_watchdogs_requests(run) -> None:
    """`before_run`: what a watchdog left in the inbox while no loop was running."""
    _create_control_db(run)
    enqueue_external_command(
        "clear_pause", {"paused_until": _PAST}, idempotency_key=f"clear_pause:{_PAST}",
        repo=run.repo,
    )
    enqueue_external_command(
        "reconcile_leases", {"reason": "watchdog_start"},
        idempotency_key="reconcile:watchdog_start", repo=run.repo,
    )


def test_the_graph_loop_applies_what_the_watchdog_queued(run_lane):
    run = run_lane(_graph_scenario(
        "w8_inbox_drained", tasks=[], state={"paused_until": _PAST},
        before_run=_queue_the_watchdogs_requests,
    ))

    assert run.returncode == 0
    store = maestro.state.control_store()
    rows = store.conn.execute(
        "SELECT command_type, status FROM command_inbox ORDER BY received_at, command_id"
    ).fetchall()
    assert sorted((r["command_type"], r["status"]) for r in rows) == [
        ("clear_pause", "committed"), ("reconcile_leases", "committed"),
    ]
    assert maestro.state.read_state()["paused_until"] is None
    applied = _journal_details("command_applied")
    assert len(applied) == 2 and any("reason=watchdog_start" in line for line in applied)


def test_a_reconciliation_request_after_a_consumed_one_is_a_new_command(tmp_path, monkeypatch):
    """A settled command is never re-opened by its key, so the second start would have
    been swallowed once a consumer existed. While one waits, a restart loop adds none."""
    db = tmp_path / ".orchestrator" / "control.sqlite3"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("MAESTRO_REPO", str(tmp_path))

    def pending() -> list:
        store = ControlStore.open_controller(db)
        try:
            return store.pending_commands()
        finally:
            store.close()

    ControlStore.open_controller(db).close()
    assert watchdog.request_lease_reconciliation("watchdog_start")
    assert watchdog.request_lease_reconciliation("watchdog_start")
    [first] = pending()

    store = ControlStore.open_controller(db)
    store.claim_command(first.command_id)
    store.settle_command(first.command_id, "committed")
    store.close()

    assert watchdog.request_lease_reconciliation("watchdog_start")
    assert watchdog.request_lease_reconciliation("watchdog_start")
    [second] = pending()
    assert second.command_id != first.command_id
    assert second.payload == {"reason": "watchdog_start"}
