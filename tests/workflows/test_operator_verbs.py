"""P13 W8 lane 4: `ctl retry` / `ctl cancel` stop one graph node, on the runner.

What these pin is which process gets signalled. A claim's lease holds the launcher's pid
until the worker binds its own, and the launcher is the controller, so stopping the wrong
row's pid would kill the loop that was asked to stop a node. A real store, a fake tmux
and a `terminate` seam: nothing here signals a process, except the two tests under
"real signals", which stop a process of their own.

The loop's side (the command inbox, a fresh attempt that runs, the run's settlement) is
`tests/graph_engineering/test_w8_operator_verbs.py`.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from maestro.control.store import pid_alive
from tests.hang_bound import HANG_SECONDS
from maestro.workflows import runner

RUN = "run_test"
LEASE = "worktree:T-001"
WINDOW = "maestro-T-001-implementer"


@pytest.fixture
def run_ctx(store, repo, revision, dispatcher, dead_pids, artifacts):
    return runner.start_run(
        store, revision, repo=repo, run_id=RUN,
        dispatcher=dispatcher, artifacts=artifacts, is_alive=dead_pids,
    )


class _Signals:
    """The `terminate` seam. A pid dies on a signal in `obeys`, and only then."""

    def __init__(self, probe, obeys=(signal.SIGTERM, signal.SIGKILL)):
        self.probe, self.obeys, self.sent = probe, obeys, []

    def __call__(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))
        if sig in self.obeys:
            self.probe.alive.discard(pid)


def _seams(signals) -> dict:
    return {"terminate": signals, "sleep": lambda seconds: None, "grace_seconds": 0.5}


def _attempt(store, node_id: str = "implementer"):
    return store.conn.execute(
        "SELECT * FROM attempts WHERE node_id = ? ORDER BY rowid DESC LIMIT 1", (node_id,)
    ).fetchone()


def _counters(store, node_id: str = "implementer") -> dict:
    row = store.conn.execute(
        "SELECT quality_failures, retry_resets, blocked_attempts FROM node_states"
        " WHERE run_id = ? AND node_id = ?", (RUN, node_id),
    ).fetchone()
    return dict(row)


def _killed_windows(tmux) -> list:
    return [call[-1] for call in tmux.calls if call[:2] == ["tmux", "kill-window"]]


def _running_implementer(run_ctx, store, dead_pids, pid: int = 424242):
    """The implementer claimed, launched and bound to a live `pid`."""
    run_ctx.tick()
    run_ctx.inbox(_attempt(store)["attempt_id"]).bind_pid(dead_pids.keep(pid))
    run_ctx.tick()
    assert run_ctx.phases()["implementer"] == "running"
    return _attempt(store)


def _running_gate(run_ctx, store, worker, pid: int) -> None:
    """The gate (a worker that holds no lease) running under `pid`."""
    run_ctx.tick()
    attempt = _attempt(store)
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    worker(runner.Claim(
        attempt_id=attempt["attempt_id"], run_id=RUN, node_id="implementer",
        operation_id=operation_id, resource_id=None, lease_epoch=1, attempt_index=1,
        inbox=run_ctx.inbox(attempt["attempt_id"]),
    )).finish(operation_id, "succeeded")
    run_ctx.tick()
    gate = _attempt(store, "gate")["attempt_id"]
    run_ctx.inbox(gate).bind_pid(pid)
    run_ctx.tick()
    assert run_ctx.phases()["gate"] == "running"
    assert store.conn.execute(
        "SELECT COUNT(*) FROM writer_leases WHERE owner_attempt_id = ?", (gate,)
    ).fetchone()[0] == 0


# ── cancel ───────────────────────────────────────────────────────────────────────────


def test_cancel_stops_the_bound_writer_and_fails_the_node(run_ctx, store, tmux, dead_pids):
    attempt = _running_implementer(run_ctx, store, dead_pids)
    signals = _Signals(dead_pids)

    assert run_ctx.operator_cancel("implementer", **_seams(signals)) == ["implementer"]

    assert signals.sent == [(424242, signal.SIGTERM)]
    assert _killed_windows(tmux) == [f"agents:{WINDOW}"]
    assert run_ctx.phases()["implementer"] == "failed"
    settled = _attempt(store)
    assert settled["attempt_id"] == attempt["attempt_id"]
    assert (settled["status"], settled["failure_kind"], settled["failure_class"]) == (
        "failed", "operator_canceled", "blocked")
    assert store.lease(LEASE) is None
    assert store.unreconciled_intents() == []
    assert _counters(store) == {"quality_failures": 0, "retry_resets": 0, "blocked_attempts": 0}


def test_a_writer_that_ignores_sigterm_is_killed(run_ctx, store, dead_pids):
    _running_implementer(run_ctx, store, dead_pids)
    signals = _Signals(dead_pids, obeys=(signal.SIGKILL,))

    run_ctx.operator_cancel("implementer", **_seams(signals))

    assert signals.sent == [(424242, signal.SIGTERM), (424242, signal.SIGKILL)]
    assert run_ctx.phases()["implementer"] == "failed"


def test_a_writer_that_survives_sigkill_refuses_and_changes_nothing(run_ctx, store, dead_pids):
    """Failing the node while its writer lives would let a second writer into the tree."""
    attempt = _running_implementer(run_ctx, store, dead_pids)
    signals = _Signals(dead_pids, obeys=())

    with pytest.raises(runner.OperatorRefused, match="424242.*survived SIGKILL"):
        run_ctx.operator_retry("implementer", **_seams(signals))

    assert run_ctx.phases()["implementer"] == "running"
    assert _attempt(store)["status"] == "running"
    assert store.lease(LEASE).owner_attempt_id == attempt["attempt_id"]
    assert [row["status"] for row in store.unreconciled_intents()] == ["dispatched"]


def test_an_unbound_claim_is_released_and_no_pid_is_signalled(run_ctx, store, tmux, dead_pids):
    """Until the worker binds, the lease row's pid is the launcher's: this process."""
    run_ctx.tick()
    assert store.lease(LEASE).writer_pid == os.getpid()
    dead_pids.keep(os.getpid())
    signals = _Signals(dead_pids)

    run_ctx.operator_cancel("implementer", **_seams(signals))

    assert signals.sent == []
    assert _killed_windows(tmux) == [f"agents:{WINDOW}"]
    assert run_ctx.phases()["implementer"] == "failed"
    assert _attempt(store)["status"] == "failed"
    assert store.lease(LEASE) is None


def test_a_pid_the_worker_reported_but_no_tick_has_bound_yet_is_stopped(
    run_ctx, store, dead_pids
):
    run_ctx.tick()
    run_ctx.inbox(_attempt(store)["attempt_id"]).bind_pid(dead_pids.keep(515151))
    signals = _Signals(dead_pids)

    run_ctx.operator_cancel("implementer", **_seams(signals))

    assert signals.sent == [(515151, signal.SIGTERM)]
    assert store.lease(LEASE) is None


def test_a_lease_less_worker_is_stopped_by_its_bound_pid(run_ctx, store, worker, dead_pids):
    _running_gate(run_ctx, store, worker, dead_pids.keep(606060))
    signals = _Signals(dead_pids)

    run_ctx.operator_cancel("gate", **_seams(signals))

    assert signals.sent == [(606060, signal.SIGTERM)]
    assert run_ctx.phases()["gate"] == "failed"
    assert _attempt(store, "gate")["failure_kind"] == "operator_canceled"


def test_the_controllers_own_pid_is_never_signalled(run_ctx, store, worker, dead_pids):
    """A step the controller runs itself carries the controller's pid as its writer."""
    _running_gate(run_ctx, store, worker, dead_pids.keep(os.getpid()))
    signals = _Signals(dead_pids)

    run_ctx.operator_cancel("gate", **_seams(signals))

    assert signals.sent == []
    assert run_ctx.phases()["gate"] == "failed"


