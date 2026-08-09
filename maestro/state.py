"""Runtime state: the state document, the launch-time sidecar and the journal.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_state.py`; none of them is fixed here.
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


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def read_state() -> dict:
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


def append_journal(event: str, detail: str, session_id: str = "") -> None:
    record = {"ts": now_iso(), "event": event, "agent": "B5.1-orchestrator",
              "session_id": session_id, "detail": detail}
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
