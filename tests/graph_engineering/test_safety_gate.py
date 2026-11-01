"""P13 W9: safety refusals happen before tokens or an operator question."""
import pytest
import json
from types import SimpleNamespace

import maestro.state
from maestro.hitl import verify
from maestro.workflows import handlers

from maestro.workflows import routing as wf_routing

from .test_dispatch_wiring import _graph_scenario, _task, _windows
from .test_end_to_end import FEATURE, _V1, _DanAnswers, _git, _happy_scenario, _run_status
from .test_end_to_end import _git_sandbox, _TwoFileQueries
from .conftest import fixture_routing
from .test_dispatch_wiring import _strong_routing
from .test_failure_ladder import _patch_fixture_cli_driver
from .test_merge_guard import _WritingAgent
from maestro.workflows import scope_gate
from .test_dispatch_wiring import _events


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


@pytest.mark.parametrize("checks", [[], [{"id": "V1", "kind": "auto", "check": "true"}],
                                     [{"kind": "auto", "cmd": "true"}], ["not-a-check"]])
def test_no_required_checks_refuses_before_routing_or_launch(run_lane, monkeypatch, checks):
    routed = []
    original = wf_routing.resolve

    def resolve(*args, **kwargs):
        routed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(wf_routing, "resolve", resolve)
    run = run_lane(_graph_scenario("w9_no_required", tasks=[_task("TASK-001", verifications=checks)]))
    assert run.returncode == 0
    assert routed == []
    assert _windows(run) == []
    refusals = _events("PreValidationRefused")
    assert refusals
    assert "verification" in json.loads(refusals[0]["payload"])["reason"]
    store = maestro.state.control_store()
    assert store.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM writer_leases WHERE resource_id LIKE 'worktree:%'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["manual", "dan"])
def test_required_judgment_check_is_valid_before_launch(run_lane, kind):
    run = run_lane(_graph_scenario("w9_judgment", tasks=[_task(
        "TASK-001", verifications=[{"id": "V1", "kind": kind, "check": "observe result"}])]))
    assert run.returncode == 0
    assert _windows(run)
    assert _events("PreValidationRefused") == []


class _CorrectingScopeAgent(_WritingAgent):
    def complete(self, spec):
        if spec.prompt.startswith("You are the implementer ") and self.briefs("implementer"):
            extra = spec.cwd / "stray.json"
            if extra.exists():
                _git(spec.cwd, "rm", "stray.json")
        return super().complete(spec)


