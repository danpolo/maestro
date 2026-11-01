"""P10B: host resource admission in the live launch loop.

`P10B_HOST_RESOURCE_ADMISSION.md` §3 names six acceptance conditions; each has a test here
(or, for the two ledger-only ones, in `tests/control/test_resources.py`). The loop is
driven through the same one-poll harness as the P08 wiring tests, with the host's capacity
pinned so no test depends on the machine it runs on.
"""
from __future__ import annotations

import os

import pytest

from maestro import orchestrator, scheduling, taskgraph
from maestro.control.resources import COUNTED_POOL, HostReservationLedger
from tests.test_orchestrator_scheduling import (  # noqa: F401 — `loop_env` is a fixture
    _agent_task,
    _entry,
    _launched_ids,
    _script_task,
    loop_env,
)

RAM = 8000.0


@pytest.fixture
def host(loop_env, monkeypatch):
    """8 cores, 8 GB, no GPU; a fresh eligibility clock; a mutable poll clock."""
    capacity = scheduling.HostCapacity(limits={"cpu": 8.0, "ram_mb": RAM, "vram_mb": 0.0})
    monkeypatch.setattr(orchestrator, "_host_capacity", lambda: capacity)
    monkeypatch.setattr(orchestrator, "_ELIGIBILITY", scheduling.EligibilityClock())
    clock = {"now": 1000.0}
    sleep = orchestrator.time.sleep
    monkeypatch.setattr(
        orchestrator, "time",
        type("T", (), {"time": staticmethod(lambda: clock["now"]),
                       "monotonic": staticmethod(lambda: clock["now"]),
                       "sleep": staticmethod(sleep)}),
    )
    loop_env.cap = (10, 10.0)
    loop_env.clock = clock
    return loop_env


def _poll(box) -> None:
    """One more poll, carrying whatever is in flight from the last one."""
    box.in_flight = list(box.state.get("in_flight", []))
    box.polls = 0
    box.state.pop("halted", None)
    assert orchestrator.main() == 0


def _heavy(task_id: str, ram_mb: float = RAM / 2 + 1000, **extra) -> dict:
    return _agent_task(task_id, resources={"ram_mb": ram_mb}, **extra)


# ── ROADMAP resource requests ─────────────────────────────────────────────────────────


def _validate(block: str) -> list:
    return taskgraph.validate(taskgraph.parse_document(f"```yaml\n{block}\n```\n"))


def test_a_resources_block_parses_into_the_task_spec_and_round_trips():
    data = {"id": "T1", "resources": {"cpu": 2, "ram_mb": 4096, "vram_mb": 0}}
    spec = taskgraph.normalize_spec(data)
    assert spec.resources == {"cpu": 2, "ram_mb": 4096}
    assert "resources" not in spec.legacy_fields
    assert spec.to_yaml_dict()["resources"] == {"cpu": 2, "ram_mb": 4096}
    assert taskgraph.normalize_spec(spec.to_yaml_dict()) == spec


def test_a_task_without_resources_requests_nothing():
    assert taskgraph.normalize_spec({"id": "T1"}).resources == {}
    assert scheduling.ResourceRequest.from_task({"id": "T1"}).counted == {}


@pytest.mark.parametrize("block, fragment", [
    ("resources: {ram: 4000}", "unknown resources key"),
    ("resources: {ram_mb: -1}", "non-negative"),
    ("resources: {cpu: true}", "must be a number"),
    ("resources: {cpu: two}", "must be a number"),
    ("resources: 4000", "must be a mapping"),
])
def test_a_malformed_resources_block_is_a_validation_issue(block, fragment):
    (issue,) = _validate(f"id: T1\n{block}")
    assert issue.code == "invalid_resources"
    assert fragment in issue.message and issue.task_id == "T1"


