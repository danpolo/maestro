"""`[P3]`: the legacy lanes compile to graphs, and the legacy sentinels read as envelopes.

Two halves, both about the seam rather than about new behaviour (`D08`).

The compilation half asserts that each of the five execution kinds the roadmap already
speaks produces the lane the baseline measured — same steps, same order, no step invented
and none dropped. `tests/graph_engineering/test_baseline_lanes.py` pinned what the loop
does; this pins that the graph says the same thing.

The sentinel half asserts the facade. The interesting assertions are the ones where the
mapping is *not* the obvious one: `PAUSED` is not a node outcome, `SWITCH` is not a
failure, and a `DONE` alongside a `SWITCH` wins.
"""
from __future__ import annotations

import json

import pytest

from maestro.workflows import compiler, handlers
from maestro.workflows.models import (
    WorkflowRevision,
    WorkflowValidationError,
)

from .conftest import task_spec


# ── the five lanes ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fields,expected_kind,expected_nodes",
    [
        ({}, "ordinary", ("implementer", "gate", "proof_review", "merge", "accept")),
        ({"kind": "script", "run": "true"}, "script", ("script", "gate", "merge", "accept")),
        ({"dispatch": "manual"}, "manual", ("prep", "human_action", "gate", "merge", "accept")),
        (
            {"dispatch": "async-job", "launch_cmd": "tmux new-window -d job"},
            "async",
            ("async_launch", "await_job", "gate", "accept"),
        ),
        (
            {"resumable": True},
            "resumable",
            ("implementer", "gate", "merge", "accept", "cooldown"),
        ),
    ],
)
def test_each_execution_kind_compiles_to_its_baseline_lane(
    fields, expected_kind, expected_nodes
):
    spec = task_spec(**fields)
    assert spec.execution_kind == expected_kind
    revision = compiler.compile_workflow(spec)
    for node_id in expected_nodes:
        assert node_id in revision.node_ids, f"{expected_kind} lane lost {node_id}"


def test_the_ordinary_lane_is_implementer_gate_review_merge_accept_in_that_order():
    revision = compiler.compile_workflow(task_spec())
    order = [n for n in revision.topological_order() if n != "park"]
    assert order == ["implementer", "gate", "proof_review", "merge", "accept"]


def test_the_manual_lane_merges_the_prep_only_after_dan_and_the_gate():
    """G7: without a merge the prep commits never reached the target, yet accept graduated
    the task. The graph merges after Dan's action and the gate (legacy merged the prep
    before Dan acted — a deliberate difference, DESIGN §16)."""
    revision = compiler.compile_workflow(task_spec(dispatch="manual"))
    order = [n for n in revision.topological_order() if n != "park"]
    assert order == ["prep", "human_action", "gate", "merge", "accept"]
    assert compiler.CRITERION_CHECKS["controller_merge"](list(revision.nodes))


def test_a_manual_lane_with_a_dan_check_observes_after_the_merge():
    revision = compiler.compile_workflow(task_spec(
        dispatch="manual",
        verifications=[{"id": "V-DAN", "kind": "dan", "check": "the timer fires"}],
    ))
    order = [n for n in revision.topological_order() if n != "park"]
    assert order == ["prep", "human_action", "gate", "merge", "operator_check", "accept"]
    assert compiler.CRITERION_CHECKS["controller_merge"](list(revision.nodes))


def test_the_script_lane_pays_no_proof_review():
    """B8's zero-LLM path. The legacy loop hands a script's DONE to the *unchanged* gate
    and never briefs a reviewer; a compiled lane that added one would be a new cost."""
    revision = compiler.compile_workflow(task_spec(kind="script", run="make docs"))
    assert "proof_review" not in revision.node_ids
    assert revision.node("script").params["command"] == "make docs"


def test_a_script_task_with_no_run_command_compiles_but_carries_the_emptiness():
    """The legacy loop parks a `kind: script` task with no `run:`. The graph does not
    refuse to compile — refusing would lose the task — it carries an empty command that
    the handler fails closed on, which is where the legacy park happens too."""
    revision = compiler.compile_workflow(task_spec(kind="script"))
    assert revision.node("script").params["command"] == ""


def test_the_async_lane_takes_no_writer_lease():
    """`dispatch: async-job` holds a resource label but no session and no worktree writer —
    the whole reason the legacy loop gave it its own branch."""
    revision = compiler.compile_workflow(
        task_spec(dispatch="async-job", launch_cmd="run-eval")
    )
    assert revision.node("async_launch").wants_writer_lease is False
    assert revision.node("async_launch").params["command"] == "run-eval"


