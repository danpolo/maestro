"""Read the exact pending graph request and its durable run context for /ask."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from maestro.control.artifacts import ArtifactStore
from maestro.control.store import control_db_path
from maestro.hitl import verify


def request_context(repo: Path, state: Mapping, ident: str) -> dict | None:
    found = verify._awaiting_entry(state, ident)
    if found is None:
        return None
    task, entry = found
    req_id = str(entry.get("request_id") or "")
    record = verify._load(req_id)
    if not record or record.get("status") != "pending":
        return None
    if (verify._questions_dir() / f"{req_id}.answer").exists():
        return None
    if record.get("type") not in (verify.REQUEST_TYPE, verify.ACTION_TYPE):
        return None
    if record.get("id") != req_id or record.get("task_id") != task or not entry.get("run_id"):
        raise ValueError("waiting request has no matching request/task/run identity")
    if record.get("run_id") != entry["run_id"]:
        raise ValueError("waiting request belongs to another run")
    snapshot = str(entry.get("snapshot") or "")
    if str(record.get("snapshot") or "") != snapshot:
        raise ValueError("waiting request belongs to another snapshot")
    if record["type"] == verify.REQUEST_TYPE and not snapshot:
        raise ValueError("waiting verification has no snapshot")
    worktree = str(record.get("worktree") or "")
    if not worktree or not Path(worktree).is_dir():
        raise ValueError("waiting request's implementation worktree is missing")
    context = []
    # A regular client store performs schema migration at open. Operator support must
    # only read the controller's existing schema, including on older pinned versions.
    db = control_db_path(repo)
    if not db.is_file():
        raise ValueError("waiting request's control database is missing")
    try:
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            run = conn.execute("SELECT task_id, status FROM task_runs WHERE run_id = ?",
                               (entry["run_id"],)).fetchone()
            if run is None or run["task_id"] != task or run["status"] != "running":
                raise ValueError("run is not active for the waiting task")
            node = "prep" if record["type"] == verify.ACTION_TYPE else "implementer"
            attempt = conn.execute(
                "SELECT attempt_id, backend_id, model_id, manifest_artifact_id "
                "FROM attempts WHERE run_id = ? AND node_id = ? "
                "AND status = 'succeeded' ORDER BY rowid DESC LIMIT 1",
                (entry["run_id"], node),
            ).fetchone()
            if attempt is None or not attempt["manifest_artifact_id"]:
                raise ValueError("waiting run has no saved implementer/prep context")
            context.append(json.dumps(dict(attempt)))
            # ArtifactStore's read API needs only the connection to resolve references;
            # preserve its immutable hash checks without opening a writable store.
            artifacts = ArtifactStore(repo / ".orchestrator" / "artifacts", SimpleNamespace(conn=conn))
            manifest = json.loads(artifacts.read(attempt["manifest_artifact_id"]))
            if not isinstance(manifest, dict) or manifest.get("task_id") != task:
                raise ValueError("saved context does not belong to the waiting task")
            context.append(json.dumps({key: manifest.get(key) for key in (
                "task_id", "acceptance_criteria", "task_notes", "dan_decisions",
                "dan_answers", "relevant_decisions",
                "entrypoints", "rework_note", "omitted_material",
            )}, ensure_ascii=False))
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read run context: {exc}") from exc
    return {
        "task_id": task, "worktree": worktree,
        "action": str(record.get("question") or ""),
        "graph_request": {"request_id": req_id, "run_id": entry["run_id"],
                          "snapshot": snapshot, "type": record.get("type"),
                          "attempt_id": record.get("attempt_id")},
        "graph_context": "\n".join(context),
    }
