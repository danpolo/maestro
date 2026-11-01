"""The transactional control plane: SQLite authority, event store, artifacts.

`[P1]` of the graph-engineering evolution. Everything the controller mutates lives in
`.orchestrator/control.sqlite3`; the legacy `state.json` and `journal.ndjson` become
read-only projections exported after each commit (`D08`, incremental vertical evolution).
"""
from __future__ import annotations

from maestro.control.artifacts import ArtifactStore, MissingArtifact
from maestro.control.lifecycle import (
    IllegalTransition,
    Lifecycle,
    Projections,
    replay,
)
from maestro.control.models import (
    ArtifactRef,
    Command,
    Event,
    EventType,
    WriterLease,
)
from maestro.control.store import (
    ControlStore,
    FencedWriteError,
    SingleWriterViolation,
    StaleRevisionError,
    control_db_path,
    enqueue_external_command,
    open_active_store,
)

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "Command",
    "ControlStore",
    "Event",
    "EventType",
    "FencedWriteError",
    "IllegalTransition",
    "Lifecycle",
    "MissingArtifact",
    "Projections",
    "SingleWriterViolation",
    "StaleRevisionError",
    "WriterLease",
    "control_db_path",
    "enqueue_external_command",
    "open_active_store",
    "replay",
]
