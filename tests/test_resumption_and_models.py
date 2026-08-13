"""Tests for session resumption after quota reset and dynamic model selection."""
import json
from pathlib import Path
import pytest

from maestro import implementer, quota, orchestrator, state, backends


def test_resolve_implementer_model_explicit():
    task_haiku = {"id": "T1", "model": "haiku"}
    task_opus = {"id": "T2", "model": "opus"}
    task_sonnet = {"id": "T3", "model": "sonnet"}
    
    assert implementer.resolve_implementer_model(task_haiku) == "claude-haiku-4-5"
    assert implementer.resolve_implementer_model(task_opus) == "claude-opus-5"
    assert implementer.resolve_implementer_model(task_sonnet) == "claude-sonnet-5"


def test_resolve_implementer_model_heuristics():
    task_complex = {"id": "T4", "title": "Core extraction and architecture refactor"}
    task_standard = {"id": "T5", "title": "Update documentation and add tests"}

    assert implementer.resolve_implementer_model(task_complex) == "claude-opus-5"
    assert implementer.resolve_implementer_model(task_standard) == "claude-sonnet-5"


def test_launch_implementer_resumption(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    
    (workspace / "session_uuid.txt").write_text("test-uuid-1234", encoding="utf-8")
    
    # Stub subprocess.run
    called_cmds = []
    def fake_run(cmd, **kwargs):
        called_cmds.append(cmd)
        return None
    
    monkeypatch.setattr(implementer.subprocess, "run", fake_run)
    monkeypatch.setattr(implementer, "append_journal", lambda *a, **kw: None)

    task = {"id": "T1", "title": "Test Task"}
    implementer.launch_implementer(
        task=task,
        session_id="impl-T1-1",
        workspace=workspace,
        worktree=worktree,
        resume=True,
        session_uuid="test-uuid-1234",
    )
    
    launch_py = (workspace / "launch.py").read_text(encoding="utf-8")
    assert "--resume" in launch_py
    assert "test-uuid-1234" in launch_py
    assert "resume_prompt.txt" in launch_py
    assert (workspace / "session_uuid.txt").read_text().strip() == "test-uuid-1234"


def test_quota_pause_stores_resumable_task(tmp_path, monkeypatch):
    workspace = tmp_path / "impl-T10-20260812-100000"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "session_uuid.txt").write_text("uuid-quota-9999", encoding="utf-8")
    
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    
    monkeypatch.setattr(state, "STATE_JSON", state_file)
    monkeypatch.setattr(quota, "USAGE_JSON", tmp_path / "usage.json")
    monkeypatch.setattr(quota, "append_journal", lambda *a, **kw: None)
    monkeypatch.setattr(quota, "notify_telegram", lambda *a, **kw: None)
    monkeypatch.setattr(state, "STATE_JSON", state_file)
    
    quota._pause_for_usage_limit("T10", "2026-08-12T22:00:00Z", "quota hit", workspace)
    
    st = state.read_state()
    assert "resumable_tasks" in st
    assert "T10" in st["resumable_tasks"]
    assert st["resumable_tasks"]["T10"]["session_uuid"] == "uuid-quota-9999"
