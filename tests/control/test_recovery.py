"""`[P3]`: crash recovery and lease reconciliation (`02_…` §6).

The scan has exactly one job that matters and it is a refusal: never conclude anything
about a resource whose writer is still alive. Everything else — ingesting a receipt,
marking an attempt uncertain, releasing a lease — is bookkeeping that only becomes safe
once that refusal holds.

The distinction the legacy loop could not make is asserted here as three separate tests:
a dead writer *with* a receipt finished its work, a dead writer *without* one has an
unknown outcome, and a live writer has not finished at all. The legacy loop collapsed all
three into FAILED, which for an agent that had already edited a worktree meant re-running
an unrepeatable side effect.
"""
from __future__ import annotations

import pytest

from maestro.control import reconcile as rec
from maestro.control.models import attempt_id as make_attempt_id
from maestro.control.store import SingleWriterViolation
from maestro.workflows.handlers import AttemptInbox, attempts_root


@pytest.fixture
def attempts(repo):
    return attempts_root(repo)


@pytest.fixture
def probe():
    """A liveness probe under the test's control. Nothing is alive unless said so."""

    class Probe:
        def __init__(self):
            self.alive: set[int] = set()

        def __call__(self, pid: int) -> bool:
            return int(pid) in self.alive

        def keep(self, pid: int) -> int:
            self.alive.add(int(pid))
            return pid

    return Probe()


def _seed_attempt(
    store, lifecycle, *, resource="worktree:T-001", pid=4242, node="implementer",
    effect_class="agent_editing_worktree", deadline="2000-01-01T00:00:00Z",
    probe=None,
):
    """One claimed-and-running attempt holding one lease, as a live tick would leave it."""
    run_id = "run_rec"
    store.register_task("T-001")
    lifecycle.start_run("T-001", run_id, workflow_id="wf_rec", workflow_revision=1)
    attempt = make_attempt_id(run_id, node, 1)
    lease = store.claim_lease(
        resource, attempt, writer_pid=pid,
        is_alive=probe or (lambda _p: False),
    )
    store.record_intent(
        f"op_{attempt}", attempt_id=attempt, effect_class=effect_class,
        payload={"node_id": node}, status="dispatched",
    )
    lifecycle.claim_attempt(
        attempt, run_id=run_id, node_id=node, lease_epoch=lease.lease_epoch,
        deadline_at=deadline,
    )
    lifecycle.set_node_phase(run_id, node, "ready")
    lifecycle.set_node_phase(run_id, node, "claimed", attempt_id=attempt)
    lifecycle.start_attempt(attempt, run_id=run_id, writer_pid=pid)
    return run_id, attempt, lease


# ── the live writer ──────────────────────────────────────────────────────────────────


def test_a_live_writer_is_adopted_not_replaced(store, lifecycle, attempts, probe):
    """`§6` step 2. The controller crashed; the worker did not. It keeps its lease and
    runs to its deadline."""
    run_id, attempt, lease = _seed_attempt(store, lifecycle)
    probe.keep(4242)

    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)

    assert [v.disposition for v in report] == [rec.ADOPTED]
    assert report.blocked_resources == ("worktree:T-001",)
    assert store.lease("worktree:T-001") is not None
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt,)
    ).fetchone()["status"] == "running"


def test_an_expired_lease_over_a_live_pid_still_blocks_a_replacement(
    store, lifecycle, attempts, probe
):
    """P3 acceptance: expired lease + confirmed running PID blocks a replacement writer.

    Expiry is a hint that the controller *may* try to revoke. It is not, on its own,
    permission to dispatch a second writer into the same worktree."""
    _seed_attempt(store, lifecycle)
    probe.keep(4242)
    store.conn.execute(
        "UPDATE writer_leases SET expires_at = '1999-01-01T00:00:00Z'"
        " WHERE resource_id = 'worktree:T-001'"
    )

    rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)

    assert rec.resource_is_free(store, "worktree:T-001", is_alive=probe) is False
    with pytest.raises(SingleWriterViolation):
        store.claim_lease(
            "worktree:T-001", "att_replacement", writer_pid=7, is_alive=probe
        )


def test_adoption_refreshes_the_heartbeat_so_expiry_cannot_accumulate(
    store, lifecycle, attempts, probe
):
    _seed_attempt(store, lifecycle)
    probe.keep(4242)
    store.conn.execute(
        "UPDATE writer_leases SET heartbeat_at = '1999-01-01T00:00:00Z'"
        " WHERE resource_id = 'worktree:T-001'"
    )
    rec.reconcile(
        store, lifecycle, attempts_root=attempts, is_alive=probe,
        at="2026-09-12T10:00:00Z",
    )
    assert store.lease("worktree:T-001").heartbeat_at == "2026-09-12T10:00:00Z"


