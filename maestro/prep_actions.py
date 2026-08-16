"""Sidecar store for orchestrator-prepared human actions.

Records, per task, the single human action the orchestrator has prepared for the
operator (e.g. a notebook link, a script command). Persists as JSON at
`.orchestrator/prepared_actions.json` alongside `state.json`. Never raises on missing or
corrupt files — callers can always assume a dict is returned.

Extracted verbatim from the reference project's `scripts/prepared_actions.py`, which the
reference orchestrator imports as a sibling module (`import prepared_actions as
prep_actions`) and uses as a module at nine call sites. Bodies are unchanged — only the
import block and the derivation of the module-level path global differ: the reference
computes it from `__file__`'s grandparent, maestro from `Paths.from_env()`. This module
is a leaf: it depends on nothing in the package but `maestro.paths`, so the call sites
that reach it can bind it with a plain import.

Behavioural surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_prep_actions.py`; none of them is fixed here.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
PREPARED_ACTIONS_FILE: Path = _PATHS.orch_dir / "prepared_actions.json"


def _resolve(path: Path | None) -> Path:
    return path if path is not None else PREPARED_ACTIONS_FILE


def load_actions(path: Path | None = None) -> dict[str, dict]:
    """Read and return the full map {task_id: entry}. Returns {} on missing or corrupt file."""
    p = _resolve(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def get_action(task_id: str, path: Path | None = None) -> dict | None:
    """Return the entry for task_id, or None if not present."""
    return load_actions(path).get(task_id)


def has_action(task_id: str, path: Path | None = None) -> bool:
    """Return True iff an entry exists for task_id."""
    return task_id in load_actions(path)


def set_action(
    task_id: str,
    action: str,
    post_action_cmd: str | None = None,
    dan_id: int | None = None,
    path: Path | None = None,
) -> dict:
    """
    Upsert an entry for task_id. Creates .orchestrator/ dir if missing.
    Writes atomically (tmp file + os.replace). Returns the stored entry.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    actions = load_actions(p)
    entry: dict = {
        "action": action,
        "post_action_cmd": post_action_cmd,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "dan_id": dan_id,
    }
    actions[task_id] = entry

    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(actions, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return entry


def remove_action(task_id: str, path: Path | None = None) -> bool:
    """
    Delete the entry for task_id. Returns True if removed, False if absent.
    Persists atomically. Returns False if the file doesn't exist.
    """
    p = _resolve(path)
    actions = load_actions(p)
    if task_id not in actions:
        return False

    del actions[task_id]

    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(actions, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return True
