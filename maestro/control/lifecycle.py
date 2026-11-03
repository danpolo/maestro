"""Event reducers and the projections they build (`[P1]`).

Two halves that must agree:

* `replay(events)` is a pure fold — no database, no filesystem, no clock. Hand it the
  event stream and it reconstructs every projection exactly. That is P01's acceptance
  condition "replay of the event stream reconstructs exact projections without external
  I/O", and it is why the reducers live here rather than inside `store.py`.
* `Lifecycle` is the controller-side façade. Each method appends its event and writes the
  projection rows it justifies *inside one transaction*, so the log and the tables can
  never disagree across a crash.

State machines are the ones in `02_EXECUTION_ENGINE_AND_DURABILITY.md` §2, and they fail
closed: an undeclared transition raises `IllegalTransition` rather than being coerced.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace

from maestro.control.models import (
    ATTEMPT_STATUSES,
    NODE_PHASES,
    NODE_TRANSITIONS,
    RUN_STATUSES,
    TASK_STATUSES,
    TASK_TRANSITIONS,
    AttemptProjection,
    Event,
    EventType,
    NodeProjection,
    RunProjection,
    TaskProjection,
    WriterLease,
    now_iso,
)
from maestro.control.store import CONTROLLER_RESOURCE, ControlStore


class IllegalTransition(RuntimeError):
    """A state machine was asked for an edge it does not have."""


@dataclass(frozen=True)
class Projections:
    """Everything the event stream determines, keyed by its identifier."""

    tasks: dict[str, TaskProjection] = field(default_factory=dict)
    runs: dict[str, RunProjection] = field(default_factory=dict)
    nodes: dict[tuple[str, str], NodeProjection] = field(default_factory=dict)
    attempts: dict[str, AttemptProjection] = field(default_factory=dict)
    leases: dict[str, WriterLease] = field(default_factory=dict)
    sequence: int = 0


def check_task_transition(current: str, target: str) -> None:
    if target not in TASK_STATUSES:
        raise IllegalTransition(f"unknown task status {target!r}")
    if current == target:
        return
    if target not in TASK_TRANSITIONS[current]:
        raise IllegalTransition(f"task may not move {current!r} -> {target!r}")


def check_node_transition(current: str, target: str) -> None:
    if target not in NODE_PHASES:
        raise IllegalTransition(f"unknown node phase {target!r}")
    if current == target:
        return
    if target not in NODE_TRANSITIONS[current]:
        raise IllegalTransition(f"node may not move {current!r} -> {target!r}")


# --- the pure fold --------------------------------------------------------------------


def apply(state: Projections, event: Event) -> Projections:
    """Fold one event into the projections. Pure: no I/O, no clock, no randomness."""
    tasks = dict(state.tasks)
    runs = dict(state.runs)
    nodes = dict(state.nodes)
    attempts = dict(state.attempts)
    leases = dict(state.leases)
    body = event.payload
    kind = event.type

    if kind == EventType.TASK_REGISTERED:
        tasks[event.aggregate_id] = TaskProjection(
            task_id=event.aggregate_id,
            current_spec_revision=int(body.get("spec_revision", 1)),
            status=body.get("status", "planned"),
            hold=bool(body.get("hold", False)),
        )
    elif kind == EventType.TASK_REVISION_APPLIED:
        task = tasks.get(event.aggregate_id)
        if task is not None:
            tasks[event.aggregate_id] = replace(
                task, current_spec_revision=int(body["spec_revision"])
            )
    elif kind == EventType.TASK_STATUS_CHANGED:
        task = tasks.get(event.aggregate_id)
        if task is not None:
            tasks[event.aggregate_id] = replace(task, status=body["status"])
    elif kind == EventType.RUN_STARTED:
        runs[body["run_id"]] = RunProjection(
            run_id=body["run_id"],
            task_id=event.aggregate_id,
            workflow_id=body["workflow_id"],
            workflow_revision=int(body["workflow_revision"]),
            status="running",
            started_at=event.recorded_at,
        )
    elif kind == EventType.RUN_FINISHED:
        run = runs.get(body["run_id"])
        if run is not None:
            runs[body["run_id"]] = replace(
                run, status=body["status"], finished_at=event.recorded_at
            )
    elif kind == EventType.NODE_PHASE_CHANGED:
        key = (body["run_id"], body["node_id"])
        node = nodes.get(key) or NodeProjection(
            run_id=body["run_id"], node_id=body["node_id"], phase="pending"
        )
        nodes[key] = replace(
            node,
            phase=body["phase"],
            attempt_index=int(body.get("attempt_index", node.attempt_index)),
            last_attempt_id=body.get("attempt_id", node.last_attempt_id),
        )
    elif kind in (
        EventType.NODE_ATTEMPT_CLAIMED,
        EventType.NODE_ATTEMPT_STARTED,
        EventType.NODE_ATTEMPT_HEARTBEAT,
        EventType.NODE_ATTEMPT_FINISHED,
    ):
        attempts[event.attempt_id] = _reduce_attempt(
            attempts.get(event.attempt_id), event, kind, body
        )
    elif kind == EventType.LEASE_CLAIMED:
        leases[event.aggregate_id] = WriterLease(
            resource_id=event.aggregate_id,
            lease_epoch=int(body["lease_epoch"]),
            owner_attempt_id=body["owner_attempt_id"],
            writer_pid=body.get("writer_pid"),
            claimed_at=event.recorded_at,
            heartbeat_at=event.recorded_at,
            expires_at=body.get("expires_at", event.recorded_at),
        )
    elif kind == EventType.LEASE_PID_BOUND:
        lease = leases.get(event.aggregate_id)
        if lease is not None:
            leases[event.aggregate_id] = lease.bound(
                int(body["writer_pid"]), event.recorded_at
            )
    elif kind == EventType.LEASE_HEARTBEAT:
        lease = leases.get(event.aggregate_id)
        if lease is not None:
            leases[event.aggregate_id] = replace(
                lease, heartbeat_at=event.recorded_at,
                expires_at=body.get("expires_at", lease.expires_at),
            )
    elif kind in (EventType.LEASE_RELEASED, EventType.LEASE_REVOKED):
        leases.pop(event.aggregate_id, None)

    return Projections(
        tasks=tasks, runs=runs, nodes=nodes, attempts=attempts,
        leases=leases, sequence=event.sequence,
    )


def _reduce_attempt(current, event: Event, kind: str, body: dict) -> AttemptProjection:
    base = current or AttemptProjection(
        attempt_id=event.attempt_id,
        run_id=body.get("run_id", event.run_id or ""),
        node_id=body.get("node_id", ""),
        status="pending",
    )
    if kind == EventType.NODE_ATTEMPT_CLAIMED:
        return replace(
            base, status="claimed",
            lease_epoch=int(body.get("lease_epoch", base.lease_epoch)),
            deadline_at=body.get("deadline_at", base.deadline_at),
        )
    if kind == EventType.NODE_ATTEMPT_STARTED:
        return replace(
            base, status="running",
            writer_pid=body.get("writer_pid", base.writer_pid),
            started_at=event.recorded_at, heartbeat_at=event.recorded_at,
        )
    if kind == EventType.NODE_ATTEMPT_HEARTBEAT:
        return replace(base, heartbeat_at=event.recorded_at)
    return replace(
        base, status=body["status"], heartbeat_at=event.recorded_at,
        outcome_ref=body.get("outcome_ref", base.outcome_ref),
    )


def replay(events) -> Projections:
    """Fold the whole stream. The only input is the stream itself."""
    state = Projections()
    for event in events:
        state = apply(state, event)
    return state


# --- the controller-side façade ---------------------------------------------------------


class Lifecycle:
    """Commits events and the projection rows they justify, atomically."""

    def __init__(self, store: ControlStore):
        self.store = store

    # -- tasks --

    def set_task_status(
        self, task_id: str, status: str, *, at: str | None = None, conn=None
    ) -> Event:
        stamp = at or now_iso()
        row = self.store.task(task_id)
        if row is None:
            raise IllegalTransition(f"task {task_id!r} is not registered")
        check_task_transition(row["status"], status)
        with self._transaction(conn) as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, stamp, task_id),
            )
            return self.store.append_event(
                EventType.TASK_STATUS_CHANGED, task_id,
                payload={"status": status}, recorded_at=stamp, conn=conn,
            )

    # -- runs --

    def start_run(
        self, task_id: str, run_id: str, *, workflow_id: str, workflow_revision: int,
        at: str | None = None, model_policy_revision: str | None = None,
    ) -> Event:
        stamp = at or now_iso()
        payload = {"run_id": run_id, "workflow_id": workflow_id,
                   "workflow_revision": workflow_revision}
        if model_policy_revision is not None:
            # The shared model policy revision the task is bound to for this run.
            payload["model_policy_revision"] = model_policy_revision
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO task_runs"
                " (run_id, task_id, workflow_id, workflow_revision, status, started_at)"
                " VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, task_id, workflow_id, workflow_revision, stamp),
            )
            return self.store.append_event(
                EventType.RUN_STARTED, task_id, payload=payload,
                run_id=run_id, recorded_at=stamp, conn=conn,
            )

    def finish_run(self, task_id: str, run_id: str, status: str, *, at: str | None = None) -> Event:
        if status not in RUN_STATUSES:
            raise IllegalTransition(f"unknown run status {status!r}")
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE task_runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, stamp, run_id),
            )
            return self.store.append_event(
                EventType.RUN_FINISHED, task_id,
                payload={"run_id": run_id, "status": status},
                run_id=run_id, recorded_at=stamp, conn=conn,
            )

    # -- nodes --

    def set_node_phase(
        self, run_id: str, node_id: str, phase: str, *, attempt_id: str | None = None,
        attempt_index: int | None = None, at: str | None = None, conn=None,
    ) -> Event:
        stamp = at or now_iso()
        row = self.store.conn.execute(
            "SELECT * FROM node_states WHERE run_id = ? AND node_id = ?", (run_id, node_id)
        ).fetchone()
        current = row["phase"] if row else "pending"
        check_node_transition(current, phase)
        index = attempt_index if attempt_index is not None else (row["attempt_index"] if row else 0)
        last = attempt_id or (row["last_attempt_id"] if row else None)
        with self._transaction(conn) as conn:
            conn.execute(
                "INSERT INTO node_states"
                " (run_id, node_id, phase, attempt_index, last_attempt_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, node_id) DO UPDATE SET"
                "  phase = excluded.phase, attempt_index = excluded.attempt_index,"
                "  last_attempt_id = excluded.last_attempt_id, updated_at = excluded.updated_at",
                (run_id, node_id, phase, index, last, stamp),
            )
            payload = {
                "run_id": run_id, "node_id": node_id, "phase": phase,
                "attempt_index": index,
            }
            if last is not None:
                payload["attempt_id"] = last
            return self.store.append_event(
                EventType.NODE_PHASE_CHANGED, run_id,
                payload=payload, run_id=run_id, attempt_id=last,
                recorded_at=stamp, conn=conn,
            )

    def override_phase(
        self, run_id: str, node_id: str, phase: str, *, reason: str,
        attempt_id: str | None = None, at: str | None = None, conn=None,
        trigger_attempt_id: str | None = None,
    ) -> Event:
        """The one sanctioned bypass of `check_node_transition` (P12B F6/F7).

        `NODE_TRANSITIONS` describes what a node does on its own; the retry ladder is a
        *controller* decision taken above it, and encoding it as an ordinary edge would
        make every phase reachable from every other. So the bypass is a separate door
        with a closed set of two moves, named in the event it appends:

        * anything -> `pending`, when a retry cone is reset (a `skipped` descendant of the
          cone included, which is why the source phase is unconstrained);
        * `succeeded` -> `failed`, when a diagnoser's accepted proposal is later refused.

        The event is an ordinary `NODE_PHASE_CHANGED` carrying `override`, so `replay`
        folds it exactly as it folds a legal move and the projections stay reconstructible.
        """
        stamp = at or now_iso()
        row = self.store.conn.execute(
            "SELECT * FROM node_states WHERE run_id = ? AND node_id = ?", (run_id, node_id)
        ).fetchone()
        current = row["phase"] if row else "pending"
        if not (phase == "pending" or (current, phase) == ("succeeded", "failed")):
            raise IllegalTransition(
                f"{node_id}: {current!r} -> {phase!r} is not one of the overrides the "
                f"failure ladder may take (any -> 'pending', 'succeeded' -> 'failed')"
            )
        index = row["attempt_index"] if row else 0
        last = attempt_id or (row["last_attempt_id"] if row else None)
        with self._transaction(conn) as conn:
            conn.execute(
                "INSERT INTO node_states"
                " (run_id, node_id, phase, attempt_index, last_attempt_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, node_id) DO UPDATE SET"
                "  phase = excluded.phase, attempt_index = excluded.attempt_index,"
                "  last_attempt_id = excluded.last_attempt_id, updated_at = excluded.updated_at",
                (run_id, node_id, phase, index, last, stamp),
            )
            payload = {
                "run_id": run_id, "node_id": node_id, "phase": phase,
                "attempt_index": index, "override": reason,
            }
            if trigger_attempt_id is not None:
                payload["trigger_attempt_id"] = trigger_attempt_id
            if last is not None:
                payload["attempt_id"] = last
            return self.store.append_event(
                EventType.NODE_PHASE_CHANGED, run_id,
                payload=payload, run_id=run_id, attempt_id=last,
                recorded_at=stamp, conn=conn,
            )

    def requeue_cone(
        self, attempt_id: str, *, run_id: str, root: str, cone: Sequence[str], record,
        outcome_ref: str | None = None, skipped: Sequence[str] = (), at: str | None = None,
    ) -> None:
        """P12B F6: a `bad_output` under the retry budget re-runs its cone, atomically.

        One transaction, for the same reason `requeue_blocked` is one: the node is never
        observable as `failed`, so no `on_failure` edge opens between the failure and the
        retry. Attempts are kept — the history of what was tried is the ladder's evidence —
        and only the root's `quality_failures` moves, because the root is the node whose
        output was bad.

        `skipped` is the cone's already-skipped descendants, handed in by the caller: the
        graph is the runner's to read, and a second reader of the edge list here would be
        a second place for the cone to be decided.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(
                attempt_id, run_id=run_id, status="failed", outcome_ref=outcome_ref,
                at=stamp, conn=conn,
            )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)
            conn.execute(
                "UPDATE node_states SET quality_failures = quality_failures + 1"
                " WHERE run_id = ? AND node_id = ?",
                (run_id, root),
            )
            for node_id in cone:
                self.override_phase(
                    run_id, node_id, "pending", reason=f"retry_cone: {record.kind}",
                    attempt_id=attempt_id if node_id == root else None, at=stamp, conn=conn,
                    trigger_attempt_id=attempt_id,
                )
                conn.execute(
                    "UPDATE node_states SET retry_resets = retry_resets + 1"
                    " WHERE run_id = ? AND node_id = ?",
                    (run_id, node_id),
                )
            for node_id in skipped:
                self.override_phase(
                    run_id, node_id, "pending", reason=f"retry_cone: {record.kind}",
                    at=stamp, conn=conn, trigger_attempt_id=attempt_id,
                )

    def requeue_for_question(
        self, attempt_id: str, *, run_id: str, node_id: str, cone: Sequence[str], record,
        outcome_ref: str | None = None, skipped: Sequence[str] = (), at: str | None = None,
    ) -> None:
        """P12 pilot finding D26: `node_id` asked Dan; its cone waits for the answer, unspent.

        `requeue_cone` without the budget: the attempt is `failed` with kind `dan_question`
        and the cone goes back to `pending` in one transaction, but neither
        `quality_failures` nor `retry_resets` moves. A question about an undefined case is
        not bad work, and the runner holds the nodes (`WorkflowRunner._awaiting_dan`) until
        every question of the round is answered.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(
                attempt_id, run_id=run_id, status="failed", outcome_ref=outcome_ref,
                at=stamp, conn=conn,
            )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)
            for target in dict.fromkeys((*cone, node_id, *skipped)):
                self.override_phase(
                    run_id, target, "pending", reason="dan_question",
                    attempt_id=attempt_id if target == node_id else None, at=stamp,
                    conn=conn, trigger_attempt_id=attempt_id,
                )

    def fail_quality(
        self, attempt_id: str, *, run_id: str, node_id: str, root: str, record,
        outcome_ref: str | None = None, at: str | None = None, spend_to: int = 0,
    ) -> None:
        """P12B F6: the exhausted end of the ladder — today's `failed` path, counted.

        The root's `quality_failures` is spent here too, so a third bad output stores 3.
        F7's repaired run inherits that number and therefore gets no further retries.
        `spend_to` is W13's early entry: it spends the whole budget at once, so a run
        that stopped on its first attempt also gets one final run after the repair.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(
                attempt_id, run_id=run_id, status="failed", outcome_ref=outcome_ref,
                at=stamp, conn=conn,
            )
            row = conn.execute(
                "SELECT phase FROM node_states WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            if row is not None and row["phase"] == "claimed":
                self.set_node_phase(
                    run_id, node_id, "running", attempt_id=attempt_id, at=stamp, conn=conn,
                )
            self.set_node_phase(
                run_id, node_id, "failed", attempt_id=attempt_id, at=stamp, conn=conn,
            )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)
            conn.execute(
                "UPDATE node_states SET quality_failures = MAX(quality_failures + 1, ?)"
                " WHERE run_id = ? AND node_id = ?",
                (spend_to, run_id, root),
            )

    def fail_dead_writer_node(
        self, attempt_id: str, *, run_id: str, node_id: str, record, at: str | None = None
    ) -> None:
        """P12B F8: a node whose writer died with no receipt is `failed`, atomically.

        The attempt is left as the reconciler wrote it (`uncertain` or `failed`, A05) and
        no `outcome_ref` is written. For a candidate the join then finds no document and
        reports it `abandoned`; for a probed effect that did land, or that no probe can
        settle, the failure is the escalation A05 asks for.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT phase FROM node_states WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            if row is not None and row["phase"] == "claimed":
                self.set_node_phase(
                    run_id, node_id, "running", attempt_id=attempt_id, at=stamp, conn=conn,
                )
            self.set_node_phase(
                run_id, node_id, "failed", attempt_id=attempt_id, at=stamp, conn=conn,
            )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)

    def requeue_probed(
        self, attempt_id: str, *, run_id: str, node_id: str, record, at: str | None = None
    ) -> None:
        """P12: a dead writer's postcondition probe proved its effect absent; run it again.

        The attempt becomes `failed` (the probe established the outcome the missing receipt
        could not), and the node goes back to `ready` in the same transaction, so no
        `on_failure` edge opens in between. No retry budget is spent: nothing the node
        produced was judged, so it counts as a blocked attempt, as a F4 requeue does.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(attempt_id, run_id=run_id, status="failed", at=stamp, conn=conn)
            row = conn.execute(
                "SELECT phase FROM node_states WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            path = ["running", "failed", "ready"]
            if row is not None and row["phase"] == "running":
                path = path[1:]
            for phase in path:
                self.set_node_phase(
                    run_id, node_id, phase, attempt_id=attempt_id, at=stamp, conn=conn,
                )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)
            conn.execute(
                "UPDATE node_states SET blocked_attempts = blocked_attempts + 1"
                " WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            )

    def fail_operator_canceled(
        self, attempt_id: str, *, run_id: str, node_id: str, record, at: str | None = None
    ) -> None:
        """P13 W8 `ctl cancel`: the stopped attempt and its node are `failed`, atomically.

        `fail_dead_writer_node` plus the attempt: no reconciler settled this one, the
        operator did. From `uncertain` the node goes straight to `failed`.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(attempt_id, run_id=run_id, status="failed", at=stamp, conn=conn)
            row = conn.execute(
                "SELECT phase FROM node_states WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            if row is not None and row["phase"] == "claimed":
                self.set_node_phase(
                    run_id, node_id, "running", attempt_id=attempt_id, at=stamp, conn=conn,
                )
            self.set_node_phase(
                run_id, node_id, "failed", attempt_id=attempt_id, at=stamp, conn=conn,
            )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)

    def requeue_operator(
        self, attempt_id: str, *, run_id: str, node_id: str, record,
        skipped: Sequence[str] = (), at: str | None = None,
    ) -> None:
        """P13 W8 `ctl retry`: the stopped attempt is `failed` and its node runs again.

        One transaction, so the node is never observable as `failed` and no `on_failure`
        edge opens. `blocked_attempts` moves, as it does for a probed-absent re-run, so
        `pre_validate` does not count the stopped attempt against a non-ladder node.
        Neither `quality_failures` nor `retry_resets` moves: `retry_resets` is an agent's
        ladder index, and spending it would let a later operator retry be refused.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(attempt_id, run_id=run_id, status="failed", at=stamp, conn=conn)
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)
            for target in dict.fromkeys((node_id, *skipped)):
                self.override_phase(
                    run_id, target, "pending", reason="operator_retry",
                    attempt_id=attempt_id if target == node_id else None, at=stamp,
                    conn=conn, trigger_attempt_id=attempt_id,
                )
            conn.execute(
                "UPDATE node_states SET blocked_attempts = blocked_attempts + 1"
                " WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            )

    # -- attempts --

    def claim_attempt(
        self, attempt_id: str, *, run_id: str, node_id: str, lease_epoch: int,
        deadline_at: str | None = None, at: str | None = None,
    ) -> Event:
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO attempts"
                " (attempt_id, run_id, node_id, status, lease_epoch, deadline_at)"
                " VALUES (?, ?, ?, 'claimed', ?, ?)"
                " ON CONFLICT(attempt_id) DO UPDATE SET status = 'claimed',"
                "  lease_epoch = excluded.lease_epoch, deadline_at = excluded.deadline_at",
                (attempt_id, run_id, node_id, lease_epoch, deadline_at),
            )
            return self.store.append_event(
                EventType.NODE_ATTEMPT_CLAIMED, run_id,
                payload={
                    "run_id": run_id, "node_id": node_id, "lease_epoch": lease_epoch,
                    "deadline_at": deadline_at,
                },
                run_id=run_id, attempt_id=attempt_id, recorded_at=stamp, conn=conn,
            )

    def start_attempt(
        self, attempt_id: str, *, run_id: str, writer_pid: int, at: str | None = None,
    ) -> Event:
        """Record the attempt running under its *writing* PID (`[INV-02]`)."""
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET status = 'running', writer_pid = ?,"
                " started_at = ?, heartbeat_at = ? WHERE attempt_id = ?",
                (writer_pid, stamp, stamp, attempt_id),
            )
            return self.store.append_event(
                EventType.NODE_ATTEMPT_STARTED, run_id,
                payload={"writer_pid": writer_pid},
                run_id=run_id, attempt_id=attempt_id, recorded_at=stamp, conn=conn,
            )

    def heartbeat_attempt(self, attempt_id: str, *, run_id: str, at: str | None = None) -> Event:
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET heartbeat_at = ? WHERE attempt_id = ?", (stamp, attempt_id)
            )
            return self.store.append_event(
                EventType.NODE_ATTEMPT_HEARTBEAT, run_id, payload={},
                run_id=run_id, attempt_id=attempt_id, recorded_at=stamp, conn=conn,
            )

    def finish_attempt(
        self, attempt_id: str, *, run_id: str, status: str, outcome_ref: str | None = None,
        at: str | None = None, conn=None,
    ) -> Event:
        if status not in ATTEMPT_STATUSES:
            raise IllegalTransition(f"unknown attempt status {status!r}")
        stamp = at or now_iso()
        with self._transaction(conn) as conn:
            conn.execute(
                "UPDATE attempts SET status = ?, heartbeat_at = ?, outcome_ref = ?"
                " WHERE attempt_id = ?",
                (status, stamp, outcome_ref, attempt_id),
            )
            return self.store.append_event(
                EventType.NODE_ATTEMPT_FINISHED, run_id,
                payload={"status": status, "outcome_ref": outcome_ref},
                run_id=run_id, attempt_id=attempt_id, recorded_at=stamp, conn=conn,
            )

    def record_routing_decision(
        self, attempt_id: str, *, run_id: str, fields: Mapping, at: str | None = None
    ) -> None:
        """Write the W3 routing decision and start telemetry onto a claimed attempt.

        Here rather than in the runner because `attempts` rows are written in exactly one
        module, and a second writer would be a second place to keep the FSM honest. This
        one only fills the nullable P12A columns: it never touches `status`, so it cannot
        move an attempt through the lifecycle by accident.
        """
        self._set_attempt_fields(attempt_id, fields)

    def record_routing_settle(
        self, attempt_id: str, *, run_id: str, fields: Mapping, at: str | None = None
    ) -> None:
        """The settle-side half of W3: closing usage, wall time, reset crossing."""
        self._set_attempt_fields(attempt_id, fields)

    def record_failure(
        self, attempt_id: str, *, run_id: str, record, at: str | None = None, conn=None
    ) -> None:
        """P12B F3: the classified failure onto a finished attempt. Append-only, like W3."""
        self._set_attempt_fields(attempt_id, {
            "failure_kind": record.kind,
            "failure_class": record.failure_class,
            "failure_json": json.dumps(record.to_dict()),
        }, conn=conn)

    def requeue_blocked(
        self, attempt_id: str, *, run_id: str, node_id: str, record,
        pause_row: Mapping | None, exclude_route: tuple[str, str] | None,
        outcome_ref: str | None = None, at: str | None = None,
    ) -> None:
        """P12B F4: a `blocked` attempt goes back to `ready` without spending a retry.

        One transaction, so the node is never observable as `failed` (no `on_failure`
        edge can open) and the pause or route exclusion lands with the requeue or not at
        all. Every phase move still appends its own event, so replay stays exact.
        """
        stamp = at or now_iso()
        with self.store.transaction() as conn:
            self.finish_attempt(
                attempt_id, run_id=run_id, status="failed", outcome_ref=outcome_ref,
                at=stamp, conn=conn,
            )
            row = conn.execute(
                "SELECT * FROM node_states WHERE run_id = ? AND node_id = ?", (run_id, node_id)
            ).fetchone()
            path = ["running", "failed", "ready"]
            if row is not None and row["phase"] == "running":
                path = path[1:]
            for phase in path:
                self.set_node_phase(
                    run_id, node_id, phase, attempt_id=attempt_id, at=stamp, conn=conn,
                )
            self.record_failure(attempt_id, run_id=run_id, record=record, conn=conn)
            assignments = [
                "blocked_attempts = blocked_attempts + 1",
                "first_blocked_at = COALESCE(first_blocked_at, ?)",
            ]
            params: list = [stamp]
            if exclude_route is not None:
                excluded = set(self.node_counters(run_id, node_id)["excluded_routes"])
                excluded.add(tuple(exclude_route))
                assignments.append("excluded_routes_json = ?")
                params.append(json.dumps(sorted(list(pair) for pair in excluded)))
            conn.execute(
                f"UPDATE node_states SET {', '.join(assignments)}"
                " WHERE run_id = ? AND node_id = ?",
                [*params, run_id, node_id],
            )
            if pause_row is not None:
                self.store.record_pool_pause(pause_row, conn=conn)

    def node_counters(self, run_id: str, node_id: str) -> dict:
        """The P12B ladder counters for one node; zeros when the node has no row yet."""
        row = self.store.conn.execute(
            "SELECT blocked_attempts, quality_failures, excluded_routes_json,"
            " first_blocked_at, retry_resets FROM node_states"
            " WHERE run_id = ? AND node_id = ?",
            (run_id, node_id),
        ).fetchone()
        if row is None:
            return {
                "blocked_attempts": 0, "quality_failures": 0,
                "excluded_routes": frozenset(), "first_blocked_at": None,
                "retry_resets": 0,
            }
        pairs = json.loads(row["excluded_routes_json"] or "[]")
        return {
            "blocked_attempts": int(row["blocked_attempts"] or 0),
            "quality_failures": int(row["quality_failures"] or 0),
            "excluded_routes": frozenset(tuple(pair) for pair in pairs),
            "first_blocked_at": row["first_blocked_at"],
            "retry_resets": int(row["retry_resets"] or 0),
        }

    def stepped_down_attempts(
        self, run_id: str, node_id: str | None = None, *, workflow_id: str | None = None
    ) -> int:
        """How many of the run's attempts (or one node's) ran stepped down (P12B F5).

        With `workflow_id`, every run of that workflow is counted rather than this one
        (P12B F7). A repair opens a new run of the same workflow, and both facts the
        count answers are about the workflow, not the run: nothing ever steps down twice,
        and work a weaker model touched is checked by the full suite — neither of which
        a repaired run may forget just because its `run_id` is new.
        """
        sql = "SELECT COUNT(*) FROM attempts WHERE stepped_down = 1"
        params: tuple
        if workflow_id is not None:
            sql += " AND run_id IN (SELECT run_id FROM task_runs WHERE workflow_id = ?)"
            params = (workflow_id,)
        else:
            sql += " AND run_id = ?"
            params = (run_id,)
        if node_id is not None:
            sql += " AND node_id = ?"
            params += (node_id,)
        return int(self.store.conn.execute(sql, params).fetchone()[0])

    def _transaction(self, conn):
        """Join the caller's open transaction, or open one (they do not nest)."""
        return nullcontext(conn) if conn is not None else self.store.transaction()

    def _set_attempt_fields(self, attempt_id: str, fields: Mapping, *, conn=None) -> None:
        allowed = {
            "binding_id", "binding_json", "backend_id", "model_id", "effort",
            "shape_key_json", "manifest_artifact_id", "route_refusal", "agent_version",
            "session_id", "session_mode", "usage_start_json", "usage_settle_json",
            "wall_time_sec", "reset_crossing",
            "failure_kind", "failure_class", "failure_json", "stepped_down", "start_sha",
            "end_sha",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise IllegalTransition(
                f"attempt {attempt_id}: not routing telemetry columns: {sorted(unknown)}"
            )
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._transaction(conn) as conn:
            conn.execute(
                f"UPDATE attempts SET {assignments} WHERE attempt_id = ?",
                [*fields.values(), attempt_id],
            )

    def attempt_claimed_at(self, attempt_id: str) -> str | None:
        """When this attempt was claimed, read from its own claim event.

        The claim stamp is already durable in the event stream, so `wall_time_sec` is
        measured against what actually happened rather than against a column that would
        have to be kept in step with it.
        """
        row = self.store.conn.execute(
            "SELECT recorded_at FROM events WHERE type = ? AND attempt_id = ?"
            " ORDER BY sequence LIMIT 1",
            (EventType.NODE_ATTEMPT_CLAIMED, attempt_id),
        ).fetchone()
        return row["recorded_at"] if row else None

    # -- recovery --

    def stored_projections(self) -> Projections:
        """Read the projection tables back. Compare with `replay()` to detect drift."""
        conn = self.store.conn
        tasks = {
            row["task_id"]: TaskProjection(
                task_id=row["task_id"],
                current_spec_revision=int(row["current_spec_revision"]),
                status=row["status"],
                hold=bool(row["hold"]),
            )
            for row in conn.execute("SELECT * FROM tasks")
        }
        runs = {
            row["run_id"]: RunProjection(
                run_id=row["run_id"], task_id=row["task_id"],
                workflow_id=row["workflow_id"],
                workflow_revision=int(row["workflow_revision"]),
                status=row["status"], started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
            for row in conn.execute("SELECT * FROM task_runs")
        }
        nodes = {
            (row["run_id"], row["node_id"]): NodeProjection(
                run_id=row["run_id"], node_id=row["node_id"], phase=row["phase"],
                attempt_index=int(row["attempt_index"]),
                last_attempt_id=row["last_attempt_id"],
            )
            for row in conn.execute("SELECT * FROM node_states")
        }
        attempts = {
            row["attempt_id"]: AttemptProjection(
                attempt_id=row["attempt_id"], run_id=row["run_id"], node_id=row["node_id"],
                status=row["status"], lease_epoch=int(row["lease_epoch"]),
                writer_pid=row["writer_pid"], started_at=row["started_at"],
                heartbeat_at=row["heartbeat_at"], deadline_at=row["deadline_at"],
                outcome_ref=row["outcome_ref"],
            )
            for row in conn.execute("SELECT * FROM attempts")
        }
        leases = {}
        for row in conn.execute("SELECT * FROM writer_leases"):
            if row["resource_id"] == CONTROLLER_RESOURCE:
                continue          # the controller's own lock is not a domain lease
            leases[row["resource_id"]] = WriterLease(
                resource_id=row["resource_id"], lease_epoch=int(row["lease_epoch"]),
                owner_attempt_id=row["owner_attempt_id"], writer_pid=int(row["writer_pid"]),
                claimed_at=row["claimed_at"], heartbeat_at=row["heartbeat_at"],
                expires_at=row["expires_at"],
            )
        return Projections(
            tasks=tasks, runs=runs, nodes=nodes, attempts=attempts,
            leases=leases, sequence=self.store.head_sequence(),
        )
