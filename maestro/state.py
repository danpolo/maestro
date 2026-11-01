"""Runtime state: the state document, the launch-time sidecar and the journal.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_state.py`; none of them is fixed here.

**`[P1]` adds one conditional layer on top of that, and only one.** When this project has
a `.orchestrator/control.sqlite3`, the SQLite store is the authority and `state.json`
becomes a sequence-stamped read-only projection of it: `write_state` commits the document
as an event and then exports it, and `read_state` re-exports before reading whenever the
file on disk carries a sequence other than the one the database last exported. An outside
edit to `state.json` therefore cannot survive a single read — legacy files cannot
independently overwrite SQLite authority.

When there is no control database — every project that has not been migrated, and every
characterisation fixture — none of this runs and the behaviour above is byte-identical to
the reference. That is `D08`: one authority migrates at a time, with a working export
projection on both sides of the move.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
STATE_JSON            = _PATHS.state
JOURNAL               = _PATHS.journal
WORKSPACES            = _PATHS.workspaces
USAGE_JSON            = _PATHS.usage
CONTROL_DB            = _PATHS.control_db

#: Key stamped into an exported `state.json` naming the event sequence it projects.
CONTROL_SEQUENCE_KEY = "control_sequence"

_CONTROL = None          # cached controller store, opened on first use
_CONTROL_PATH = None     # the path `_CONTROL` was opened against


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def control_store():
    """This project's controller store, or None when it has no control database.

    Absence of the file is the migration switch, so nothing needs configuring and an
    un-migrated project pays nothing. Presence means the controller lock is taken here:
    a second live writer raises `SingleWriterViolation` rather than being allowed to
    interleave writes with the real controller (`[INV-01]`).
    """
    global _CONTROL, _CONTROL_PATH
    if not CONTROL_DB.exists():
        return None
    if _CONTROL is not None and _CONTROL_PATH == CONTROL_DB:
        return _CONTROL
    from maestro.control.store import ControlStore

    _CONTROL = ControlStore.open_controller(CONTROL_DB)
    _CONTROL_PATH = CONTROL_DB
    return _CONTROL


def _reset_control_store() -> None:
    """Drop the cached store. For tests and for a repo root that moved under us."""
    global _CONTROL, _CONTROL_PATH
    if _CONTROL is not None:
        _CONTROL.close()
    _CONTROL, _CONTROL_PATH = None, None


def _export_state_projection(store, document: dict, sequence: int) -> None:
    """Write `state.json` as the read-only projection of event `sequence`."""
    projected = dict(document)
    projected[CONTROL_SEQUENCE_KEY] = sequence
    tmp = STATE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(projected, indent=2, ensure_ascii=False) + "\n")
    tmp.rename(STATE_JSON)
    store.record_export(STATE_JSON.name, sequence)


def _authoritative_state(store) -> dict | None:
    """The last state document committed to SQLite, or None if none ever was."""
    from maestro.control.models import EventType

    events = store.events(aggregate_id="legacy_state")
    for event in reversed(events):
        if event.type == EventType.LEGACY_STATE_WRITTEN:
            return event.payload.get("document", {})
    return None


def _reconcile_state_projection(store) -> None:
    """Re-export `state.json` if what is on disk is not the exported projection.

    A stale stamp, a missing stamp, an unreadable file or a missing file all mean the
    same thing: whatever is there was not produced by the last commit, so it is not
    authority and gets replaced by one that is.
    """
    expected = store.exported_sequence(STATE_JSON.name)
    if expected == 0:
        return
    try:
        on_disk = json.loads(STATE_JSON.read_text())
        if isinstance(on_disk, dict) and on_disk.get(CONTROL_SEQUENCE_KEY) == expected:
            return
    except Exception:
        pass
    document = _authoritative_state(store)
    if document is not None:
        _export_state_projection(store, document, expected)


def read_state() -> dict:
    store = control_store()
    if store is not None:
        _reconcile_state_projection(store)
    s = json.loads(STATE_JSON.read_text())
    s.setdefault("hitl_mode", False)
    s.setdefault("waiting_on_dan", {})
    s.setdefault("dan_id_counter", 0)
    s.setdefault("retry_counts", {})       # B11: persistent retry counts
    s.setdefault("parked_tasks", [])        # B11: tasks parked after failure
    s.setdefault("launch_times", {})        # Bug #2 (2026-06-26): persist TASK_TIMEOUT clock across restarts
    return s


def write_state(state: dict) -> None:
    state["updated_at"] = now_iso()
    store = control_store()
    if store is not None:
        from maestro.control.models import EventType

        document = {k: v for k, v in state.items() if k != CONTROL_SEQUENCE_KEY}
        event = store.append_event(
            EventType.LEGACY_STATE_WRITTEN, "legacy_state", payload={"document": document}
        )
        _export_state_projection(store, document, event.sequence)
        return
    tmp = STATE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    tmp.rename(STATE_JSON)


def _persist_launch_time(sid: str, ts: float) -> None:
    """Bug #2 (2026-06-26): persist a session's launch epoch to state.json so the
    TASK_TIMEOUT clock survives orchestrator restarts. Previously launch_times was
    in-memory only, so reconcile_in_flight re-adopted survivors with a fresh
    time.time() each restart — a task spanning N restarts could run ~TASK_TIMEOUT*N."""
    state = read_state()
    lt = state.get("launch_times", {})
    lt[sid] = ts
    state["launch_times"] = lt
    write_state(state)


def append_journal(event: str, detail: str, session_id: str = "", *, shadow: bool = True) -> None:
    """`shadow=False` is for an external actor while a controller runs: text line only."""
    record = {"ts": now_iso(), "event": event, "agent": "B5.1-orchestrator",
              "session_id": session_id, "detail": detail}
    store = control_store() if shadow else None
    if store is not None:
        # The journal shadows into the event store first, so the durable copy exists
        # before the append-only text file does. The text line itself is unchanged —
        # its five fields are consumed by the watchdog's stall detector and by
        # `tests/characterization/test_state.py`, and neither may grow a sixth.
        from maestro.control.models import EventType

        store.append_event(
            EventType.LEGACY_JOURNAL_APPENDED, "legacy_journal",
            payload=dict(record), run_id=session_id or None,
        )
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