def test_an_uncertain_node_is_cancelled(run_ctx, store, dead_pids):
    """What a run stuck before P13 W8 looks like: nobody decided the node."""
    run_ctx.tick()
    attempt = _attempt(store)
    run_ctx.lifecycle.finish_attempt(attempt["attempt_id"], run_id=RUN, status="uncertain")
    run_ctx.lifecycle.set_node_phase(
        RUN, "implementer", "uncertain", attempt_id=attempt["attempt_id"])

    run_ctx.operator_cancel("implementer", **_seams(_Signals(dead_pids)))

    assert run_ctx.phases()["implementer"] == "failed"
    assert _attempt(store)["failure_kind"] == "operator_canceled"


def test_cancelling_the_run_stops_every_live_node_and_settles_it_canceled(
    run_ctx, store, tmux, dead_pids
):
    _running_implementer(run_ctx, store, dead_pids)
    signals = _Signals(dead_pids)

    assert run_ctx.operator_cancel(**_seams(signals)) == ["implementer"]

    assert signals.sent == [(424242, signal.SIGTERM)]
    assert run_ctx.phases()["implementer"] == "failed"
    run = store.conn.execute("SELECT status FROM task_runs WHERE run_id = ?", (RUN,)).fetchone()
    assert run["status"] == "canceled"
    assert store.lease(LEASE) is None
    assert run_ctx.settle() == "canceled"