def test_the_resumable_lane_routes_a_failed_gate_to_cooldown_not_to_park():
    """Lane 5's distinguishing edge: for a resumable task a failed gate is a sitting."""
    revision = compiler.compile_workflow(task_spec(resumable=True))
    targets = {
        e.target_node_id
        for e in revision.outgoing("gate")
        if e.guard_predicate == "on_failure"
    }
    assert targets == {"cooldown"}


def test_an_unknown_execution_kind_refuses_to_compile():
    spec = task_spec()
    object.__setattr__(spec, "execution_kind", "telepathy")
    with pytest.raises(compiler.WorkflowError, match="no workflow template"):
        compiler.compile_workflow(spec)


# ── the manual barrier ───────────────────────────────────────────────────────────────


def test_a_manual_prep_task_cannot_reach_accept_without_the_human_node():
    """P3 acceptance criterion: manual prep tasks cannot graduate without explicit human
    action. Asserted structurally — there is no path from prep to accept that avoids it."""
    revision = compiler.compile_workflow(task_spec(dispatch="manual"))
    assert "human_action" in revision.terminal_requirements

    reachable = {"prep"}
    changed = True
    while changed:
        changed = False
        for edge in revision.edges:
            if edge.source_node_id in reachable and edge.source_node_id != "human_action":
                if edge.target_node_id not in reachable:
                    reachable.add(edge.target_node_id)
                    changed = True
    assert "accept" not in reachable


def test_the_human_handler_has_no_success_path(repo):
    """Not "usually parks" — cannot succeed. The only discharge is a recorded receipt."""
    spec = task_spec(dispatch="manual")
    revision = compiler.compile_workflow(spec)
    node = revision.node("human_action")
    ctx = handlers.HandlerContext(
        run_id="run_x", task_id=spec.task_id, node=node, attempt_id="att_x",
        inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "att_x"),
        workspace=repo, params=node.params,
    )
    result = handlers.resolve(node.handler_ref)(ctx)
    assert result.status == "paused"
    assert result.node_phase is None
    assert result.task_status == "waiting"


# ── determinism and serialisation ────────────────────────────────────────────────────


def test_compiling_the_same_spec_twice_produces_the_identical_graph():
    """`§5.3` carry-forward needs to recognise a node across a repair, which it cannot do
    if the compiler's output drifts between runs."""
    spec = task_spec()
    first = compiler.compile_workflow(spec, workflow_id="wf_fixed")
    second = compiler.compile_workflow(spec, workflow_id="wf_fixed")
    assert first.dag_hash == second.dag_hash
    assert first == second


def test_a_revision_round_trips_through_the_json_it_is_stored_as():
    revision = compiler.compile_workflow(task_spec(), workflow_id="wf_rt")
    assert WorkflowRevision.from_json(revision.to_json()) == revision


def test_a_stored_revision_reloads_as_the_same_graph(store, repo):
    revision = compiler.compile_workflow(task_spec(), workflow_id="wf_store")
    store.register_task(revision.task_id)
    compiler.store_revision(store, revision)
    assert compiler.load_revision(store, "wf_store", 1) == revision
    assert compiler.latest_revision(store, "wf_store") == revision


def test_a_revision_is_immutable_once_stored(store):
    revision = compiler.compile_workflow(task_spec(), workflow_id="wf_once")
    store.register_task(revision.task_id)
    compiler.store_revision(store, revision)
    with pytest.raises(Exception):
        compiler.store_revision(store, revision)


def test_repair_produces_revision_n_plus_one_with_a_parent_and_a_reason():
    first = compiler.compile_workflow(task_spec(), workflow_id="wf_rep")
    second = compiler.repair(first, reason="prerequisite discovered")
    assert (second.revision, second.parent_revision) == (2, 1)
    assert second.mutation_reason == "prerequisite discovered"


def test_repair_without_a_reason_is_refused():
    first = compiler.compile_workflow(task_spec(), workflow_id="wf_rep2")
    with pytest.raises(compiler.WorkflowError, match="mutation reason"):
        compiler.repair(first, reason="")


# ── structural validation ────────────────────────────────────────────────────────────


def test_an_edge_to_an_unknown_node_is_refused():
    from maestro.workflows.models import EdgeSpec

    revision = compiler.compile_workflow(task_spec(), workflow_id="wf_bad")
    with pytest.raises(WorkflowValidationError, match="is not a node"):
        WorkflowRevision(
            **{
                **revision.to_dict(),
                "nodes": revision.nodes,
                "edges": revision.edges + (EdgeSpec("accept", "nowhere"),),
                "terminal_requirements": revision.terminal_requirements,
                "backend_eligibility": revision.backend_eligibility,
            }
        )


