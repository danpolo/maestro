"""`[P3]`: the frontier, the guards and the 6-step controlled-operation protocol.

The frontier tests are pure — a revision and a phase map, no database — because that is
what makes them able to assert the awkward cases (a join with one failed parent, an
optional branch that must not gate its siblings) exhaustively rather than by example.

The protocol tests use a real store and a fake tmux, and the assertion that matters most
is negative: after a crash between launch and receipt, a second tick must NOT dispatch a
second writer into the same worktree. Everything else in this file exists to make that one
assertion trustworthy.
"""
from __future__ import annotations

import os

import pytest

from maestro.control.store import SingleWriterViolation
from maestro.workflows import compiler, handlers, runner
from maestro.workflows.models import (
    EdgeSpec,
    NodeResult,
    NodeSpec,
    RetryPolicy,
    WorkflowRevision,
)

from .conftest import task_spec


# ── a small graph to reason about, built by hand ─────────────────────────────────────


def _node(node_id: str, **kwargs) -> NodeSpec:
    kwargs.setdefault("kind", "function")
    kwargs.setdefault("handler_ref", "maestro.handlers.noop")
    return NodeSpec(node_id=node_id, **kwargs)


def _graph(nodes, edges, terminals=()) -> WorkflowRevision:
    return WorkflowRevision(
        workflow_id="wf_hand",
        revision=1,
        task_id="T-hand",
        task_spec_revision=1,
        compiler_version="test",
        policy_hash="",
        code_base_sha="",
        execution_objective="paid_efficiency",
        nodes=tuple(nodes),
        edges=tuple(edges),
        terminal_requirements=tuple(terminals),
    )


@pytest.fixture
def diamond() -> WorkflowRevision:
    """root -> (left | right) -> join. The AND-join and the sibling-independence case."""
    return _graph(
        [_node("root"), _node("left"), _node("right"), _node("join", kind="join")],
        [
            EdgeSpec("root", "left", guard_predicate="on_success"),
            EdgeSpec("root", "right", guard_predicate="on_success"),
            EdgeSpec("left", "join", guard_predicate="on_success"),
            EdgeSpec("right", "join", guard_predicate="on_success"),
        ],
        ["join"],
    )


# ── guards and the frontier ──────────────────────────────────────────────────────────


def test_roots_are_ready_immediately(diamond):
    assert runner.ready_frontier(diamond, {}) == ("root",)


def test_a_success_guard_opens_only_on_success(diamond):
    phases = {"root": "succeeded"}
    assert set(runner.ready_frontier(diamond, phases)) == {"left", "right"}


def test_an_and_join_waits_for_every_required_parent(diamond):
    phases = {"root": "succeeded", "left": "succeeded"}
    assert "join" not in runner.ready_frontier(diamond, phases)
    phases["right"] = "succeeded"
    assert runner.ready_frontier(diamond, phases) == ("join",)


def test_a_failed_parent_skips_the_join_but_not_its_sibling(diamond):
    """P3 acceptance: sibling branches survive independent branch failure.

    `left` failing decides the join at once — an AND-join with a dead required parent can
    never run, and pretending otherwise would leave it waiting forever on a branch that is
    already lost. What must NOT happen is the failure reaching across: `right` keeps
    running, is not skipped, and is not cancelled."""
    phases = {"root": "succeeded", "left": "failed", "right": "running"}
    assert runner.node_is_skipped(diamond, "join", phases) is True
    assert runner.node_is_skipped(diamond, "right", phases) is False
    assert runner.propagate_skips(diamond, phases) == {"join": "skipped"}

    phases["right"] = "succeeded"
    assert runner.propagate_skips(diamond, phases) == {"join": "skipped"}


def test_a_skipped_source_never_satisfies_an_edge(diamond):
    """`§5.1`. A node that did not run cannot stand in for one that did — which is what
    stops a skipped branch discharging a required terminal check (`[INV-04]`)."""
    phases = {"root": "skipped"}
    assert runner.edge_state(diamond, diamond.edges[0], phases) == "closed"
    assert runner.ready_frontier(diamond, phases) == ()


def test_skips_propagate_transitively_to_a_fixed_point(diamond):
    changed = runner.propagate_skips(diamond, {"root": "failed"})
    assert changed == {"left": "skipped", "right": "skipped", "join": "skipped"}


