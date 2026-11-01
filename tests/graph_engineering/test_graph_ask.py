"""Graph operator support must use the exact durable request without altering it."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.hitl import graph_ask, telegram


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    tree = repo / ".orchestrator/worktrees/T7"
    tree.mkdir(parents=True)
    questions = repo / ".orchestrator/questions"
    questions.mkdir()
    monkeypatch.setattr(telegram, "QUESTIONS_DIR", questions)
    record = {"id": "verify-T7-snap", "type": "verification", "status": "pending",
              "task_id": "T7", "run_id": "run_7", "snapshot": "snap",
              "worktree": str(tree), "question": "Check attribution"}
    request = questions / "verify-T7-snap.json"
    request.write_text(json.dumps(record))
    state = {"awaiting_dan_verification": {"T7": {
        "request_id": "verify-T7-snap", "run_id": "run_7", "snapshot": "snap"}}}
    db = repo / ".orchestrator/control.sqlite3"
    with ControlStore.open_controller(db) as store:
        # Seed real durable artifacts in an isolated test controller.
        store.conn.execute("PRAGMA foreign_keys=OFF")
        store.conn.execute("INSERT INTO task_runs VALUES ('run_7','T7','wf',1,'running','now',NULL)")
        artifact = ArtifactStore(repo / ".orchestrator/artifacts", store)
        ref = artifact.put_bytes(json.dumps({"task_id": "T7", "task_notes": "Frozen notes",
            "acceptance_criteria": ["Pin belongs to its adder"], "entrypoints": ["app.py"],
            "dan_answers": "Owner question: Dan is role a",
            "relevant_decisions": [{"path": "docs/adr/0005.md", "excerpt": "Preserve first attribution"}],
            "dan_decisions": [{"question": "adder?", "answer": "role a"}]}).encode(),
            type="context_manifest")
        store.conn.execute("INSERT INTO attempts (attempt_id,run_id,node_id,status,manifest_artifact_id) "
                           "VALUES ('att_7','run_7','implementer','succeeded',?)", (ref.artifact_id,))
    return SimpleNamespace(repo=repo, tree=tree, request=request, record=record,
                           state=state, db=db, artifact=artifact.path_for(ref.content_hash))


def test_reads_immutable_context_without_changing_request_or_database(waiting):
    before = waiting.request.read_bytes(), waiting.db.read_bytes()
    payload = graph_ask.request_context(waiting.repo, waiting.state, "t7")
    assert payload["worktree"] == str(waiting.tree)
    assert "Frozen notes" in payload["graph_context"]
    assert "Pin belongs to its adder" in payload["graph_context"]
    assert "Dan is role a" in payload["graph_context"]
    assert "Preserve first attribution" in payload["graph_context"]
    assert before == (waiting.request.read_bytes(), waiting.db.read_bytes())
    assert not waiting.request.with_suffix(".answer").exists()


@pytest.mark.parametrize("field,value", [("run_id", "other-run"), ("snapshot", "other-snapshot"),
                                           ("task_id", "other-task")])
def test_refuses_request_from_another_identity(waiting, field, value):
    waiting.record[field] = value
    waiting.request.write_text(json.dumps(waiting.record))
    with pytest.raises(ValueError):
        graph_ask.request_context(waiting.repo, waiting.state, "T7")


def test_manual_request_uses_saved_prep_context(waiting):
    waiting.record["type"] = "manual-action"
    waiting.record.pop("snapshot")
    waiting.state["awaiting_dan_verification"]["T7"].pop("snapshot")
    waiting.request.write_text(json.dumps(waiting.record))
    with sqlite3.connect(waiting.db) as conn:
        conn.execute("UPDATE attempts SET node_id='prep'")
    payload = graph_ask.request_context(waiting.repo, waiting.state, "T7")
    assert payload["graph_request"]["type"] == "manual-action"
    assert "Frozen notes" in payload["graph_context"]


@pytest.mark.parametrize("missing", ["tree", "artifact", "db", "attempt"])
def test_refuses_missing_implementation_context(waiting, missing):
    if missing == "tree":
        waiting.tree.rmdir()
    elif missing == "artifact":
        waiting.artifact.unlink()
    elif missing == "db":
        waiting.db.unlink()
    else:
        with sqlite3.connect(waiting.db) as conn:
            conn.execute("DELETE FROM attempts")
    with pytest.raises((ValueError, RuntimeError, OSError)):
        graph_ask.request_context(waiting.repo, waiting.state, "T7")
