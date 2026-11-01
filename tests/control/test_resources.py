"""The host reservation ledger (`[P8]`, `05_SCHEDULING_AND_RESOURCES.md` §3).

Every ledger here lives under `tmp_path`. The concurrency tests use real processes and a
real `flock`, because a lock that is only ever contended by threads of one interpreter is
not the lock two maestro controllers will contend.
"""
from __future__ import annotations

import json
import multiprocessing
import os

import pytest

from maestro.backends.catalog import QuotaObservation, QuotaWindow, ResourcePoolState
from maestro.control import resources
from maestro.control.resources import HostReservationLedger, HostResourcePoolState
from tests.hang_bound import HANG_SECONDS


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "home" / "reservations.json"


def _ledger(path, *, alive=lambda pid: True, boot_id="boot-a", clock=None):
    return HostReservationLedger(path, is_alive=alive, boot_id=boot_id, clock=clock or Clock())


def _reserve(ledger, run_id, *, project="proj-a", pool="anthropic-max", pid=4242, limit=2):
    return ledger.reserve(pool, project_id=project, run_id=run_id, writer_pid=pid, limit=limit)


# ── the reservation protocol ─────────────────────────────────────────────────────────


def test_a_reservation_records_exactly_the_coordination_fields(ledger_path):
    ledger = _ledger(ledger_path)
    result = _reserve(ledger, "run-1")
    assert result.admission.admitted
    stored = json.loads(ledger_path.read_text())["reservations"]
    assert len(stored) == 1
    assert set(stored[0]) == {
        "reservation_id", "project_id", "run_id", "pool_id", "writer_pid",
        "boot_id", "reserved_at", "heartbeat_at", "lease_expiry",
    }
    assert stored[0]["project_id"] == "proj-a"
    assert stored[0]["pool_id"] == "anthropic-max"


def test_a_full_pool_refuses_with_a_reason_naming_the_holders(ledger_path):
    ledger = _ledger(ledger_path)
    assert _reserve(ledger, "run-1", limit=1).admission.admitted
    refused = _reserve(ledger, "run-2", project="proj-b", limit=1)
    assert not refused.admission.admitted
    assert refused.reservation is None
    assert refused.admission.reason == "pool_saturated"
    assert "proj-a" in refused.admission.detail


def test_re_reserving_the_same_run_refreshes_instead_of_taking_a_second_slot(ledger_path):
    clock = Clock()
    ledger = _ledger(ledger_path, clock=clock)
    first = _reserve(ledger, "run-1", limit=1).reservation
    clock.now += 60
    again = _reserve(ledger, "run-1", limit=1)
    assert again.admission.admitted
    assert again.reservation.reservation_id == first.reservation_id
    assert again.reservation.lease_expiry == first.lease_expiry + 60
    assert ledger.active("anthropic-max") == 1


def test_an_unlimited_reservation_still_records_pressure(ledger_path):
    ledger = _ledger(ledger_path)
    for n in range(5):
        assert _reserve(ledger, f"run-{n}", limit=None).admission.admitted
    assert ledger.active("anthropic-max") == 5


def test_heartbeat_extends_the_lease_and_release_frees_the_slot(ledger_path):
    clock = Clock()
    ledger = _ledger(ledger_path, clock=clock)
    row = _reserve(ledger, "run-1", limit=1).reservation
    clock.now += 100
    assert ledger.heartbeat(row.reservation_id, ttl_sec=300)
    assert ledger.reservations()[0].lease_expiry == clock.now + 300
    assert ledger.release(row.reservation_id)
    assert _reserve(ledger, "run-2", limit=1).admission.admitted
    assert not ledger.release("no-such-id")


# ── reconciliation: release on proven death ──────────────────────────────────────────


def test_a_dead_holder_is_purged_and_its_slot_returns(ledger_path):
    alive = {4242}
    ledger = _ledger(ledger_path, alive=lambda pid: pid in alive)
    _reserve(ledger, "run-1", pid=4242, limit=1)
    assert not _reserve(ledger, "run-2", pid=5000, limit=1).admission.admitted
    alive.discard(4242)
    assert ledger.reconcile() != []
    assert _reserve(ledger, "run-2", pid=5000, limit=1).admission.admitted


def test_a_reservation_from_a_previous_boot_is_purged_even_if_the_pid_is_reused(ledger_path):
    _reserve(_ledger(ledger_path, boot_id="boot-a"), "run-1", limit=1)
    after_reboot = _ledger(ledger_path, boot_id="boot-b")
    assert after_reboot.active("anthropic-max") == 0


