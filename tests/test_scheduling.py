"""Ready frontiers, physical admission and greedy ranking (`[P8]`).

Pure by construction: a capacity, some requests and a clock. The acceptance conditions of
`P08_RESOURCE_ADMISSION_SCHEDULING.md` §3 each have a test named after them.
"""
from __future__ import annotations

import pytest

from maestro import scheduling
from maestro.backends.catalog import Admission
from maestro.scheduling import (
    AdmissionEngine,
    Candidate,
    EligibilityClock,
    HostCapacity,
    ResourceRequest,
    SchedulingPolicy,
)
from maestro.taskgraph import TaskGraph
from maestro.workflows.models import EdgeSpec, NodeSpec, WorkflowRevision

POLICY = SchedulingPolicy(aging_interval_sec=100, starvation_after_sec=500)


def _engine(**limits):
    capacity = HostCapacity(limits={k.replace("__", ":"): v for k, v in limits.items()})
    return AdmissionEngine(capacity)


def _agent(pool="anthropic-max", backend="claude", **kw):
    return ResourceRequest.of(agent_backend=backend, pool_id=pool, **kw)


# ── admission ────────────────────────────────────────────────────────────────────────


def test_counted_dimensions_are_bounded_by_capacity():
    engine = _engine(cpu=4, ram_mb=8000)
    big = ResourceRequest.of(cpu=3, ram_mb=6000)
    assert engine.check(big).admitted
    engine.take(big)
    answer = engine.check(ResourceRequest.of(cpu=2))
    assert not answer.admitted and answer.reason == "resource_exhausted"
    assert "cpu" in answer.detail
    engine.release(big)
    assert engine.check(ResourceRequest.of(cpu=2)).admitted


def test_a_declared_dimension_with_no_capacity_refuses():
    """No GPU declared as 0 VRAM: a VRAM request is refused, not waved through."""
    engine = _engine(vram_mb=0)
    assert engine.check(ResourceRequest.of(vram_mb=1)).reason == "resource_exhausted"


def test_exceeds_capacity_names_the_first_dimension_the_whole_host_cannot_hold():
    capacity = HostCapacity(limits={"cpu": 8.0, "ram_mb": 8000.0, "vram_mb": 0.0})
    # Fits the whole host (even if not free right now): not oversized.
    assert scheduling.exceeds_capacity(ResourceRequest.of(ram_mb=8000), capacity) is None
    # Sorted: `ram_mb` is named before `vram_mb`, both being too big.
    assert scheduling.exceeds_capacity(
        ResourceRequest.of(ram_mb=16000.5, vram_mb=1), capacity
    ) == "exceeds_host_capacity: ram_mb requested 16000.5, host capacity 8000"
    assert scheduling.exceeds_capacity(
        ResourceRequest.of(vram_mb=1), capacity
    ) == "exceeds_host_capacity: vram_mb requested 1, host capacity 0"
    # A dimension with no bound never triggers it.
    assert scheduling.exceeds_capacity(
        _agent(ram_mb=100), HostCapacity(limits={"ram_mb": 8000.0})
    ) is None
    assert scheduling.EXCEEDS_CAPACITY == "exceeds_host_capacity"


def test_worktree_writer_leases_and_the_target_branch_lock_are_exclusive():
    engine = _engine()
    writer = ResourceRequest.of(writer_lease=scheduling.worktree_lock("T1"))
    merge = ResourceRequest.of(target_branch="main")
    engine.take(writer)
    engine.take(merge)
    assert engine.check(writer).reason == "lock_held"
    assert engine.check(ResourceRequest.of(target_branch="main")).reason == "lock_held"
    assert engine.check(ResourceRequest.of(writer_lease=scheduling.worktree_lock("T2"))).admitted


def test_agent_slots_are_per_backend():
    engine = _engine(agent__claude=2, agent__codex=1)
    engine.take(_agent(pool=None))
    engine.take(_agent(pool=None))
    assert not engine.check(_agent(pool=None)).admitted
    assert engine.check(_agent(pool=None, backend="codex")).admitted


def test_pool_admission_is_consulted_only_for_requests_naming_a_pool():
    asked = []

    def pool_admit(pool_id):
        asked.append(pool_id)
        return Admission(False, reason="quota_exhausted", detail="99%")

    engine = AdmissionEngine(HostCapacity(), pool_admit=pool_admit)
    assert engine.check(_agent()).reason == "quota_exhausted"
    assert engine.check(ResourceRequest.of(cpu=1)).admitted
    assert asked == ["anthropic-max"]


def test_from_node_reads_the_runners_writer_lease_keys():
    node = NodeSpec(
        node_id="merge", kind="function", handler_ref="h",
        resource_request={"writer_lease": True, "resource_id": "target_branch", "cpu": 2},
    )
    request = ResourceRequest.from_node(node, task_id="T1")
    assert request.exclusive == frozenset({"target_branch"})
    assert request.counted == {"cpu": 2.0}
    assert request.is_deterministic

    implement = NodeSpec(
        node_id="implement", kind="agent", handler_ref="h", agent_definition_ref="impl",
        resource_request={"writer_lease": True},
    )
    request = ResourceRequest.from_node(implement, task_id="T1")
    assert request.exclusive == frozenset({"worktree:T1"})
    assert not request.is_deterministic