def test_a_cycle_is_refused():
    from maestro.workflows.models import EdgeSpec

    revision = compiler.compile_workflow(task_spec(), workflow_id="wf_cyc")
    with pytest.raises(WorkflowValidationError, match="cycle"):
        WorkflowRevision(
            **{
                **revision.to_dict(),
                "nodes": revision.nodes,
                "edges": revision.edges + (EdgeSpec("accept", "implementer"),),
                "terminal_requirements": revision.terminal_requirements,
                "backend_eligibility": revision.backend_eligibility,
            }
        )


# ── the sentinel facade ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sentinel,status,node_phase,task_status",
    [
        ("DONE", "succeeded", "succeeded", None),
        ("FAILED", "failed", "failed", None),
        ("INCOMPLETE", "incomplete", "failed", "waiting"),
        ("PAUSED", "paused", None, "waiting"),
        ("SWITCH", "handover", "uncertain", None),
    ],
)
def test_each_legacy_sentinel_maps_to_its_envelope(
    tmp_path, sentinel, status, node_phase, task_status
):
    workspace = tmp_path / "ws"
    handlers.write_sentinel(workspace, sentinel, "detail text")
    result = handlers.result_from_workspace(
        workspace, run_id="r", node_id="n", attempt_id="a"
    )
    assert (result.status, result.node_phase, result.task_status) == (
        status, node_phase, task_status
    )
    assert result.outputs["sentinel"] == sentinel


def test_a_workspace_that_has_not_spoken_yields_no_envelope(tmp_path):
    """Silence is not failure. Reading it as one is what made the legacy loop kill agents
    that were still working."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert handlers.read_sentinel(workspace).present is False
    assert handlers.result_from_workspace(
        workspace, run_id="r", node_id="n", attempt_id="a"
    ) is None


def test_a_terminal_claim_outranks_a_controller_written_switch(tmp_path):
    """A workspace can hold both: the controller asked for a handover, the implementer
    had already finished. The work that happened wins."""
    workspace = tmp_path / "ws"
    handlers.write_sentinel(workspace, "SWITCH", "to=codex")
    handlers.write_sentinel(workspace, "DONE", "DONE")
    assert handlers.read_sentinel(workspace).name == "DONE"


def test_paused_outranks_switch_but_not_a_terminal_claim(tmp_path):
    workspace = tmp_path / "ws"
    handlers.write_sentinel(workspace, "SWITCH", "to=codex")
    handlers.write_sentinel(workspace, "PAUSED", "17")
    assert handlers.read_sentinel(workspace).name == "PAUSED"


def test_synthesis_reads_a_result_document_when_no_sentinel_was_written(tmp_path):
    """The legacy zombie guard: an implementer that wrote result.json and died."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.json").write_text(json.dumps({"status": "DONE"}))
    reading = handlers.read_sentinel(workspace, synthesize=True)
    assert (reading.name, reading.synthesized) == ("DONE", True)


@pytest.mark.parametrize("status", ["BLOCKED", "NEEDS-REPLAN", "gibberish", ""])
def test_synthesis_fails_closed_on_any_non_terminal_claim(tmp_path, status):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.json").write_text(json.dumps({"status": status}))
    assert handlers.read_sentinel(workspace, synthesize=True).name == "FAILED"