def test_an_expired_lease_over_a_live_pid_still_counts(ledger_path):
    """Expiry means "stopped heartbeating", not "stopped consuming the account"."""
    clock = Clock()
    ledger = _ledger(ledger_path, clock=clock)
    _reserve(ledger, "run-1", limit=1)
    clock.now += 10_000
    assert ledger.reservations()[0].expired(clock.now)
    assert not _reserve(ledger, "run-2", limit=1).admission.admitted


def test_sync_project_releases_gone_runs_refreshes_live_ones_and_adopts_orphans(ledger_path):
    ledger = _ledger(ledger_path)
    gone = _reserve(ledger, "run-gone", limit=None).reservation
    kept = _reserve(ledger, "run-kept", limit=None).reservation
    other = _reserve(ledger, "run-x", project="proj-b", limit=None).reservation

    report = ledger.sync_project(
        "proj-a", {"run-kept": "anthropic-max", "run-adopted": "anthropic-max"},
        writer_pid=777,
    )
    assert report.released == [gone.reservation_id]
    assert report.refreshed == [kept.reservation_id]
    assert len(report.adopted) == 1
    rows = {(r.project_id, r.run_id): r for r in ledger.reservations()}
    assert set(rows) == {("proj-a", "run-kept"), ("proj-a", "run-adopted"), ("proj-b", "run-x")}
    assert rows[("proj-a", "run-kept")].writer_pid == 777
    assert rows[("proj-b", "run-x")].reservation_id == other.reservation_id


def test_sync_project_with_nothing_live_and_nothing_held_writes_no_file(ledger_path):
    _ledger(ledger_path).sync_project("proj-a", {}, writer_pid=1)
    assert not ledger_path.exists()


def test_a_corrupt_ledger_is_quarantined_not_fatal(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("{ not json")
    ledger = _ledger(ledger_path)
    assert _reserve(ledger, "run-1", limit=1).admission.admitted
    assert list(ledger_path.parent.glob("reservations.json.corrupt-*"))


def test_a_malformed_row_is_dropped_and_the_rest_survive(ledger_path):
    ledger = _ledger(ledger_path)
    _reserve(ledger, "run-1", limit=None)
    document = json.loads(ledger_path.read_text())
    document["reservations"].append({"project_id": "half a row"})
    ledger_path.write_text(json.dumps(document))
    assert [r.run_id for r in ledger.reservations()] == ["run-1"]


def test_the_default_path_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv(resources.ENV_VAR, str(tmp_path / "x.json"))
    assert resources.default_ledger_path() == tmp_path / "x.json"


# ── acceptance: two projects share one pool's pressure ────────────────────────────────


def test_two_projects_sharing_an_account_pool_share_its_pressure(ledger_path):
    project_a = _ledger(ledger_path)
    project_b = _ledger(ledger_path)
    assert _reserve(project_a, "a-1", project="proj-a", limit=2).admission.admitted
    assert _reserve(project_b, "b-1", project="proj-b", limit=2).admission.admitted
    refused = _reserve(project_a, "a-2", project="proj-a", limit=2)
    assert not refused.admission.admitted

    # A sync by one project never touches the other's rows.
    project_a.sync_project("proj-a", {}, writer_pid=4242)
    assert [r.project_id for r in project_b.reservations()] == ["proj-b"]
    assert _reserve(project_a, "a-2", project="proj-a", limit=2).admission.admitted


def test_the_router_contract_counts_the_hosts_reservations(ledger_path):
    """`HostResourcePoolState` answers `[P6]`'s question with every project counted."""
    ledger = _ledger(ledger_path)
    pools = HostResourcePoolState(ledger=ledger)
    pools.pool("anthropic-max", concurrency_limit=1)
    assert pools.admit("anthropic-max", now_epoch=0).admitted
    _reserve(_ledger(ledger_path), "b-1", project="proj-b", limit=None)
    answer = pools.admit("anthropic-max", now_epoch=0)
    assert not answer.admitted and answer.reason == "pool_saturated"
    assert isinstance(pools, ResourcePoolState)


def test_quota_exhaustion_still_wins_over_free_host_slots(ledger_path):
    pools = HostResourcePoolState(ledger=_ledger(ledger_path))
    pools.pool("anthropic-max", concurrency_limit=4)
    pools.observe(QuotaObservation(
        pool_id="anthropic-max",
        windows={300: QuotaWindow(window_minutes=300, used_pct=99.0, resets_at=5000)},
    ))
    assert pools.admit("anthropic-max", now_epoch=0).reason == "quota_exhausted"


# ── acceptance: no oversubscription under concurrent claims ───────────────────────────


def _claimer(path: str, worker: int, limit: int, rounds: int, results) -> None:
    ledger = HostReservationLedger(path, boot_id="boot-a")
    for n in range(rounds):
        result = ledger.reserve(
            "shared-pool", project_id=f"proj-{worker % 2}", run_id=f"w{worker}-{n}",
            writer_pid=os.getpid(), limit=limit,
        )
        if not result.admission.admitted:
            continue
        # Read what the file says *while* holding the slot. A lost update or a torn
        # read would show here as more rows than the limit.
        held = len(ledger.reservations("shared-pool"))
        results.put((worker, held))
        ledger.release(result.reservation.reservation_id)


def test_leases_never_oversubscribe_under_concurrent_claim_stress(ledger_path):
    limit, workers, rounds = 3, 8, 40
    ledger_path.parent.mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_claimer, args=(str(ledger_path), w, limit, rounds, results))
        for w in range(workers)
    ]
    for proc in procs:
        proc.start()
    observed = []
    while any(p.is_alive() for p in procs) or not results.empty():
        try:
            observed.append(results.get(timeout=0.5))
        except Exception:  # noqa: BLE001 — queue.Empty from a multiprocessing queue
            pass
    for proc in procs:
        proc.join(timeout=HANG_SECONDS)
        assert proc.exitcode == 0

    assert observed, "no worker ever obtained a slot"
    assert max(held for _, held in observed) <= limit
    assert len({w for w, _ in observed}) > 1
    final = json.loads(ledger_path.read_text())
    assert final["reservations"] == []


