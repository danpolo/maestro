"""The transactional control store: pragmas, inbox, events, CAS, leases (`[P1]`).

These are specification tests, not characterisation: P01 states what the store must do,
and each acceptance condition in `phases/P01_TRANSACTIONAL_CONTROL.md` §3 is pinned by at
least one test below.
"""
from __future__ import annotations

import os
import signal

import pytest

from maestro.control.lifecycle import IllegalTransition, Lifecycle, replay
from maestro.control.models import EventType, attempt_id
from maestro.control.store import (
    CONTROLLER_RESOURCE,
    ControlStore,
    FencedWriteError,
    SingleWriterViolation,
    StaleRevisionError,
    pid_alive,
)


# --- database & schema ---------------------------------------------------------------


@pytest.mark.real_fsync
def test_opens_in_wal_mode_with_foreign_keys_and_busy_timeout(store):
    assert store.pragma("journal_mode").lower() == "wal"
    assert store.pragma("foreign_keys") == 1
    assert store.pragma("busy_timeout") == 10000
    assert store.pragma("synchronous") == 1          # NORMAL


def test_schema_creates_every_table_the_contract_names(store):
    names = {
        row["name"]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "command_inbox", "events", "tasks", "task_revisions", "task_dependencies",
        "workflow_revisions", "task_runs", "writer_leases", "operation_intents",
        "operation_receipts", "verification_evidence", "artifacts",
    } <= names


def test_opening_twice_is_safe(db_path, store):
    """The DDL is idempotent, so a restart against an existing database re-applies it."""
    again = ControlStore.open_client(db_path)
    try:
        assert again.head_sequence() == store.head_sequence()
    finally:
        again.close()


# --- single writer [INV-01] -----------------------------------------------------------


def test_a_client_may_not_write_events(db_path, store):
    client = ControlStore.open_client(db_path)
    try:
        with pytest.raises(SingleWriterViolation):
            client.append_event("Anything", "agg")
    finally:
        client.close()


def test_a_client_may_not_settle_a_command(db_path, store):
    client = ControlStore.open_client(db_path)
    try:
        command = client.enqueue("pause", {}, idempotency_key="k1")
        with pytest.raises(SingleWriterViolation):
            client.settle_command(command.command_id, "committed")
    finally:
        client.close()


def test_a_second_controller_is_refused_while_the_first_is_alive(db_path, store):
    with pytest.raises(SingleWriterViolation):
        ControlStore.open_controller(db_path, pid=os.getpid() + 1_000_000)


def test_a_controller_lock_held_by_a_dead_pid_is_reclaimed(db_path, tmp_path):
    """A crashed controller must not lock the project out forever."""
    dead_pid = _a_pid_that_is_gone()
    first = ControlStore(db_path, role="controller", pid=dead_pid)
    first._take_controller_lock()
    first.conn.close()                      # crash: lock row survives, process does not

    second = ControlStore.open_controller(db_path)
    try:
        lease = second.lease(CONTROLLER_RESOURCE)
        assert lease.writer_pid == os.getpid()
        assert lease.lease_epoch == 2       # epoch advanced rather than being reused
    finally:
        second.close()


def _a_pid_that_is_gone() -> int:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    assert not pid_alive(pid)
    return pid


# --- idempotent command inbox ----------------------------------------------------------


def test_a_command_is_recorded_as_received(store):
    command = store.enqueue("pause", {"by": "telegram"}, idempotency_key="tg:1")
    assert command.status == "received"
    assert command.payload == {"by": "telegram"}
    assert store.pending_commands() == [command]


def test_a_duplicate_idempotency_key_commits_once(store):
    first = store.enqueue("pause", {"by": "telegram"}, idempotency_key="tg:1")
    second = store.enqueue("pause", {"by": "telegram"}, idempotency_key="tg:1")
    assert second.command_id == first.command_id
    rows = store.conn.execute("SELECT COUNT(*) AS n FROM command_inbox").fetchone()
    assert rows["n"] == 1


def test_a_duplicate_cannot_resurrect_a_settled_command(store):
    first = store.enqueue("approve", {"task": "T1"}, idempotency_key="tg:7")
    store.claim_command(first.command_id)
    store.settle_command(first.command_id, "committed")

    again = store.enqueue("approve", {"task": "T1"}, idempotency_key="tg:7")
    assert again.status == "committed"
    assert store.pending_commands() == []