@pytest.mark.parametrize("node_id, reason", [
    ("accept", "accept is pending"),
    ("nope", "'nope' is not a node of wf_test"),
])
def test_a_node_that_cannot_be_stopped_is_refused(run_ctx, dead_pids, node_id, reason):
    run_ctx.tick()
    for verb in (run_ctx.operator_cancel, run_ctx.operator_retry):
        with pytest.raises(runner.OperatorRefused, match=reason):
            verb(node_id, **_seams(_Signals(dead_pids)))


# ── retry ────────────────────────────────────────────────────────────────────────────


def test_retry_requeues_the_node_and_spends_no_quality_budget(run_ctx, store, tmux, dead_pids):
    attempt = _running_implementer(run_ctx, store, dead_pids)
    signals = _Signals(dead_pids)

    run_ctx.operator_retry("implementer", **_seams(signals))

    assert signals.sent == [(424242, signal.SIGTERM)]
    assert _killed_windows(tmux) == [f"agents:{WINDOW}"]
    assert run_ctx.phases()["implementer"] == "pending"
    stopped = _attempt(store)
    assert stopped["attempt_id"] == attempt["attempt_id"]
    assert (stopped["status"], stopped["failure_kind"], stopped["failure_class"]) == (
        "failed", "operator_retry", "blocked")
    assert store.lease(LEASE) is None
    assert store.unreconciled_intents() == []
    # `pre_validate` reads `retry_resets` as an agent's ladder index and subtracts
    # `blocked_attempts` for every other node, so this is what keeps the retry free.
    assert _counters(store) == {"quality_failures": 0, "retry_resets": 0, "blocked_attempts": 1}


# ── real signals ─────────────────────────────────────────────────────────────────────
#
# The verbs with no seam: `os.kill`, `time.sleep` and `store.pid_alive` against a real
# process, which the fakes above cannot say anything about.

_WRITER = """
import signal, sys, time
mode, ready, heard = sys.argv[1:]
def on_term(signum, frame):
    open(heard, "w").close()
    if mode == "obeys":
        raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
open(ready, "w").close()
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    time.sleep(0.05)
"""

_DETACH = (
    "import subprocess, sys\n"
    "print(subprocess.Popen([sys.executable, '-c', *sys.argv[1:]], stdin=subprocess.DEVNULL,"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)\n"
)


@pytest.fixture
def real_writer(tmp_path):
    """Start a writer that is not this process's child, as a tmux window's is not.

    A killed child stays a zombie until its parent reaps it, and `kill(pid, 0)` still
    finds a zombie; an orphan is reaped at once. `start(mode)` returns its pid and the
    file it touches when SIGTERM reaches it. `mode="ignores"` survives SIGTERM.
    """
    started: list[int] = []

    def start(mode: str):
        ready, heard = tmp_path / f"{mode}.ready", tmp_path / f"{mode}.heard"
        launched = subprocess.run(
            [sys.executable, "-c", _DETACH, _WRITER, mode, str(ready), str(heard)],
            capture_output=True, text=True, check=True,
        )
        started.append(int(launched.stdout))
        deadline = time.monotonic() + HANG_SECONDS
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the writer never installed its SIGTERM handler"
        return started[-1], heard

    yield start
    for pid in started:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.fixture
def real_ctx(store, repo, revision, dispatcher, artifacts):
    """A run on the real liveness probe."""
    return runner.start_run(
        store, revision, repo=repo, run_id=RUN, dispatcher=dispatcher, artifacts=artifacts,
    )


def _bound_to(real_ctx, store, pid: int) -> None:
    real_ctx.tick()
    real_ctx.inbox(_attempt(store)["attempt_id"]).bind_pid(pid)
    real_ctx.tick()
    assert real_ctx.phases()["implementer"] == "running"


def test_a_real_writer_dies_of_the_sigterm_it_is_sent(real_ctx, store, real_writer):
    pid, heard = real_writer("obeys")
    _bound_to(real_ctx, store, pid)
    began = time.monotonic()

    real_ctx.operator_cancel("implementer", grace_seconds=20.0)

    assert heard.exists() and not pid_alive(pid)
    assert time.monotonic() - began < 15, "the grace ran out: SIGTERM did not stop the writer"
    assert real_ctx.phases()["implementer"] == "failed"
    assert store.lease(LEASE) is None


def test_a_real_writer_that_ignores_sigterm_is_killed_after_the_grace(
    real_ctx, store, real_writer
):
    pid, heard = real_writer("ignores")
    _bound_to(real_ctx, store, pid)
    began = time.monotonic()

    real_ctx.operator_retry("implementer", grace_seconds=0.5)

    assert time.monotonic() - began >= 0.5
    assert heard.exists(), "SIGTERM never reached the writer"
    assert not pid_alive(pid)
    assert real_ctx.phases()["implementer"] == "pending"
    assert store.lease(LEASE) is None
