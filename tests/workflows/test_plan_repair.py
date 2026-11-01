"""`[P10]`: local plan repair — revision `N + 1`, carry-forward, cancellation, `[INV-08]`.

The pure half (`plan_repair`) is tested on hand-built graphs and phase maps, because that
is where the awkward refusals live: a stale proposal, a cycle, a dropped gate, an
uncertain node in the cone. The durable half (`apply_repair`) runs against a real store
and a fake tmux, and its most important assertion is ordering: the obsolete consumer is
stopped before revision `N + 1` exists, and nothing it counted is forgotten.
"""
from __future__ import annotations

import dataclasses
import signal

import pytest

from maestro import taskgraph
from maestro.workflows import compiler, runner
from maestro.workflows.compiler import (
    PlanRepairProposal,
    PlanRepairRejected,
    RepairCounters,
    StaleRepairProposal,
    apply_repair,
    plan_repair,
    repair_counters,
)
from maestro.workflows.models import EdgeSpec, NodeSpec, WorkflowRevision

from .conftest import task_spec


def _node(node_id: str, **kwargs) -> NodeSpec:
    kwargs.setdefault("kind", "function")
    kwargs.setdefault("handler_ref", "maestro.handlers.noop")
    return NodeSpec(node_id=node_id, **kwargs)


def _edge(src: str, dst: str) -> EdgeSpec:
    return EdgeSpec(source_node_id=src, target_node_id=dst)


def _chain(*ids: str) -> WorkflowRevision:
    return WorkflowRevision(
        workflow_id="wf_chain", revision=1, task_id="T-001", task_spec_revision=1,
        compiler_version="test", policy_hash="p", code_base_sha="c",
        execution_objective="paid_efficiency",
        nodes=tuple(_node(i) for i in ids),
        edges=tuple(_edge(a, b) for a, b in zip(ids, ids[1:])),
        terminal_requirements=(ids[-1],),
    )


def _proposal(rev: WorkflowRevision, **kwargs) -> PlanRepairProposal:
    kwargs.setdefault("reason", "test")
    return PlanRepairProposal(workflow_id=rev.workflow_id, base_revision=rev.revision, **kwargs)


# ── plan_repair: N + 1, carry-forward, cancellation ─────────────────────────────────


def test_a_discovered_prerequisite_creates_revision_n_plus_one():
    first = _chain("a", "b", "c")
    phases = {"a": "succeeded", "b": "running", "c": "pending"}
    plan = plan_repair(
        first,
        _proposal(
            first, kind="discovered_prerequisite", reason="b needs a schema migration",
            add_nodes=(_node("migrate"),),
            add_edges=(_edge("a", "migrate"), _edge("migrate", "b")),
        ),
        phases,
    )
    assert plan.revision.revision == 2
    assert plan.revision.parent_revision == 1
    assert "b needs a schema migration" in plan.revision.mutation_reason
    assert plan.carried == {"a": "succeeded"}
    assert set(plan.invalidated) == {"migrate", "b", "c"}
    assert plan.cancel == ("b",)


def test_carried_nodes_keep_their_artifacts_and_invalidated_ones_lose_them():
    first = _chain("a", "b", "c")
    phases = {"a": "succeeded", "b": "succeeded", "c": "failed"}
    plan = plan_repair(
        first,
        _proposal(first, replace_nodes=(_node("b", params={"fixed": True}),)),
        phases,
        artifacts={"a": ["art_a"], "b": ["art_b"]},
    )
    assert plan.carried == {"a": "succeeded"}
    assert plan.carried_artifacts == {"a": ("art_a",)}
    assert "b" in plan.invalidated and "c" in plan.invalidated


def test_unaffected_sibling_branch_is_carried_and_left_running():
    rev = WorkflowRevision(
        workflow_id="wf_fan", revision=1, task_id="T-001", task_spec_revision=1,
        compiler_version="test", policy_hash="p", code_base_sha="c",
        execution_objective="paid_efficiency",
        nodes=tuple(_node(i) for i in ("root", "left", "right", "join")),
        edges=(_edge("root", "left"), _edge("root", "right"),
               _edge("left", "join"), _edge("right", "join")),
        terminal_requirements=("join",),
    )
    phases = {"root": "succeeded", "left": "succeeded", "right": "failed", "join": "pending"}
    plan = plan_repair(rev, _proposal(rev, rerun_nodes=("right",)), phases)
    assert plan.carried == {"root": "succeeded", "left": "succeeded"}
    assert set(plan.invalidated) == {"right", "join"}