def test_the_label_mutex_is_not_folded_into_resources():
    spec = taskgraph.normalize_spec({"id": "T1", "resource": "eval-cpu", "resources": {"cpu": 1}})
    assert spec.legacy_fields["resource"] == "eval-cpu"
    assert orchestrator._resource_set({"resource": "eval-cpu"}) == {"eval-cpu"}


def test_a_task_request_never_names_an_agent_slot_or_pool():
    request = scheduling.ResourceRequest.from_task(
        {"id": "S1", "kind": "script", "resources": {"cpu": 4, "ram_mb": 100}}
    )
    assert request.is_deterministic
    assert request.counted == {"cpu": 4.0, "ram_mb": 100.0}


# ── acceptance: RAM for one of two heavy tasks ───────────────────────────────────────


def test_with_ram_for_one_of_two_heavy_tasks_one_launches_and_the_other_waits(host):
    host.runnable = [_heavy("H1"), _heavy("H2")]

    assert orchestrator.main() == 0

    assert host.launched == ["H1"]
    why = host.state["admission_waiting"]["H2"]
    assert why["reason"] == "resource_exhausted" and "ram_mb" in why["detail"]
    assert any(e == "launch_waiting_admission" and "H2" in d for e, d in host.journal)
    (row,) = HostReservationLedger().reservations(COUNTED_POOL)
    assert row.amounts == {"ram_mb": RAM / 2 + 1000}

    # H1 is still running: H2 keeps waiting.
    host.runnable = [_heavy("H2")]
    _poll(host)
    assert host.launched == ["H1"]

    # H1 leaves in_flight: its reservation is released and H2 launches.
    host.state["in_flight"] = []
    _poll(host)
    assert host.launched == ["H1", "H2"]
    assert host.state["admission_waiting"] == {}
    assert [r.run_id for r in HostReservationLedger().reservations(COUNTED_POOL)] == [
        e["session_id"] for e in host.state["in_flight"] if e["task_id"] == "H2"
    ]


def test_a_task_that_can_never_fit_parks_with_the_capacity_in_its_reason(host, monkeypatch):
    """P12A W9: larger than the whole host is not a wait — it would wait forever."""
    parked: list = []
    monkeypatch.setattr(orchestrator, "park_failed", lambda tid, reason: parked.append((tid, reason)))
    host.runnable = [_heavy("HUGE", ram_mb=RAM * 2), _heavy("FITS", ram_mb=1000)]

    assert orchestrator.main() == 0

    assert host.launched == ["FITS"]
    [(task_id, reason)] = parked
    assert task_id == "HUGE"
    assert reason == f"exceeds_host_capacity: ram_mb requested {RAM * 2:g}, host capacity {RAM:g}"
    assert "HUGE" not in host.state.get("admission_waiting", {})
    assert any(e == "launch_parked_oversized" and "HUGE" in d for e, d in host.journal)
    assert [r.amounts for r in HostReservationLedger().reservations(COUNTED_POOL)] == [
        {"ram_mb": 1000.0}
    ], "the oversized task reserved nothing"


# ── P12A W10: an async job's hold is the snapshot it was admitted with ──────────────


def _async_heavy(task_id: str, **resources) -> dict:
    return {
        "id": task_id, "title": f"async {task_id}", "dispatch": "async-job",
        "launch_cmd": "true", "resources": dict(resources),
        "verifications": [{"id": "eval", "kind": "auto", "await": True, "cmd": "true"}],
    }