def test_an_optional_failure_branch_does_not_gate_the_success_path():
    """The `park` node exists to *receive* a failure. A required-edge reading of it would
    make every ordinary lane wait for its own park branch to resolve."""
    revision = compiler.compile_workflow(task_spec())
    phases = {"implementer": "succeeded"}
    assert "gate" in runner.ready_frontier(revision, phases)
    assert runner.node_is_skipped(revision, "park", phases) is False


def test_a_run_whose_terminal_requirement_was_skipped_fails():
    revision = compiler.compile_workflow(task_spec())
    phases = dict.fromkeys(revision.node_ids, "skipped")
    phases["implementer"] = "failed"
    assert runner.run_outcome(revision, phases) == "failed"


def test_a_run_succeeds_only_when_every_terminal_requirement_succeeded(diamond):
    assert runner.run_outcome(diamond, {"join": "running"}) is None
    assert runner.run_outcome(diamond, {"join": "succeeded"}) == "succeeded"
    assert runner.run_outcome(diamond, {"join": "failed"}) == "failed"


def test_carry_forward_keeps_an_unchanged_succeeded_node_and_drops_a_rewritten_one():
    """`§5.3` invariant 3, with `D06`'s caveat: identity is the NodeSpec, not the name."""
    first = compiler.compile_workflow(task_spec(), workflow_id="wf_cf")
    phases = {"implementer": "succeeded", "gate": "succeeded"}

    same = compiler.repair(first, reason="downstream rework")
    assert set(runner.carry_forward(first, same, phases)) == {"implementer", "gate"}

    rewritten_gate = _node("gate", handler_ref="maestro.handlers.verification")
    nodes = tuple(rewritten_gate if n.node_id == "gate" else n for n in first.nodes)
    changed = compiler.repair(first, reason="gate rewritten", nodes=nodes)
    assert set(runner.carry_forward(first, changed, phases)) == {"implementer"}


# ── the 6-step protocol ──────────────────────────────────────────────────────────────


@pytest.fixture
def run_ctx(store, repo, revision, dispatcher, dead_pids, artifacts):
    """A started run over the ordinary lane, dispatching into a fake tmux."""
    return runner.start_run(
        store, revision, repo=repo, run_id="run_test",
        dispatcher=dispatcher, artifacts=artifacts, is_alive=dead_pids,
    )


def test_a_tick_claims_before_it_launches(run_ctx, store, tmux):
    """Step 2 precedes step 3. A launch that was not first recorded cannot be reconciled,
    which is the whole of the crash-after-launch problem."""
    report = run_ctx.tick()
    assert report.dispatched == ["implementer"]

    lease = store.lease("worktree:T-001")
    assert lease is not None
    intents = store.unreconciled_intents()
    assert [row["status"] for row in intents] == ["dispatched"]
    assert tmux.windows == ["maestro-T-001-implementer"]


def test_the_claim_binds_a_lease_an_intent_and_an_event_together(run_ctx, store):
    from maestro.control.models import EventType

    run_ctx.tick()
    kinds = [e.type for e in store.events()]
    assert EventType.LEASE_CLAIMED in kinds
    assert EventType.NODE_ATTEMPT_CLAIMED in kinds
    assert store.unreconciled_intents()[0]["effect_class"] == "agent_editing_worktree"


def test_dispatch_uses_a_named_window_of_the_agents_session(run_ctx, tmux):
    run_ctx.tick()
    cmd = [c for c in tmux.calls if c[:2] == ["tmux", "new-window"]][0]
    assert cmd[:5] == ["tmux", "new-window", "-t", "agents", "-n"]
    assert "&" not in " ".join(cmd[-1:]).split()[0:1]


def test_the_lease_rebinds_to_the_pid_the_worker_reports(run_ctx, store, worker, dead_pids):
    """Step 4, `[INV-02]`: the claim records the launcher's PID and the worker replaces it.
    A launcher PID left in a lease is the defect the invariant is named after."""
    run_ctx.tick()
    claim_pid = store.lease("worktree:T-001").writer_pid
    assert claim_pid == os.getpid()

    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    inbox = run_ctx.inbox(attempt["attempt_id"])
    inbox.bind_pid(dead_pids.keep(424242))

    run_ctx.tick()
    assert store.lease("worktree:T-001").writer_pid == 424242
    assert run_ctx.phases()["implementer"] == "running"


