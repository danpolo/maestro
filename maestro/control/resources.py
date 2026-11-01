"""The host reservation ledger: account-pool pressure shared across projects (`[P8]`).

`05_SCHEDULING_AND_RESOURCES.md` §3. Every maestro project on a host has its own control
database and its own controller (`D02`), but several of them may drive the *same*
subscription account. A per-project concurrency cap cannot see the other projects, so two
projects each "within their cap" can oversubscribe the account between them. This module
is the one place they meet: `~/.maestro/reservations.json`, a list of short-lived
reservations each naming the project, run, pool and the PID that holds it.

**Coordination metadata, never domain state** (§3.4). A reservation is exactly the fields
of `Reservation` and nothing else; no task status, no outcome, no evidence. Losing the file
loses nothing a controller cannot re-assert on its next `sync_project`, which is why a
corrupt file is quarantined and restarted rather than refused.

**Concurrency discipline.** Every read-modify-write runs under an exclusive `flock` on a
sidecar lock file and replaces the ledger atomically (`os.replace`), so a reader never sees
a half-written document and two controllers claiming the last slot at once cannot both
get it. The check and the insert are one critical section — `reserve` never asks "is there
room?" in one lock and takes the room in another.

**Counted host dimensions** (`[P10B]`). The same rows also carry CPU / RAM / VRAM: a
reservation in `COUNTED_POOL` holds `amounts` instead of one slot, and `reserve_counted`
refuses when every project's held amounts plus the new ones would exceed the host's
capacity. One row per run, reserved before launch, refreshed and released by the same
`sync_project` call as pool slots, purged on process death by the same rule.

**Release is proven, not assumed** (`[INV-02]`'s shape, host-wide). A reservation is purged
when its PID is dead or it was taken in a previous boot. An *expired* reservation over a
living PID is still counted: expiry says the holder stopped heartbeating, not that it
stopped consuming the account, and admitting another call on that guess is precisely the
oversubscription the ledger exists to prevent.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from maestro.backends.catalog import Admission, ResourcePoolState, RESET_JITTER_SEC
from maestro.control.store import pid_alive

#: Overrides the ledger location outright. The test suite points this at a temp file so
#: no test can reach the developer's real host ledger.
ENV_VAR = "MAESTRO_HOST_LEDGER"
LEDGER_FILENAME = "reservations.json"
SCHEMA_VERSION = 1
DEFAULT_TTL_SEC = 300
#: The pseudo-pool a counted (cpu / ram_mb / vram_mb) reservation lives in.
COUNTED_POOL = "host:counted"
BUDGET_PREFIX = "budget:"


def default_ledger_path() -> Path:
    """`$MAESTRO_HOST_LEDGER`, else `reservations.json` under the maestro home."""
    raw = os.environ.get(ENV_VAR)
    if raw:
        return Path(raw).expanduser()
    from maestro import bootstrap  # stdlib-only module; local to keep this import inert

    return bootstrap.home() / LEDGER_FILENAME


def current_boot_id() -> str:
    """The kernel's boot id, or `""` where there is none to read.

    A PID recorded before a reboot names nothing afterwards — or worse, names an unrelated
    process that happens to have been given the same number.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Reservation:
    """One held slot in one account pool, or counted host amounts in `COUNTED_POOL`.

    Every field is coordination metadata.
    """

    reservation_id: str
    project_id: str
    run_id: str
    pool_id: str
    writer_pid: int
    boot_id: str
    reserved_at: float
    heartbeat_at: float
    lease_expiry: float
    amounts: Mapping[str, float] = field(default_factory=dict)
    budget: Mapping = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        if not self.amounts:
            data.pop("amounts")  # a pool slot row stays exactly the P08 shape
        if not self.budget:
            data.pop("budget")
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "Reservation | None":
        """A row, or `None` for anything malformed — one bad row must not void the rest."""
        try:
            return cls(
                reservation_id=str(data["reservation_id"]),
                project_id=str(data["project_id"]),
                run_id=str(data["run_id"]),
                pool_id=str(data["pool_id"]),
                writer_pid=int(data["writer_pid"]),
                boot_id=str(data.get("boot_id") or ""),
                reserved_at=float(data["reserved_at"]),
                heartbeat_at=float(data["heartbeat_at"]),
                lease_expiry=float(data["lease_expiry"]),
                amounts={str(k): float(v) for k, v in (data.get("amounts") or {}).items()},
                budget=dict(data.get("budget") or {}),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def expired(self, now: float) -> bool:
        return now >= self.lease_expiry


@dataclass(frozen=True)
class ReserveResult:
    """`reservation` is set exactly when `admission.admitted`."""

    admission: Admission
    reservation: Optional[Reservation] = None
    #: Actual B7 transaction inputs/results; absent on legacy slot/count calls.
    budget_decision: Mapping = field(default_factory=dict)


@dataclass
class SyncReport:
    purged: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)


