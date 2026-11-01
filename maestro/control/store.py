"""The single-writer SQLite control store (`[P1]`, `[INV-01]`).

`.orchestrator/control.sqlite3` is the transactional state authority. Exactly one
controller process per project may write events, projections and leases; every other
actor — the CLI, the Telegram control plane, the watchdog, worker processes — reaches the
database only through `command_inbox`, and only through `enqueue_external_command`.

Two roles, enforced rather than documented:

* `ControlStore.open_controller()` takes the controller lock, a row in `writer_leases`
  keyed `controller` and bound to the live PID that holds it. A second controller against
  a live holder raises `SingleWriterViolation`; a dead holder is reclaimed, because a
  crashed controller must not lock the project out forever.
* `ControlStore.open_client()` may enqueue commands and read. Every mutating method
  raises `SingleWriterViolation`.

The DDL is `01_DATA_CONTRACTS_AND_SCHEMAS.md` §3 verbatim, plus three projection tables
(`node_states`, `attempts`, `projection_exports`) the reducers in `lifecycle.py` need and
that section does not name.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from maestro.control.models import (
    COMMAND_STATUSES,
    SCHEMA_VERSION,
    Command,
    Event,
    EventType,
    WriterLease,
    event_id,
    new_command_id,
    now_iso,
)

CONTROLLER_RESOURCE = "controller"

#: Seconds a controller lock survives without a heartbeat. Only consulted for a holder
#: whose PID is already gone — a live PID holds its lock however long it runs.
CONTROLLER_LEASE_TTL = 60


class ControlStoreError(RuntimeError):
    """Base class for every refusal this module makes."""


class SingleWriterViolation(ControlStoreError):
    """A non-controller tried to write, or a second controller tried to open. `[INV-01]`"""


class StaleRevisionError(ControlStoreError):
    """A compare-and-set saw a revision other than the one the caller expected."""


class FencedWriteError(ControlStoreError):
    """A write arrived carrying a superseded `lease_epoch`. `[INV-02]`"""


class MissingLease(ControlStoreError):
    """An operation named a lease that no longer exists."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS command_inbox (
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('received', 'processing', 'committed', 'rejected')),
    payload TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    schema_version INTEGER NOT NULL,
    type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    run_id TEXT,
    attempt_id TEXT,
    causation_id TEXT,
    correlation_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    current_spec_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned', 'active', 'waiting', 'blocked', 'accepted', 'canceled')),
    hold BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_revisions (
    task_id TEXT NOT NULL,
    spec_revision INTEGER NOT NULL,
    spec_yaml TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (task_id, spec_revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    spec_revision INTEGER NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id, spec_revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS workflow_revisions (
    workflow_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_spec_revision INTEGER NOT NULL,
    parent_revision INTEGER,
    compiler_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    code_base_sha TEXT NOT NULL,
    execution_objective TEXT NOT NULL,
    dag_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'paused', 'succeeded', 'failed', 'canceled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS writer_leases (
    resource_id TEXT PRIMARY KEY,
    lease_epoch INTEGER NOT NULL,
    owner_attempt_id TEXT NOT NULL,
    writer_pid INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_intents (
    operation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    effect_class TEXT NOT NULL,
    intent_payload TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'dispatched', 'reconciled', 'failed'))
);

CREATE TABLE IF NOT EXISTS operation_receipts (
    receipt_id TEXT PRIMARY KEY,
    operation_id TEXT UNIQUE NOT NULL,
    attempt_id TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    exit_status INTEGER NOT NULL,
    receipt_payload TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operation_intents(operation_id)
);

CREATE TABLE IF NOT EXISTS verification_evidence (
    evidence_id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL,
    exit_status INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    validity TEXT NOT NULL CHECK(validity IN ('current', 'stale', 'invalidated')),
    recorded_at TEXT NOT NULL,
    -- `[P4]` columns. `§2.10` names them; `[P1]` created the table with only the four
    -- fingerprint fields because nothing produced evidence yet. `_migrate_columns` adds
    -- them to a database that predates this revision.
    verifier_version TEXT NOT NULL DEFAULT '',
    external_snapshot_id TEXT,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    output_artifact_refs TEXT NOT NULL DEFAULT '[]',
    reused_from TEXT,
    reuse_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_fingerprint
    ON verification_evidence(check_id, input_fingerprint, environment_fingerprint);

-- `[P4]` `AcceptanceDecision` (`§2.10`). One row per decision, accepted or rejected: a
-- refusal is as much a durable engineering fact as an acceptance, and a table that only
-- recorded successes could not answer "why did this task not graduate?".
CREATE TABLE IF NOT EXISTS acceptance_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_spec_revision INTEGER NOT NULL,
    policy_hash TEXT NOT NULL,
    integrated_source_snapshot TEXT NOT NULL,
    required_evidence_ids TEXT NOT NULL,
    review_dispositions TEXT NOT NULL,
    exemptions TEXT NOT NULL,
    required_effect_receipts TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted', 'rejected')),
    reason TEXT NOT NULL,
    event_id TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acceptance_task ON acceptance_decisions(task_id, run_id);

-- P12A W3: one *controller-verified* outcome per bound attempt, append-only. Never
-- UPDATEd: a verdict that is later reopened is a new row naming the one it amends, so
-- the history of what was believed when stays readable. `verified_by` records who
-- decided — the controller's own run outcome, never the agent's self-report (D06).
CREATE TABLE IF NOT EXISTS routing_outcomes (
    outcome_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    shape_key_json TEXT NOT NULL,
    success INTEGER NOT NULL,
    verified_by TEXT NOT NULL,
    amends TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routing_outcomes_attempt
    ON routing_outcomes(attempt_id, recorded_at);

-- P12A W6: one affected-selection shadow comparison per verification node result,
-- keyed by run, node and snapshot. Written by the controller when it ingests the result,
-- never by a worker; `qualify_affected` reads these rows to decide whether selection may
-- become authoritative. A new table, so `CREATE TABLE IF NOT EXISTS` is its migration.
CREATE TABLE IF NOT EXISTS shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    outcomes_json TEXT NOT NULL,
    discrepancies_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (run_id, node_id, snapshot_id)
);

-- P12B F4: one pool pause per blocked attempt. Append-only; the in-memory pool state is
-- rebuilt from these rows at the start of every poll, so a pause survives a restart.
-- `paused_until` is Zulu ISO; `reset_known` is 0 when the pause is only a backoff guess.
CREATE TABLE IF NOT EXISTS pool_pauses (
    pool_id TEXT NOT NULL,
    paused_until TEXT NOT NULL,
    reset_known INTEGER NOT NULL,
    reason TEXT NOT NULL,
    failure_kind TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (attempt_id, pool_id)
);

-- A recovered pause stays in the audit trail but no longer gates admission.
CREATE TABLE IF NOT EXISTS pool_pause_recoveries (
    pool_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    recovered_at TEXT NOT NULL,
    usage_source TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    PRIMARY KEY (pool_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    type TEXT NOT NULL,
    storage_rel_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Projection tables the reducers maintain. §3 names the aggregates but not these rows;
-- they hold exactly what `lifecycle.replay()` recomputes from the event stream, so a
-- divergence between the two is a detectable bug rather than a silent one.
CREATE TABLE IF NOT EXISTS node_states (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    attempt_index INTEGER NOT NULL DEFAULT 0,
    last_attempt_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    writer_pid INTEGER,
    started_at TEXT,
    heartbeat_at TEXT,
    deadline_at TEXT,
    outcome_ref TEXT,
    -- P12A W3: the durable routing decision, and the telemetry that is cheap to read at
    -- claim and at settle. Every column is nullable, and a value the driver cannot
    -- measure stays NULL rather than being estimated (G6) — `NULL` here means "not
    -- measurable", never "zero". `_migrate_columns` adds them to a live database.
    binding_id TEXT,
    binding_json TEXT,
    backend_id TEXT,
    model_id TEXT,
    effort TEXT,
    shape_key_json TEXT,
    manifest_artifact_id TEXT,
    route_refusal TEXT,
    agent_version TEXT,
    session_id TEXT,
    session_mode TEXT,
    usage_start_json TEXT,
    usage_settle_json TEXT,
    wall_time_sec REAL,
    reset_crossing INTEGER,
    -- P12B F3: the classified failure, when this attempt settled `failed` under a
    -- routed run. Nullable for the same reason as the routing columns above.
    failure_kind TEXT,
    failure_class TEXT,
    failure_json TEXT
);

-- Fencing tokens, kept apart from the lease rows on purpose: a lease is deleted when it
-- is released, and an epoch counter that restarts at 1 with the next claim is not a
-- fencing token at all — a late write from the released holder would pass the check.
CREATE TABLE IF NOT EXISTS resource_epochs (
    resource_id TEXT PRIMARY KEY,
    last_epoch INTEGER NOT NULL
);

-- Sequence stamps for every legacy file exported as a read-only projection.
CREATE TABLE IF NOT EXISTS projection_exports (
    name TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    exported_at TEXT NOT NULL
);
"""


def control_db_path(repo: Path | None = None) -> Path:
    """Where this project's control database lives."""
    from maestro.paths import Paths

    paths = Paths(repo=Path(repo).resolve()) if repo is not None else Paths.from_env()
    return paths.control_db


def pid_alive(pid: int) -> bool:
    """Positive proof a PID is still running (`[INV-02]` enforcement step 3).

    `kill(pid, 0)` rather than a `/proc` stat so this stays honest on any POSIX host.
    `EPERM` means the process exists and is not ours — still alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class _Role:
    name: str

    @property
    def is_controller(self) -> bool:
        return self.name == "controller"


class ControlStore:
    """A connection to `control.sqlite3` scoped to one role."""

    def __init__(self, path: Path, role: str = "client", *, pid: int | None = None):
        self.path = Path(path)
        self.role = _Role(role)
        self.pid = os.getpid() if pid is None else pid
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self.conn.executescript(SCHEMA)
        self._migrate_columns()
        self._holds_controller_lock = False

    # --- connection setup ----------------------------------------------------------

    #: Columns added to an existing table after `[P1]` shipped. `CREATE TABLE IF NOT
    #: EXISTS` is a no-op on a database that already has the table, so a column added to
    #: SCHEMA alone would exist on fresh databases and be missing on every live one.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("verification_evidence", "verifier_version", "TEXT NOT NULL DEFAULT ''"),
        ("verification_evidence", "external_snapshot_id", "TEXT"),
        ("verification_evidence", "started_at", "TEXT NOT NULL DEFAULT ''"),
        ("verification_evidence", "finished_at", "TEXT NOT NULL DEFAULT ''"),
        ("verification_evidence", "output_artifact_refs", "TEXT NOT NULL DEFAULT '[]'"),
        ("verification_evidence", "reused_from", "TEXT"),
        ("verification_evidence", "reuse_reason", "TEXT"),
        # P12A W3. All nullable: an attempt that ran before this revision, or one the
        # graph runner never routed, simply has no routing decision to report.
        ("attempts", "binding_id", "TEXT"),
        ("attempts", "binding_json", "TEXT"),
        ("attempts", "backend_id", "TEXT"),
        ("attempts", "model_id", "TEXT"),
        ("attempts", "effort", "TEXT"),
        ("attempts", "shape_key_json", "TEXT"),
        ("attempts", "manifest_artifact_id", "TEXT"),
        ("attempts", "route_refusal", "TEXT"),
        ("attempts", "agent_version", "TEXT"),
        ("attempts", "session_id", "TEXT"),
        ("attempts", "session_mode", "TEXT"),
        ("attempts", "usage_start_json", "TEXT"),
        ("attempts", "usage_settle_json", "TEXT"),
        ("attempts", "wall_time_sec", "REAL"),
        ("attempts", "reset_crossing", "INTEGER"),
        # P12B F3.
        ("attempts", "failure_kind", "TEXT"),
        ("attempts", "failure_class", "TEXT"),
        ("attempts", "failure_json", "TEXT"),
        # P12B F4. Counters are cumulative across plan repairs (W7 carries them).
        ("node_states", "blocked_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("node_states", "quality_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("node_states", "excluded_routes_json", "TEXT"),
        ("node_states", "first_blocked_at", "TEXT"),
        # P12B F6. `retry_resets` is deliberately *run*-scoped: `compiler.apply_repair`
        # does not carry it into the repaired run, so the final run's re-dispatch is
        # permitted even though the node's cumulative `quality_failures` is spent.
        ("node_states", "retry_resets", "INTEGER NOT NULL DEFAULT 0"),
        ("attempts", "start_sha", "TEXT"),
        # P13 W8: where a writer's tree stood when its attempt ended. With `start_sha`
        # it names the commits the attempt made (D10's reviewer floor reads them).
        ("attempts", "end_sha", "TEXT"),
        # P12B F5: 1 when the attempt ran on a stepped-down route; NULL otherwise.
        ("attempts", "stepped_down", "INTEGER"),
    )

    def _migrate_columns(self) -> None:
        """Add any `_ADDED_COLUMNS` this database does not have yet. Idempotent."""
        for table, column, decl in self._ADDED_COLUMNS:
            existing = {
                row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _apply_pragmas(self) -> None:
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 10000")

    def pragma(self, name: str):
        return self.conn.execute(f"PRAGMA {name}").fetchone()[0]

    @classmethod
    def open_controller(cls, path: Path, *, pid: int | None = None) -> "ControlStore":
        store = cls(path, role="controller", pid=pid)
        store._take_controller_lock()
        return store

    @classmethod
    def open_client(cls, path: Path) -> "ControlStore":
        return cls(path, role="client")

    def close(self) -> None:
        if self._holds_controller_lock:
            self.conn.execute(
                "DELETE FROM writer_leases WHERE resource_id = ? AND writer_pid = ?",
                (CONTROLLER_RESOURCE, self.pid),
            )
            self._holds_controller_lock = False
        self.conn.close()

    def __enter__(self) -> "ControlStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- single-writer enforcement --------------------------------------------------

    def _require_controller(self) -> None:
        if not self.role.is_controller:
            raise SingleWriterViolation(
                "only the controller process may write control.sqlite3; external actors "
                "enqueue a command instead (see enqueue_external_command)"
            )

    def _take_controller_lock(self) -> None:
        """Claim the project-wide controller lock, or refuse to start. `[INV-01]`"""
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id = ?", (CONTROLLER_RESOURCE,)
            ).fetchone()
            if row is not None and row["writer_pid"] != self.pid:
                if pid_alive(row["writer_pid"]):
                    raise SingleWriterViolation(
                        f"controller pid {row['writer_pid']} already owns {self.path}; "
                        "a clean launcher exit is not proof a writer stopped"
                    )
            epoch = self._next_epoch(self.conn, CONTROLLER_RESOURCE)
            stamp = now_iso()
            self.conn.execute(
                "INSERT INTO writer_leases"
                " (resource_id, lease_epoch, owner_attempt_id, writer_pid,"
                "  claimed_at, heartbeat_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(resource_id) DO UPDATE SET"
                "  lease_epoch = excluded.lease_epoch,"
                "  owner_attempt_id = excluded.owner_attempt_id,"
                "  writer_pid = excluded.writer_pid,"
                "  claimed_at = excluded.claimed_at,"
                "  heartbeat_at = excluded.heartbeat_at,"
                "  expires_at = excluded.expires_at",
                (
                    CONTROLLER_RESOURCE,
                    epoch,
                    CONTROLLER_RESOURCE,
                    self.pid,
                    stamp,
                    stamp,
                    _plus_seconds(stamp, CONTROLLER_LEASE_TTL),
                ),
            )
        self._holds_controller_lock = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit. `IMMEDIATE` so a writer conflict surfaces at BEGIN."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- command inbox ---------------------------------------------------------------

    def enqueue(
        self,
        command_type: str,
        payload: dict,
        *,
        idempotency_key: str,
        received_at: str | None = None,
    ) -> Command:
        """Insert a command, or return the one this idempotency key already named.

        Callable from either role: the inbox is precisely the surface external actors
        are allowed to touch. A duplicate never inserts a second row and never rewrites
        the first, so a retrying caller cannot resurrect a command the controller has
        already committed or rejected.
        """
        stamp = received_at or now_iso()
        command_id = new_command_id()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM command_inbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return _command_from_row(existing)
            conn.execute(
                "INSERT INTO command_inbox"
                " (command_id, command_type, idempotency_key, received_at, status, payload, error)"
                " VALUES (?, ?, ?, ?, 'received', ?, NULL)",
                (
                    command_id,
                    command_type,
                    idempotency_key,
                    stamp,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
        return Command(
            command_id=command_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            received_at=stamp,
            status="received",
            payload=payload,
        )

    def command(self, command_id: str) -> Command | None:
        row = self.conn.execute(
            "SELECT * FROM command_inbox WHERE command_id = ?", (command_id,)
        ).fetchone()
        return _command_from_row(row) if row else None

    def pending_commands(self) -> list[Command]:
        rows = self.conn.execute(
            "SELECT * FROM command_inbox WHERE status = 'received' ORDER BY received_at, command_id"
        ).fetchall()
        return [_command_from_row(row) for row in rows]

    def claimed_commands(self) -> list[Command]:
        """Commands still `processing`: claimed, and not settled by whoever claimed them."""
        rows = self.conn.execute(
            "SELECT * FROM command_inbox WHERE status = 'processing' ORDER BY received_at, command_id"
        ).fetchall()
        return [_command_from_row(row) for row in rows]

    def claim_command(self, command_id: str) -> Command | None:
        """Move one `received` command to `processing`. Controller only."""
        self._require_controller()
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE command_inbox SET status = 'processing'"
                " WHERE command_id = ? AND status = 'received'",
                (command_id,),
            ).rowcount
            if not changed:
                return None
            row = conn.execute(
                "SELECT * FROM command_inbox WHERE command_id = ?", (command_id,)
            ).fetchone()
        return _command_from_row(row)

    def settle_command(self, command_id: str, status: str, error: str | None = None) -> None:
        self._require_controller()
        if status not in COMMAND_STATUSES:
            raise ControlStoreError(f"unknown command status {status!r}")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE command_inbox SET status = ?, error = ? WHERE command_id = ?",
                (status, error, command_id),
            )

    # --- event store -----------------------------------------------------------------

    def append_event(
        self,
        type: str,
        aggregate_id: str,
        *,
        correlation_id: str | None = None,
        payload: dict | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        causation_id: str | None = None,
        recorded_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Event:
        """Append one event and return it with its assigned sequence.

        Pass `conn` from inside an open `transaction()` to make the event and the
        projection rows it justifies commit or roll back together.
        """
        self._require_controller()
        if conn is not None:
            return self._insert_event(
                conn, type, aggregate_id, correlation_id, payload, run_id,
                attempt_id, causation_id, recorded_at,
            )
        with self.transaction() as tx:
            return self._insert_event(
                tx, type, aggregate_id, correlation_id, payload, run_id,
                attempt_id, causation_id, recorded_at,
            )

    def _insert_event(
        self, conn, type, aggregate_id, correlation_id, payload,
        run_id, attempt_id, causation_id, recorded_at,
    ) -> Event:
        body = payload or {}
        stamp = recorded_at or now_iso()
        correlation = correlation_id or aggregate_id
        cursor = conn.execute(
            "INSERT INTO events"
            " (event_id, schema_version, type, aggregate_id, run_id, attempt_id,"
            "  causation_id, correlation_id, recorded_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                # The real event_id needs the sequence, which AUTOINCREMENT only assigns
                # on insert, so a unique placeholder goes in and is rewritten below.
                f"evt_pending_{new_command_id()}",
                SCHEMA_VERSION, type, aggregate_id, run_id, attempt_id,
                causation_id, correlation, stamp,
                json.dumps(body, ensure_ascii=False, sort_keys=True),
            ),
        )
        sequence = int(cursor.lastrowid)
        eid = event_id(sequence)
        conn.execute("UPDATE events SET event_id = ? WHERE sequence = ?", (eid, sequence))
        event = Event(
            event_id=eid, sequence=sequence, type=type, aggregate_id=aggregate_id,
            correlation_id=correlation, recorded_at=stamp, payload=body,
            run_id=run_id, attempt_id=attempt_id, causation_id=causation_id,
        )
        # Nullable W2 facts share the core event transaction, behind a savepoint.
        # A failed diagnostic must neither undo the event nor leave partial capture.
        if type in ('DanQuestionAsked', 'DenyListHitAsked', 'DenyListHitRuled',
                    'PermissionAsked', 'PermissionRuled', 'PermissionRefused', 'ControllerError',
                    'WorkflowRepairRefused',
                    'NodeAttemptClaimed'):
            conn.execute('SAVEPOINT w2_observation')
            try:
                from maestro.workflows.telemetry_runtime import observe_event
                observe_event(self, event, conn=conn)
            except Exception:
                conn.execute('ROLLBACK TO w2_observation')
            finally:
                conn.execute('RELEASE w2_observation')
        return event

    def events(self, *, after: int = 0, aggregate_id: str | None = None) -> list[Event]:
        if aggregate_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE sequence > ? ORDER BY sequence", (after,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE sequence > ? AND aggregate_id = ? ORDER BY sequence",
                (after, aggregate_id),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def head_sequence(self) -> int:
        row = self.conn.execute("SELECT MAX(sequence) AS head FROM events").fetchone()
        return int(row["head"] or 0)

    # --- tasks & compare-and-set ------------------------------------------------------

    def register_task(
        self, task_id: str, *, spec_revision: int = 1, status: str = "planned",
        hold: bool = False, at: str | None = None,
    ) -> None:
        self._require_controller()
        stamp = at or now_iso()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tasks"
                " (task_id, current_spec_revision, status, hold, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, spec_revision, status, int(hold), stamp, stamp),
            )
            self.append_event(
                EventType.TASK_REGISTERED, task_id,
                payload={"spec_revision": spec_revision, "status": status, "hold": hold},
                recorded_at=stamp, conn=conn,
            )

    def apply_task_revision(
        self, task_id: str, *, spec_revision: int, spec_yaml: str, source_hash: str,
        expected_revision: int, at: str | None = None,
    ) -> None:
        """Compare-and-set on `tasks.current_spec_revision`.

        A caller holding a stale read fails closed with `StaleRevisionError` rather than
        clobbering the newer revision — the acceptance condition "stale revisions fail
        CAS checks".
        """
        self._require_controller()
        stamp = at or now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT current_spec_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise StaleRevisionError(f"task {task_id!r} is not registered")
            current = int(row["current_spec_revision"])
            if current != expected_revision:
                raise StaleRevisionError(
                    f"task {task_id!r} is at revision {current}, caller expected {expected_revision}"
                )
            if spec_revision <= current:
                raise StaleRevisionError(
                    f"revision {spec_revision} does not advance past {current}"
                )
            conn.execute(
                "INSERT INTO task_revisions"
                " (task_id, spec_revision, spec_yaml, source_hash, applied_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, spec_revision, spec_yaml, source_hash, stamp),
            )
            conn.execute(
                "UPDATE tasks SET current_spec_revision = ?, updated_at = ?"
                " WHERE task_id = ? AND current_spec_revision = ?",
                (spec_revision, stamp, task_id, expected_revision),
            )
            self.append_event(
                EventType.TASK_REVISION_APPLIED, task_id,
                payload={"spec_revision": spec_revision, "source_hash": source_hash},
                recorded_at=stamp, conn=conn,
            )

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    # --- writer leases ----------------------------------------------------------------

    def lease(self, resource_id: str) -> WriterLease | None:
        row = self.conn.execute(
            "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        return _lease_from_row(row) if row else None

    def claim_lease(
        self, resource_id: str, owner_attempt_id: str, *, writer_pid: int,
        ttl_seconds: int = 300, at: str | None = None,
        is_alive: Callable[[int], bool] = pid_alive,
    ) -> WriterLease:
        """Claim `resource_id`, fencing epoch incremented.

        `[INV-02]`: a replacement claim is refused while the previous *writing* PID is
        still alive, whatever the lease's expiry says and whatever exit code the
        launcher returned. Reallocation needs positive proof of termination, which
        `revoke_lease` obtains.
        """
        self._require_controller()
        stamp = at or now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if row is not None and row["owner_attempt_id"] != owner_attempt_id:
                if is_alive(row["writer_pid"]):
                    raise SingleWriterViolation(
                        f"{resource_id} is held by attempt {row['owner_attempt_id']} whose "
                        f"writer pid {row['writer_pid']} is still alive"
                    )
            epoch = self._next_epoch(conn, resource_id)
            conn.execute(
                "INSERT INTO writer_leases"
                " (resource_id, lease_epoch, owner_attempt_id, writer_pid,"
                "  claimed_at, heartbeat_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(resource_id) DO UPDATE SET"
                "  lease_epoch = excluded.lease_epoch,"
                "  owner_attempt_id = excluded.owner_attempt_id,"
                "  writer_pid = excluded.writer_pid,"
                "  claimed_at = excluded.claimed_at,"
                "  heartbeat_at = excluded.heartbeat_at,"
                "  expires_at = excluded.expires_at",
                (
                    resource_id, epoch, owner_attempt_id, writer_pid,
                    stamp, stamp, _plus_seconds(stamp, ttl_seconds),
                ),
            )
            self.append_event(
                EventType.LEASE_CLAIMED, resource_id,
                payload={
                    "lease_epoch": epoch, "owner_attempt_id": owner_attempt_id,
                    "writer_pid": writer_pid, "expires_at": _plus_seconds(stamp, ttl_seconds),
                },
                attempt_id=owner_attempt_id, recorded_at=stamp, conn=conn,
            )
        return _lease_from_row(
            self.conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
            ).fetchone()
        )

    def _next_epoch(self, conn: sqlite3.Connection, resource_id: str) -> int:
        """The next fencing token for `resource_id`. Monotonic for the life of the store."""
        conn.execute(
            "INSERT INTO resource_epochs (resource_id, last_epoch) VALUES (?, 1)"
            " ON CONFLICT(resource_id) DO UPDATE SET last_epoch = last_epoch + 1",
            (resource_id,),
        )
        row = conn.execute(
            "SELECT last_epoch FROM resource_epochs WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        return int(row["last_epoch"])

    def bind_writer_pid(
        self, resource_id: str, *, lease_epoch: int, writer_pid: int, at: str | None = None,
    ) -> WriterLease:
        """Rebind the lease to the leaf process that actually writes (`[INV-02]` step 1).

        Step 4 of the controlled-operation protocol: the claim records the launcher's
        best guess, the worker reports the PID that owns the worktree, and this is where
        the lease stops trusting the guess.
        """
        self._require_controller()
        stamp = at or now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if row is None:
                raise MissingLease(f"no lease on {resource_id!r}")
            self._check_epoch(row, lease_epoch, resource_id)
            conn.execute(
                "UPDATE writer_leases SET writer_pid = ?, heartbeat_at = ?"
                " WHERE resource_id = ? AND lease_epoch = ?",
                (writer_pid, stamp, resource_id, lease_epoch),
            )
            self.append_event(
                EventType.LEASE_PID_BOUND, resource_id,
                payload={"lease_epoch": lease_epoch, "writer_pid": writer_pid},
                attempt_id=row["owner_attempt_id"], recorded_at=stamp, conn=conn,
            )
        return self.lease(resource_id)

    def heartbeat_lease(
        self, resource_id: str, *, lease_epoch: int, ttl_seconds: int = 300,
        at: str | None = None,
    ) -> WriterLease:
        self._require_controller()
        stamp = at or now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if row is None:
                raise MissingLease(f"no lease on {resource_id!r}")
            self._check_epoch(row, lease_epoch, resource_id)
            conn.execute(
                "UPDATE writer_leases SET heartbeat_at = ?, expires_at = ?"
                " WHERE resource_id = ? AND lease_epoch = ?",
                (stamp, _plus_seconds(stamp, ttl_seconds), resource_id, lease_epoch),
            )
            self.append_event(
                EventType.LEASE_HEARTBEAT, resource_id,
                payload={
                    "lease_epoch": lease_epoch,
                    "expires_at": _plus_seconds(stamp, ttl_seconds),
                },
                recorded_at=stamp, conn=conn,
            )
        return self.lease(resource_id)

    def release_lease(self, resource_id: str, *, lease_epoch: int, at: str | None = None) -> None:
        self._require_controller()
        stamp = at or now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if row is None:
                raise MissingLease(f"no lease on {resource_id!r}")
            self._check_epoch(row, lease_epoch, resource_id)
            conn.execute("DELETE FROM writer_leases WHERE resource_id = ?", (resource_id,))
            self.append_event(
                EventType.LEASE_RELEASED, resource_id,
                payload={"lease_epoch": lease_epoch}, recorded_at=stamp, conn=conn,
            )

    def _check_epoch(self, row: sqlite3.Row, lease_epoch: int, resource_id: str) -> None:
        if int(row["lease_epoch"]) != int(lease_epoch):
            raise FencedWriteError(
                f"write to {resource_id!r} carries superseded epoch {lease_epoch}; "
                f"current epoch is {row['lease_epoch']}"
            )

    def revoke_lease(
        self, resource_id: str, *, at: str | None = None,
        is_alive: Callable[[int], bool] = pid_alive,
        terminate: Callable[[int, int], None] = os.kill,
        grace_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Reclaim an expired lease, proving the writer is gone first.

        SIGTERM, bounded grace, SIGKILL, verify — §4.1 enforcement step 4. Returns True
        when the lease was reclaimed; False when the writer survived even SIGKILL, in
        which case the lease stays put and the resource stays unallocatable. Failing
        closed here is the whole point: the alternative is two writers in one worktree.
        """
        self._require_controller()
        stamp = at or now_iso()
        row = self.conn.execute(
            "SELECT * FROM writer_leases WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            return True
        pid = int(row["writer_pid"])
        if is_alive(pid):
            terminate(pid, signal.SIGTERM)
            deadline = grace_seconds
            while deadline > 0 and is_alive(pid):
                sleep(min(0.25, deadline))
                deadline -= 0.25
            if is_alive(pid):
                terminate(pid, signal.SIGKILL)
                sleep(0.25)
            if is_alive(pid):
                return False
        with self.transaction() as conn:
            conn.execute("DELETE FROM writer_leases WHERE resource_id = ?", (resource_id,))
            self.append_event(
                EventType.LEASE_REVOKED, resource_id,
                payload={"lease_epoch": int(row["lease_epoch"]), "writer_pid": pid},
                attempt_id=row["owner_attempt_id"], recorded_at=stamp, conn=conn,
            )
        return True

    # --- controlled operations: intents and receipts ------------------------------------
    #
    # `[P1]` created both tables and left them empty: nothing dispatched an external effect
    # yet. `[P3]` is what fills them, so the accessors land here, next to the leases they
    # are committed alongside — step 2 of the controlled-operation protocol writes a lease
    # row and an intent row in one transaction, and splitting their code across modules is
    # how that stops being one transaction.

    def record_intent(
        self, operation_id: str, *, attempt_id: str, effect_class: str,
        payload: dict | None = None, status: str = "pending", at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Commit an `OperationIntent` before the side effect happens (`[INV-07]`).

        Pass `conn` from inside an open `transaction()` so the intent, the lease and the
        claim event commit together or not at all. A crash between them is the one case
        the protocol cannot reconcile, so it is made impossible rather than handled.
        """
        self._require_controller()
        target = conn or self.conn
        target.execute(
            "INSERT INTO operation_intents"
            " (operation_id, attempt_id, effect_class, intent_payload, committed_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                operation_id, attempt_id, effect_class,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                at or now_iso(), status,
            ),
        )

    def set_intent_status(self, operation_id: str, status: str) -> None:
        self._require_controller()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE operation_intents SET status = ? WHERE operation_id = ?",
                (status, operation_id),
            )

    def intent(self, operation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM operation_intents WHERE operation_id = ?", (operation_id,)
        ).fetchone()

    def intents_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM operation_intents WHERE attempt_id = ? ORDER BY committed_at",
            (attempt_id,),
        ))

    def unreconciled_intents(self) -> list[sqlite3.Row]:
        """Intents that were committed or dispatched and never reconciled.

        These are exactly the operations whose outcome the controller does not know, which
        is what a recovery scan must enumerate (`§6`).
        """
        return list(self.conn.execute(
            "SELECT * FROM operation_intents WHERE status IN ('pending', 'dispatched')"
            " ORDER BY committed_at"
        ))

    def record_receipt(
        self, *, operation_id: str, attempt_id: str, exit_status: int,
        payload: dict | None = None, receipt_id: str | None = None, at: str | None = None,
    ) -> str:
        """Commit an `OperationReceipt` and mark its intent reconciled (`[INV-07]`).

        Idempotent by `operation_id`: ingesting the same worker receipt twice — which a
        crash between the read and the commit makes likely — records one row.
        """
        self._require_controller()
        existing = self.conn.execute(
            "SELECT receipt_id FROM operation_receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if existing is not None:
            return existing["receipt_id"]
        rid = receipt_id or f"rcp_{new_command_id()[4:]}"
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO operation_receipts"
                " (receipt_id, operation_id, attempt_id, completed_at, exit_status,"
                "  receipt_payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rid, operation_id, attempt_id, at or now_iso(), int(exit_status),
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                "UPDATE operation_intents SET status = 'reconciled' WHERE operation_id = ?",
                (operation_id,),
            )
        return rid

    def receipt_for(self, operation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM operation_receipts WHERE operation_id = ?", (operation_id,)
        ).fetchone()

    # --- authoritative evidence and acceptance (`[P4]`) ---------------------------------
    #
    # The rows below are the only thing acceptance is allowed to read. An agent's DONE
    # sentinel, a reviewer's prose, a `tests.pass` flag in a result.json: none of them
    # reaches this table, because none of them was executed by the controller (`D06`).

    def record_evidence(self, evidence: Mapping, *, conn: sqlite3.Connection | None = None) -> str:
        """Insert one `VerificationEvidence` row. Idempotent by `evidence_id`."""
        self._require_controller()
        target = conn or self.conn
        columns = (
            "evidence_id", "check_id", "source_snapshot_id", "attempt_id", "command_hash",
            "input_fingerprint", "environment_fingerprint", "exit_status", "outcome",
            "validity", "recorded_at", "verifier_version", "external_snapshot_id",
            "started_at", "finished_at", "output_artifact_refs", "reused_from",
            "reuse_reason",
        )
        values = [evidence.get(name) for name in columns]
        placeholders = ", ".join("?" for _ in columns)
        target.execute(
            f"INSERT OR IGNORE INTO verification_evidence ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            values,
        )
        return str(evidence["evidence_id"])

    def evidence(self, evidence_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM verification_evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()

    def evidence_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM verification_evidence WHERE attempt_id = ? ORDER BY recorded_at",
            (attempt_id,),
        ))

    def find_current_evidence(
        self, *, check_id: str, source_snapshot_id: str, command_hash: str,
        input_fingerprint: str, environment_fingerprint: str,
    ) -> sqlite3.Row | None:
        """The exact-four-hash lookup of `§2.1`. Anything less than all four is a miss.

        There is deliberately no "close enough" query here — no matching on `check_id`
        plus snapshot, no ignoring the environment. Every relaxation of this WHERE clause
        is a way to accept a stale proof.
        """
        return self.conn.execute(
            "SELECT * FROM verification_evidence"
            " WHERE check_id = ? AND source_snapshot_id = ? AND command_hash = ?"
            "   AND input_fingerprint = ? AND environment_fingerprint = ?"
            "   AND validity = 'current'"
            " ORDER BY recorded_at DESC LIMIT 1",
            (check_id, source_snapshot_id, command_hash, input_fingerprint,
             environment_fingerprint),
        ).fetchone()

    def invalidate_evidence(
        self, *, check_id: str | None = None, except_input_fingerprint: str | None = None,
        validity: str = "invalidated",
    ) -> int:
        """Mark evidence no longer current. Returns how many rows moved."""
        self._require_controller()
        clauses = ["validity = 'current'"]
        params: list[object] = [validity]
        if check_id is not None:
            clauses.append("check_id = ?")
            params.append(check_id)
        if except_input_fingerprint is not None:
            clauses.append("input_fingerprint != ?")
            params.append(except_input_fingerprint)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE verification_evidence SET validity = ? WHERE {' AND '.join(clauses)}",
                params,
            )
            return cursor.rowcount

    def record_acceptance(
        self, decision: Mapping, *, conn: sqlite3.Connection | None = None
    ) -> str:
        """Insert one `AcceptanceDecision` row (`§3.1` step 6)."""
        self._require_controller()
        target = conn or self.conn
        columns = (
            "decision_id", "task_id", "run_id", "task_spec_revision", "policy_hash",
            "integrated_source_snapshot", "required_evidence_ids", "review_dispositions",
            "exemptions", "required_effect_receipts", "decision", "reason", "event_id",
            "decided_at",
        )
        placeholders = ", ".join("?" for _ in columns)
        target.execute(
            f"INSERT OR IGNORE INTO acceptance_decisions ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            [decision.get(name) for name in columns],
        )
        return str(decision["decision_id"])

    def acceptances_for(self, task_id: str, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id is None:
            return list(self.conn.execute(
                "SELECT * FROM acceptance_decisions WHERE task_id = ? ORDER BY decided_at",
                (task_id,),
            ))
        return list(self.conn.execute(
            "SELECT * FROM acceptance_decisions WHERE task_id = ? AND run_id = ?"
            " ORDER BY decided_at",
            (task_id, run_id),
        ))

    def record_routing_outcome(
        self, outcome: Mapping, *, conn: sqlite3.Connection | None = None
    ) -> str:
        """Append one verified routing outcome (P12A W3). Never updates an existing row.

        `INSERT`, following the `record_acceptance` precedent, because these rows are the
        evidence the router's statistics are folded from: rewriting one would silently
        restate history that a ranking has already been computed against.
        """
        self._require_controller()
        target = conn or self.conn
        columns = (
            "outcome_id", "attempt_id", "run_id", "shape_key_json", "success",
            "verified_by", "amends", "recorded_at",
        )
        placeholders = ", ".join("?" for _ in columns)
        target.execute(
            f"INSERT OR IGNORE INTO routing_outcomes ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            [outcome.get(name) for name in columns],
        )
        return str(outcome["outcome_id"])

    def routing_outcomes(
        self, *, attempt_id: str | None = None, run_id: str | None = None
    ) -> list[sqlite3.Row]:
        """Every recorded outcome, oldest first — amendments after what they amend."""
        clauses, params = [], []
        if attempt_id is not None:
            clauses.append("attempt_id = ?")
            params.append(attempt_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return list(self.conn.execute(
            f"SELECT * FROM routing_outcomes{where} ORDER BY recorded_at, rowid", params
        ))

    def record_shadow_comparison(self, row: Mapping) -> str:
        """Store one shadow comparison (P12A W6). Re-ingesting the same one is a no-op."""
        self._require_controller()
        columns = (
            "comparison_id", "run_id", "node_id", "snapshot_id", "attempt_id",
            "selection_json", "outcomes_json", "discrepancies_json", "recorded_at",
        )
        placeholders = ", ".join("?" for _ in columns)
        self.conn.execute(
            f"INSERT OR IGNORE INTO shadow_comparisons ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            [row.get(name) for name in columns],
        )
        return str(row["comparison_id"])

    def shadow_comparisons(self) -> list[sqlite3.Row]:
        """Every stored shadow comparison, in insert order."""
        return list(self.conn.execute("SELECT * FROM shadow_comparisons ORDER BY rowid"))

    def record_pool_pause(
        self, row: Mapping, *, conn: sqlite3.Connection | None = None
    ) -> None:
        """Append one pool pause (P12B F4). Re-recording the same attempt's pause is a no-op."""
        self._require_controller()
        target = conn or self.conn
        columns = (
            "pool_id", "paused_until", "reset_known", "reason", "failure_kind",
            "attempt_id", "recorded_at",
        )
        placeholders = ", ".join("?" for _ in columns)
        target.execute(
            f"INSERT OR IGNORE INTO pool_pauses ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            [row.get(name) for name in columns],
        )

    def active_pool_pauses(self, now_iso: str) -> list[sqlite3.Row]:
        """One row per pool: its latest `paused_until`, when that is still after `now_iso`."""
        rows = self.conn.execute(
            "SELECT p.* FROM pool_pauses p WHERE p.paused_until > ?"
            " AND NOT EXISTS (SELECT 1 FROM pool_pause_recoveries r"
            " WHERE r.pool_id = p.pool_id AND r.attempt_id = p.attempt_id)"
            " ORDER BY pool_id, paused_until DESC, rowid DESC",
            (now_iso,),
        )
        latest: dict[str, sqlite3.Row] = {}
        for row in rows:
            latest.setdefault(row["pool_id"], row)
        return list(latest.values())

    def record_pool_recovery(self, pause: Mapping, *, recovered_at: str,
                             usage_source: str, usage_json: str) -> None:
        """Retire one measured-recovered pause without erasing the failed attempt."""
        self._require_controller()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO pool_pause_recoveries"
                " (pool_id, attempt_id, recovered_at, usage_source, usage_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (pause["pool_id"], pause["attempt_id"], recovered_at,
                 usage_source, usage_json),
            )

    def recovered_pool_pauses(self) -> list[sqlite3.Row]:
        """Recovery markers that may still need their legacy state mirror cleared."""
        return list(self.conn.execute(
            "SELECT p.pool_id, p.paused_until, p.attempt_id, r.recovered_at"
            " FROM pool_pause_recoveries r JOIN pool_pauses p"
            " ON p.pool_id = r.pool_id AND p.attempt_id = r.attempt_id"
            " ORDER BY r.recovered_at DESC"
        ))

    # --- legacy projection stamps ------------------------------------------------------

    def record_export(self, name: str, sequence: int, *, at: str | None = None) -> None:
        self._require_controller()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO projection_exports (name, sequence, exported_at) VALUES (?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET sequence = excluded.sequence,"
                " exported_at = excluded.exported_at",
                (name, sequence, at or now_iso()),
            )

    def exported_sequence(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT sequence FROM projection_exports WHERE name = ?", (name,)
        ).fetchone()
        return int(row["sequence"]) if row else 0


# --- row adapters ---------------------------------------------------------------------


def _command_from_row(row: sqlite3.Row) -> Command:
    return Command(
        command_id=row["command_id"],
        command_type=row["command_type"],
        idempotency_key=row["idempotency_key"],
        received_at=row["received_at"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        error=row["error"],
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        event_id=row["event_id"],
        sequence=int(row["sequence"]),
        type=row["type"],
        aggregate_id=row["aggregate_id"],
        correlation_id=row["correlation_id"],
        recorded_at=row["recorded_at"],
        payload=json.loads(row["payload"]),
        schema_version=int(row["schema_version"]),
        run_id=row["run_id"],
        attempt_id=row["attempt_id"],
        causation_id=row["causation_id"],
    )


def _lease_from_row(row: sqlite3.Row) -> WriterLease:
    return WriterLease(
        resource_id=row["resource_id"],
        lease_epoch=int(row["lease_epoch"]),
        owner_attempt_id=row["owner_attempt_id"],
        writer_pid=int(row["writer_pid"]),
        claimed_at=row["claimed_at"],
        heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"],
    )


def _plus_seconds(stamp: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone as _tz

    base = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the external-actor surface ----------------------------------------------------------


def open_active_store(repo: Path | None = None) -> ControlStore | None:
    """A client store when this project has a control database, else None.

    Absence is the signal, not a flag: a project that has not been migrated onto `[P1]`
    keeps running the legacy paths byte-for-byte, which is what `D08` asks for.
    """
    path = control_db_path(repo)
    if not path.exists():
        return None
    return ControlStore.open_client(path)


def enqueue_external_command(
    command_type: str, payload: dict, *, idempotency_key: str, repo: Path | None = None,
) -> Command | None:
    """The only way the CLI, Telegram or the watchdog may reach the control plane.

    Returns None when the project has no control database — the caller then keeps its
    legacy behaviour. Never raises on a store-level failure: an external actor must not
    be taken down by the control plane, and a dropped command is recoverable where a
    dead watchdog is not.
    """
    store = open_active_store(repo)
    if store is None:
        return None
    try:
        return store.enqueue(command_type, payload, idempotency_key=idempotency_key)
    except sqlite3.Error:
        return None
    finally:
        store.close()