def test_only_one_claim_of_a_command_succeeds(store):
    command = store.enqueue("pause", {}, idempotency_key="tg:2")
    assert store.claim_command(command.command_id).status == "processing"
    assert store.claim_command(command.command_id) is None


def test_a_rejected_command_records_its_error(store):
    command = store.enqueue("unpark", {"task": "T9"}, idempotency_key="tg:3")
    store.claim_command(command.command_id)
    store.settle_command(command.command_id, "rejected", error="T9 is not parked")
    assert store.command(command.command_id).error == "T9 is not parked"


# --- event store --------------------------------------------------------------------------


def test_events_carry_a_strictly_increasing_sequence(store):
    events = [store.append_event("Ping", "agg", payload={"i": i}) for i in range(5)]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5]
    assert store.head_sequence() == 5


def test_event_ids_embed_their_sequence_and_are_unique(store):
    events = [store.append_event("Ping", "agg") for _ in range(3)]
    assert events[1].event_id.startswith("evt_000002_")
    assert len({e.event_id for e in events}) == 3


def test_no_event_id_placeholder_survives_the_insert(store):
    for _ in range(3):
        store.append_event("Ping", "agg")
    ids = [row["event_id"] for row in store.conn.execute("SELECT event_id FROM events")]
    assert all(i.startswith("evt_0") for i in ids)


def test_events_can_be_read_back_per_aggregate(store):
    store.append_event("A", "one")
    store.append_event("B", "two")
    store.append_event("C", "one")
    assert [e.type for e in store.events(aggregate_id="one")] == ["A", "C"]