def test_out_of_scope_commit_returns_to_implementer_before_a_deny_question(run_lane, monkeypatch):
    agent = _CorrectingScopeAgent([{"stray.json": '{"command": "sudo whoami"}\n'}, {}])
    dan = _DanAnswers([])
    _patch_fixture_cli_driver(monkeypatch, agent)
    monkeypatch.setattr(verify, "send_via_bot", dan.send)
    monkeypatch.setattr(verify, "telegram_configured", lambda: True)
    run = run_lane(_happy_scenario("w9_scope_rework", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert dan.sent == []
    assert len(agent.briefs("implementer")) == 2
    assert "out_of_scope" in agent.briefs("implementer")[1]
    assert "stray.json" in agent.briefs("implementer")[1]


def test_reasoned_roadmap_scope_exception_allows_a_needed_file(run_lane, monkeypatch):
    agent = _WritingAgent({"needed.txt": "support\n"})
    _patch_fixture_cli_driver(monkeypatch, agent)
    run = run_lane(_happy_scenario("w9_scope_exception", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1,
        scope_exceptions=[{"path": "needed.txt", "reason": "supporting fixture for V1"}])]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 1


def test_blocking_reviewer_path_widens_the_gate_for_rework(run_lane, monkeypatch):
    agent = _WritingAgent([{}, {"needed.txt": "fixed\n"}], reviews=[
        ("rejected", ["The required support fixture is missing; add needed.txt."]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)
    run = run_lane(_happy_scenario("w9_reviewer_scope", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 2


def test_reviewer_extensionless_paths_widen_the_gate(run_lane, monkeypatch):
    paths = {"Dockerfile": "fixed\n", "scripts/deploy": "fixed\n"}

    def seed(run):
        for rel in paths:
            path = run.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old\n")
        _git_sandbox(run)

    monkeypatch.setattr(_TwoFileQueries, "FILES", list(paths))
    agent = _WritingAgent([{}, paths], reviews=[
        ("rejected", ["Fix Dockerfile and scripts/deploy."]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)
    strong = _strong_routing()
    run = run_lane(_happy_scenario("w9_extensionless_reviewer", before_run=seed,
        routing=lambda: fixture_routing(entries=list(strong.catalog.entries.values()), queries=_TwoFileQueries()),
        tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 2


def test_reviewer_can_explicitly_name_a_new_extensionless_file(run_lane, monkeypatch):
    agent = _WritingAgent([{}, {"Dockerfile": "fixture\n"}], reviews=[
        ("rejected", ["Add `Dockerfile` to satisfy the task."]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)
    run = run_lane(_happy_scenario("w9_new_extensionless", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 2


def test_invalid_reviewer_reference_does_not_break_valid_scope_rework(run_lane, monkeypatch):
    agent = _WritingAgent([{}, {"needed.txt": "fixed\n"}], reviews=[
        ("rejected", ["Add needed.txt; compare ../other/pkg.py without editing it."]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)
    run = run_lane(_happy_scenario("w9_invalid_reviewer_reference", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 2
    assert _events("ControllerError") == []


def test_unreasoned_scope_exception_is_refused_before_a_worker_starts(run_lane):
    run = run_lane(_graph_scenario("w9_unreasoned_scope", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1,
        scope_exceptions=[{"path": "needed.txt"}])]))
    assert run.returncode == 0
    assert _windows(run) == []


@pytest.mark.parametrize("scope,path,allowed", [
    (["src/"], "src/deep/a.py", True),
    (["src/*.py"], "src/deep/a.py", False),
    (["src/**/*.py"], "src/deep/a.py", True),
    (["src/**/*.py"], "src/a.py", True),
])
def test_scope_directory_and_glob_boundaries(tmp_path, scope, path, allowed):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "lane@example.invalid")
    _git(tmp_path, "config", "user.name", "lane")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("change\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "change")
    task = SimpleNamespace(scope=scope, legacy_fields={})
    assert bool(scope_gate.findings(tmp_path, scope_gate.policy(task, base))) is not allowed


def test_scope_rename_checks_both_paths_and_delete_is_a_change(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "lane@example.invalid")
    _git(tmp_path, "config", "user.name", "lane")
    (tmp_path / "old.txt").write_text("old\n")
    (tmp_path / "delete.txt").write_text("delete\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "mv", "old.txt", "new.txt")
    _git(tmp_path, "rm", "delete.txt")
    _git(tmp_path, "commit", "-q", "-m", "move and delete")
    doc = scope_gate.policy(SimpleNamespace(scope=["new.txt"], legacy_fields={}), base)
    findings = scope_gate.findings(tmp_path, doc)
    assert {line.split(";", 1)[0] for line in findings} == {
        "out_of_scope: old.txt", "out_of_scope: delete.txt"}
    doc["base"] = "missing-revision"
    assert scope_gate.findings(tmp_path, doc)[0].startswith("scope_diff_unavailable")


def test_scope_against_a_branch_name_counts_only_the_task_s_own_commits(tmp_path):
    """GDT-14 review M2b: with no recorded `code_base_sha` the base is the target branch;
    once another task merges there, a two-dot diff reported its files as this task's
    changes (a false `out_of_scope`). The change set is taken from the fork point."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "lane@example.invalid")
    _git(tmp_path, "config", "user.name", "lane")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "impl/TASK-1")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mine.py").write_text("mine\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "task")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "other.py").write_text("another task's merged work\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "another task merged")
    _git(tmp_path, "checkout", "-q", "impl/TASK-1")
    doc = scope_gate.policy(SimpleNamespace(scope=["src/"], legacy_fields={}), "main")
    assert scope_gate.findings(tmp_path, doc) == []
    (tmp_path / "stray.py").write_text("outside\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "stray")
    assert [f.split(";", 1)[0] for f in scope_gate.findings(tmp_path, doc)] == [
        "out_of_scope: stray.py"]


def test_reviewer_basename_is_resolved_only_when_unambiguous():
    task = SimpleNamespace(scope=[FEATURE], legacy_fields={})
    doc = scope_gate.policy(task, "base", reviewer_paths=["auth.py"],
                            snapshot_files=["src/auth.py", "src/other.py"])
    assert doc["reviewer_paths"] == ["src/auth.py"]
    ambiguous = scope_gate.policy(task, "base", reviewer_paths=["auth.py"],
                                  snapshot_files=["a/auth.py", "b/auth.py"])
    assert ambiguous["reviewer_paths"] == []


def test_external_reviewer_path_is_never_reinterpreted_as_relative():
    from maestro.workflows.runner import _path_tokens

    task = SimpleNamespace(scope=[FEATURE], legacy_fields={})
    tokens = _path_tokens("Compare /external/pkg.py; edit needed.txt.")
    doc = scope_gate.policy(task, "base", reviewer_paths=tokens)
    assert doc["reviewer_paths"] == ["needed.txt"]


def test_scope_diff_exception_becomes_a_blocking_finding(tmp_path, monkeypatch):
    from maestro import validation

    def unavailable(*args):
        raise OSError("git unavailable")

    monkeypatch.setattr(validation, "changed_paths", unavailable)
    doc = scope_gate.policy(SimpleNamespace(scope=[FEATURE], legacy_fields={}), "base")
    assert scope_gate.findings(tmp_path, doc)[0].startswith("scope_diff_unavailable")


def test_scope_gate_precedes_manual_post_action(run_lane, monkeypatch):
    agent = _CorrectingScopeAgent([{"stray.json": "stray\n"}, {}])
    _patch_fixture_cli_driver(monkeypatch, agent)
    original = handlers.verification_handler
    posts = []

    def verification(ctx):
        import dataclasses
        return original(dataclasses.replace(ctx, inputs={**ctx.inputs, "post_action_cmd": "fixture-post"}))

    monkeypatch.setitem(handlers._REGISTRY, "maestro.handlers.verification", verification)
    monkeypatch.setattr(handlers, "_run_post_action_cmd", lambda *args, output=None: posts.append(args) or "")
    run = run_lane(_happy_scenario("w9_scope_before_post", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 2
    assert len(posts) == 1


def test_scope_is_rechecked_after_post_action_commits(run_lane, monkeypatch):
    import dataclasses

    agent = _CorrectingScopeAgent([{}, {}])
    _patch_fixture_cli_driver(monkeypatch, agent)
    original = handlers.verification_handler
    posts = []

    def verification(ctx):
        return original(dataclasses.replace(ctx, inputs={**ctx.inputs, "post_action_cmd": "fixture-post"}))

    def post(command, tree, *, output=None):
        if not posts:
            (tree / "stray.json").write_text("stray\n")
            _git(tree, "add", "stray.json")
            _git(tree, "commit", "-q", "-m", "post action")
        posts.append(command)
        return ""

    monkeypatch.setitem(handlers._REGISTRY, "maestro.handlers.verification", verification)
    monkeypatch.setattr(handlers, "_run_post_action_cmd", post)
    run = run_lane(_happy_scenario("w9_scope_after_post", tasks=[_task(
        "TASK-001", scope=[FEATURE], verifications=_V1)]))
    assert run.returncode == 0
    assert _run_status() == "succeeded"
    assert len(agent.briefs("implementer")) == 2
