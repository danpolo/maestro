"""Ready frontiers, physical admission and the greedy ranking (`[P8]`).

`05_SCHEDULING_AND_RESOURCES.md` §1-§2. Three questions that the legacy loop answered in
one `for` statement, kept apart here because conflating them is what produced its worst
behaviour:

1. **Is it logically ready?** Two frontiers. `roadmap_frontier` over tasks (prerequisites
   accepted, no hold, policy permits) and `workflow_frontier` over nodes (activated
   predecessors succeeded, required inputs bound). Nothing about resources.
2. **Does it physically fit right now?** `AdmissionEngine` over counted dimensions (CPU,
   RAM, VRAM, per-backend agent slots, account pools) and exclusive locks (worktree writer
   leases, the target-branch lock). Nothing about order.
3. **In what order are fitting things admitted?** `rank` / `schedule`: operator priority,
   aging, downstream leverage, resource fit — deterministic, and every decision carries the
   components that produced it.

**The negative invariant** (§1.3). A saturated agent or inference pool must not delay a
ready deterministic job. It holds structurally: a deterministic request names no `agent:`
or `pool:` dimension, admission checks only the dimensions a request names, and `schedule`
walks the whole ranking instead of stopping at the first refusal. A full pool is a reason
for *that* candidate to wait, never a dependency edge for the ones behind it.

**Starvation.** Greedy admission alone starves a large request: smaller ones keep filling
the room it needs. Two mechanisms, both bounded to the starved candidate's own shortfall.
Aging raises a continuously eligible candidate's effective priority one step per
`aging_interval_sec`, so it eventually outranks newer work. And once it has waited
`starvation_after_sec`, the dimensions it is short of are *protected*: lower-ranked
candidates needing those dimensions wait while it drains. Dimensions it does not need stay
open — which is why protection cannot violate the negative invariant above.

Learned schedulers (HEFT, CP-SAT) are deliberately absent (§2.1.5, Experiment E5).
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maestro.backends.catalog import Admission

# ── resource vocabulary ──────────────────────────────────────────────────────────────

CPU = "cpu"
RAM_MB = "ram_mb"
VRAM_MB = "vram_mb"
AGENT_PREFIX = "agent:"
POOL_PREFIX = "pool:"


def agent_dimension(backend: str) -> str:
    return f"{AGENT_PREFIX}{backend}"


def pool_dimension(pool_id: str) -> str:
    return f"{POOL_PREFIX}{pool_id}"


def worktree_lock(task_id: str) -> str:
    return f"worktree:{task_id}"


def target_branch_lock(branch: str) -> str:
    return f"target_branch:{branch}"


@dataclass(frozen=True)
class ResourceRequest:
    """What one candidate consumes: amounts of counted dimensions, and exclusive locks."""

    counted: Mapping[str, float] = field(default_factory=dict)
    exclusive: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        *,
        cpu: float = 0,
        ram_mb: float = 0,
        vram_mb: float = 0,
        agent_backend: str | None = None,
        pool_id: str | None = None,
        writer_lease: str | None = None,
        target_branch: str | None = None,
        locks: Iterable[str] = (),
    ) -> "ResourceRequest":
        counted: dict[str, float] = {}
        for name, amount in ((CPU, cpu), (RAM_MB, ram_mb), (VRAM_MB, vram_mb)):
            if amount:
                counted[name] = float(amount)
        if agent_backend:
            counted[agent_dimension(agent_backend)] = 1.0
        if pool_id:
            counted[pool_dimension(pool_id)] = 1.0
        exclusive = set(locks)
        if writer_lease:
            exclusive.add(writer_lease)
        if target_branch:
            exclusive.add(target_branch_lock(target_branch))
        return cls(counted=counted, exclusive=frozenset(exclusive))

    @classmethod
    def from_node(cls, node: Any, *, task_id: str) -> "ResourceRequest":
        """A `NodeSpec`'s `resource_request`, in this module's vocabulary.

        Reads the same keys `WorkflowRunner.resource_id_for` does, so the writer lease the
        scheduler admits against is the one the runner will actually claim.
        """
        request = dict(getattr(node, "resource_request", {}) or {})
        writer_lease = None
        if request.get("writer_lease"):
            writer_lease = str(request.get("resource_id") or worktree_lock(task_id))
        backend = request.get("agent_backend")
        if not backend and getattr(node, "kind", "") == "agent":
            backend = "any"
        return cls.of(
            cpu=float(request.get("cpu", 0) or 0),
            ram_mb=float(request.get("ram_mb", 0) or 0),
            vram_mb=float(request.get("vram_mb", 0) or 0),
            agent_backend=backend or None,
            pool_id=request.get("pool_id") or None,
            writer_lease=writer_lease,
            target_branch=request.get("target_branch") or None,
        )

    @classmethod
    def from_task(cls, task: Mapping) -> "ResourceRequest":
        """A ROADMAP task's `resources:` block as a request (`[P10B]`).

        Counted host dimensions only. A task never names an agent slot or an account
        pool here — those belong to the lane that launches it — so a `kind: script` task
        is deterministic by construction. Raises `taskgraph.ResourceSpecError` on a
        malformed block; the caller refuses to admit rather than guessing.
        """
        from maestro.taskgraph import parse_resources  # local: taskgraph is heavy

        amounts = parse_resources(task.get("resources"), task_id=str(task.get("id") or ""))
        return cls.of(**{k: float(v) for k, v in amounts.items()})

    @property
    def is_deterministic(self) -> bool:
        """Consumes no agent slot and no inference pool."""
        return not any(
            name.startswith((AGENT_PREFIX, POOL_PREFIX)) for name in self.counted
        )

    def dimensions(self) -> frozenset[str]:
        return frozenset(self.counted) | self.exclusive


@dataclass(frozen=True)
class HostCapacity:
    """Upper bounds per counted dimension. A dimension absent here is unbounded."""

    limits: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def detect(
        cls,
        *,
        agent_slots: Mapping[str, int] | None = None,
        pools: Mapping[str, int] | None = None,
        reserve_cpu: float = 1.0,
        meminfo: Path = Path("/proc/meminfo"),
    ) -> "HostCapacity":
        """This host's CPU, RAM and VRAM, plus the agent and pool bounds given.

        CPU, RAM and VRAM are always declared, so a request for VRAM on a host with no
        GPU is refused rather than waved through.
        """
        limits: dict[str, float] = {
            CPU: max(1.0, float(os.cpu_count() or 1) - reserve_cpu),
            RAM_MB: _mem_total_mb(meminfo),
            VRAM_MB: _vram_total_mb(),
        }
        for backend, slots in (agent_slots or {}).items():
            limits[agent_dimension(backend)] = float(slots)
        for pool_id, slots in (pools or {}).items():
            limits[pool_dimension(pool_id)] = float(slots)
        return cls(limits=limits)


def _mem_total_mb(meminfo: Path) -> float:
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, IndexError, ValueError):
        pass
    return 0.0


def _vram_total_mb() -> float:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    if result.returncode != 0:
        return 0.0
    try:
        return sum(float(v) for v in result.stdout.split())
    except ValueError:
        return 0.0


# ── admission ────────────────────────────────────────────────────────────────────────

#: The refusal reason for a request no amount of waiting can satisfy (`[P12A]` W9).
EXCEEDS_CAPACITY = "exceeds_host_capacity"


def exceeds_capacity(request: ResourceRequest, capacity: HostCapacity) -> str | None:
    """Why `request` can never fit this host even when it is idle, else `None`.

    Names the first counted dimension (sorted) whose bound is strictly smaller than the
    amount requested. A dimension with no bound is unbounded and never triggers it.
    """
    for dimension in sorted(request.counted):
        limit = capacity.limits.get(dimension)
        amount = request.counted[dimension]
        if limit is not None and float(limit) < amount:
            return (
                f"{EXCEEDS_CAPACITY}: {dimension} requested {amount:g}, "
                f"host capacity {float(limit):g}"
            )
    return None


class AdmissionEngine:
    """Tracks what is held and answers "does this request fit now?".

    `pool_admit` is the account-pool question — `ResourcePoolState.admit`, or the host
    ledger's `HostResourcePoolState.admit` — asked last and only for requests that name a
    pool, so quota and cross-project pressure refuse the agent work they concern and
    nothing else.
    """

    def __init__(
        self,
        capacity: HostCapacity,
        *,
        held: Mapping[str, float] | None = None,
        locked: Iterable[str] = (),
        pool_admit: Callable[[str], Admission] | None = None,
    ):
        self.capacity = capacity
        self.held: dict[str, float] = dict(held or {})
        self.locked: set[str] = set(locked)
        self.pool_admit = pool_admit

    def free(self, dimension: str) -> float | None:
        limit = self.capacity.limits.get(dimension)
        if limit is None:
            return None
        return float(limit) - self.held.get(dimension, 0.0)

    def shortfall(self, request: ResourceRequest) -> frozenset[str]:
        """The dimensions `request` cannot have right now."""
        short = {lock for lock in request.exclusive if lock in self.locked}
        for dimension, amount in request.counted.items():
            free = self.free(dimension)
            if free is not None and amount > free:
                short.add(dimension)
            elif dimension.startswith(POOL_PREFIX) and self.pool_admit is not None:
                if not self.pool_admit(dimension[len(POOL_PREFIX):]).admitted:
                    short.add(dimension)
        return frozenset(short)

    def check(self, request: ResourceRequest) -> Admission:
        for lock in sorted(request.exclusive):
            if lock in self.locked:
                return Admission(False, reason="lock_held", detail=f"{lock} is held")
        for dimension in sorted(request.counted):
            amount = request.counted[dimension]
            free = self.free(dimension)
            if free is not None and amount > free:
                return Admission(
                    False,
                    reason="resource_exhausted",
                    detail=(
                        f"{dimension}: needs {amount:g}, {max(free, 0.0):g} of "
                        f"{self.capacity.limits[dimension]:g} free"
                    ),
                )
        if self.pool_admit is not None:
            for dimension in sorted(request.counted):
                if dimension.startswith(POOL_PREFIX):
                    answer = self.pool_admit(dimension[len(POOL_PREFIX):])
                    if not answer.admitted:
                        return answer
        return Admission(True)

    def take(self, request: ResourceRequest) -> None:
        for dimension, amount in request.counted.items():
            self.held[dimension] = self.held.get(dimension, 0.0) + amount
        self.locked |= request.exclusive

    def release(self, request: ResourceRequest) -> None:
        for dimension, amount in request.counted.items():
            self.held[dimension] = max(0.0, self.held.get(dimension, 0.0) - amount)
        self.locked -= request.exclusive


# ── the two ready frontiers ──────────────────────────────────────────────────────────


def roadmap_frontier(
    graph: Any,
    *,
    holds: Iterable[str] = (),
    permits: Callable[[Any], bool] | None = None,
) -> tuple[str, ...]:
    """Tasks whose prerequisites are accepted, with no hold, that policy permits (§1.1).

    `holds` are the operator's runtime holds (parked tasks) on top of the roadmap's own
    `hold:` flag, which `TaskGraph.ready` already honours.
    """
    held = {str(h) for h in holds}
    return tuple(
        task_id
        for task_id in graph.ready()
        if task_id not in held and (permits is None or permits(graph.spec(task_id)))
    )


def workflow_frontier(
    revision: Any,
    phases: Mapping[str, str],
    *,
    bound_inputs: Mapping[str, Iterable[str]] | None = None,
) -> tuple[str, ...]:
    """Nodes whose activated predecessors succeeded and whose inputs are bound (§1.2).

    The edge half is the runner's own `ready_frontier`, not a second implementation of
    guard semantics. A node with declared `input_bindings` is held until every one of them
    appears in `bound_inputs[node_id]`.
    """
    from maestro.workflows.runner import ready_frontier  # local: runner imports the store

    bound = bound_inputs or {}
    ready = []
    for node_id in ready_frontier(revision, phases):
        wanted = set(revision.node(node_id).input_bindings)
        if wanted <= set(bound.get(node_id, ())):
            ready.append(node_id)
    return tuple(ready)


def task_leverage(graph: Any, task_id: str) -> int:
    """How many not-yet-accepted tasks transitively wait on `task_id`."""
    done = graph.completed_ids
    return sum(1 for t in graph.transitive_dependents(task_id) if t not in done)


def node_leverage(revision: Any, node_id: str) -> int:
    """How many nodes transitively descend from `node_id`."""
    seen: set[str] = set()
    frontier = [node_id]
    while frontier:
        for edge in revision.outgoing(frontier.pop()):
            if edge.target_node_id not in seen:
                seen.add(edge.target_node_id)
                frontier.append(edge.target_node_id)
    return len(seen)


def legacy_task_leverage(task_id: str, tasks: Iterable[Mapping]) -> int:
    """`task_leverage` over the legacy loop's roadmap dicts (`deps`, `status`)."""
    dependents: dict[str, set[str]] = {}
    for task in tasks:
        if task.get("status") == "complete":
            continue
        deps = task.get("deps") or []
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            dependents.setdefault(str(dep), set()).add(str(task.get("id")))
    seen: set[str] = set()
    frontier = [str(task_id)]
    while frontier:
        for child in dependents.get(frontier.pop(), ()):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return len(seen)


