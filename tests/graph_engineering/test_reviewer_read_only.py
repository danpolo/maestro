"""Field-notes F1: a non-writer agent may write its verdict, never the tree it reviews.

The reviewer, planner and diagnoser are declared `frozen_snapshot_reader`
(`compiler._proof_review_node`), but `worker._run_agent_cli` used to hand every agent
`writable=True` with the reviewed worktree as `cwd`, and the model-endpoint `ToolBox`
let every role `write_file` into the worktree. These tests pin the boundary at the
dispatch level, where it is actually enforced: the `CompletionSpec` the driver receives,
the argv each CLI driver builds from it, and the model-endpoint tool box.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import permissions
from maestro.backends import codex as codex_driver
from maestro.backends.base import Completion as CompletionResult
from maestro.backends.model_runtime import ConfinementBreach, ToolBox, ToolCall
from maestro.confinement import CommitBoundary
from maestro.workflows import handlers, worker


class _RecordingDriver:
    permission_lists = False

    def __init__(self):
        self.specs = []

    def complete(self, spec):
        self.specs.append(spec)
        return CompletionResult(text="DONE", returncode=0)


def _git_tree(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "seed"],
                   check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                    "PATH": "/usr/bin:/bin"})
    return path


def _dispatch(monkeypatch, tmp_path, permissions_ref: str):
    driver = _RecordingDriver()
    monkeypatch.setattr("maestro.backends.registry.get_backend", lambda name: driver)
    monkeypatch.setattr("maestro.backends.registry.driver_name_for_backend_id",
                        lambda backend_id: "claude")
    worktree = _git_tree(tmp_path / "tree")
    if permissions_ref == "task_worktree_writer":
        task_tree = tmp_path / "task-tree"
        subprocess.run(["git", "-C", str(worktree), "worktree", "add", "-qb",
                        "impl/T1", str(task_tree)], check=True)
        worktree = task_tree
    workspace = tmp_path / "ws"
    workspace.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    node = SimpleNamespace(
        permissions_ref=permissions_ref,
        timeout_policy=SimpleNamespace(execution_timeout_sec=60),
    )
    context = SimpleNamespace(
        node=node, worktree=worktree, workspace=workspace,
        inbox=SimpleNamespace(path=inbox),
    )
    args = SimpleNamespace(backend_id="claude-cli", effort_config=(), effort_arg=(),
                           model="m")
    worker._run_agent_cli(args, context, "brief")
    return driver.specs[0], worktree, workspace


def _within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def test_a_reviewer_runs_in_its_workspace_and_is_granted_no_part_of_the_worktree(
    monkeypatch, tmp_path
):
    spec, worktree, workspace = _dispatch(monkeypatch, tmp_path, "frozen_snapshot_reader")

    assert Path(spec.cwd) == workspace
    assert spec.writable is True  # the verdict file still has to be written
    assert not any(_within(d, worktree) for d in spec.add_dirs)
    assert not any(_within(d, worktree / ".git") for d in spec.add_dirs)


def test_a_reviewers_claude_edit_rules_name_only_the_workspace(monkeypatch, tmp_path):
    spec, worktree, workspace = _dispatch(monkeypatch, tmp_path, "frozen_snapshot_reader")

    args = permissions.cli_args(cwd=spec.cwd, add_dirs=tuple(spec.add_dirs),
                                writable=spec.writable)
    allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
    edits = [rule for rule in allowed if rule.startswith(("Edit(", "Write("))]
    assert edits == [f"Edit(/{workspace.resolve()}/**)"]
    assert "Read" in allowed  # the worktree is still readable, from anywhere


def test_a_reviewers_codex_sandbox_writes_only_the_workspace(monkeypatch, tmp_path):
    spec, worktree, workspace = _dispatch(monkeypatch, tmp_path, "frozen_snapshot_reader")

    argv = codex_driver.completion_argv("codex", spec, last_message_file=tmp_path / "out")
    assert argv[argv.index("-C") + 1] == str(workspace)
    granted = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--add-dir"]
    assert not any(_within(Path(d), worktree) for d in granted)


def test_a_writer_still_works_and_commits_in_its_worktree(monkeypatch, tmp_path):
    spec, worktree, workspace = _dispatch(monkeypatch, tmp_path, "task_worktree_writer")

    assert Path(spec.cwd) == worktree
    assert spec.writable is True
    assert workspace in spec.add_dirs
    own_git = Path(subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"], text=True).strip())
    common_git = Path(subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True).strip())
    assert own_git != common_git
    assert set(spec.add_dirs) == {workspace, own_git}
    # Native drivers stage objects and exact task refs privately. The dispatch
    # grant must not expose the common object store or sibling refs/reflogs.
    for shared in (common_git / 'objects', common_git / 'refs', common_git / 'logs'):
        assert not any(_within(shared, d) for d in spec.add_dirs)


# ── the model-endpoint path ──────────────────────────────────────────────────────────


def _boundary(tmp_path):
    repo = _git_tree(tmp_path / "repo")
    return CommitBoundary.for_task(repo, "t1")


def test_a_read_only_tool_box_writes_its_verdict_in_the_workspace(tmp_path):
    boundary = _boundary(tmp_path)
    workspace = tmp_path / "ws"
    box = ToolBox(boundary=boundary, writes_to=workspace)

    result = box.dispatch(ToolCall(name="write_file", call_id="1",
                                   arguments={"path": "review.json", "content": "{}"}))
    assert result.ok
    assert (workspace / "review.json").read_text() == "{}"
    assert result.mutated is False  # the reviewed tree did not change


def test_a_read_only_tool_box_refuses_to_write_the_worktree(tmp_path):
    boundary = _boundary(tmp_path)
    box = ToolBox(boundary=boundary, writes_to=tmp_path / "ws")
    target = boundary.worktree / "src.py"

    with pytest.raises(ConfinementBreach):
        box.dispatch(ToolCall(name="write_file", call_id="1",
                              arguments={"path": str(target), "content": "x"}))
    assert not target.exists()


@pytest.mark.parametrize("path", [
    handlers.RESULT_FILE,
    handlers.RECEIPT_FILE,
    handlers.ENVELOPE_FILE,
    handlers.HEARTBEAT_FILE,
    f"./{handlers.RESULT_FILE}",
    f"{handlers.RESULT_FILE}.tmp",          # the name the worker's atomic write replaces from
    f"{handlers.RECEIPT_FILE}/x",           # a directory there would block the worker's own write
])
def test_a_read_only_tool_box_refuses_the_inbox_protocol_files(tmp_path, path):
    """P13 W8 review F5: the workspace is the attempt inbox, so a reader that could write
    `result.json` and `receipt.json` could report its own node as succeeded and approved."""
    workspace = tmp_path / "ws"
    box = ToolBox(boundary=_boundary(tmp_path), writes_to=workspace,
                  reserved=handlers.INBOX_PROTOCOL_FILES)

    with pytest.raises(ConfinementBreach, match="reserved"):
        box.dispatch(ToolCall(name="write_file", call_id="1",
                              arguments={"path": path, "content": "{}"}))
    assert not (workspace / handlers.RESULT_FILE).exists()
    assert not (workspace / handlers.RECEIPT_FILE).exists()

    verdict = box.dispatch(ToolCall(name="write_file", call_id="2",
                                    arguments={"path": handlers.REVIEW_FILE, "content": "{}"}))
    assert verdict.ok and (workspace / handlers.REVIEW_FILE).read_text() == "{}"
    nested = box.dispatch(ToolCall(name="write_file", call_id="3", arguments={
        "path": f"notes/{handlers.RESULT_FILE}", "content": "{}"}))
    assert nested.ok, "only the inbox's own entries are reserved"


def test_a_read_only_tool_box_still_reads_the_worktree(tmp_path):
    boundary = _boundary(tmp_path)
    boundary.worktree.mkdir(parents=True, exist_ok=True)
    (boundary.worktree / "a.txt").write_text("hello")
    box = ToolBox(boundary=boundary, writes_to=tmp_path / "ws")

    result = box.dispatch(ToolCall(name="read_file", call_id="1", arguments={"path": "a.txt"}))
    assert result.ok and result.content == "hello"


# ── detection: a read-only node that changed the tree anyway ─────────────────────────


def test_a_non_writer_that_changed_the_tree_fails_as_bad_output(tmp_path):
    driver = SimpleNamespace(parse_exit=lambda code, tail: SimpleNamespace(kind="ok"))
    completion = CompletionResult(text="DONE", returncode=0)

    record = worker._classify_cli(driver, completion, tmp_path / "none.log",
                                  ("abc", ""), ("abc", " M src.py"), writer=False)
    assert record is not None and record.kind == "garbage_output"
    assert "read-only" in record.evidence

    assert worker._classify_cli(driver, completion, tmp_path / "none.log",
                                ("abc", ""), ("abc", " M src.py"), writer=True) is None