# ── [P10B] counted host dimensions ────────────────────────────────────────────────────

RAM = {"ram_mb": 8000.0, "cpu": 8.0}


def _reserve_counted(ledger, run_id, amounts, *, project="proj-a", pid=4242, capacity=RAM):
    return ledger.reserve_counted(
        amounts, capacity=capacity, project_id=project, run_id=run_id, writer_pid=pid
    )


def test_a_counted_reservation_is_refused_past_capacity_naming_the_holders(ledger_path):
    ledger = _ledger(ledger_path)
    held = _reserve_counted(ledger, "run-1", {"ram_mb": 6000, "cpu": 2})
    assert held.admission.admitted and held.reservation.pool_id == resources.COUNTED_POOL
    refused = _reserve_counted(_ledger(ledger_path), "run-2", {"ram_mb": 4000}, project="proj-b")
    assert not refused.admission.admitted and refused.reservation is None
    assert refused.admission.reason == "resource_exhausted"
    assert "ram_mb" in refused.admission.detail and "proj-a" in refused.admission.detail
    # A dimension with room left is still open to a request that needs only it.
    assert _reserve_counted(ledger, "run-3", {"cpu": 6}, project="proj-b").admission.admitted


def test_a_counted_row_carries_its_amounts_and_a_pool_row_keeps_the_p08_shape(ledger_path):
    ledger = _ledger(ledger_path)
    _reserve(ledger, "run-slot")
    _reserve_counted(ledger, "run-ram", {"ram_mb": 100, "vram_mb": 0})
    stored = {r["run_id"]: r for r in json.loads(ledger_path.read_text())["reservations"]}
    assert "amounts" not in stored["run-slot"]
    assert stored["run-ram"]["amounts"] == {"ram_mb": 100.0}
    assert ledger.held_amounts() == {"ram_mb": 100.0}
    assert ledger.held_amounts(exclude_project="proj-a") == {}


def test_re_reserving_counted_amounts_for_the_same_run_does_not_double_count(ledger_path):
    ledger = _ledger(ledger_path)
    first = _reserve_counted(ledger, "run-1", {"ram_mb": 5000}).reservation
    again = _reserve_counted(ledger, "run-1", {"ram_mb": 5000})
    assert again.admission.admitted
    assert again.reservation.reservation_id == first.reservation_id
    assert ledger.held_amounts() == {"ram_mb": 5000.0}