def test_a_failed_transaction_appends_no_event(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.append_event("Ping", "agg", conn=conn)
            raise RuntimeError("boom")
    assert store.head_sequence() == 0


# --- compare-and-set ------------------------------------------------------------------------


def test_a_task_revision_advances_under_the_expected_revision(store):
    store.register_task("T-001")
    store.apply_task_revision(
        "T-001", spec_revision=2, spec_yaml="goal: x", source_hash="abc", expected_revision=1
    )
    assert store.task("T-001")["current_spec_revision"] == 2


def test_a_stale_revision_fails_the_cas_check(store):
    store.register_task("T-001")
    store.apply_task_revision(
        "T-001", spec_revision=2, spec_yaml="a", source_hash="h1", expected_revision=1
    )
    with pytest.raises(StaleRevisionError):
        store.apply_task_revision(
            "T-001", spec_revision=3, spec_yaml="b", source_hash="h2", expected_revision=1
        )
    assert store.task("T-001")["current_spec_revision"] == 2


def test_a_failed_cas_leaves_no_revision_row_and_no_event(store):
    store.register_task("T-001")
    before = store.head_sequence()
    with pytest.raises(StaleRevisionError):
        store.apply_task_revision(
            "T-001", spec_revision=5, spec_yaml="b", source_hash="h", expected_revision=9
        )
    assert store.head_sequence() == before
    rows = store.conn.execute("SELECT COUNT(*) AS n FROM task_revisions").fetchone()
    assert rows["n"] == 0


def test_a_revision_that_does_not_advance_is_refused(store):
    store.register_task("T-001")
    with pytest.raises(StaleRevisionError):
        store.apply_task_revision(
            "T-001", spec_revision=1, spec_yaml="a", source_hash="h", expected_revision=1
        )


# --- writer leases and PID binding [INV-02] ---------------------------------------------------


def test_a_lease_binds_to_the_writing_pid_not_the_launcher(store):
    """The launcher's PID is what the claim knows; the worker reports the real writer."""
    launcher_pid, writer_pid = 4242, 9999
    store.claim_lease("worktree:impl/T-001", "att_1", writer_pid=launcher_pid,
                      is_alive=lambda pid: False)
    lease = store.bind_writer_pid("worktree:impl/T-001", lease_epoch=1, writer_pid=writer_pid)
    assert lease.writer_pid == writer_pid


def test_claiming_increments_the_fencing_epoch(store):
    store.claim_lease("r", "att_1", writer_pid=1, is_alive=lambda pid: False)
    store.release_lease("r", lease_epoch=1)
    lease = store.claim_lease("r", "att_2", writer_pid=2, is_alive=lambda pid: False)
    assert lease.lease_epoch == 2


def test_a_second_attempt_cannot_claim_a_resource_whose_writer_is_alive(store):
    store.claim_lease("worktree:impl/T-001", "att_1", writer_pid=os.getpid())
    with pytest.raises(SingleWriterViolation):
        store.claim_lease("worktree:impl/T-001", "att_2", writer_pid=123)


def test_a_resource_is_reclaimable_once_its_writer_is_gone(store):
    dead = _a_pid_that_is_gone()
    store.claim_lease("worktree:impl/T-001", "att_1", writer_pid=dead)
    lease = store.claim_lease("worktree:impl/T-001", "att_2", writer_pid=os.getpid())
    assert lease.owner_attempt_id == "att_2"


def test_a_write_carrying_a_superseded_epoch_is_fenced_off(store):
    store.claim_lease("r", "att_1", writer_pid=1, is_alive=lambda pid: False)
    store.release_lease("r", lease_epoch=1)
    store.claim_lease("r", "att_2", writer_pid=2, is_alive=lambda pid: False)
    with pytest.raises(FencedWriteError):
        store.heartbeat_lease("r", lease_epoch=1)
    with pytest.raises(FencedWriteError):
        store.release_lease("r", lease_epoch=1)


def test_revoking_terminates_the_writer_before_reallocating(store):
    """SIGTERM, bounded grace, SIGKILL, verify — §4.1 enforcement step 4."""
    signals, alive = [], {"pid": True}

    def terminate(pid, sig):
        signals.append(sig)
        if sig == signal.SIGKILL:
            alive["pid"] = False

    reclaimed = store.claim_lease("r", "att_1", writer_pid=777, is_alive=lambda pid: False)
    assert reclaimed.lease_epoch == 1
    assert store.revoke_lease(
        "r", is_alive=lambda pid: alive["pid"], terminate=terminate,
        grace_seconds=0.5, sleep=lambda s: None,
    )
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert store.lease("r") is None


def test_a_lease_whose_writer_survives_sigkill_is_never_reallocated(store):
    """Fail closed: an unkillable writer keeps the resource, rather than sharing it."""
    store.claim_lease("r", "att_1", writer_pid=777, is_alive=lambda pid: False)
    assert not store.revoke_lease(
        "r", is_alive=lambda pid: True, terminate=lambda pid, sig: None,
        grace_seconds=0.5, sleep=lambda s: None,
    )
    assert store.lease("r").owner_attempt_id == "att_1"


def test_pid_alive_is_false_for_a_reaped_child():
    assert not pid_alive(_a_pid_that_is_gone())
    assert pid_alive(os.getpid())


# --- projections are exactly the replay of the stream -------------------------------------------


def test_replay_reconstructs_the_stored_projections(store, lifecycle):
    run = "run_abc"
    store.register_task("T-001")
    lifecycle.set_task_status("T-001", "active")
    lifecycle.start_run("T-001", run, workflow_id="wf_1", workflow_revision=1)
    lifecycle.set_node_phase(run, "impl", "ready")
    att = attempt_id(run, "impl", 1)
    lifecycle.claim_attempt(att, run_id=run, node_id="impl", lease_epoch=1)
    lifecycle.set_node_phase(run, "impl", "claimed", attempt_id=att, attempt_index=1)
    lifecycle.start_attempt(att, run_id=run, writer_pid=4321)
    lifecycle.set_node_phase(run, "impl", "running")
    lifecycle.heartbeat_attempt(att, run_id=run)
    lifecycle.finish_attempt(att, run_id=run, status="succeeded", outcome_ref="art_x")
    lifecycle.set_node_phase(run, "impl", "succeeded")
    lifecycle.finish_run("T-001", run, "succeeded")
    store.claim_lease("worktree:impl/T-001", att, writer_pid=99, is_alive=lambda pid: False)
    store.heartbeat_lease("worktree:impl/T-001", lease_epoch=1)

    assert replay(store.events()) == lifecycle.stored_projections()


def test_replay_needs_nothing_but_the_events(store, lifecycle, db_path):
    """No filesystem, no database, no clock: the fold runs on a plain list."""
    store.register_task("T-002")
    lifecycle.set_task_status("T-002", "active")
    events = list(store.events())
    store.close()
    db_path.unlink()

    projections = replay(events)
    assert projections.tasks["T-002"].status == "active"
    assert projections.sequence == events[-1].sequence


def test_replay_of_a_prefix_gives_the_state_at_that_point(store, lifecycle):
    store.register_task("T-003")
    mid = store.head_sequence()
    lifecycle.set_task_status("T-003", "active")
    at_mid = replay([e for e in store.events() if e.sequence <= mid])
    assert at_mid.tasks["T-003"].status == "planned"


def test_the_controller_lock_is_not_a_domain_lease(store, lifecycle):
    """It has no event, so replay must not see it — and neither must the projection."""
    assert store.lease(CONTROLLER_RESOURCE) is not None
    assert lifecycle.stored_projections().leases == {}
    assert replay(store.events()).leases == {}


# --- state machines fail closed --------------------------------------------------------------


def test_an_undeclared_task_transition_is_refused(store, lifecycle):
    store.register_task("T-001")
    lifecycle.set_task_status("T-001", "active")
    lifecycle.set_task_status("T-001", "accepted")
    with pytest.raises(IllegalTransition):
        lifecycle.set_task_status("T-001", "active")


def test_an_undeclared_node_transition_is_refused(store, lifecycle):
    with pytest.raises(IllegalTransition):
        lifecycle.set_node_phase("run_1", "impl", "running")     # pending -> running


def test_a_refused_transition_appends_no_event(store, lifecycle):
    before = store.head_sequence()
    with pytest.raises(IllegalTransition):
        lifecycle.set_node_phase("run_1", "impl", "succeeded")
    assert store.head_sequence() == before


def test_an_unknown_status_is_refused_rather_than_stored(store, lifecycle):
    store.register_task("T-001")
    with pytest.raises(IllegalTransition):
        lifecycle.set_task_status("T-001", "finished-ish")


def test_a_failed_node_may_be_re_armed_for_a_retry(store, lifecycle):
    run = "run_r"
    lifecycle.set_node_phase(run, "impl", "ready")
    lifecycle.set_node_phase(run, "impl", "claimed")
    lifecycle.set_node_phase(run, "impl", "failed")
    lifecycle.set_node_phase(run, "impl", "ready")
    assert lifecycle.stored_projections().nodes[(run, "impl")].phase == "ready"


def test_an_event_is_appended_for_every_committed_transition(store, lifecycle):
    store.register_task("T-001")
    lifecycle.set_task_status("T-001", "active")
    types = [e.type for e in store.events()]
    assert types == [EventType.TASK_REGISTERED, EventType.TASK_STATUS_CHANGED]


# --- P12B F4: pool pauses and the blocked requeue ------------------------------------------


def _pause(pool, until, attempt, *, known=1):
    return {
        "pool_id": pool, "paused_until": until, "reset_known": known, "reason": "quota",
        "failure_kind": "quota_exhausted", "attempt_id": attempt,
        "recorded_at": "2026-09-17T00:00:00Z",
    }


def test_pool_pauses_are_append_only_and_read_back_latest_per_pool(store):
    store.record_pool_pause(_pause("p1", "2026-09-18T00:00:00Z", "a1"))
    store.record_pool_pause(_pause("p1", "2026-09-19T00:00:00Z", "a2"))
    store.record_pool_pause(_pause("p1", "2026-09-30T00:00:00Z", "a1"))  # same key: ignored
    store.record_pool_pause(_pause("p2", "2026-09-16T00:00:00Z", "a3"))  # already over

    active = store.active_pool_pauses("2026-09-17T12:00:00Z")
    assert [(r["pool_id"], r["paused_until"], r["attempt_id"]) for r in active] == [
        ("p1", "2026-09-19T00:00:00Z", "a2")]
    assert store.active_pool_pauses("2026-09-19T00:00:00Z") == []
    assert len(store.conn.execute("SELECT * FROM pool_pauses").fetchall()) == 3


def test_a_client_may_not_record_a_pool_pause(db_path, store):
    client = ControlStore.open_client(db_path)
    try:
        with pytest.raises(SingleWriterViolation):
            client.record_pool_pause(_pause("p1", "2026-09-18T00:00:00Z", "a1"))
    finally:
        client.close()


def test_a_blocked_requeue_is_one_transaction_and_replays_exactly(store, lifecycle):
    from maestro.workflows.failures import FailureRecord

    run = "run_blk"
    store.register_task("T-002")
    lifecycle.start_run("T-002", run, workflow_id="wf_2", workflow_revision=1)
    lifecycle.set_node_phase(run, "impl", "ready")
    att = attempt_id(run, "impl", 1)
    lifecycle.claim_attempt(att, run_id=run, node_id="impl", lease_epoch=1)
    lifecycle.set_node_phase(run, "impl", "claimed", attempt_id=att, attempt_index=1)

    record = FailureRecord(kind="context_overflow", evidence="too long")
    lifecycle.requeue_blocked(
        att, run_id=run, node_id="impl", record=record, pause_row=None,
        exclude_route=("cli", "m1"), at="2026-09-17T01:00:00Z",
    )
    att2 = attempt_id(run, "impl", 2)
    lifecycle.claim_attempt(att2, run_id=run, node_id="impl", lease_epoch=2)
    lifecycle.set_node_phase(run, "impl", "claimed", attempt_id=att2, attempt_index=2)
    lifecycle.start_attempt(att2, run_id=run, writer_pid=1)
    lifecycle.set_node_phase(run, "impl", "running")
    lifecycle.requeue_blocked(
        att2, run_id=run, node_id="impl",
        record=FailureRecord(kind="rate_limited", retry_after_sec=30),
        pause_row=_pause("p1", "2026-09-17T02:00:00Z", att2),
        exclude_route=("cli", "m1"), at="2026-09-17T01:30:00Z",
    )

    counters = lifecycle.node_counters(run, "impl")
    assert counters == {
        "blocked_attempts": 2, "quality_failures": 0,
        "excluded_routes": frozenset({("cli", "m1")}),
        "first_blocked_at": "2026-09-17T01:00:00Z",
        "retry_resets": 0,
    }
    row = store.conn.execute("SELECT phase, attempt_index FROM node_states").fetchone()
    assert (row["phase"], row["attempt_index"]) == ("ready", 2)
    statuses = store.conn.execute(
        "SELECT status, failure_kind FROM attempts ORDER BY attempt_id").fetchall()
    assert [tuple(r) for r in statuses] == [
        ("failed", "context_overflow"), ("failed", "rate_limited")]
    assert [r["attempt_id"] for r in store.active_pool_pauses("2026-09-17T00:00:00Z")] == [att2]
    assert replay(store.events()) == lifecycle.stored_projections()
    assert lifecycle.node_counters(run, "other")["excluded_routes"] == frozenset()


def test_a_failed_requeue_leaves_nothing_behind(store, lifecycle):
    from types import SimpleNamespace

    run = "run_bad"
    store.register_task("T-003")
    lifecycle.start_run("T-003", run, workflow_id="wf_3", workflow_revision=1)
    lifecycle.set_node_phase(run, "impl", "ready")
    att = attempt_id(run, "impl", 1)
    lifecycle.claim_attempt(att, run_id=run, node_id="impl", lease_epoch=1)
    lifecycle.set_node_phase(run, "impl", "claimed", attempt_id=att, attempt_index=1)
    before = len(store.events())
    # A record that cannot be serialised fails after the attempt and phase moves.
    broken = SimpleNamespace(kind="timeout", failure_class="blocked")
    with pytest.raises(AttributeError):
        lifecycle.requeue_blocked(
            att, run_id=run, node_id="impl", record=broken,
            pause_row=None, exclude_route=None,
        )
    assert len(store.events()) == before
    row = store.conn.execute("SELECT phase, blocked_attempts FROM node_states").fetchone()
    assert (row["phase"], row["blocked_attempts"]) == ("claimed", 0)
    assert store.conn.execute("SELECT status FROM attempts").fetchone()[0] == "claimed"


# --- P12B F6: the quality retry, its cone, and the one sanctioned phase override ------


def _claimed(store, lifecycle, run, node_id="impl", task="T-F6", index=1):
    """A node claimed for attempt `index`, with the run open."""
    if store.task(task) is None:
        store.register_task(task)
        lifecycle.start_run(task, run, workflow_id="wf_f6", workflow_revision=1)
    att = attempt_id(run, node_id, index)
    lifecycle.set_node_phase(run, node_id, "ready", attempt_index=index)
    lifecycle.claim_attempt(att, run_id=run, node_id=node_id, lease_epoch=1)
    lifecycle.set_node_phase(run, node_id, "claimed", attempt_id=att, attempt_index=index)
    return att


def _record(kind="empty_output"):
    from maestro.workflows.failures import FailureRecord

    return FailureRecord(kind=kind, evidence="nothing came back")


def test_override_phase_permits_only_the_two_moves_the_ladder_needs(store, lifecycle):
    run = "run_ov"
    att = _claimed(store, lifecycle, run)
    lifecycle.set_node_phase(run, "impl", "running", attempt_id=att)
    lifecycle.override_phase(run, "impl", "pending", reason="retry_cone", attempt_id=att)
    assert store.conn.execute(
        "SELECT phase FROM node_states WHERE node_id = 'impl'").fetchone()[0] == "pending"

    lifecycle.set_node_phase(run, "impl", "ready", attempt_index=1)
    lifecycle.set_node_phase(run, "impl", "claimed", attempt_id=att)
    lifecycle.set_node_phase(run, "impl", "running", attempt_id=att)
    lifecycle.set_node_phase(run, "impl", "succeeded", attempt_id=att)
    lifecycle.override_phase(run, "impl", "failed", reason="repair refused", attempt_id=att)
    assert store.conn.execute(
        "SELECT phase FROM node_states WHERE node_id = 'impl'").fetchone()[0] == "failed"


def test_override_phase_refuses_anything_outside_that_closed_set(store, lifecycle):
    run = "run_ov2"
    att = _claimed(store, lifecycle, run)
    for phase in ("succeeded", "running", "skipped"):
        with pytest.raises(IllegalTransition):
            lifecycle.override_phase(run, "impl", phase, reason="no", attempt_id=att)
    lifecycle.set_node_phase(run, "impl", "running", attempt_id=att)
    lifecycle.set_node_phase(run, "impl", "failed", attempt_id=att)
    with pytest.raises(IllegalTransition):
        lifecycle.override_phase(run, "impl", "failed", reason="only from succeeded")


def test_an_overridden_phase_replays_exactly(store, lifecycle):
    run = "run_ov3"
    att = _claimed(store, lifecycle, run)
    lifecycle.set_node_phase(run, "impl", "running", attempt_id=att)
    event = lifecycle.override_phase(run, "impl", "pending", reason="retry_cone", attempt_id=att)
    assert event.payload["override"] == "retry_cone"
    assert replay(store.events()) == lifecycle.stored_projections()


def test_requeue_cone_resets_every_node_and_spends_one_quality_failure(store, lifecycle):
    run = "run_cone"
    att = _claimed(store, lifecycle, run, node_id="gate")
    lifecycle.set_node_phase(run, "gate", "running", attempt_id=att)
    lifecycle.set_node_phase(run, "impl", "ready")
    lifecycle.set_node_phase(run, "impl", "claimed")
    lifecycle.set_node_phase(run, "impl", "running")
    lifecycle.set_node_phase(run, "impl", "succeeded")
    lifecycle.set_node_phase(run, "merge", "skipped")

    lifecycle.requeue_cone(
        att, run_id=run, root="impl", cone=("impl", "gate"), record=_record(),
        outcome_ref="art_1", skipped=("merge",),
    )

    phases = dict(store.conn.execute("SELECT node_id, phase FROM node_states"))
    assert phases == {"impl": "pending", "gate": "pending", "merge": "pending"}
    assert lifecycle.node_counters(run, "impl")["quality_failures"] == 1
    assert lifecycle.node_counters(run, "gate")["quality_failures"] == 0
    resets = dict(store.conn.execute("SELECT node_id, retry_resets FROM node_states"))
    assert (resets["impl"], resets["gate"], resets["merge"]) == (1, 1, 0)
    row = store.conn.execute(
        "SELECT status, outcome_ref, failure_kind FROM attempts WHERE attempt_id = ?", (att,)
    ).fetchone()
    assert (row["status"], row["outcome_ref"], row["failure_kind"]) == (
        "failed", "art_1", "empty_output")
    assert replay(store.events()) == lifecycle.stored_projections()


def test_fail_quality_writes_the_node_failed_and_counts_the_last_failure(store, lifecycle):
    run = "run_exhausted"
    att = _claimed(store, lifecycle, run)
    lifecycle.fail_quality(
        att, run_id=run, node_id="impl", root="impl", record=_record(), outcome_ref=None,
    )

    state = store.conn.execute(
        "SELECT phase, quality_failures, retry_resets FROM node_states").fetchone()
    assert (state["phase"], state["quality_failures"], state["retry_resets"]) == ("failed", 1, 0)
    row = store.conn.execute(
        "SELECT status, failure_kind FROM attempts WHERE attempt_id = ?", (att,)).fetchone()
    assert (row["status"], row["failure_kind"]) == ("failed", "empty_output")
    assert replay(store.events()) == lifecycle.stored_projections()


def test_start_sha_is_attempt_telemetry_the_ladder_may_write(store, lifecycle):
    run = "run_sha"
    att = _claimed(store, lifecycle, run)
    lifecycle.record_routing_decision(att, run_id=run, fields={"start_sha": "abc123"})
    assert store.conn.execute(
        "SELECT start_sha FROM attempts WHERE attempt_id = ?", (att,)).fetchone()[0] == "abc123"
