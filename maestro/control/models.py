"""Typed commands, events, projections and the identifier grammar (`[P1]`).

Pure data and pure functions only: nothing here opens a database, touches the filesystem
or reads a clock that the caller did not hand it. That is what lets
`lifecycle.replay()` reconstruct every projection from the event stream alone, which is
the acceptance condition P01 is graded on.

Identifier formats come from `01_DATA_CONTRACTS_AND_SCHEMAS.md` §1 and are deliberately
opaque: nothing downstream may parse ownership out of a title, a TMUX window name or any
other mutable string.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

SCHEMA_VERSION = 1

#: nanoid alphabet — URL-safe, no separator characters, so an ID stays one path segment.
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def nanoid(size: int = 12) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(size))


def now_iso() -> str:
    """UTC, second resolution, Zulu — the same stamp shape `maestro.state` writes."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- identifier constructors --------------------------------------------------------


def new_run_id() -> str:
    return f"run_{nanoid(16)}"


def new_command_id() -> str:
    return f"cmd_{nanoid(16)}"


def new_operation_id() -> str:
    return f"op_{nanoid(16)}"


def attempt_id(run_id: str, node_id: str, attempt_index: int) -> str:
    return f"att_{run_id}_{node_id}_{attempt_index:02d}"


def artifact_id(content_hash: str) -> str:
    return f"art_{content_hash[:8]}_{nanoid(10)}"


def event_id(sequence: int) -> str:
    return f"evt_{sequence:06d}_{nanoid(10)}"


def evidence_id(check_id: str, source_snapshot_id: str) -> str:
    return f"evi_{check_id}_{source_snapshot_id[:8]}_{nanoid(10)}"


def decision_id(task_id: str, run_id: str) -> str:
    return f"acc_{task_id}_{run_id[-8:]}_{nanoid(8)}"


# --- vocabularies -------------------------------------------------------------------


class EventType:
    """Event type names. Constants rather than an Enum so the stored value is the name."""

    TASK_REGISTERED = "TaskRegistered"
    TASK_REVISION_APPLIED = "TaskRevisionApplied"
    TASK_STATUS_CHANGED = "TaskStatusChanged"
    RUN_STARTED = "RunStarted"
    RUN_FINISHED = "RunFinished"
    NODE_PHASE_CHANGED = "NodePhaseChanged"
    NODE_ATTEMPT_CLAIMED = "NodeAttemptClaimed"
    NODE_ATTEMPT_STARTED = "NodeAttemptStarted"
    NODE_ATTEMPT_HEARTBEAT = "NodeAttemptHeartbeat"
    NODE_ATTEMPT_FINISHED = "NodeAttemptFinished"
    LEASE_CLAIMED = "WriterLeaseClaimed"
    LEASE_PID_BOUND = "WriterLeasePidBound"
    LEASE_HEARTBEAT = "WriterLeaseHeartbeat"
    LEASE_RELEASED = "WriterLeaseReleased"
    LEASE_REVOKED = "WriterLeaseRevoked"
    EVIDENCE_RECORDED = "VerificationEvidenceRecorded"
    ACCEPTANCE_DECIDED = "AcceptanceDecided"
    INTEGRATION_STAGED = "IntegrationStaged"
    TARGET_FAST_FORWARDED = "TargetBranchFastForwarded"
    ARTIFACT_COMMITTED = "ArtifactCommitted"
    COMMAND_COMMITTED = "CommandCommitted"
    LEGACY_STATE_WRITTEN = "LegacyStateWritten"
    LEGACY_JOURNAL_APPENDED = "LegacyJournalAppended"
    MODEL_POLICY_ADOPTED = "ModelPolicyAdopted"


#: Task run lifecycle (`02_EXECUTION_ENGINE_AND_DURABILITY.md` §2.1).
TASK_STATUSES = frozenset(
    {"planned", "active", "waiting", "blocked", "accepted", "canceled"}
)
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"active", "canceled"}),
    "active": frozenset({"waiting", "blocked", "accepted", "canceled"}),
    "waiting": frozenset({"active", "blocked", "canceled"}),
    "blocked": frozenset({"active", "canceled"}),
    "accepted": frozenset(),
    "canceled": frozenset(),
}

#: Node lifecycle (§2.2).
NODE_PHASES = frozenset(
    {"pending", "ready", "claimed", "running", "succeeded", "failed", "skipped", "uncertain"}
)
NODE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "skipped"}),
    "ready": frozenset({"claimed", "skipped"}),
    "claimed": frozenset({"running", "failed", "uncertain"}),
    "running": frozenset({"succeeded", "failed", "uncertain"}),
    "succeeded": frozenset(),
    "failed": frozenset({"ready"}),        # retry / repair re-arms the frontier
    "uncertain": frozenset({"ready", "failed", "succeeded"}),
    "skipped": frozenset(),
}

RUN_STATUSES = frozenset({"running", "paused", "succeeded", "failed", "canceled"})
ATTEMPT_STATUSES = frozenset(
    {"pending", "claimed", "running", "succeeded", "failed", "uncertain"}
)
COMMAND_STATUSES = frozenset({"received", "processing", "committed", "rejected"})


# --- records ------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One row of the idempotent inbox. External actors only ever write these."""

    command_id: str
    command_type: str
    idempotency_key: str
    received_at: str
    status: str
    payload: dict
    error: str | None = None


@dataclass(frozen=True)
class Event:
    """An append-only entry in the causal log (`§2.9`)."""

    event_id: str
    sequence: int
    type: str
    aggregate_id: str
    correlation_id: str
    recorded_at: str
    payload: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    run_id: str | None = None
    attempt_id: str | None = None
    causation_id: str | None = None

    @property
    def artifact_refs(self) -> list[str]:
        refs = self.payload.get("artifact_refs", [])
        return list(refs) if isinstance(refs, (list, tuple)) else []


@dataclass(frozen=True)
class ArtifactRef:
    """Immutable pointer into the content-addressed store (`§2.8`)."""

    artifact_id: str
    type: str
    content_hash: str
    storage_ref: str
    size_bytes: int
    created_at: str
    schema_version: str = "1"
    source_snapshot_id: str | None = None
    producer_attempt_id: str | None = None
    validity_dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class WriterLease:
    """A fenced claim on one writable resource (`[INV-02]`).

    `writer_pid` is the PID of the process that actually writes. A launcher PID here is
    a defect, not a detail: see `02_EXECUTION_ENGINE_AND_DURABILITY.md` §4.1.
    """

    resource_id: str
    lease_epoch: int
    owner_attempt_id: str
    writer_pid: int | None
    claimed_at: str
    heartbeat_at: str
    expires_at: str

    def bound(self, writer_pid: int, at: str) -> "WriterLease":
        return replace(self, writer_pid=writer_pid, heartbeat_at=at)


@dataclass(frozen=True)
class TaskProjection:
    task_id: str
    current_spec_revision: int
    status: str
    hold: bool = False


@dataclass(frozen=True)
class RunProjection:
    run_id: str
    task_id: str
    workflow_id: str
    workflow_revision: int
    status: str
    started_at: str
    finished_at: str | None = None


@dataclass(frozen=True)
class NodeProjection:
    run_id: str
    node_id: str
    phase: str
    attempt_index: int = 0
    last_attempt_id: str | None = None


@dataclass(frozen=True)
class AttemptProjection:
    """Physical execution attempt of a node (`§2.7`)."""

    attempt_id: str
    run_id: str
    node_id: str
    status: str
    lease_epoch: int = 0
    writer_pid: int | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    deadline_at: str | None = None
    outcome_ref: str | None = None