def test_detect_declares_cpu_ram_and_vram(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16384000 kB\nMemFree: 1 kB\n")
    capacity = HostCapacity.detect(agent_slots={"claude": 2}, pools={"p": 1}, meminfo=meminfo)
    assert capacity.limits["cpu"] >= 1
    assert capacity.limits["ram_mb"] == pytest.approx(16000)
    assert "vram_mb" in capacity.limits
    assert capacity.limits["agent:claude"] == 2 and capacity.limits["pool:p"] == 1


# ── the two ready frontiers ──────────────────────────────────────────────────────────


def _graph(tasks: dict, accepted=()) -> TaskGraph:
    from maestro.taskgraph import Acceptance, normalize_spec

    specs = {}
    for tid, fields in tasks.items():
        block = {"id": tid, "title": tid, "status": "pending", "deps": []}
        block.update(fields)
        specs[tid] = normalize_spec(block)
    return TaskGraph(
        specs=specs,
        order=tuple(tasks),
        acceptances={t: Acceptance(task_id=t, kind="legacy_accepted") for t in accepted},
    )


def test_roadmap_frontier_needs_accepted_prerequisites_no_hold_and_policy():
    graph = _graph({
        "A": {},
        "B": {"deps": ["A"]},
        "C": {},
        "D": {"hold": True},
        "E": {},
    })
    assert scheduling.roadmap_frontier(graph) == ("A", "C", "E")
    assert scheduling.roadmap_frontier(graph, holds={"C"}) == ("A", "E")
    assert scheduling.roadmap_frontier(
        graph, permits=lambda spec: spec.task_id != "E"
    ) == ("A", "C")
    accepted = _graph({"A": {}, "B": {"deps": ["A"]}}, accepted=("A",))
    assert "B" in scheduling.roadmap_frontier(accepted)


def _revision(nodes, edges) -> WorkflowRevision:
    return WorkflowRevision(
        workflow_id="wf", revision=1, task_id="T1", task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="",
        execution_objective="paid_efficiency", nodes=tuple(nodes), edges=tuple(edges),
        terminal_requirements=(nodes[-1].node_id,),
    )


def _fn(node_id, **kw):
    return NodeSpec(node_id=node_id, kind="function", handler_ref="h", **kw)


def test_workflow_frontier_needs_completed_predecessors_and_bound_inputs():
    revision = _revision(
        [_fn("build"), _fn("test", input_bindings={"snapshot": "artifact"}), _fn("lint")],
        [EdgeSpec("build", "test", guard_predicate="on_success"),
         EdgeSpec("build", "lint", guard_predicate="on_success")],
    )
    assert scheduling.workflow_frontier(revision, {}) == ("build",)
    done = {"build": "succeeded"}
    assert scheduling.workflow_frontier(revision, done) == ("lint",)
    assert set(scheduling.workflow_frontier(
        revision, done, bound_inputs={"test": ["snapshot"]}
    )) == {"test", "lint"}


def test_leverage_counts_what_is_still_waiting_downstream():
    graph = _graph({"A": {}, "B": {"deps": ["A"]}, "C": {"deps": ["B"]}, "D": {}})
    assert scheduling.task_leverage(graph, "A") == 2
    assert scheduling.task_leverage(graph, "D") == 0
    revision = _revision(
        [_fn("a"), _fn("b"), _fn("c")],
        [EdgeSpec("a", "b", guard_predicate="on_success"),
         EdgeSpec("b", "c", guard_predicate="on_success")],
    )
    assert scheduling.node_leverage(revision, "a") == 2
    legacy = [{"id": "A"}, {"id": "B", "deps": ["A"]}, {"id": "C", "deps": "B"},
              {"id": "Z", "deps": ["A"], "status": "complete"}]
    assert scheduling.legacy_task_leverage("A", legacy) == 2


# ── ranking ──────────────────────────────────────────────────────────────────────────


def test_rank_order_is_priority_then_aging_then_leverage_then_fit():
    engine = _engine(cpu=1)
    now = 1000.0
    candidates = [
        Candidate("fit-low-leverage", ResourceRequest.of(cpu=1), eligible_since=now),
        Candidate("no-fit", ResourceRequest.of(cpu=2), eligible_since=now),
        Candidate("leverage", ResourceRequest.of(cpu=1), eligible_since=now, leverage=3),
        Candidate("aged", ResourceRequest.of(cpu=1), eligible_since=now - 250),
        Candidate("operator", ResourceRequest.of(cpu=1), operator_priority=5,
                  eligible_since=now),
    ]
    ranking = scheduling.rank(candidates, engine, now=now, policy=POLICY)
    assert [e.candidate_id for e in ranking] == [
        "operator", "aged", "leverage", "fit-low-leverage", "no-fit",
    ]
    aged = next(e for e in ranking if e.candidate_id == "aged")
    assert aged.aging_steps == 2 and aged.effective_priority == 2
    assert "aging=2" in aged.describe()


def test_an_equal_effective_priority_goes_to_the_explicit_priority():
    engine = _engine()
    now = 1000.0
    ranking = scheduling.rank(
        [Candidate("aged", eligible_since=now - 100),
         Candidate("pinned", operator_priority=1, eligible_since=now)],
        engine, now=now, policy=POLICY,
    )
    assert [e.candidate_id for e in ranking] == ["pinned", "aged"]


def test_the_eligibility_clock_resets_when_a_candidate_leaves_the_frontier():
    clock = EligibilityClock()
    assert clock.observe(["A", "B"], 10) == {"A": 10, "B": 10}
    assert clock.observe(["A", "B"], 50) == {"A": 10, "B": 10}
    assert clock.observe(["A"], 60) == {"A": 10}
    assert clock.observe(["A", "B"], 70) == {"A": 10, "B": 70}
    clock.forget("A")
    assert clock.observe(["A"], 80) == {"A": 80}


# ── acceptance conditions ────────────────────────────────────────────────────────────


def test_a_saturated_inference_pool_cannot_delay_a_ready_deterministic_job():
    engine = AdmissionEngine(
        HostCapacity(limits={"cpu": 4, "agent:claude": 1}),
        pool_admit=lambda pool: Admission(False, reason="pool_saturated", detail="full"),
    )
    engine.take(_agent(pool=None))  # the one claude slot is already running
    now = 10_000.0
    candidates = [
        # Ranked ahead of the test job, starved long enough to protect what it lacks.
        Candidate("impl-A", _agent(), operator_priority=9, eligible_since=0),
        Candidate("impl-B", _agent(), operator_priority=8, eligible_since=0),
        Candidate("pytest", ResourceRequest.of(cpu=1, writer_lease="worktree:T9"),
                  eligible_since=now),
    ]
    decision = scheduling.schedule(candidates, engine, now=now, policy=POLICY)
    assert decision.admitted == ["pytest"]
    assert set(decision.waiting) == {"impl-A", "impl-B"}
    assert decision.waiting["impl-A"].reason in {"resource_exhausted", "pool_saturated"}


def test_continuous_eligibility_aging_eventually_admits_a_starved_task():
    """A 2-CPU job behind an endless stream of fresh, higher-priority 1-CPU jobs."""
    engine = _engine(cpu=2)
    clock = EligibilityClock()
    running: list[tuple[int, ResourceRequest]] = []  # (finishes at tick, request)
    admitted_at = None
    for tick in range(60):
        now = tick * 50.0
        for finish, request in [r for r in running if r[0] <= tick]:
            engine.release(request)
            running.remove((finish, request))
        # Two fresh small jobs every tick, each running two ticks, so without protection
        # the host is never empty enough for the big one.
        requests = {f"small-{tick}-{i}": ResourceRequest.of(cpu=1) for i in range(2)}
        requests["big"] = ResourceRequest.of(cpu=2)
        since = clock.observe(requests, now)
        candidates = [
            Candidate(cid, requests[cid], operator_priority=0 if cid == "big" else 3,
                      eligible_since=since[cid])
            for cid in requests
        ]
        decision = scheduling.schedule(candidates, engine, now=now, policy=POLICY)
        for cid in decision.admitted:
            running.append((tick + 2, requests[cid]))
            clock.forget(cid)
        if "big" in decision.admitted:
            admitted_at = tick
            break
        assert "big" in decision.waiting
    assert admitted_at is not None, "the starved job was never admitted"
    # Not admitted until it had aged past the fresh stream's explicit priority.
    assert admitted_at * 50.0 >= POLICY.aging_interval_sec * 3


def test_protection_holds_back_only_the_dimensions_the_starved_job_lacks():
    engine = _engine(cpu=2, ram_mb=1000)
    engine.take(ResourceRequest.of(cpu=1))
    decision = scheduling.schedule(
        [
            Candidate("starved", ResourceRequest.of(cpu=2), operator_priority=5,
                      eligible_since=0),
            Candidate("wants-cpu", ResourceRequest.of(cpu=1), eligible_since=10_000),
            Candidate("wants-ram", ResourceRequest.of(ram_mb=500), eligible_since=10_000),
        ],
        engine, now=10_000, policy=POLICY,
    )
    assert decision.admitted == ["wants-ram"]
    assert decision.waiting["wants-cpu"].reason == "reserved_for_starved"
    assert decision.protected == {"cpu": "starved"}
    assert any("reserved_for_starved" in line for line in decision.explain())


def test_schedule_is_deterministic():
    def run():
        engine = _engine(cpu=3)
        cands = [Candidate(f"c{i}", ResourceRequest.of(cpu=1), eligible_since=0)
                 for i in (5, 3, 9, 1)]
        return scheduling.schedule(cands, engine, now=0, policy=POLICY).admitted

    assert run() == run() == ["c1", "c3", "c5"]
