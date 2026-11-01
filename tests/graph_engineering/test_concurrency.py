from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from maestro import graph_experiments
from maestro.graph_experiments import run_process_batch, sqlite_contention_probe


def test_bounded_batch_runs_real_processes_and_records_their_rss(tmp_path):
    command = [sys.executable, "-c", "import time; payload=bytearray(1024*1024); time.sleep(0.12)"]

    serial = run_process_batch(command, task_count=4, concurrency=1, cwd=tmp_path)
    parallel = run_process_batch(command, task_count=4, concurrency=2, cwd=tmp_path)

    assert serial["task_count"] == parallel["task_count"] == 4
    assert serial["exit_codes"] == parallel["exit_codes"] == [0, 0, 0, 0]
    assert serial["peak_rss_kib"] > 0
    assert parallel["peak_rss_kib"] > 0


class _ScriptedBatch:
    """A clock that advances only when the batch sleeps, and processes that each run
    for `duration` of it, so queueing, not host load, decides every measurement."""

    def __init__(self, duration):
        self.duration = duration
        self.now = 1000.0
        self.pids = iter(range(1, 1000))

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def popen(self, command, **kwargs):
        clock, ends = self, self.now + self.duration
        return SimpleNamespace(pid=next(self.pids),
                               poll=lambda: 0 if clock.now >= ends else None)


def test_bounded_batch_records_queue_delay_elapsed_time_and_peak_rss(tmp_path, monkeypatch):
    duration = 0.12
    batch = _ScriptedBatch(duration)
    monkeypatch.setattr(graph_experiments, "time", batch)
    monkeypatch.setattr(graph_experiments, "subprocess",
                        SimpleNamespace(Popen=batch.popen, DEVNULL=subprocess.DEVNULL))
    monkeypatch.setattr(graph_experiments, "_rss_kib", lambda pid: 1024)
    command = ["scripted"]

    serial = run_process_batch(command, task_count=4, concurrency=1, cwd=tmp_path)
    parallel = run_process_batch(command, task_count=4, concurrency=2, cwd=tmp_path)

    assert serial["task_count"] == parallel["task_count"] == 4
    assert serial["exit_codes"] == parallel["exit_codes"] == [0, 0, 0, 0]
    assert serial["elapsed_ms"] > parallel["elapsed_ms"]
    assert serial["max_queue_delay_ms"] > parallel["max_queue_delay_ms"]
    # The last serial task waits for three predecessors; a parallel one for one round.
    # One poll interval (5 ms) of slack per completed round.
    assert serial["max_queue_delay_ms"] == pytest.approx(3 * duration * 1000, abs=3 * 5.1)
    assert parallel["max_queue_delay_ms"] == pytest.approx(duration * 1000, abs=5.1)
    assert serial["elapsed_ms"] == pytest.approx(4 * duration * 1000, abs=4 * 5.1)
    assert parallel["elapsed_ms"] == pytest.approx(2 * duration * 1000, abs=2 * 5.1)
    # RSS is summed over the children alive together.
    assert serial["peak_rss_kib"] == 1024
    assert parallel["peak_rss_kib"] == 2048


def test_sqlite_concurrent_claims_never_assign_one_job_twice(tmp_path):
    result = sqlite_contention_probe(tmp_path / "sqlite", job_count=12, worker_count=4)

    assert result["claimed_jobs"] == 12
    assert result["duplicate_claims"] == 0
    assert result["writer_attempts"] >= 12
    assert result["max_writer_wait_ms"] >= 0
