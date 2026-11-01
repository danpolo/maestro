"""Crash recovery and writer-lease reconciliation (`[P3]`, `02_…` §6).

The controller is allowed to die at any instant. What it is not allowed to do is come back
and guess. This module is the quiescent recovery scan of `§6`, and its whole design turns
on one distinction the legacy loop could not make:

    a dead worker that never did the work   vs   a dead worker that did the work
                                                 and died before reporting it

The legacy reconcile loop read a workspace, found no sentinel, and marked the task FAILED —
which for an agent that had already edited a worktree meant re-running an unrepeatable side
effect. Here, a dead PID is not a verdict. It is a question, answered by looking for the
receipt the worker writes *last* (`handlers.AttemptInbox`): receipt present means the work
finished and only the reporting was lost, so it is ingested; receipt absent means the
outcome is genuinely unknown, so the attempt becomes `uncertain` and stays there until a
postcondition probe or a human resolves it. `A05` is what forbids the convenient third
option of retrying anyway.

The other half is `[INV-02]`, and it is a refusal rather than an action. A lease whose
`expires_at` has passed but whose `writer_pid` is *alive* is not expired in any sense that
matters: the process is still writing. Reconciliation adopts it — refreshes the heartbeat,
lets it run to its deadline — and explicitly does not hand the resource to a replacement.
The only path from a live writer to a free resource is `ControlStore.revoke_lease`, which
proves termination first (SIGTERM, bounded grace, SIGKILL, verify) and fails closed when
the writer survives even that.

Nothing in here is a side effect on the world: no process is killed, no worktree touched.
Reconciliation reads, decides, and records. Killing is a separate, explicitly requested act.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from maestro.control.lifecycle import Lifecycle
from maestro.control.models import now_iso
from maestro.control.store import CONTROLLER_RESOURCE, ControlStore, pid_alive

#: What the scan decided about one attempt. Names are the vocabulary the report prints and
#: the tests assert on, so they are constants rather than prose.
ADOPTED = "adopted"          # writer alive: keep it, refresh the lease
INGESTED = "ingested"        # writer dead, receipt on disk: the work finished
UNCERTAIN = "uncertain"      # writer dead, no receipt, effect not repeatable
FAILED = "failed"            # writer dead, no receipt, effect repeatable
ORPHANED = "orphaned"        # a lease whose attempt the store never recorded
AWAITING_BIND = "awaiting_bind"  # dispatched, not yet reported its writing PID


@dataclass(frozen=True)
class LeaseVerdict:
    """One reconciled lease. Immutable, so a report can be logged and re-read."""

    resource_id: str
    attempt_id: str
    lease_epoch: int
    writer_pid: int | None
    disposition: str
    detail: str = ""

    @property
    def released(self) -> bool:
        """Did the resource become allocatable? Never true while a writer lives."""
        return self.disposition in (INGESTED, UNCERTAIN, FAILED, ORPHANED)

    @property
    def quiet(self) -> bool:
        """Did the scan reach a conclusion that changed nothing? (`AWAITING_BIND`)"""
        return self.disposition == AWAITING_BIND


@dataclass
class ReconcileReport:
    """Everything one scan concluded. Empty is the healthy steady state."""

    verdicts: list[LeaseVerdict] = field(default_factory=list)
    scanned_at: str = ""

    def __iter__(self) -> Iterator[LeaseVerdict]:
        return iter(self.verdicts)

    def __len__(self) -> int:
        return len(self.verdicts)

    def of(self, disposition: str) -> tuple[LeaseVerdict, ...]:
        return tuple(v for v in self.verdicts if v.disposition == disposition)

    @property
    def adopted(self) -> tuple[LeaseVerdict, ...]:
        return self.of(ADOPTED)

    @property
    def blocked_resources(self) -> tuple[str, ...]:
        """Resources a live writer still owns. A dispatcher must not offer these."""
        return tuple(v.resource_id for v in self.verdicts if not v.released)


class Reconciler:
    """The recovery scan, as an object so its probes are injectable.

    `is_alive` and the inbox root are seams for the same reason `store.revoke_lease` takes
    `is_alive`: a test that needs a "surviving writer" must be able to produce one without
    forking a process it might then leak into the developer's session.
    """

    def __init__(
        self,
        store: ControlStore,
        lifecycle: Lifecycle | None = None,
        *,
        attempts_root: Path | None = None,
        is_alive: Callable[[int], bool] = pid_alive,
    ):
        self.store = store
        self.lifecycle = lifecycle or Lifecycle(store)
        self.attempts_root = Path(attempts_root) if attempts_root else None
        self.is_alive = is_alive

    # --- the scan -------------------------------------------------------------------

    def reconcile(self, *, at: str | None = None) -> ReconcileReport:
        """One pass over every domain lease. Step 2 of `tick()`.

        Called on every tick *and* at controller startup, deliberately the same code: a
        restart is not a special case, it is a tick that happens to find more to do. A
        recovery path that only runs after a crash is a recovery path that is never tested.
        """
        stamp = at or now_iso()
        report = ReconcileReport(scanned_at=stamp)
        for row in self._domain_leases():
            report.verdicts.append(self._reconcile_lease(row, stamp))
        for row in self._unleased_running():
            verdict = self._reconcile_unleased(row, stamp)
            if verdict is not None:
                report.verdicts.append(verdict)
        return report

    def _unleased_running(self) -> list:
        """P13 W8: running attempts that hold no lease, so no lease scan ever reaches them.

        A reader, a test designer in its scratch workspace, an async launch and a
        controller-run step claim no resource (`lease_epoch` 0). Their death used to go
        unnoticed, which left the node `running` and its run open forever.
        """
        return list(
            self.store.conn.execute(
                "SELECT * FROM attempts WHERE status = 'running' AND lease_epoch = 0"
                " AND writer_pid IS NOT NULL"
            )
        )

    def _reconcile_unleased(self, attempt, stamp: str) -> LeaseVerdict | None:
        """The dead-writer verdict of `_adjudicate_dead_writer`, with no lease to release.

        A live writer and a receipt on disk are both left alone: the first is working, the
        second is the ingest path's.
        """
        attempt_id, pid = attempt["attempt_id"], int(attempt["writer_pid"])
        if self.is_alive(pid) or self._disk_receipt(attempt_id) is not None:
            return None
        repeatable = self._effect_is_repeatable(attempt_id)
        disposition = FAILED if repeatable else UNCERTAIN
        self.lifecycle.finish_attempt(
            attempt_id, run_id=attempt["run_id"], status=disposition, at=stamp
        )
        self._fail_open_intents(attempt_id)
        self._observe_dead_worker(attempt_id, stamp)
        return LeaseVerdict(
            "", attempt_id, 0, pid, disposition,
            f"writer pid {pid} gone with no receipt; effect "
            + ("is repeatable — failed" if repeatable else "is not repeatable — uncertain"),
        )

    def _domain_leases(self) -> list:
        """Every lease except the controller's own lock.

        The controller lock is this process's; reconciling it would mean probing our own
        PID, finding it alive, and adopting ourselves.
        """
        return list(
            self.store.conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id != ?", (CONTROLLER_RESOURCE,)
            )
        )

    def _reconcile_lease(self, row, stamp: str) -> LeaseVerdict:
        resource_id = row["resource_id"]
        attempt_id = row["owner_attempt_id"]
        epoch = int(row["lease_epoch"])
        pid = int(row["writer_pid"])

        if self.is_alive(pid):
            return self._adopt(resource_id, attempt_id, epoch, pid, stamp)

        attempt = self.store.conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

        # Between dispatch (step 3) and PID binding (step 4) the lease holds the
        # *launcher's* PID, which is a placeholder that exists only to keep the resource
        # unallocatable. Probing it decides nothing about the worker — the worker has not
        # named itself yet — so adjudicating here would kill a healthy launch on the tick
        # after it started. The attempt waits until it either binds a PID or runs out of
        # deadline, and the resource stays held throughout (`[INV-02]`).
        if attempt is not None and attempt["status"] == "claimed" and not _past(
            attempt["deadline_at"], stamp
        ):
            return LeaseVerdict(
                resource_id, attempt_id, epoch, pid, AWAITING_BIND,
                "dispatched; worker has not reported its writing pid yet",
            )

        if attempt is None:
            # A lease with no attempt behind it cannot be adjudicated, only cleaned up.
            self._release(resource_id, epoch, stamp)
            return LeaseVerdict(
                resource_id, attempt_id, epoch, pid, ORPHANED,
                "lease has no recorded attempt",
            )
        return self._adjudicate_dead_writer(row, attempt, stamp)

    def _adopt(self, resource_id, attempt_id, epoch, pid, stamp) -> LeaseVerdict:
        """The writer survived our crash. Refresh the lease and stand back (`§6` step 2).

        This is the branch that makes an expired lease with a live PID safe: the heartbeat
        is refreshed *because* the process is alive, so the expiry never becomes an excuse
        to dispatch a second writer into the same worktree.
        """
        self.store.heartbeat_lease(resource_id, lease_epoch=epoch, at=stamp)
        return LeaseVerdict(
            resource_id, attempt_id, epoch, pid, ADOPTED,
            f"writer pid {pid} still running; adopted to its deadline",
        )

    def _adjudicate_dead_writer(self, row, attempt, stamp: str) -> LeaseVerdict:
        resource_id = row["resource_id"]
        attempt_id = row["owner_attempt_id"]
        epoch = int(row["lease_epoch"])
        pid = int(row["writer_pid"])
        run_id = attempt["run_id"]

        receipt = self._disk_receipt(attempt_id)
        if receipt is not None:
            exit_status = int(receipt.get("exit_status", 1))
            self._ingest_receipt(attempt_id, receipt)
            status = "succeeded" if exit_status == 0 else "failed"
            self.lifecycle.finish_attempt(
                attempt_id, run_id=run_id, status=status, at=stamp,
                outcome_ref=str(receipt.get("payload", {}).get("outcome_ref") or "") or None,
            )
            self._release(resource_id, epoch, stamp)
            return LeaseVerdict(
                resource_id, attempt_id, epoch, pid, INGESTED,
                f"writer pid {pid} gone, receipt found (exit {exit_status}) — ingested",
            )

        repeatable = self._effect_is_repeatable(attempt_id)
        disposition = FAILED if repeatable else UNCERTAIN
        self.lifecycle.finish_attempt(
            attempt_id, run_id=run_id, status=disposition, at=stamp
        )
        self._fail_open_intents(attempt_id)
        self._release(resource_id, epoch, stamp)
        self._observe_dead_worker(attempt_id, stamp)
        return LeaseVerdict(
            resource_id, attempt_id, epoch, pid, disposition,
            f"writer pid {pid} gone with no receipt; effect "
            + ("is repeatable — failed" if repeatable else "is not repeatable — uncertain"),
        )

    def _observe_dead_worker(self, attempt_id, stamp):
        try:
            from maestro.workflows.telemetry_runtime import observe_dead_worker
            observe_dead_worker(self.store, attempt_id, at=stamp)
        except Exception:
            pass

    # --- probes ---------------------------------------------------------------------

    def _disk_receipt(self, attempt_id: str) -> dict | None:
        """The receipt a worker wrote last, if it got that far (`§6` step 2).

        Deliberately the *receipt* and not the result document: a result written and a
        receipt written are two different moments, and only the second means the worker
        reached the end of its own protocol.
        """
        if self.attempts_root is None:
            return None
        from maestro.workflows.handlers import AttemptInbox

        return AttemptInbox(self.attempts_root, attempt_id).receipt()

    def _effect_is_repeatable(self, attempt_id: str) -> bool:
        """`A05`: may this attempt be re-run blind?

        Fails closed twice over. No intent at all means the attempt died before recording
        what it was about to do, and an unrecorded effect is the least knowable of all —
        `uncertain`, not `failed`. And a mixture of classes is repeatable only if *every*
        recorded intent is.
        """
        from maestro.workflows.models import IDEMPOTENT_EFFECT_CLASSES

        intents = self.store.intents_for_attempt(attempt_id)
        if not intents:
            return False
        return all(row["effect_class"] in IDEMPOTENT_EFFECT_CLASSES for row in intents)

    # --- commits ---------------------------------------------------------------------

    def _ingest_receipt(self, attempt_id: str, receipt: dict) -> None:
        operation_id = str(receipt.get("operation_id") or "")
        if not operation_id or self.store.intent(operation_id) is None:
            return
        self.store.record_receipt(
            operation_id=operation_id,
            attempt_id=attempt_id,
            exit_status=int(receipt.get("exit_status", 1)),
            payload=dict(receipt.get("payload") or {}),
        )

    def _fail_open_intents(self, attempt_id: str) -> None:
        """Mark the attempt's unreconciled intents failed, so they stop being open questions.

        `failed` rather than `reconciled`: no receipt ever arrived, so the effect's outcome
        was never established. The status records that the controller stopped waiting, not
        that it learned anything.
        """
        for row in self.store.intents_for_attempt(attempt_id):
            if row["status"] in ("pending", "dispatched"):
                self.store.set_intent_status(row["operation_id"], "failed")

    def _release(self, resource_id: str, epoch: int, stamp: str) -> None:
        self.store.release_lease(resource_id, lease_epoch=epoch, at=stamp)


def _past(deadline: str | None, stamp: str) -> bool:
    """Has `stamp` passed `deadline`? A missing deadline is never past.

    String comparison, not datetime parsing: every stamp in this system is the same
    second-resolution Zulu format, which sorts lexicographically. A parse here would be a
    second place for a format assumption to drift.
    """
    return bool(deadline) and stamp > deadline


def reconcile(
    store: ControlStore,
    lifecycle: Lifecycle | None = None,
    *,
    attempts_root: Path | None = None,
    is_alive: Callable[[int], bool] = pid_alive,
    at: str | None = None,
) -> ReconcileReport:
    """One recovery scan. The function form, for callers that hold no reconciler."""
    return Reconciler(
        store, lifecycle, attempts_root=attempts_root, is_alive=is_alive
    ).reconcile(at=at)


def resource_is_free(
    store: ControlStore, resource_id: str, *, is_alive: Callable[[int], bool] = pid_alive
) -> bool:
    """May a *new* writer be dispatched to `resource_id`? (`[INV-02]` enforcement step 3)

    The question a dispatcher must ask before every claim, and the answer ignores
    `expires_at` entirely. An expired lease over a living process is a held resource; the
    expiry only tells the controller it may *try* to revoke, and revocation is a separate
    act with its own proof obligation.
    """
    lease = store.lease(resource_id)
    if lease is None:
        return True
    if lease.writer_pid is None:
        return False
    return not is_alive(int(lease.writer_pid))