def test_synthesis_fails_closed_on_an_unreadable_result_document(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.json").write_text("{ not json")
    assert handlers.read_sentinel(workspace, synthesize=True).name == "FAILED"


def test_synthesis_is_off_unless_asked_for(tmp_path):
    """Synthesising a verdict is a recovery decision. A passing reader must not make one."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.json").write_text(json.dumps({"status": "DONE"}))
    assert handlers.read_sentinel(workspace).present is False


def test_the_envelope_carries_the_result_document_verbatim(tmp_path):
    workspace = tmp_path / "ws"
    handlers.write_sentinel(workspace, "DONE")
    (workspace / "result.json").write_text(json.dumps({"summary": "did it", "tests": {"pass": True}}))
    result = handlers.result_from_workspace(
        workspace, run_id="r", node_id="n", attempt_id="a"
    )
    assert result.outputs["result"]["summary"] == "did it"


def test_writing_a_file_that_is_not_a_legacy_sentinel_is_refused(tmp_path):
    with pytest.raises(handlers.WorkflowError):
        handlers.write_sentinel(tmp_path / "ws", "MAYBE")


def test_a_done_envelope_is_a_claim_not_an_acceptance(tmp_path):
    """`D06`: the sentinel moves the node. Whether the task is accepted is decided two
    nodes later, by the controller, on frozen evidence."""
    workspace = tmp_path / "ws"
    handlers.write_sentinel(workspace, "DONE")
    result = handlers.result_from_workspace(
        workspace, run_id="r", node_id="implementer", attempt_id="a"
    )
    assert result.node_phase == "succeeded"
    assert result.source == "legacy_sentinel"
    revision = compiler.compile_workflow(task_spec())
    assert revision.terminal_requirements == ("accept",)
    assert "implementer" not in revision.terminal_requirements


# ── the reviewer's verdict (P12 pilot finding D6) ───────────────────────────────────


def _review_context(tmp_path, role="reviewer"):
    from maestro.workflows.models import NodeSpec

    node = NodeSpec(
        node_id="proof_review", kind="agent", handler_ref="maestro.handlers.agent",
        agent_definition_ref=role,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return handlers.HandlerContext(
        run_id="r", task_id="T", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(tmp_path / "attempts", "a"), workspace=workspace,
    )


def test_a_reviewer_that_approves_succeeds_and_records_its_disposition(tmp_path):
    ctx = _review_context(tmp_path)
    handlers.write_review(ctx.workspace, "approved", ["nit: a typo"])
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    assert result.status == "succeeded"
    assert result.outputs["review"]["verdict"] == "approved"
    # D16: an approval's unmarked finding is a follow-up — recorded, not blocking.
    assert result.outputs["review"]["findings"] == []
    assert result.outputs["review"]["follow_ups"] == ["nit: a typo"]


def test_only_blocking_findings_fail_a_review_and_follow_ups_are_kept(tmp_path):
    """P12 pilot finding D16 (Dan, 2026-09-26): the reviewer marks each finding blocking
    or follow_up; only blocking ones fail the node, follow-ups are recorded."""
    ctx = _review_context(tmp_path)
    handlers.write_review(ctx.workspace, "changes_requested", [
        {"finding": "runs row not written on failure", "severity": "follow_up"},
    ])
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    assert result.status == "succeeded"
    review = result.outputs["review"]
    assert review["verdict"] == "approved"
    assert review["reviewer_verdict"] == "changes_requested"
    assert review["follow_ups"] == ["runs row not written on failure"]

    (tmp_path / "second").mkdir()
    ctx = _review_context(tmp_path / "second")
    handlers.write_review(ctx.workspace, "approved", [
        {"finding": "unbounded Retry-After sleep", "severity": "blocking"},
        {"finding": "SQL outside db.py", "severity": "follow_up"},
        "an unmarked finding",
    ])
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    assert result.status == "failed"
    assert result.outputs["failure"]["kind"] == "verification_failed"
    review = result.outputs["review"]
    assert review["verdict"] == "changes_requested"
    assert review["findings"] == ["unbounded Retry-After sleep"]
    assert review["follow_ups"] == ["SQL outside db.py", "an unmarked finding"]


def test_a_rejection_s_unmarked_findings_are_blocking(tmp_path):
    ctx = _review_context(tmp_path)
    handlers.write_review(ctx.workspace, "changes_requested", [
        "no severity given", {"finding": "odd severity", "severity": "maybe"},
    ])
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    assert result.status == "failed"
    assert result.outputs["review"]["findings"] == ["no severity given", "odd severity"]
    assert result.outputs["review"]["follow_ups"] == []


@pytest.mark.parametrize("verdict", ["changes_requested", "rejected"])
def test_a_reviewer_that_rejects_fails_the_work_with_its_findings(tmp_path, verdict):
    """D6: DuetFlow 02's reviewer wrote DONE describing four P1 defects, and the graph
    merged it, because DONE was all it read."""
    ctx = _review_context(tmp_path)
    handlers.write_review(ctx.workspace, verdict, ["collect CLI is a stub"])
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    assert result.status == "failed"
    assert result.outputs["failure"]["kind"] == "verification_failed"
    assert "collect CLI is a stub" in result.outputs["failure"]["evidence"]
    assert result.outputs["review"]["findings"] == ["collect CLI is a stub"]


@pytest.mark.parametrize("document", [
    None,                                            # DONE and no verdict at all
    "{ not json",
    json.dumps({"verdict": "looks fine"}),
    json.dumps({"verdict": "rejected", "findings": []}),
])
def test_an_inconclusive_review_fails_closed(tmp_path, document):
    """`[INV-04]`: no verdict is not an approval."""
    ctx = _review_context(tmp_path)
    if document is not None:
        (ctx.workspace / handlers.REVIEW_FILE).write_text(document)
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    assert result.status == "failed"
    assert result.outputs["failure"]["kind"] == "garbage_output"


def test_a_non_reviewer_agent_needs_no_verdict(tmp_path):
    ctx = _review_context(tmp_path, role="implementer")
    handlers.write_sentinel(ctx.workspace, "DONE")
    assert handlers.agent_handler(ctx).status == "succeeded"