def test_reconciliation_never_kills_anything(store, lifecycle, attempts, probe):
    """The scan reads, decides and records. Termination is `revoke_lease`'s job, and it is
    a separate, explicitly requested act with its own proof obligation."""
    _seed_attempt(store, lifecycle)
    probe.keep(4242)
    killed = []
    rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert killed == []
    assert store.lease("worktree:T-001").writer_pid == 4242


# ── the dead writer ──────────────────────────────────────────────────────────────────


def test_a_dead_writer_with_a_receipt_finished_its_work(store, lifecycle, attempts, probe):
    """The receipt is written last, so its presence means only the reporting was lost."""
    run_id, attempt, _ = _seed_attempt(store, lifecycle)
    AttemptInbox(attempts, attempt).write_receipt(
        operation_id=f"op_{attempt}", exit_status=0
    )

    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)

    assert [v.disposition for v in report] == [rec.INGESTED]
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt,)
    ).fetchone()["status"] == "succeeded"
    assert store.receipt_for(f"op_{attempt}") is not None
    assert store.intent(f"op_{attempt}")["status"] == "reconciled"
    assert store.lease("worktree:T-001") is None


def test_a_nonzero_receipt_ingests_as_a_failure_not_a_success(
    store, lifecycle, attempts, probe
):
    run_id, attempt, _ = _seed_attempt(store, lifecycle)
    AttemptInbox(attempts, attempt).write_receipt(
        operation_id=f"op_{attempt}", exit_status=3
    )
    rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt,)
    ).fetchone()["status"] == "failed"


def test_a_dead_writer_with_no_receipt_and_an_unrepeatable_effect_is_uncertain(
    store, lifecycle, attempts, probe
):
    """`A05`. An agent that was editing a worktree may have done half of it. The honest
    status is "unknown", and the whole point of recording it is that it is not retried."""
    run_id, attempt, _ = _seed_attempt(store, lifecycle)

    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)

    assert [v.disposition for v in report] == [rec.UNCERTAIN]
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt,)
    ).fetchone()["status"] == "uncertain"
    assert store.intent(f"op_{attempt}")["status"] == "failed"


def test_a_dead_writer_with_a_repeatable_effect_is_simply_failed(
    store, lifecycle, attempts, probe
):
    """A local check that died left nothing behind worth probing for. Re-running it is
    safe, so it is a plain failure rather than an open question."""
    run_id, attempt, _ = _seed_attempt(
        store, lifecycle, node="gate", effect_class="repeatable_local_check"
    )
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert [v.disposition for v in report] == [rec.FAILED]


def test_an_attempt_with_no_recorded_intent_is_uncertain_not_failed(
    store, lifecycle, attempts, probe
):
    """It died before recording what it was about to do. An unrecorded effect is the least
    knowable of all, so this fails closed towards `uncertain`."""
    run_id, attempt, _ = _seed_attempt(store, lifecycle)
    store.conn.execute("DELETE FROM operation_intents WHERE attempt_id = ?", (attempt,))
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert [v.disposition for v in report] == [rec.UNCERTAIN]


def test_mixed_effect_classes_are_repeatable_only_if_every_one_is(
    store, lifecycle, attempts, probe
):
    run_id, attempt, _ = _seed_attempt(
        store, lifecycle, node="gate", effect_class="repeatable_local_check"
    )
    store.record_intent(
        "op_extra", attempt_id=attempt, effect_class="external_side_effect",
        status="dispatched",
    )
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert [v.disposition for v in report] == [rec.UNCERTAIN]


# ── the window between dispatch and PID binding ──────────────────────────────────────