# ── ranking ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One thing on a ready frontier, with what the ranking needs to know about it."""

    candidate_id: str
    request: ResourceRequest = field(default_factory=ResourceRequest)
    operator_priority: int = 0
    eligible_since: float = 0.0
    leverage: int = 0


@dataclass(frozen=True)
class SchedulingPolicy:
    aging_interval_sec: float = 600.0
    starvation_after_sec: float = 1800.0


class EligibilityClock:
    """When each candidate became *continuously* eligible.

    A candidate that drops off the frontier (dispatched, held, blocked again) loses its
    age: aging rewards waiting while ready, not having once been ready.
    """

    def __init__(self) -> None:
        self.since: dict[str, float] = {}

    def observe(self, candidate_ids: Iterable[str], now: float) -> dict[str, float]:
        current = {str(c) for c in candidate_ids}
        self.since = {c: self.since.get(c, now) for c in current}
        return dict(self.since)

    def forget(self, candidate_id: str) -> None:
        self.since.pop(str(candidate_id), None)


@dataclass(frozen=True)
class RankEntry:
    """Why a candidate sits where it does. Every field is one term of the sort key."""

    candidate_id: str
    operator_priority: int
    aging_steps: int
    leverage: int
    fits: bool
    waited_sec: float

    @property
    def effective_priority(self) -> int:
        return self.operator_priority + self.aging_steps

    def sort_key(self) -> tuple:
        return (
            -self.effective_priority,
            -self.operator_priority,
            -self.leverage,
            not self.fits,
            self.candidate_id,
        )

    def describe(self) -> str:
        return (
            f"{self.candidate_id}: priority={self.operator_priority}"
            f"+aging={self.aging_steps} leverage={self.leverage} "
            f"fits={'yes' if self.fits else 'no'} waited={self.waited_sec:.0f}s"
        )


