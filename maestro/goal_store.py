"""The goals store: one file for Dan's goal decisions, the replanning history and the goals'
durable progress (P13 GDT-8, GDT-9).

`.orchestrator/goal_decisions.json` holds eight sections: `decisions` (GDT-7's goal-conflict
decisions, unchanged), `episodes` (each accepted task's goal impact, GDT-8), `checkpoints`
(milestone checkpoint results), `replans` (replan proposals and Dan's answers), and since
GDT-9 `criteria` (one observation per criterion and contract revision: the gate's reruns of
an accepted task and the checkpoints' reruns, `maestro.goal_state`) and `contract` (each
legal contract revision seen, so a silent removal is caught), and since GDT-10 `finish` (each completion-gate
verification run, keyed by contract revision and commit, `maestro.goal_completion`) and
`operator` (Dan's operator-check sign-offs at a revision and commit). Every writer goes through
`update`, which rewrites one section and keeps the others: GDT-7's `record` wrote
`{"decisions": ...}` alone and would have dropped the rest. The read-modify-write holds an
exclusive `fcntl` lock on the store's directory, the write is atomic, and a corrupt file is
moved aside (`goal_decisions.json.corrupt-<utc>`), never overwritten.

Version 2 (GDT-9): every write stamps `"version": 2`. A file without a version is GDT-8's
layout and reads the same; a missing section reads `[]`; keys this maestro does not know are
kept, so a file a newer maestro wrote survives an older writer. The path stays, so nothing
migrates.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

#: Relative to the project root.
STORE = ".orchestrator/goal_decisions.json"
SECTIONS = ("decisions", "episodes", "checkpoints", "replans", "criteria", "contract",
            "finish", "operator")
#: The layout every write stamps; a file without one is GDT-8's (version 1).
VERSION = 2


def path_for(repo: Path) -> Path:
    return Path(repo) / STORE


def _empty(sections=SECTIONS) -> dict[str, list[dict]]:
    return {s: [] for s in sections}


def read_file(path: Path, sections) -> dict:
    """The stored document at `path` with every one of `sections`; empty sections when
    there is no file, `ValueError` when it is corrupt (not an object, or a section that is
    not a list). Shared with `maestro.backlog` (GDT-11)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty(sections)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} holds no store")
    out = dict(payload)
    for section in sections:
        rows = payload.get(section, [])
        if not isinstance(rows, list):
            raise ValueError(f"{path}: {section} is not a list")
        out[section] = [dict(r) for r in rows if isinstance(r, dict)]
    return out


def _read(path: Path) -> dict:
    return read_file(path, SECTIONS)


def read(repo: Path) -> dict[str, list[dict]]:
    """Every section (`[]` each when there is no file or it does not read)."""
    try:
        return _read(path_for(repo))
    except (OSError, ValueError):
        return _empty()


def problem(repo: Path) -> str:
    """Why the store does not read (`""` when it reads or there is none). A reader only
    reports it; the next writer moves the file aside (`update`)."""
    path = path_for(repo)
    try:
        _read(path)
    except (OSError, ValueError) as exc:
        return f"{path} does not read ({type(exc).__name__}: {exc}); the next write moves " \
               "it aside and starts empty"[:500]
    return ""


def update(repo: Path, section: str, mutate: Callable[[list[dict]], list[dict]]) -> Path:
    """Replace `section` with `mutate(current rows)`, keeping every other section.

    Locked, atomic; a corrupt file is moved aside and the store starts empty. A file that
    cannot be read at all raises."""
    return update_file(path_for(repo), SECTIONS, section, mutate, version=VERSION)


def update_file(path: Path, sections, section: str,
                mutate: Callable[[list[dict]], list[dict]], *, version: int) -> Path:
    """`update` for any store laid out as sections of rows (`maestro.backlog`, GDT-11)."""
    if section not in sections:
        raise ValueError(f"unknown {path.name} section {section!r}; one of "
                         f"{', '.join(sections)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            stored = read_file(path, sections)
        except ValueError:
            stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            os.replace(path, path.with_name(f"{path.name}.corrupt-{stamp}"))
            stored = _empty(sections)
        stored[section] = [dict(r) for r in mutate(list(stored[section]))]
        found = stored.get("version")
        # A newer maestro's version is kept with its keys; anything else becomes ours.
        stored["version"] = found if isinstance(found, int) and found > version else version
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(stored, handle, indent=2)
                handle.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    finally:
        os.close(lock)   # closing the descriptor releases the lock
    return path