class HostReservationLedger:
    """Read and write `~/.maestro/reservations.json` under an exclusive host lock."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        is_alive: Callable[[int], bool] = pid_alive,
        boot_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path) if path is not None else default_ledger_path()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.is_alive = is_alive
        self.boot_id = current_boot_id() if boot_id is None else boot_id
        self.clock = clock

    # --- the critical section --------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[list[Reservation]]:
        """Yield the live row list under an exclusive lock; write it back if it changed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                rows = self._load()
                before = [r.to_dict() for r in rows]
                yield rows
                if [r.to_dict() for r in rows] != before:
                    self._write(rows)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load(self) -> list[Reservation]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            document = json.loads(raw)
            entries = document["reservations"]
            if not isinstance(entries, list):
                raise TypeError("reservations is not a list")
        except (ValueError, KeyError, TypeError):
            # Only a hand edit gets here (every write is an atomic replace). Keep the
            # evidence, start empty; every live controller re-asserts its runs on its next
            # `sync_project`, so the window this opens is one poll wide.
            quarantine = self.path.with_name(f"{self.path.name}.corrupt-{int(self.clock())}")
            with contextlib.suppress(OSError):
                os.replace(self.path, quarantine)
            return []
        rows = [Reservation.from_dict(e) for e in entries if isinstance(e, Mapping)]
        return [r for r in rows if r is not None]

    def _write(self, rows: list[Reservation]) -> None:
        document = {
            "schema_version": SCHEMA_VERSION,
            "reservations": [r.to_dict() for r in rows],
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def _is_dead(self, row: Reservation) -> bool:
        if row.boot_id and self.boot_id and row.boot_id != self.boot_id:
            return True
        return not self.is_alive(row.writer_pid)

    def _purge(self, rows: list[Reservation]) -> list[str]:
        dead = [r for r in rows if self._is_dead(r) or
                (r.budget.get('state') == 'waiting' and r.expired(self.clock()))]
        for row in dead:
            rows.remove(row)
        return [r.reservation_id for r in dead]

    # --- reads -----------------------------------------------------------------------

    def reservations(self, pool_id: str | None = None) -> list[Reservation]:
        """Rows whose holder is still alive, optionally for one pool."""
        with self._locked() as rows:
            self._purge(rows)
            return [r for r in rows if pool_id is None or r.pool_id == pool_id]

    def active(self, pool_id: str) -> int:
        return len(self.reservations(pool_id))

    def held_amounts(self, *, exclude_project: str | None = None) -> dict[str, float]:
        """Counted amounts every live reservation holds, optionally not one project's."""
        return _sum_amounts(
            r for r in self.reservations(COUNTED_POOL) if r.project_id != exclude_project
        )

    # --- writes ----------------------------------------------------------------------

    def reserve(
        self,
        pool_id: str,
        *,
        project_id: str,
        run_id: str,
        writer_pid: int,
        limit: int | None,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> ReserveResult:
        """Take one slot in `pool_id` before launching, or say why not.

        `limit=None` records the reservation without bounding the pool — pressure is
        still visible to every other project. Re-reserving the same `(project, run,
        pool)` refreshes the existing row rather than taking a second slot, so a
        controller that retries a launch after a crash cannot count itself twice.
        """
        now = self.clock()
        with self._locked() as rows:
            self._purge(rows)
            for index, row in enumerate(rows):
                if (row.project_id, row.run_id, row.pool_id) == (project_id, run_id, pool_id):
                    rows[index] = _refreshed(row, now, ttl_sec, writer_pid=writer_pid)
                    return ReserveResult(Admission(True), rows[index])
            held = [r for r in rows if r.pool_id == pool_id]
            if limit is not None and len(held) >= int(limit):
                holders = sorted({r.project_id for r in held})
                return ReserveResult(
                    Admission(
                        False,
                        reason="pool_saturated",
                        detail=(
                            f"host pool {pool_id} has {len(held)} of {limit} slots "
                            f"reserved (projects: {', '.join(holders)})"
                        ),
                    )
                )
            row = Reservation(
                reservation_id=uuid.uuid4().hex,
                project_id=project_id,
                run_id=run_id,
                pool_id=pool_id,
                writer_pid=int(writer_pid),
                boot_id=self.boot_id,
                reserved_at=now,
                heartbeat_at=now,
                lease_expiry=now + float(ttl_sec),
            )
            rows.append(row)
            return ReserveResult(Admission(True), row)

    def reserve_counted(
        self,
        amounts: Mapping[str, float],
        *,
        capacity: Mapping[str, float],
        project_id: str,
        run_id: str,
        writer_pid: int,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> ReserveResult:
        """Hold `amounts` for `run_id` before launching, or say which dimension is short.

        `capacity` is the host's total per dimension; a dimension it does not name is
        recorded without being bounded. The check sums every project's live counted rows
        in the same critical section as the insert. Re-reserving the same `(project,
        run)` refreshes that row and does not count it twice.
        """
        now = self.clock()
        wanted = {str(k): float(v) for k, v in amounts.items() if v}
        with self._locked() as rows:
            self._purge(rows)
            for index, row in enumerate(rows):
                if (row.project_id, row.run_id, row.pool_id) == (project_id, run_id, COUNTED_POOL):
                    rows[index] = _refreshed(row, now, ttl_sec, writer_pid=writer_pid)
                    return ReserveResult(Admission(True), rows[index])
            counted = [r for r in rows if r.pool_id == COUNTED_POOL]
            held = _sum_amounts(counted)
            for dimension in sorted(wanted):
                limit = capacity.get(dimension)
                if limit is None:
                    continue
                if held.get(dimension, 0.0) + wanted[dimension] > float(limit):
                    holders = sorted({r.project_id for r in counted if r.amounts.get(dimension)})
                    return ReserveResult(Admission(
                        False,
                        reason="resource_exhausted",
                        detail=(
                            f"host {dimension}: needs {wanted[dimension]:g}, "
                            f"{max(float(limit) - held.get(dimension, 0.0), 0.0):g} of "
                            f"{float(limit):g} free (held by: {', '.join(holders) or 'none'})"
                        ),
                    ))
            row = _new_row(project_id, run_id, COUNTED_POOL, writer_pid, self.boot_id,
                           now, ttl_sec, amounts=wanted)
            rows.append(row)
            return ReserveResult(Admission(True), row)

    def heartbeat(self, reservation_id: str, *, ttl_sec: float = DEFAULT_TTL_SEC) -> bool:
        now = self.clock()
        with self._locked() as rows:
            for index, row in enumerate(rows):
                if row.reservation_id == reservation_id:
                    rows[index] = _refreshed(row, now, ttl_sec)
                    return True
        return False

    def reserve_budget(
        self, pool_id: str, amounts: Mapping[str, float], *, capacity: Mapping[str, float],
        resets: Mapping[str, float], project_id: str, run_id: str, writer_pid: int,
        priority: float = 1.0, ttl_sec: float = DEFAULT_TTL_SEC,
        projection: Mapping | None = None,
    ) -> ReserveResult:
        """Atomically check and hold estimated percentages for one selected attempt.

        Costs never cross account pools. Percentages already include burn and the
        expected runtime share, so capacity is simply target minus measured use.
        Reasserting an admitted attempt retains its frozen charge.
        """
        now = self.clock()
        def number(value):
            try:
                return (not isinstance(value, bool) and isinstance(value, (int, float))
                        and math.isfinite(value))
            except OverflowError:
                return False
        if (not number(priority) or priority <= 0
                or any(not number(v) or v < 0 for v in amounts.values())
                or any(not number(v) or not 0 <= v <= 100 for v in capacity.values())
                or any(not number(v) or v <= now for v in resets.values())):
            raise ValueError('budget requires finite numeric percentages, current resets and positive priority')
        wanted = {str(k): float(v) for k, v in amounts.items()}
        caps = {str(k): float(v) for k, v in capacity.items()}
        deadlines = {str(k): float(v) for k, v in resets.items()}
        if (not wanted or wanted.keys() != caps.keys() or wanted.keys() != deadlines.keys()
                or not math.isfinite(priority) or priority <= 0
                or any(not math.isfinite(v) or v < 0 for v in (*wanted.values(), *caps.values()))
                or any(not math.isfinite(v) or v <= now for v in deadlines.values())):
            raise ValueError('budget requires finite nonnegative percentages, current resets and positive priority')
        frozen_projection = None
        if projection is not None:
            if (not isinstance(projection, Mapping)
                    or set(projection) != {'started_at', 'runtime_sec', 'full_amounts'}
                    or not number(projection['started_at']) or projection['started_at'] > now
                    or not number(projection['runtime_sec']) or projection['runtime_sec'] <= 0
                    or not isinstance(projection['full_amounts'], Mapping)
                    or projection['full_amounts'].keys() != wanted.keys()
                    or any(not number(v) or v < wanted[k]
                           for k, v in projection['full_amounts'].items())):
                raise ValueError('budget projection requires a finite start, positive runtime and full charges')
            frozen_projection = dict(started_at=float(projection['started_at']),
                runtime_sec=float(projection['runtime_sec']),
                full_amounts={k: float(v) for k, v in projection['full_amounts'].items()})
            for dimension, full in frozen_projection['full_amounts'].items():
                share = min(max(deadlines[dimension] - frozen_projection['started_at'], 0.0)
                            / frozen_projection['runtime_sec'], 1.0)
                if not math.isclose(wanted[dimension], full * share, rel_tol=1e-12):
                    raise ValueError('budget projection does not match its recorded window charge')
        budget_pool = BUDGET_PREFIX + pool_id
        with self._locked() as rows:
            purged = self._purge(rows)
            held = [r for r in rows if r.pool_id == budget_pool and r.budget.get('state') == 'held']
            inputs = dict(pool_id=pool_id, project_id=project_id, run_id=run_id,
                writer_pid=writer_pid, now_epoch=now, amounts=wanted, capacity=caps,
                resets=deadlines, priority=float(priority), projection=frozen_projection,
                held=[r.to_dict() for r in held], purged=purged)
            result = dict(shared_exposure={}, fairness='not_reached', reasserted=False)

            def decided(admission, reservation=None):
                return ReserveResult(admission, reservation, dict(inputs=inputs, result={
                    **result, **asdict(admission),
                    'reservation_id': reservation.reservation_id if reservation else None,
                    'reservation': reservation.to_dict() if reservation else None}))

            for row in held:
                if (row.project_id, row.run_id) == (project_id, run_id):
                    refreshed = _refreshed(row, now, ttl_sec, writer_pid=writer_pid)
                    rows[rows.index(row)] = refreshed
                    result['reasserted'] = True
                    return decided(Admission(True), refreshed)
            # Demand is coordination metadata too. Persist its first eligible time
            # under this same lock; live admitted rows and waiting rows stay distinct.
            previous = next((r for r in rows if r.pool_id == budget_pool
                and (r.project_id, r.run_id) == (project_id, run_id)), None)
            rows[:] = [r for r in rows if not (r.pool_id.startswith(BUDGET_PREFIX)
                and r.budget.get('state') == 'waiting'
                and (r.project_id, r.run_id) == (project_id, run_id))]
            row = (_refreshed(previous, now, ttl_sec, writer_pid=writer_pid) if previous
                else _new_row(project_id, run_id, budget_pool, writer_pid, self.boot_id,
                              now, ttl_sec, amounts=wanted))
            row = _with_amounts(row, wanted)
            metadata = dict(state='waiting', capacity=caps,
                            resets=deadlines, priority=float(priority))
            if frozen_projection is not None:
                metadata['projection'] = frozen_projection
            row = _with_budget(row, metadata)
            rows.append(row)
            inputs['waiting'] = [r.to_dict() for r in rows if r.pool_id == budget_pool
                                 and r.budget.get('state') == 'waiting']
            for dimension, limit in caps.items():
                used = math.fsum(_budget_exposure(r, dimension, deadlines[dimension], now)
                                 for r in held)
                result['shared_exposure'][dimension] = used
                if wanted[dimension] > max(limit - used, 0.0):
                    return decided(Admission(False, 'budget_exhausted',
                        f'{pool_id} {dimension}: needs {wanted[dimension]:g}%, '
                        f'{max(limit-used, 0):g}% shared headroom remains'))
            waiting = [r for r in rows if r.pool_id == budget_pool
                       and r.budget.get('state') == 'waiting']
            oldest = min(waiting, key=lambda r: (r.reserved_at, r.project_id, r.run_id))
            starved = now - oldest.reserved_at >= 1800
            result.update(oldest_waiter=oldest.reservation_id, starvation_guard=starved,
                          fairness='starvation_override' if starved else 'priority_share')
            if starved and oldest.reservation_id != row.reservation_id:
                return decided(Admission(False, 'budget_starvation_guard',
                    f'{pool_id}: preserving headroom for waiting project {oldest.project_id}'))
            if not starved:
                weights = {}
                for contender in [*held, *waiting]:
                    age = (max(0, now-contender.reserved_at) // 600
                           if contender.budget.get('state') == 'waiting' else 0)
                    weight = contender.budget['priority'] + age
                    weights[contender.project_id] = max(weights.get(contender.project_id, 0), weight)
                # Scale before summing, keeping extreme finite priorities finite.
                scale = max(weights.values())
                total = math.fsum(value / scale for value in weights.values())
                dimension = 'account:300'
                if dimension in caps:
                    allocations = {}
                    for holder in held:
                        allocations[holder.project_id] = (allocations.get(holder.project_id, 0)
                            + _budget_exposure(holder, dimension, deadlines[dimension], now))
                    shares = {project: caps[dimension] * (weight/scale) / total
                              for project, weight in weights.items()}
                    result.update(weights=weights, project_allocations=allocations, shares=shares)
                    peers = [r for r in waiting if r.project_id != project_id
                             and allocations.get(r.project_id, 0) < shares[r.project_id]]
                    # When no blocked peer's request fits inside its own share,
                    # equal standoffs would idle headroom until the starvation
                    # guard; the oldest waiter proceeds instead.
                    peer_fits = any(allocations.get(r.project_id, 0) + r.amounts.get(dimension, 0.0)
                                    <= shares[r.project_id] for r in peers)
                    peer_below_share = bool(peers) and (
                        peer_fits or oldest.reservation_id != row.reservation_id)
                    if (peer_below_share and allocations.get(project_id, 0) + wanted[dimension]
                            > shares[project_id]):
                        return decided(Admission(False, 'budget_fair_share',
                            f'{pool_id}: project priority share {shares[project_id]:g}% '
                            'reserved while another project waits'))
            admitted = _with_budget(row, {**row.budget, 'state': 'held'})
            rows[rows.index(row)] = admitted
            return decided(Admission(True), admitted)

    def release_budget(self, project_id: str, attempt_id: str) -> None:
        """Release exactly one attempt's budget, including a refused claim."""
        with self._locked() as rows:
            rows[:] = [r for r in rows if not (r.project_id == project_id
                and r.run_id == attempt_id and r.pool_id.startswith(BUDGET_PREFIX))]

    def sync_budget(self, project_id: str, live: Mapping[str, Mapping | None], *,
                    writer_pid: int, ttl_sec: float = DEFAULT_TTL_SEC) -> SyncReport:
        """Heartbeat/reassert frozen live attempt holds; never readmit running work.

        A live attempt without a stored budget retains an existing hold, covering
        a controller crash between its claim and the durable routing write.
        Legacy slot/count sync cannot erase these independently keyed holds.
        """
        now = self.clock()
        report = SyncReport()
        with self._locked() as rows:
            report.purged = self._purge(rows)
            seen = set()
            for row in list(rows):
                if row.project_id != project_id or not row.pool_id.startswith(BUDGET_PREFIX):
                    continue
                if row.budget.get('state') == 'waiting':
                    continue  # retries refresh demand; an unpolled wait expires safely
                if row.run_id not in live:
                    rows.remove(row)
                    report.released.append(row.reservation_id)
                else:
                    rows[rows.index(row)] = _refreshed(row, now, ttl_sec, writer_pid=writer_pid)
                    seen.add(row.run_id)
                    report.refreshed.append(row.reservation_id)
            for attempt_id, request in live.items():
                if attempt_id in seen or not request:
                    continue
                row = _new_row(project_id, attempt_id, BUDGET_PREFIX + request['pool_id'],
                    writer_pid, self.boot_id, now, ttl_sec, amounts=request['amounts'])
                rows.append(_with_budget(row, {key: request[key]
                    for key in ('state', 'capacity', 'resets', 'priority', 'projection')
                    if key in request}))
                report.adopted.append(row.reservation_id)
        return report

    def release(self, reservation_id: str) -> bool:
        with self._locked() as rows:
            for row in rows:
                if row.reservation_id == reservation_id:
                    rows.remove(row)
                    return True
        return False

    def reconcile(self) -> list[str]:
        """Purge reservations whose holder is provably gone (§3.3). Ids purged."""
        with self._locked() as rows:
            return self._purge(rows)

    def sync_project(
        self,
        project_id: str,
        live: Mapping[str, str],
        *,
        writer_pid: int,
        counted: Mapping[str, Mapping[str, float]] | None = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> SyncReport:
        """Make this project's rows exactly `live` (`run_id -> pool_id`) plus `counted`
        (`run_id -> amounts`, `[P10B]`), in one lock.

        The heartbeat and the restart path are the same call. Rows for runs that are no
        longer live are released; live runs are refreshed; live runs with no row — work a
        restarted controller adopted — are recorded *without* a limit check, because the
        work is already running and refusing to count it would under-report pressure.
        """
        now = self.clock()
        report = SyncReport()
        counted_live = {
            run_id: {str(k): float(v) for k, v in amounts.items() if v}
            for run_id, amounts in (counted or {}).items()
        }
        counted_live = {run_id: a for run_id, a in counted_live.items() if a}
        with self._locked() as rows:
            report.purged = self._purge(rows)
            seen: set[tuple[str, str]] = set()
            for row in list(rows):
                if row.project_id != project_id:
                    continue
                if row.pool_id.startswith(BUDGET_PREFIX):
                    continue  # budget has attempt identities, not legacy run/slot identities
                if row.pool_id == COUNTED_POOL:
                    keep = row.run_id in counted_live
                else:
                    keep = live.get(row.run_id) == row.pool_id
                if not keep:
                    rows.remove(row)
                    report.released.append(row.reservation_id)
                    continue
                refreshed = _refreshed(row, now, ttl_sec, writer_pid=writer_pid)
                if row.pool_id == COUNTED_POOL:
                    refreshed = _with_amounts(refreshed, counted_live[row.run_id])
                rows[rows.index(row)] = refreshed
                seen.add((row.run_id, row.pool_id))
                report.refreshed.append(row.reservation_id)
            wanted = [(run_id, pool_id, {}) for run_id, pool_id in sorted(live.items())]
            wanted += [(run_id, COUNTED_POOL, a) for run_id, a in sorted(counted_live.items())]
            for run_id, pool_id, amounts in wanted:
                if (run_id, pool_id) in seen:
                    continue
                row = _new_row(project_id, run_id, pool_id, writer_pid, self.boot_id,
                               now, ttl_sec, amounts=amounts)
                rows.append(row)
                report.adopted.append(row.reservation_id)
        return report


def _budget_exposure(row: Reservation, dimension: str, reset_at: float, now: float) -> float:
    """Attribute a live frozen estimate to a newly observed window under the lock.

    Same-window holds never decay on a timer. A fresh deadline advancing by
    more than the shared reset jitter proves a new window, including an early
    forced reset. Use the remaining runtime before that new reset. An overrun is
    still a live consumer, so retain its full frozen cost; legacy holds with no
    projection keep their original conservative charge. Neither case releases
    the row or changes its durable identity/estimate.
    """
    amount = row.amounts.get(dimension, 0.0)
    previous = row.budget.get('resets', {}).get(dimension)
    projection = row.budget.get('projection')
    if previous is None or reset_at - previous <= RESET_JITTER_SEC or not projection:
        return amount
    full = projection['full_amounts'].get(dimension, amount)
    runtime = projection['runtime_sec']
    elapsed = now - projection['started_at']
    remaining = 1.0 - elapsed / runtime
    if remaining <= 0 or elapsed < 0:
        return full
    return full * min(remaining, (reset_at - now) / runtime)


def _new_row(
    project_id: str, run_id: str, pool_id: str, writer_pid: int, boot_id: str,
    now: float, ttl_sec: float, *, amounts: Mapping[str, float] | None = None,
) -> Reservation:
    return Reservation(
        reservation_id=uuid.uuid4().hex,
        project_id=project_id,
        run_id=run_id,
        pool_id=pool_id,
        writer_pid=int(writer_pid),
        boot_id=boot_id,
        reserved_at=now,
        heartbeat_at=now,
        lease_expiry=now + float(ttl_sec),
        amounts=dict(amounts or {}),
    )


def _sum_amounts(rows: Iterable[Reservation]) -> dict[str, float]:
    total: dict[str, float] = {}
    for row in rows:
        for dimension, amount in row.amounts.items():
            total[dimension] = total.get(dimension, 0.0) + float(amount)
    return total


def _with_amounts(row: Reservation, amounts: Mapping[str, float]) -> Reservation:
    values = {f.name: getattr(row, f.name) for f in fields(row)}
    values["amounts"] = dict(amounts)
    return Reservation(**values)


def _with_budget(row: Reservation, budget: Mapping) -> Reservation:
    values = {f.name: getattr(row, f.name) for f in fields(row)}
    values['budget'] = dict(budget)
    return Reservation(**values)


def _refreshed(
    row: Reservation, now: float, ttl_sec: float, *, writer_pid: int | None = None
) -> Reservation:
    values = {f.name: getattr(row, f.name) for f in fields(row)}
    values.update(heartbeat_at=now, lease_expiry=now + float(ttl_sec))
    if writer_pid is not None:
        values["writer_pid"] = int(writer_pid)
    return Reservation(**values)


@dataclass
class HostResourcePoolState(ResourcePoolState):
    """`ResourcePoolState` whose concurrency count is the host's, not this process's.

    `[P6]`'s router asks `resources.admit(pool_id)`; this answers it with every project's
    reservations counted, while pauses and quota readings keep their per-pool meaning. The
    question is unchanged, which is the contract `catalog.PoolState` set out for this phase.
    """

    ledger: Optional[HostReservationLedger] = None

    def admit(self, pool_id: str, *, now_epoch: Optional[float] = None) -> Admission:
        state = self.pools.get(pool_id)
        if state is not None and self.ledger is not None:
            state.active = self.ledger.active(pool_id)
        return super().admit(pool_id, now_epoch=now_epoch)