def test_a_receipt_moves_the_node_and_releases_the_lease(run_ctx, store, worker):
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    hand = worker(
        runner.Claim(
            attempt_id=attempt["attempt_id"], run_id="run_test", node_id="implementer",
            operation_id=operation_id, resource_id=None, lease_epoch=1, attempt_index=1,
            inbox=run_ctx.inbox(attempt["attempt_id"]),
        )
    )
    hand.finish(operation_id, "succeeded")

    run_ctx.tick()
    assert run_ctx.phases()["implementer"] == "succeeded"
    assert store.lease("worktree:T-001") is None
    assert store.receipt_for(operation_id) is not None


def test_a_result_without_a_receipt_is_not_ingested_yet(run_ctx, store, worker):
    """The worker writes the receipt last. Reading a result before it would race the
    worker's own protocol, and a half-finished side effect would look finished."""
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    run_ctx.inbox(attempt["attempt_id"]).write_result(
        NodeResult(
            run_id="run_test", node_id="implementer",
            attempt_id=attempt["attempt_id"], status="succeeded",
        )
    )
    run_ctx.tick()
    assert run_ctx.phases()["implementer"] != "succeeded"


def test_a_crash_between_launch_and_receipt_does_not_dispatch_a_second_writer(
    run_ctx, store, repo, revision, dispatcher, tmux, dead_pids, artifacts
):
    """P3's first acceptance condition, asserted the way it actually happens: the tick
    dispatched, the worker bound a PID that is still alive, and the controller died. A new
    controller over the same database must find the resource held, not free."""
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    run_ctx.inbox(attempt["attempt_id"]).bind_pid(dead_pids.keep(555001))
    run_ctx.tick()
    assert store.lease("worktree:T-001").writer_pid == 555001

    restarted = runner.WorkflowRunner(
        store, revision, run_id="run_test", repo=repo, dispatcher=dispatcher,
        artifacts=artifacts, is_alive=dead_pids,
    )
    report = restarted.tick()

    assert report.dispatched == []
    assert tmux.windows == ["maestro-T-001-implementer"]
    assert store.lease("worktree:T-001").writer_pid == 555001


def test_a_live_writer_blocks_a_replacement_claim_however_the_lease_expired(
    run_ctx, store, dead_pids
):
    """P3 acceptance: an expired lease with a confirmed running PID blocks a replacement.
    Expiry is not evidence of anything; `kill(pid, 0)` is."""
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    dead_pids.keep(os.getpid())
    store.conn.execute(
        "UPDATE writer_leases SET expires_at = '1999-01-01T00:00:00Z'"
        " WHERE resource_id = 'worktree:T-001'"
    )
    with pytest.raises(SingleWriterViolation):
        store.claim_lease(
            "worktree:T-001", "att_other", writer_pid=9, is_alive=dead_pids
        )


def test_pre_validation_refuses_a_node_whose_resource_a_live_writer_holds(run_ctx, dead_pids):
    """A second node wanting the same worktree while the first writer lives. The refusal
    is the resource one, not the retry one — asserted on a node with no attempts of its
    own so the two cannot be confused."""
    run_ctx.tick()
    dead_pids.keep(os.getpid())
    second = NodeSpec(
        node_id="second_writer", kind="function", handler_ref="maestro.handlers.noop",
        resource_request={"writer_lease": True},
    )
    with pytest.raises(runner.DispatchRefused, match="live writer"):
        run_ctx.pre_validate(second)


def test_pre_validation_refuses_an_unregistered_handler(run_ctx):
    node = NodeSpec(node_id="x", kind="function", handler_ref="maestro.handlers.nope")
    with pytest.raises(runner.DispatchRefused, match="no handler registered"):
        run_ctx.pre_validate(node)


def test_pre_validation_refuses_a_node_past_its_retry_policy(run_ctx, store):
    node = NodeSpec(
        node_id="implementer", kind="function", handler_ref="maestro.handlers.noop",
        retry_policy=RetryPolicy(max_attempts=1),
    )
    run_ctx.tick()
    with pytest.raises(runner.DispatchRefused, match="retry policy"):
        run_ctx.pre_validate(node)


