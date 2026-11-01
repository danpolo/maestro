"""G8: scope findings precede checks and operator-facing deny-list screening.

The controller freezes the task's scope and reasoned exceptions from its ROADMAP.
Blocking reviewer findings may widen it for rework; agent reports never grant an escape.
An omitted scope retains the existing unrestricted task contract.
"""
from pathlib import Path, PurePosixPath

from maestro import validation


def _path(value) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("/") or ".." in PurePosixPath(text).parts:
        raise ValueError("scope paths must be nonempty repository-relative paths")
    return text.removeprefix("./").rstrip("/")


def policy(task, base: str, *, reviewer_paths=(), snapshot_files=()) -> dict:
    scope = [_path(path) for path in task.scope]
    exceptions = task.legacy_fields.get("scope_exceptions", [])
    if not isinstance(exceptions, list):
        raise ValueError("scope_exceptions must be a list of path/reason entries")
    extra = []
    for entry in exceptions:
        if not isinstance(entry, dict) or not str(entry.get("reason") or "").strip():
            raise ValueError("each scope exception needs a path and reason")
        extra.append({"path": _path(entry.get("path")), "reason": str(entry["reason"]).strip()})
    known = set(snapshot_files)
    resolved = []
    for raw in reviewer_paths:
        try:
            path = _path(raw)
        except ValueError:
            # A comparison/reference outside the repo grants nothing; it is not an
            # authored contract error and must not break gate-worker construction.
            continue
        if path not in known and "/" not in path:
            named = [f for f in known if f.rsplit("/", 1)[-1] == path]
            if len(named) > 1:
                continue
            if named:
                path = named[0]
        if path not in resolved:
            resolved.append(path)
    return {"scope": scope, "exceptions": extra, "reviewer_paths": resolved, "base": base}


def out_of_scope_finding(path: str, reason: str = "") -> str:
    """The blocking finding for a change outside the task's scope; `reason` says why
    when it is not just the path (GDT-6: goal work the task does not ask for)."""
    why = f"{reason}; " if reason else ""
    return (f"out_of_scope: {path}; {why}remove this change or have the task author declare "
            "a reasoned scope_exceptions entry for a legitimately needed file")


def findings(tree: Path, document: dict) -> list[str]:
    scope = document["scope"]
    if not scope:
        return []
    base = document.get("base")
    try:
        changed = validation.changed_paths(tree, base, since_fork=True) if base else None
    except Exception:  # An unreadable diff is a gate finding, never permission to proceed.
        changed = None
    if changed is None:
        return ["scope_diff_unavailable: cannot establish the committed changes against the run base"]
    allowed = [*scope, *(entry["path"] for entry in document["exceptions"])]
    return [out_of_scope_finding(path) for path in changed if not (validation.path_matches(path, allowed)
                                        or path in document["reviewer_paths"])]
