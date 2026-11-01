"""Fixtures for the `[P3]` workflow suite.

Two rules, both inherited from `tests/control/conftest.py` and both load-bearing: every
test works on a temporary repository root, and no test launches a real process or talks to
a real tmux server. The second is not squeamishness — a suite that forks agents leaks them
into the developer's session when an assertion fails, and a suite that depends on a tmux
daemon's scheduling cannot make deterministic claims about concurrency.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from maestro.control.artifacts import ArtifactStore
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import ControlStore
from maestro.taskgraph import normalize_spec
from maestro.workflows.compiler import compile_workflow
from maestro.workflows.handlers import AttemptInbox, TmuxDispatcher, attempts_root
from maestro.workflows.models import NodeResult


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / ".orchestrator").mkdir(parents=True)
    return root


@pytest.fixture
def store(repo):
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    yield store
    store.close()


@pytest.fixture
def lifecycle(store):
    return Lifecycle(store)


@pytest.fixture
def artifacts(repo, store):
    return ArtifactStore(repo / ".orchestrator" / "artifacts", store)


@dataclass
class FakeTmux:
    """Records `tmux new-window` invocations instead of making them.

    `fail_windows` lets a test make one specific launch fail without making the dispatcher
    a different class — a launch failure is an ordinary outcome, not an exceptional one.
    """

    launched: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    fail_windows: set = field(default_factory=set)
    existing: set = field(default_factory=set)

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if cmd[:2] == ["tmux", "new-window"]:
            window = cmd[cmd.index("-n") + 1]
            if window in self.fail_windows:
                return subprocess.CompletedProcess(cmd, 1, "", "no server running")
            self.launched.append((window, tuple(cmd)))
            self.existing.add(window)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["tmux", "list-windows"]:
            return subprocess.CompletedProcess(cmd, 0, "\n".join(sorted(self.existing)), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def windows(self) -> list[str]:
        return [name for name, _ in self.launched]


@pytest.fixture
def tmux() -> FakeTmux:
    return FakeTmux()


@pytest.fixture
def dispatcher(tmux) -> TmuxDispatcher:
    return TmuxDispatcher(runner=tmux)


def task_spec(task_id: str = "T-001", **fields):
    """A roadmap block in the document's own vocabulary, normalised the way P2 does."""
    block = {"id": task_id, "title": f"{task_id} title", "status": "pending", "deps": []}
    block.update(fields)
    return normalize_spec(block)


@pytest.fixture
def spec():
    return task_spec()


@pytest.fixture
def revision(spec):
    return compile_workflow(spec, workflow_id="wf_test", code_base_sha="deadbeef")


@dataclass
class Worker:
    """Speaks for a worker that the tests never actually launch.

    The three methods are the three moments the protocol cares about, kept separate so a
    test can stop between any two of them — which is the only way to assert what a crash
    at that instant does.
    """

    repo: Path
    attempt_id: str
    run_id: str
    node_id: str

    @property
    def inbox(self) -> AttemptInbox:
        return AttemptInbox(attempts_root(self.repo), self.attempt_id)

    def bind(self, pid: int) -> int:
        return self.inbox.bind_pid(pid)

    def report(self, status: str, **kwargs) -> NodeResult:
        result = NodeResult(
            run_id=self.run_id, node_id=self.node_id, attempt_id=self.attempt_id,
            status=status, **kwargs,
        )
        self.inbox.write_result(result)
        return result

    def receipt(self, operation_id: str, exit_status: int = 0) -> None:
        self.inbox.write_receipt(operation_id=operation_id, exit_status=exit_status)

    def finish(self, operation_id: str, status: str = "succeeded", **kwargs) -> NodeResult:
        result = self.report(status, **kwargs)
        self.receipt(operation_id, exit_status=result.exit_status)
        return result


@pytest.fixture
def worker(repo):
    def make(claim) -> Worker:
        return Worker(
            repo=repo, attempt_id=claim.attempt_id, run_id=claim.run_id,
            node_id=claim.node_id,
        )

    return make


@pytest.fixture
def dead_pids():
    """A liveness probe a test controls. Default: everything is dead."""

    class Probe:
        def __init__(self):
            self.alive: set[int] = set()

        def __call__(self, pid: int) -> bool:
            return int(pid) in self.alive

        def keep(self, pid: int) -> int:
            self.alive.add(int(pid))
            return pid

    return Probe()