def test_a_failed_launch_decides_the_node_rather_than_leaving_a_dangling_claim(
    store, repo, revision, tmux, dead_pids, artifacts
):
    tmux.fail_windows.add("maestro-T-001-implementer")
    ctx = runner.start_run(
        store, revision, repo=repo, run_id="run_fail",
        dispatcher=handlers.TmuxDispatcher(runner=tmux), artifacts=artifacts,
        is_alive=dead_pids,
    )
    ctx.tick()
    assert ctx.phases()["implementer"] == "failed"
    assert store.lease("worktree:T-001") is None
    assert store.intents_for_attempt(
        store.conn.execute("SELECT attempt_id FROM attempts").fetchone()["attempt_id"]
    )[0]["status"] == "failed"


def test_a_failed_node_skips_its_success_successors_on_the_next_tick(run_ctx, store, worker):
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    inbox = run_ctx.inbox(attempt["attempt_id"])
    inbox.write_result(
        NodeResult(
            run_id="run_test", node_id="implementer", attempt_id=attempt["attempt_id"],
            status="failed", exit_status=1,
        )
    )
    inbox.write_receipt(operation_id=operation_id, exit_status=1)

    run_ctx.tick()
    phases = run_ctx.phases()
    assert phases["implementer"] == "failed"
    assert phases["gate"] == "skipped"
    assert phases["proof_review"] == "skipped"
    assert phases["accept"] == "skipped"


def test_a_run_whose_accept_was_skipped_finishes_failed(run_ctx, store):
    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    inbox = run_ctx.inbox(attempt["attempt_id"])
    inbox.write_result(
        NodeResult(
            run_id="run_test", node_id="implementer", attempt_id=attempt["attempt_id"],
            status="failed", exit_status=1,
        )
    )
    inbox.write_receipt(operation_id=operation_id, exit_status=1)
    report = run_ctx.tick()
    assert report.outcome == "failed"
    row = store.conn.execute(
        "SELECT status FROM task_runs WHERE run_id = 'run_test'"
    ).fetchone()
    assert row["status"] == "failed"


def test_a_paused_node_neither_advances_nor_releases_the_run(store, repo, dispatcher,
                                                             dead_pids, artifacts):
    """A `human` node parks. The node does not transition and the run keeps waiting —
    which is what makes a manual prep task unable to graduate on its own."""
    revision = compiler.compile_workflow(
        task_spec(dispatch="manual"), workflow_id="wf_manual"
    )
    ctx = runner.start_run(
        store, revision, repo=repo, run_id="run_manual", dispatcher=dispatcher,
        artifacts=artifacts, is_alive=dead_pids,
    )
    ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    inbox = ctx.inbox(attempt["attempt_id"])
    inbox.write_result(
        NodeResult(
            run_id="run_manual", node_id="prep", attempt_id=attempt["attempt_id"],
            status="paused",
        )
    )
    inbox.write_receipt(operation_id=operation_id, exit_status=0)
    report = ctx.tick()
    assert ctx.phases()["prep"] not in ("succeeded", "failed", "skipped")
    assert ctx.phases()["human_action"] == "pending"
    assert report.outcome is None


def test_the_script_lane_runs_its_command_in_a_worker_not_in_the_controller(repo, tmp_path):
    """B8's lane, relocated. The handler still shells out; what changed is that the call
    happens in a process the controller only watches."""
    node = compiler.compile_workflow(
        task_spec(kind="script", run="echo hello"), workflow_id="wf_s"
    ).node("script")
    ctx = handlers.HandlerContext(
        run_id="r", task_id="T-001", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "a"),
        workspace=tmp_path, worktree=tmp_path, params=node.params,
    )
    result = handlers.resolve(node.handler_ref)(ctx)
    assert result.status == "succeeded"
    assert "hello" in result.outputs["stdout"]


def test_a_script_with_no_command_fails_closed(repo, tmp_path):
    node = compiler.compile_workflow(
        task_spec(kind="script"), workflow_id="wf_s2"
    ).node("script")
    ctx = handlers.HandlerContext(
        run_id="r", task_id="T-001", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "a"),
        workspace=tmp_path, params=node.params,
    )
    assert handlers.resolve(node.handler_ref)(ctx).status == "failed"