@pytest.mark.parametrize("edited", [
    {"ram_mb": 500},   # the block edited mid-run
    None,              # the block removed mid-run
], ids=["edited", "removed"])
def test_editing_an_async_jobs_resources_block_mid_run_keeps_its_recorded_hold(
    host, monkeypatch, edited
):
    task = _async_heavy("EVAL", ram_mb=3000, cpu=2)
    host.runnable = [task]
    assert orchestrator.main() == 0

    [pending] = [p for p in host.state["waiting_on_dan"].values()
                 if p["kind"] == "awaiting-verification"]
    assert pending["resources"] == {"cpu": 2.0, "ram_mb": 3000.0}
    held = {"async-EVAL": {"cpu": 2.0, "ram_mb": 3000.0}}
    assert orchestrator._live_counted([], host.state) == held

    changed = {k: v for k, v in task.items() if k != "resources"}
    if edited is not None:
        changed["resources"] = edited
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: dict(changed))
    host.runnable = []
    _poll(host)

    assert orchestrator._live_counted([], host.state) == held
    [row] = HostReservationLedger().reservations(COUNTED_POOL)
    assert (row.run_id, row.amounts) == ("async-EVAL", {"cpu": 2.0, "ram_mb": 3000.0})


def test_an_async_entry_written_before_the_snapshot_still_reads_the_roadmap(host, monkeypatch):
    """No `resources` key at all: the entry predates W10, so the ROADMAP is the only source."""
    monkeypatch.setattr(
        orchestrator, "get_task_by_id", lambda tid: _async_heavy(tid, ram_mb=700)
    )
    state = {"waiting_on_dan": {"1": {"task_id": "OLD", "kind": "awaiting-verification"}}}
    assert orchestrator._live_counted([], state) == {"async-OLD": {"ram_mb": 700.0}}
    # An empty recorded snapshot is a snapshot: nothing is held, whatever the ROADMAP says.
    state["waiting_on_dan"]["1"]["resources"] = {}
    assert orchestrator._live_counted([], state) == {}


def test_a_malformed_request_fails_closed_and_says_why(host):
    host.runnable = [_agent_task("BAD", resources={"ram": 1}), _agent_task("OK")]
    assert orchestrator.main() == 0
    assert host.launched == ["OK"]
    assert host.state["admission_waiting"]["BAD"]["reason"] == "invalid_resources"


def test_a_resource_holder_that_fails_to_launch_gives_everything_back(host, monkeypatch):
    """The worktree will not create: H1's RAM goes back, so H2 launches in the same poll."""
    import subprocess

    def _create(task_id, worktree, branch):
        if task_id == "H1":
            raise subprocess.CalledProcessError(1, "git worktree add")

    monkeypatch.setattr(orchestrator, "create_worktree", _create)
    monkeypatch.setattr(orchestrator.subprocess, "CalledProcessError",
                        subprocess.CalledProcessError, raising=False)
    monkeypatch.setattr(orchestrator, "park_failed", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "remove_worktree", lambda *a, **k: None)
    host.runnable = [_heavy("H1"), _heavy("H2")]

    assert orchestrator.main() == 0

    assert host.launched == ["H2"]
    assert [r.amounts for r in HostReservationLedger().reservations(COUNTED_POOL)] == [
        {"ram_mb": RAM / 2 + 1000}
    ]


# ── acceptance: agent slots stay separate ────────────────────────────────────────────


def test_a_ready_script_task_launches_in_the_same_poll_while_every_agent_slot_is_taken(
    host, tmp_path
):
    host.cap = (1, 10.0)
    host.in_flight = [_entry(tmp_path)]
    script = {**_script_task("S1"), "resources": {"cpu": 2, "ram_mb": 1000}}
    host.runnable = [_heavy("A1"), script]

    assert orchestrator.main() == 0

    assert host.launched == []
    assert _launched_ids(host) == {"T1", "S1"}
    (entry,) = [e for e in host.state["in_flight"] if e["task_id"] == "S1"]
    assert entry["resources"] == {"cpu": 2.0, "ram_mb": 1000.0}
    # The agent task was stopped by its slot, not by admission.
    assert "A1" not in host.state.get("admission_waiting", {})


# ── acceptance: other projects' reservations are subtracted ──────────────────────────