def test_rerun_of_an_unchanged_succeeded_node_invalidates_it_and_downstream():
    """A partial failure fixed outside the graph: same NodeSpec, but its success no longer
    counts, and neither does anything that consumed it."""
    first = _chain("a", "b", "c")
    phases = {"a": "succeeded", "b": "succeeded", "c": "failed"}
    plan = plan_repair(first, _proposal(first, rerun_nodes=("b",)), phases)
    assert plan.carried == {"a": "succeeded"}
    assert set(plan.invalidated) == {"b", "c"}


def test_rewiring_a_nodes_inputs_invalidates_it_even_with_an_identical_spec():
    first = _chain("a", "b", "c")
    phases = {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    plan = plan_repair(
        first,
        _proposal(first, add_nodes=(_node("x"),), add_edges=(_edge("x", "c"),)),
        phases,
    )
    assert set(plan.carried) == {"a", "b"}
    assert set(plan.invalidated) == {"x", "c"}


def test_removing_a_node_invalidates_what_it_fed():
    first = _chain("a", "b", "c")
    rev = compiler.repair(
        first, reason="add bypass", edges=(*first.edges, _edge("a", "c"))
    )
    phases = {"a": "succeeded", "b": "succeeded", "c": "running"}
    plan = plan_repair(rev, _proposal(rev, remove_nodes=("b",)), phases)
    assert plan.revision.revision == 3
    assert "b" not in plan.revision.node_ids
    assert set(plan.invalidated) == {"b", "c"}
    assert plan.cancel == ("c",)


# ── plan_repair: refusals ───────────────────────────────────────────────────────────


def test_a_discovered_prerequisite_that_closes_a_cycle_is_blocked():
    """Acceptance condition 3, at the workflow level."""
    first = _chain("a", "b", "c")
    with pytest.raises(PlanRepairRejected, match="cycle"):
        plan_repair(
            first,
            _proposal(
                first, kind="discovered_prerequisite",
                add_nodes=(_node("p"),), add_edges=(_edge("c", "p"), _edge("p", "b")),
            ),
            {"a": "succeeded", "b": "running"},
        )


def test_a_discovered_roadmap_prerequisite_that_closes_a_cycle_is_blocked():
    """Acceptance condition 3, at the roadmap level — refused before anything is staged."""
    text = (
        "# Roadmap\n"
        "\n### A\n\n```yaml\nid: A\ndeps: []\n```\n"
        "\n### B\n\n```yaml\nid: B\ndeps: [A]\n```\n"
        "\n### C\n\n```yaml\nid: C\ndeps: [B]\n```\n"
    )
    txn = taskgraph.RoadmapTransaction(taskgraph.parse_document(text))
    with pytest.raises(taskgraph.PrerequisiteCycleError) as err:
        txn.discover_prerequisite("A", "C")
    assert err.value.cycle == ("A", "C", "B")
    assert txn.specs["A"].depends_on == ()
    assert txn.render() == text

    with pytest.raises(taskgraph.PrerequisiteCycleError):
        txn.discover_prerequisite("A", {"id": "N", "deps": ["C"]})
    assert "N" not in txn.specs

    added = txn.discover_prerequisite("B", {"id": "N", "deps": ["A"]})
    assert added.task_id == "N"
    assert txn.specs["B"].depends_on == ("A", "N")
    txn.commit()


def test_a_proposal_against_an_older_revision_is_stale():
    first = _chain("a", "b")
    second = compiler.repair(first, reason="earlier repair")
    with pytest.raises(StaleRepairProposal):
        plan_repair(second, _proposal(first, rerun_nodes=("b",)), {})


def test_a_proposal_for_another_workflow_is_refused():
    first = _chain("a", "b")
    proposal = PlanRepairProposal(workflow_id="wf_other", base_revision=1, reason="x")
    with pytest.raises(PlanRepairRejected):
        plan_repair(first, proposal, {})


def test_a_repair_may_not_drop_a_terminal_requirement():
    first = _chain("a", "b", "c")
    with pytest.raises(PlanRepairRejected, match="terminal"):
        plan_repair(first, _proposal(first, remove_nodes=("c",)), {})
    with pytest.raises(PlanRepairRejected, match="terminal"):
        plan_repair(first, _proposal(first, terminal_requirements=("b",)), {})


def test_an_uncertain_node_in_the_cone_must_be_reconciled_first():
    first = _chain("a", "b", "c")
    with pytest.raises(PlanRepairRejected, match="uncertain"):
        plan_repair(first, _proposal(first, rerun_nodes=("a",)), {"b": "uncertain"})


def test_a_patch_naming_unknown_nodes_or_edges_is_refused():
    first = _chain("a", "b")
    for kwargs in (
        {"remove_nodes": ("zz",)},
        {"replace_nodes": (_node("zz"),)},
        {"add_nodes": (_node("a"),)},
        {"remove_edges": (("b", "a"),)},
        {"rerun_nodes": ("zz",)},
    ):
        with pytest.raises(PlanRepairRejected):
            plan_repair(first, _proposal(first, **kwargs), {})


def test_repair_needs_a_reason_and_a_known_kind():
    first = _chain("a", "b")
    with pytest.raises(PlanRepairRejected):
        _proposal(first, reason="")
    with pytest.raises(PlanRepairRejected):
        _proposal(first, kind="rewrite_everything")


# ── [INV-08]: counters ──────────────────────────────────────────────────────────────


def test_repair_increments_and_never_resets_counters():
    first = _chain("a", "b")
    counters = RepairCounters(
        repairs_used=2, max_repairs=5, task_attempts=7, node_attempts={"a": 3, "b": 1}
    )
    plan = plan_repair(first, _proposal(first, rerun_nodes=("b",)), {}, counters=counters)
    assert plan.counters.repairs_used == 3
    assert plan.counters.task_attempts == 7
    assert plan.counters.node_attempts == {"a": 3, "b": 1}
    assert plan.counters.max_repairs == 5


def test_an_exhausted_repair_quota_is_refused():
    first = _chain("a", "b")
    with pytest.raises(PlanRepairRejected, match="quota"):
        plan_repair(
            first, _proposal(first, rerun_nodes=("b",)), {},
            counters=RepairCounters(repairs_used=2, max_repairs=2),
        )


# ── apply_repair: the durable protocol ──────────────────────────────────────────────


@pytest.fixture
def run_ctx(store, repo, revision, dispatcher, dead_pids, artifacts):
    return runner.start_run(
        store, revision, repo=repo, run_id="run_old",
        dispatcher=dispatcher, artifacts=artifacts, is_alive=dead_pids,
    )


def _prerequisite_proposal(revision: WorkflowRevision) -> PlanRepairProposal:
    return PlanRepairProposal(
        workflow_id=revision.workflow_id, base_revision=revision.revision,
        kind="discovered_prerequisite", reason="implementer needs a vendored schema",
        add_nodes=(_node("vendor_schema"),),
        add_edges=(_edge("vendor_schema", "implementer"),),
    )


def _running_implementer(run_ctx, store, dead_pids, pid=610001):
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    run_ctx.inbox(attempt["attempt_id"]).bind_pid(dead_pids.keep(pid))
    run_ctx.tick()
    assert run_ctx.phases()["implementer"] == "running"
    return attempt["attempt_id"]


def test_apply_cancels_the_obsolete_writer_before_revision_n_plus_one_exists(
    run_ctx, store, revision, dead_pids
):
    attempt_id = _running_implementer(run_ctx, store, dead_pids)
    order: list[str] = []

    def terminate(pid, sig):
        order.append(f"kill:{sig}")
        assert compiler.latest_revision(store, revision.workflow_id).revision == 1
        dead_pids.alive.discard(pid)

    plan = plan_repair(revision, _prerequisite_proposal(revision), run_ctx.phases())
    assert plan.cancel == ("implementer",)
    new_run = apply_repair(
        store, plan, old_run_id="run_old", run_id="run_new",
        is_alive=dead_pids, terminate=terminate, sleep=lambda s: None,
    )

    assert order == [f"kill:{signal.SIGTERM}"]
    assert new_run == "run_new"
    assert store.lease("worktree:T-001") is None
    old = store.conn.execute("SELECT status FROM task_runs WHERE run_id='run_old'").fetchone()
    assert old["status"] == "canceled"
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()["status"] == "failed"
    assert all(i["status"] != "dispatched" for i in store.intents_for_attempt(attempt_id))

    stored = compiler.latest_revision(store, revision.workflow_id)
    assert (stored.revision, stored.parent_revision) == (2, 1)
    run = store.conn.execute("SELECT * FROM task_runs WHERE run_id='run_new'").fetchone()
    assert (run["workflow_revision"], run["status"]) == (2, "running")


def test_apply_preserves_cumulative_attempt_and_repair_counters(
    run_ctx, store, repo, revision, dead_pids, dispatcher, artifacts
):
    """`[INV-08]`: the new run starts counting where the old one stopped."""
    _running_implementer(run_ctx, store, dead_pids)
    plan = plan_repair(
        revision, _prerequisite_proposal(revision), run_ctx.phases(),
        counters=repair_counters(store, revision.workflow_id, run_id="run_old"),
    )
    apply_repair(
        store, plan, old_run_id="run_old", run_id="run_new", is_alive=dead_pids,
        terminate=lambda pid, sig: dead_pids.alive.discard(pid), sleep=lambda s: None,
    )
    after = runner.WorkflowRunner(
        store, plan.revision, run_id="run_new", repo=repo, dispatcher=dispatcher,
        artifacts=artifacts, is_alive=dead_pids,
    )
    assert after.attempt_index("implementer") == 1
    assert after.phases()["implementer"] == "pending"

    counters = repair_counters(store, revision.workflow_id, run_id="run_new")
    assert counters.repairs_used == 1
    assert counters.task_attempts == 1
    assert counters.node_attempts == {"implementer": 1}

    # A second repair counts on from there.
    second = plan_repair(
        plan.revision,
        PlanRepairProposal(
            workflow_id=revision.workflow_id, base_revision=2, reason="again",
            rerun_nodes=("vendor_schema",),
        ),
        after.phases(), counters=counters,
    )
    assert second.counters.repairs_used == 2
    assert second.counters.node_attempts == {"implementer": 1}


def test_apply_carries_forward_succeeded_nodes_into_the_new_run(
    run_ctx, store, repo, revision, dead_pids, worker, dispatcher, artifacts
):
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    worker(
        runner.Claim(
            attempt_id=attempt["attempt_id"], run_id="run_old", node_id="implementer",
            operation_id=operation_id, resource_id=None, lease_epoch=1, attempt_index=1,
            inbox=run_ctx.inbox(attempt["attempt_id"]),
        )
    ).finish(operation_id, "succeeded")
    run_ctx.ingest_results()
    phases = run_ctx.phases()
    assert phases["implementer"] == "succeeded"

    new_gate = _node(
        "gate", handler_ref="maestro.handlers.verification", effect_class="repeatable_local_check",
        params={"task_id": "T-001", "repaired": True},
    )
    plan = plan_repair(
        revision,
        PlanRepairProposal(
            workflow_id=revision.workflow_id, base_revision=1, kind="partial_failure",
            reason="gate command was wrong", replace_nodes=(new_gate,),
        ),
        phases,
    )
    assert "implementer" in plan.carried
    apply_repair(store, plan, old_run_id="run_old", run_id="run_new", is_alive=dead_pids)

    after = runner.WorkflowRunner(
        store, plan.revision, run_id="run_new", repo=repo, dispatcher=dispatcher,
        artifacts=artifacts, is_alive=dead_pids,
    )
    assert after.phases()["implementer"] == "succeeded"
    assert after.attempt_index("implementer") == 1
    assert after.phases()["gate"] == "pending"
    events = store.conn.execute(
        "SELECT payload FROM events WHERE type = ?", (compiler.WORKFLOW_REPAIRED,)
    ).fetchall()
    assert len(events) == 1 and '"to_revision": 2' in events[0]["payload"]


# ── the reviewer floor reads the repair lineage (P12 finding D10, edge 2) ───────────


def _strength_routing():
    """A routing stand-in whose catalog ranks one model per strength rung."""
    from types import SimpleNamespace

    from maestro.backends import catalog as cat

    models = {"fixture-l": "light", "fixture-b": "balanced", "fixture-s": "strong"}
    entry = cat.BackendCapabilityEntry(
        backend_id="fixture_cli", runtime_kind="agent_cli", installed_version="1.0.0",
        billing_category="subscription", usage_pool_id="fixture-pool",
        available_models=tuple(models),
        effort_controls=cat.EffortControls(
            supported=True, delivery_mechanism="flag", native_values=("low", "medium", "high")
        ),
        model_profiles={m: cat.ModelProfile(m) for m in models},
        pair_profiles=cat.migrate_pairs(*[(m, s, 1.0) for m, s in models.items()]),
        pairs_migrated=True, concurrency_limit=1,
    )
    return SimpleNamespace(catalog=cat.CapabilityCatalog.of([entry]))


def _writer_succeeds_on(ctx, store, worker, model: str, *, node_id: str = "implementer"):
    """Run `node_id` to success in `ctx`'s run and record the route it ran on."""
    ctx.tick()
    attempt = store.conn.execute(
        "SELECT * FROM attempts WHERE run_id = ? AND node_id = ? ORDER BY rowid DESC LIMIT 1",
        (ctx.run_id, node_id),
    ).fetchone()
    [operation_id] = [
        i["operation_id"] for i in store.unreconciled_intents()
        if i["attempt_id"] == attempt["attempt_id"]
    ]
    worker(
        runner.Claim(
            attempt_id=attempt["attempt_id"], run_id=ctx.run_id, node_id=node_id,
            operation_id=operation_id, resource_id=None, lease_epoch=1, attempt_index=1,
            inbox=ctx.inbox(attempt["attempt_id"]),
        )
    ).finish(operation_id, "succeeded")
    ctx.ingest_results()
    assert ctx.phases()[node_id] == "succeeded"
    with store.conn:
        store.conn.execute(
            "UPDATE attempts SET backend_id = 'fixture_cli', model_id = ?, binding_json = '{}'"
            " WHERE attempt_id = ?",
            (model, attempt["attempt_id"]),
        )


def _repaired_run(store, repo, revision, phases, dispatcher, artifacts, dead_pids, **proposal):
    proposal.setdefault("reason", "the plan was wrong")
    plan = plan_repair(
        revision,
        PlanRepairProposal(
            workflow_id=revision.workflow_id, base_revision=revision.revision, **proposal
        ),
        phases,
    )
    apply_repair(store, plan, old_run_id="run_old", run_id="run_new", is_alive=dead_pids)
    after = runner.WorkflowRunner(
        store, plan.revision, run_id="run_new", repo=repo, dispatcher=dispatcher,
        artifacts=artifacts, is_alive=dead_pids,
    )
    after.routing = _strength_routing()
    return plan, after


def _fixed_gate() -> NodeSpec:
    return _node(
        "gate", handler_ref="maestro.handlers.verification", effect_class="repeatable_local_check",
        params={"task_id": "T-001", "repaired": True},
    )


def test_the_reviewer_floor_counts_a_writer_a_repair_carried_forward(
    run_ctx, store, repo, revision, dead_pids, worker, dispatcher, artifacts
):
    """The carried writer has no attempt in the successor run, and its tree is the one
    reviewed: the floor is still one rung above the model that wrote it."""
    _writer_succeeds_on(run_ctx, store, worker, "fixture-b")
    plan, after = _repaired_run(
        store, repo, revision, run_ctx.phases(), dispatcher, artifacts, dead_pids,
        replace_nodes=(_fixed_gate(),),
    )
    assert "implementer" in plan.carried

    assert after._review_strength_floor(plan.revision.node("proof_review")) == ("strong", "")


def test_the_reviewer_floor_counts_what_the_old_run_wrote_under_a_rerun_writer(
    run_ctx, store, repo, revision, dead_pids, worker, dispatcher, artifacts
):
    """A writer the repair re-runs builds on the commits its old run left in the tree."""
    _writer_succeeds_on(run_ctx, store, worker, "fixture-s")
    plan, after = _repaired_run(
        store, repo, revision, run_ctx.phases(), dispatcher, artifacts, dead_pids,
        rerun_nodes=("implementer",),
    )
    assert "implementer" in plan.invalidated

    floor, refusal = after._review_strength_floor(plan.revision.node("proof_review"))
    assert floor is None and refusal.startswith("route_no_stronger_reviewer:")


def test_the_reviewer_floor_counts_a_writer_the_repair_removed_from_the_graph(
    run_ctx, store, repo, revision, dead_pids, worker, dispatcher, artifacts
):
    """A patch can replace the writer node; the old node's commits are still reviewed."""
    _writer_succeeds_on(run_ctx, store, worker, "fixture-b")
    old_writer = revision.node("implementer")
    plan, after = _repaired_run(
        store, repo, revision, run_ctx.phases(), dispatcher, artifacts, dead_pids,
        remove_nodes=("implementer",),
        add_nodes=(dataclasses.replace(old_writer, node_id="implementer_2"),),
        add_edges=tuple(
            _edge("implementer_2", e.target_node_id)
            for e in revision.edges if e.source_node_id == "implementer"
        ),
    )
    assert "implementer" not in {n.node_id for n in plan.revision.nodes}

    assert after._review_strength_floor(plan.revision.node("proof_review")) == ("strong", "")


def test_the_reviewer_floor_ignores_an_earlier_run_no_repair_links_to_this_one(
    store, repo, revision, dead_pids, worker, dispatcher, artifacts
):
    """A fresh run may start from a reset tree: only the repair lineage wrote this one."""
    earlier = runner.start_run(
        store, revision, repo=repo, run_id="run_earlier",
        dispatcher=dispatcher, artifacts=artifacts, is_alive=dead_pids,
    )
    _writer_succeeds_on(earlier, store, worker, "fixture-s")
    with store.conn:
        store.conn.execute("UPDATE task_runs SET status = 'failed' WHERE run_id = 'run_earlier'")
    old = runner.start_run(
        store, revision, repo=repo, run_id="run_old",
        dispatcher=dispatcher, artifacts=artifacts, is_alive=dead_pids,
    )
    _writer_succeeds_on(old, store, worker, "fixture-b")
    plan, after = _repaired_run(
        store, repo, revision, old.phases(), dispatcher, artifacts, dead_pids,
        replace_nodes=(_fixed_gate(),),
    )

    assert after._review_strength_floor(plan.revision.node("proof_review")) == ("strong", "")


@pytest.mark.parametrize('change', ['carry', 'rerun', 'replace'])
def test_w4_independence_keeps_the_family_of_a_repaired_writer(
    run_ctx, store, repo, revision, dead_pids, worker, dispatcher, artifacts, change
):
    """A real repair retains the author's binding even with no current-run attempt."""
    import json
    _writer_succeeds_on(run_ctx, store, worker, 'fixture-b')
    with store.conn:
        store.conn.execute('UPDATE attempts SET binding_json=? WHERE run_id=?',
                           (json.dumps({'model_family': 'claude'}), run_ctx.run_id))
    proposal = {'replace_nodes': (_fixed_gate(),)}
    if change == 'rerun':
        proposal['rerun_nodes'] = ('implementer',)
    elif change == 'replace':
        proposal = dict(remove_nodes=('implementer',),
            add_nodes=(dataclasses.replace(revision.node('implementer'), node_id='new_writer'),),
            add_edges=tuple(_edge('new_writer', e.target_node_id)
                            for e in revision.edges if e.source_node_id == 'implementer'))
    plan, after = _repaired_run(store, repo, revision, run_ctx.phases(), dispatcher,
                               artifacts, dead_pids, **proposal)
    after.routing.role_independence = {'reviewer': ('implementer',)}
    assert after._independent_families(plan.revision.node('proof_review')) == {'claude'}
    after.routing.role_independence = {'reviewer': ()}
    assert after._independent_families(plan.revision.node('proof_review')) == set()


def test_apply_refuses_when_the_writer_survives_sigkill(run_ctx, store, revision, dead_pids):
    _running_implementer(run_ctx, store, dead_pids)
    plan = plan_repair(revision, _prerequisite_proposal(revision), run_ctx.phases())
    with pytest.raises(PlanRepairRejected, match="survived"):
        apply_repair(
            store, plan, old_run_id="run_old", is_alive=dead_pids,
            terminate=lambda pid, sig: None, sleep=lambda s: None, grace_seconds=0.5,
        )
    assert compiler.latest_revision(store, revision.workflow_id).revision == 1
    assert store.lease("worktree:T-001") is not None
    old = store.conn.execute("SELECT status FROM task_runs WHERE run_id='run_old'").fetchone()
    assert old["status"] == "running"


def test_apply_refuses_a_claim_with_no_bound_pid(run_ctx, store, revision, dead_pids):
    run_ctx.tick()
    assert run_ctx.phases()["implementer"] == "claimed"
    plan = plan_repair(revision, _prerequisite_proposal(revision), run_ctx.phases())
    with pytest.raises(PlanRepairRejected, match="PID"):
        apply_repair(store, plan, old_run_id="run_old", is_alive=dead_pids)
    assert compiler.latest_revision(store, revision.workflow_id).revision == 1


def test_apply_refuses_a_plan_whose_phases_have_moved(run_ctx, store, revision, dead_pids):
    plan = plan_repair(revision, _prerequisite_proposal(revision), run_ctx.phases())
    run_ctx.tick()  # implementer is now claimed; the plan thought it was pending
    with pytest.raises(StaleRepairProposal, match="moved"):
        apply_repair(store, plan, old_run_id="run_old", is_alive=dead_pids)


def test_apply_refuses_when_the_stored_revision_has_moved(run_ctx, store, revision, dead_pids):
    plan = plan_repair(revision, _prerequisite_proposal(revision), run_ctx.phases())
    compiler.store_revision(store, compiler.repair(revision, reason="someone else first"))
    with pytest.raises(StaleRepairProposal):
        apply_repair(store, plan, old_run_id="run_old", is_alive=dead_pids)


# ── P12A W7: the proposal's wire form ───────────────────────────────────────────────


def test_a_proposal_round_trips_through_its_wire_form():
    import json

    prereq = _node("prereq", kind="agent", agent_definition_ref="implementer",
                   params={"why": "missing fixture"})
    rev = _chain("a", "b", "c")
    proposal = _proposal(
        rev, kind="discovered_prerequisite", reason="needs a fixture",
        add_nodes=(prereq,), replace_nodes=(_node("b", params={"v": 2}),),
        remove_nodes=("c",), add_edges=(_edge("a", "prereq"), _edge("prereq", "b")),
        remove_edges=(("a", "b"),), rerun_nodes=("a",), terminal_requirements=("b",),
    )
    wire = json.loads(json.dumps(proposal.to_dict()))
    assert PlanRepairProposal.from_dict(wire) == proposal
    bare = _proposal(rev)
    assert PlanRepairProposal.from_dict(json.loads(json.dumps(bare.to_dict()))) == bare
    assert bare.to_dict()["terminal_requirements"] is None


def test_a_malformed_wire_proposal_is_a_repair_rejection():
    with pytest.raises(PlanRepairRejected):
        PlanRepairProposal.from_dict({"workflow_id": "wf", "base_revision": 1})
    with pytest.raises(PlanRepairRejected):
        PlanRepairProposal.from_dict(["not", "a", "mapping"])
    with pytest.raises(PlanRepairRejected):
        PlanRepairProposal.from_dict({"workflow_id": "wf", "base_revision": 1, "reason": "r",
                                      "add_nodes": [{"node_id": "x", "kind": "bogus"}]})


# ── P12B F7: the diagnoser's verdict ─────────────────────────────────────────────────


def test_a_verdict_round_trips_and_is_omitted_from_the_wire_form_when_unset():
    """P12A's wire form is unchanged for a planner; a diagnoser adds one key."""
    plain = PlanRepairProposal(workflow_id="wf", base_revision=1, reason="r")
    assert plain.verdict is None
    assert "verdict" not in plain.to_dict()
    assert PlanRepairProposal.from_dict(plain.to_dict()) == plain

    for verdict in sorted(compiler.VERDICTS):
        diagnosed = PlanRepairProposal(
            workflow_id="wf", base_revision=1, reason="r", verdict=verdict
        )
        assert diagnosed.to_dict()["verdict"] == verdict
        assert PlanRepairProposal.from_dict(diagnosed.to_dict()) == diagnosed


def test_an_unknown_verdict_is_refused_rather_than_carried():
    with pytest.raises(PlanRepairRejected, match="unknown repair verdict"):
        PlanRepairProposal(workflow_id="wf", base_revision=1, reason="r", verdict="vibes")
    with pytest.raises(PlanRepairRejected, match="unknown repair verdict"):
        PlanRepairProposal.from_dict(
            {"workflow_id": "wf", "base_revision": 1, "reason": "r", "verdict": "vibes"}
        )