def test_the_p4_handlers_refuse_rather_than_manufacture_an_acceptance(repo, tmp_path):
    """`[INV-04]`: none of the three may report success on nothing.

    P3 registered these as handlers that PARKED, for the same reason; `[P4]` made them
    real, so the assertion moves from "parks" to "refuses". The refusals differ because
    the reasons do: `gate` has no declared verification to run (an empty closed world
    discharges nothing), while `merge` and `accept` are controller-only nodes handed no
    controller — which is what a worker reaching for the target branch looks like.
    """
    revision = compiler.compile_workflow(task_spec(), workflow_id="wf_def")
    for node_id in ("gate", "merge", "accept"):
        node = revision.node(node_id)
        ctx = handlers.HandlerContext(
            run_id="r", task_id="T-001", node=node, attempt_id="a",
            inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "a"),
            workspace=tmp_path, params=node.params,
        )
        result = handlers.resolve(node.handler_ref)(ctx)
        assert result.status == "failed", node_id
        assert result.exit_status != 0, node_id
    merge_ctx = handlers.HandlerContext(
        run_id="r", task_id="T-001", node=revision.node("merge"), attempt_id="a",
        inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "a"),
        workspace=tmp_path, params=revision.node("merge").params,
    )
    assert "INV-05" in handlers.resolve("maestro.handlers.merge")(merge_ctx).detail


def test_a_tick_with_nothing_to_do_is_idle_and_changes_nothing(run_ctx, store):
    run_ctx.tick()
    head = store.head_sequence()
    report = run_ctx.tick()
    assert report.idle
    assert store.head_sequence() == head


def test_the_worker_binds_its_own_pid_before_it_does_anything(repo, store, revision):
    """The worker entry point, driven directly. `bind_pid` defaults to `os.getpid()` so a
    launcher's PID cannot reach the envelope by accident."""
    from maestro.workflows import worker as worker_mod

    store.register_task(revision.task_id)
    compiler.store_revision(store, revision)
    inbox = handlers.AttemptInbox(handlers.attempts_root(repo), "att_w")
    exit_code = worker_mod.main([
        "--repo", str(repo), "--run-id", "run_w", "--node-id", "implementer",
        "--attempt-id", "att_w", "--operation-id", "op_w",
        "--workflow-id", revision.workflow_id, "--revision", "1",
    ])
    assert inbox.writer_pid() == os.getpid()
    assert inbox.receipt()["operation_id"] == "op_w"
    assert inbox.result().status in ("uncertain", "failed")
    assert exit_code != 0


def test_an_attempt_another_runs_reconciler_finished_still_moves_its_node(
    run_ctx, store, dead_pids
):
    """P12 D19. Every runner's reconciler scans every domain lease, so with two runs live
    the *other* run's tick can find this run's writer dead with its receipt on disk and
    finish the attempt before this run ingests it. `ingest_results` only read attempts
    still `claimed`/`running`, so the node stayed `running` forever (04-scoring, 4.5 h)."""
    from maestro.control.reconcile import Reconciler

    run_ctx.tick()
    attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
    operation_id = store.unreconciled_intents()[0]["operation_id"]
    inbox = run_ctx.inbox(attempt["attempt_id"])
    inbox.bind_pid(dead_pids.keep(515151))
    run_ctx.tick()
    assert run_ctx.phases()["implementer"] == "running"

    inbox.write_result(
        NodeResult(
            run_id="run_test", node_id="implementer",
            attempt_id=attempt["attempt_id"], status="succeeded",
        )
    )
    inbox.write_receipt(operation_id=operation_id, exit_status=0)
    dead_pids.alive.discard(515151)
    Reconciler(
        store, attempts_root=run_ctx.attempts_root, is_alive=dead_pids
    ).reconcile()
    assert store.conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = ?", (attempt["attempt_id"],)
    ).fetchone()["status"] == "succeeded"
    assert run_ctx.phases()["implementer"] == "running"

    run_ctx.tick()
    assert run_ctx.phases()["implementer"] == "succeeded"
    assert run_ctx.phases()["gate"] != "pending"