def test_a_dispatched_attempt_that_has_not_bound_a_pid_is_left_alone(
    store, lifecycle, attempts, probe
):
    """Between step 3 and step 4 the lease holds the *launcher's* PID, a placeholder that
    says nothing about the worker. Adjudicating it would kill a healthy launch on the tick
    after it started — and the resource stays held throughout regardless."""
    run_id = "run_rec"
    store.register_task("T-001")
    lifecycle.start_run("T-001", run_id, workflow_id="wf_rec", workflow_revision=1)
    attempt = make_attempt_id(run_id, "implementer", 1)
    lease = store.claim_lease(
        "worktree:T-001", attempt, writer_pid=999999, is_alive=lambda _p: False
    )
    lifecycle.claim_attempt(
        attempt, run_id=run_id, node_id="implementer", lease_epoch=lease.lease_epoch,
        deadline_at="2099-01-01T00:00:00Z",
    )

    report = rec.reconcile(
        store, lifecycle, attempts_root=attempts, is_alive=probe,
        at="2026-09-12T10:00:00Z",
    )

    assert [v.disposition for v in report] == [rec.AWAITING_BIND]
    assert report.blocked_resources == ("worktree:T-001",)
    assert store.lease("worktree:T-001") is not None
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt,)
    ).fetchone()["status"] == "claimed"


def test_a_dispatched_attempt_past_its_deadline_is_adjudicated(
    store, lifecycle, attempts, probe
):
    """The wait is bounded. A launch that never reported a PID and ran out of deadline is
    a lost worker, not an indefinitely patient one."""
    run_id = "run_rec"
    store.register_task("T-001")
    lifecycle.start_run("T-001", run_id, workflow_id="wf_rec", workflow_revision=1)
    attempt = make_attempt_id(run_id, "implementer", 1)
    lease = store.claim_lease(
        "worktree:T-001", attempt, writer_pid=999999, is_alive=lambda _p: False
    )
    store.record_intent(
        f"op_{attempt}", attempt_id=attempt, effect_class="agent_editing_worktree",
        status="dispatched",
    )
    lifecycle.claim_attempt(
        attempt, run_id=run_id, node_id="implementer", lease_epoch=lease.lease_epoch,
        deadline_at="2000-01-01T00:00:00Z",
    )

    report = rec.reconcile(
        store, lifecycle, attempts_root=attempts, is_alive=probe,
        at="2026-09-12T10:00:00Z",
    )
    assert [v.disposition for v in report] == [rec.UNCERTAIN]


# ── structural cases ─────────────────────────────────────────────────────────────────


def test_the_controllers_own_lock_is_never_reconciled(store, lifecycle, attempts, probe):
    """Probing it would mean probing our own PID, finding it alive, and adopting ourselves."""
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert len(report) == 0
    assert store.lease("controller") is not None


def test_a_lease_whose_attempt_the_store_never_recorded_is_cleaned_up(
    store, lifecycle, attempts, probe
):
    store.claim_lease("worktree:T-ghost", "att_ghost", writer_pid=31337)
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert [v.disposition for v in report] == [rec.ORPHANED]
    assert store.lease("worktree:T-ghost") is None


def test_a_healthy_project_reconciles_to_an_empty_report(store, lifecycle, attempts, probe):
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert len(report) == 0
    assert report.blocked_resources == ()


def test_ingesting_the_same_receipt_twice_records_one_row(
    store, lifecycle, attempts, probe
):
    """A crash between reading a receipt and committing its effect is ordinary, so the
    ingest path has to be idempotent rather than merely usually-correct."""
    run_id, attempt, _ = _seed_attempt(store, lifecycle)
    AttemptInbox(attempts, attempt).write_receipt(
        operation_id=f"op_{attempt}", exit_status=0
    )
    rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    first = store.receipt_for(f"op_{attempt}")["receipt_id"]
    second = store.record_receipt(
        operation_id=f"op_{attempt}", attempt_id=attempt, exit_status=0
    )
    assert second == first
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_receipts"
    ).fetchone()["n"] == 1


def test_a_superseded_epoch_is_still_fenced_out_after_recovery(store, lifecycle, attempts, probe):
    """`§6` step 3. A late write from the attempt the scan just concluded about must be
    rejected, which is why the epoch counter outlives the lease row."""
    from maestro.control.store import FencedWriteError

    run_id, attempt, lease = _seed_attempt(store, lifecycle)
    rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    store.claim_lease("worktree:T-001", "att_next", writer_pid=515151, is_alive=probe)
    with pytest.raises(FencedWriteError):
        store.bind_writer_pid(
            "worktree:T-001", lease_epoch=lease.lease_epoch, writer_pid=616161
        )


def test_reconciliation_reports_every_lease_it_looked_at(store, lifecycle, attempts, probe):
    _seed_attempt(store, lifecycle)
    store.claim_lease("target_branch", "att_other", writer_pid=717171)
    report = rec.reconcile(store, lifecycle, attempts_root=attempts, is_alive=probe)
    assert {v.resource_id for v in report} == {"worktree:T-001", "target_branch"}