def test_another_projects_counted_reservation_is_subtracted_before_admission(host):
    HostReservationLedger().reserve_counted(
        {"ram_mb": 6000}, capacity={"ram_mb": RAM}, project_id="/other/project",
        run_id="impl-X-1", writer_pid=os.getpid(),
    )
    host.runnable = [_heavy("H1", ram_mb=4000), _heavy("SMALL", ram_mb=1000)]

    assert orchestrator.main() == 0

    assert host.launched == ["SMALL"]
    assert "2000 of 8000 free" in host.state["admission_waiting"]["H1"]["detail"]
    assert {r.project_id for r in HostReservationLedger().reservations(COUNTED_POOL)} == {
        "/other/project", str(orchestrator.REPO)
    }


def test_a_ledger_refusal_at_reserve_time_withdraws_the_admission(host, monkeypatch):
    """The in-process check said yes, the ledger (another project got there first) says no."""
    monkeypatch.setattr(
        HostReservationLedger, "held_amounts", lambda self, exclude_project=None: {}
    )
    HostReservationLedger().reserve_counted(
        {"ram_mb": 6000}, capacity={"ram_mb": RAM}, project_id="/other/project",
        run_id="impl-X-1", writer_pid=os.getpid(),
    )
    host.runnable = [_heavy("H1", ram_mb=4000), _heavy("SMALL", ram_mb=1000)]

    assert orchestrator.main() == 0

    assert host.launched == ["SMALL"]
    assert "held by: /other/project" in host.state["admission_waiting"]["H1"]["detail"]


# ── acceptance: starvation ────────────────────────────────────────────────────────────


def test_an_aged_heavy_task_is_eventually_admitted_while_small_tasks_keep_arriving(host):
    """Small tasks listed ahead of it keep filling RAM; after `starvation_after_sec` the
    RAM they need is held back and the heavy task drains in."""
    policy = scheduling.SchedulingPolicy()
    heavy = _heavy("HEAVY", ram_mb=RAM)
    reasons: list[str] = []
    n = 0
    while "HEAVY" not in host.launched:
        n += 1
        assert n < 20, f"HEAVY starved; reasons seen: {reasons}"
        # Only the small task launched on the previous poll is still running.
        host.state["in_flight"] = [
            e for e in host.state.get("in_flight", []) if e["task_id"] == f"S{n - 1}"
        ]
        host.runnable = [_heavy(f"S{n}", ram_mb=RAM / 2), heavy]
        _poll(host)
        reasons.append(host.state.get("admission_waiting", {}).get(f"S{n}", {}).get("reason"))
        host.clock["now"] += policy.aging_interval_sec + 100

    assert "reserved_for_starved" in reasons
    assert n > 2, "the heavy task should have waited behind small ones first"


# ── acceptance: no resources blocks, no change ───────────────────────────────────────


def test_a_roadmap_with_no_resources_blocks_launches_in_roadmap_order(host, monkeypatch):
    def _no_detection():
        raise AssertionError("capacity must not be detected without a resources block")

    monkeypatch.setattr(orchestrator, "_host_capacity", _no_detection)
    host.runnable = [_agent_task("B"), _agent_task("A"), _agent_task("C")]

    assert orchestrator.main() == 0

    assert host.launched == ["B", "A", "C"]
    assert "admission_waiting" not in host.state
    assert HostReservationLedger().reservations(COUNTED_POOL) == []


def test_status_and_explain_can_say_why_a_task_is_waiting(host, monkeypatch, capsys, tmp_path):
    from maestro import cli, status

    host.runnable = [_heavy("H1"), _heavy("H2")]
    assert orchestrator.main() == 0
    monkeypatch.setattr(status, "_load_json", lambda path: dict(host.state))

    assert status.admission_wait("H2")["reason"] == "resource_exhausted"
    assert status.admission_wait("H1") is None
    monkeypatch.setattr(
        status, "workflow_view",
        lambda *a, **k: (_ for _ in ()).throw(status.WorkflowViewError("no run for H2")),
    )
    assert cli.cmd_explain(tmp_path, "H2") == 0
    assert "waiting on host admission — resource_exhausted" in capsys.readouterr().out