def test_sync_project_heartbeats_releases_and_adopts_counted_rows(ledger_path):
    clock = Clock()
    ledger = _ledger(ledger_path, clock=clock)
    gone = _reserve_counted(ledger, "run-gone", {"ram_mb": 1000}).reservation
    kept = _reserve_counted(ledger, "run-kept", {"ram_mb": 2000}).reservation
    slot = _reserve(ledger, "run-kept", limit=None).reservation
    other = _reserve_counted(ledger, "run-x", {"ram_mb": 500}, project="proj-b").reservation
    clock.now += 60

    report = ledger.sync_project(
        "proj-a", {"run-kept": "anthropic-max"}, writer_pid=777,
        counted={"run-kept": {"ram_mb": 2000}, "run-adopted": {"cpu": 1}, "run-none": {}},
    )

    assert report.released == [gone.reservation_id]
    assert sorted(report.refreshed) == sorted([kept.reservation_id, slot.reservation_id])
    assert len(report.adopted) == 1
    rows = {(r.project_id, r.run_id, r.pool_id): r for r in ledger.reservations()}
    counted = resources.COUNTED_POOL
    assert set(rows) == {
        ("proj-a", "run-kept", counted), ("proj-a", "run-kept", "anthropic-max"),
        ("proj-a", "run-adopted", counted), ("proj-b", "run-x", counted),
    }
    assert rows[("proj-a", "run-kept", counted)].lease_expiry == kept.lease_expiry + 60
    assert rows[("proj-a", "run-adopted", counted)].amounts == {"cpu": 1.0}
    assert rows[("proj-b", "run-x", counted)].reservation_id == other.reservation_id


def test_sync_project_without_counted_releases_this_projects_counted_rows(ledger_path):
    ledger = _ledger(ledger_path)
    _reserve_counted(ledger, "run-1", {"ram_mb": 1000})
    ledger.sync_project("proj-a", {}, writer_pid=1)
    assert ledger.held_amounts() == {}


def test_a_dead_holders_counted_reservation_is_reclaimed_on_reconcile(ledger_path):
    alive = {4242}
    ledger = _ledger(ledger_path, alive=lambda pid: pid in alive)
    held = _reserve_counted(ledger, "run-1", {"ram_mb": 8000}, pid=4242)
    assert not _reserve_counted(ledger, "run-2", {"ram_mb": 1}, pid=5000).admission.admitted
    alive.discard(4242)
    assert ledger.reconcile() == [held.reservation.reservation_id]
    assert _reserve_counted(ledger, "run-2", {"ram_mb": 8000}, pid=5000).admission.admitted


def test_a_malformed_amounts_field_drops_only_that_row(ledger_path):
    ledger = _ledger(ledger_path)
    _reserve_counted(ledger, "run-1", {"ram_mb": 10})
    document = json.loads(ledger_path.read_text())
    document["reservations"].append({**document["reservations"][0], "run_id": "bad",
                                     "reservation_id": "bad", "amounts": ["nope"]})
    ledger_path.write_text(json.dumps(document))
    assert [r.run_id for r in ledger.reservations()] == ["run-1"]


def _counted_claimer(path: str, worker: int, rounds: int, results) -> None:
    ledger = HostReservationLedger(path, boot_id="boot-a")
    for n in range(rounds):
        result = ledger.reserve_counted(
            {"ram_mb": 3000}, capacity={"ram_mb": 8000}, project_id=f"proj-{worker % 2}",
            run_id=f"w{worker}-{n}", writer_pid=os.getpid(),
        )
        if not result.admission.admitted:
            continue
        results.put((worker % 2, ledger.held_amounts().get("ram_mb", 0.0)))
        ledger.release(result.reservation.reservation_id)


def test_two_projects_never_together_reserve_more_ram_than_the_host_has(ledger_path):
    """P10B acceptance: concurrent claims from two projects sharing one host ledger."""
    workers, rounds = 8, 40
    ledger_path.parent.mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_counted_claimer, args=(str(ledger_path), w, rounds, results))
        for w in range(workers)
    ]
    for proc in procs:
        proc.start()
    observed = []
    while any(p.is_alive() for p in procs) or not results.empty():
        try:
            observed.append(results.get(timeout=0.5))
        except Exception:  # noqa: BLE001 — queue.Empty from a multiprocessing queue
            pass
    for proc in procs:
        proc.join(timeout=HANG_SECONDS)
        assert proc.exitcode == 0

    assert observed, "no worker ever reserved RAM"
    assert max(ram for _, ram in observed) <= 8000
    assert max(ram for _, ram in observed) > 3000, "claims never overlapped"
    assert {project for project, _ in observed} == {0, 1}
    assert json.loads(ledger_path.read_text())["reservations"] == []