def rank(
    candidates: Iterable[Candidate],
    engine: AdmissionEngine,
    *,
    now: float,
    policy: SchedulingPolicy = SchedulingPolicy(),
) -> list[RankEntry]:
    """Operator priority, then aging, then downstream leverage, then resource fit (§2.1).

    Aging is added to operator priority rather than compared after it, because a strictly
    lexicographic order lets a steady stream of higher-priority work starve everything
    below it forever. On a tie in effective priority the explicit priority still wins.
    """
    interval = max(float(policy.aging_interval_sec), 1e-9)
    entries = []
    for candidate in candidates:
        waited = max(0.0, now - candidate.eligible_since)
        entries.append(
            RankEntry(
                candidate_id=candidate.candidate_id,
                operator_priority=int(candidate.operator_priority),
                aging_steps=int(waited // interval),
                leverage=int(candidate.leverage),
                fits=engine.check(candidate.request).admitted,
                waited_sec=waited,
            )
        )
    return sorted(entries, key=RankEntry.sort_key)


@dataclass
class ScheduleDecision:
    ranking: list[RankEntry]
    admitted: list[str] = field(default_factory=list)
    waiting: dict[str, Admission] = field(default_factory=dict)
    protected: dict[str, str] = field(default_factory=dict)

    def explain(self) -> list[str]:
        lines = []
        for entry in self.ranking:
            if entry.candidate_id in self.admitted:
                verdict = "admitted"
            else:
                answer = self.waiting.get(entry.candidate_id)
                verdict = f"waiting ({answer.reason}: {answer.detail})" if answer else "waiting"
            lines.append(f"{entry.describe()} -> {verdict}")
        return lines


class AdmissionRound:
    """`schedule`'s admission step, one candidate at a time (`[P10B]`).

    `schedule` walks its own ranking and admits greedily. The live launch loop cannot hand
    over its whole frontier that way: between "fits" and "running" sit gates only it can
    answer (a pending question, a saturated host pool, a worktree that will not create),
    and a candidate those gates stop must neither keep what it took nor be counted as
    starved. So the loop walks its ranked queue itself, `admit`s each candidate that has
    passed its own gates, and `withdraw`s one whose launch then fails. Protection
    accumulates across the round exactly as it does inside `schedule`, which is the same
    code.
    """

    def __init__(
        self,
        engine: AdmissionEngine,
        *,
        now: float,
        policy: SchedulingPolicy = SchedulingPolicy(),
        decision: ScheduleDecision | None = None,
    ):
        self.engine = engine
        self.now = now
        self.policy = policy
        self.decision = decision if decision is not None else ScheduleDecision(ranking=[])

    def admit(self, candidate: Candidate) -> Admission:
        """Take `candidate`'s request if it fits and nothing starved has reserved it."""
        decision = self.decision
        cid = candidate.candidate_id
        blocked_by = sorted(candidate.request.dimensions() & decision.protected.keys())
        if blocked_by:
            answer = Admission(
                False,
                reason="reserved_for_starved",
                detail=", ".join(
                    f"{d} held back for {decision.protected[d]}" for d in blocked_by
                ),
            )
            decision.waiting[cid] = answer
            return answer
        answer = self.engine.check(candidate.request)
        if answer.admitted:
            self.engine.take(candidate.request)
            decision.admitted.append(cid)
            decision.waiting.pop(cid, None)
            return answer
        decision.waiting[cid] = answer
        if max(0.0, self.now - candidate.eligible_since) >= self.policy.starvation_after_sec:
            for dimension in self.engine.shortfall(candidate.request):
                decision.protected.setdefault(dimension, cid)
        return answer

    def withdraw(self, candidate: Candidate) -> None:
        """Give back what an admitted candidate took; it did not start after all."""
        if candidate.candidate_id in self.decision.admitted:
            self.decision.admitted.remove(candidate.candidate_id)
            self.engine.release(candidate.request)


def schedule(
    candidates: Iterable[Candidate],
    engine: AdmissionEngine,
    *,
    now: float,
    policy: SchedulingPolicy = SchedulingPolicy(),
) -> ScheduleDecision:
    """Greedy admission down the ranking. Admitted requests are taken on `engine`."""
    pool = {c.candidate_id: c for c in candidates}
    decision = ScheduleDecision(ranking=rank(pool.values(), engine, now=now, policy=policy))
    admission = AdmissionRound(engine, now=now, policy=policy, decision=decision)
    for entry in decision.ranking:
        admission.admit(pool[entry.candidate_id])
    return decision
