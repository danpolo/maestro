"""`maestro.workflows.compiler` under a project policy (`[P5]`).

`test_compatibility.py` already pins what the compiler does with no policy at all: the
five baseline lanes, node for node. This module pins what a *policy* changes and, just as
importantly, what it may not:

* **Determinism.** Identical inputs produce identical compiled content hashes. Without
  that, `§5.3`'s carry-forward cannot recognise a node across a plan repair, and a
  `dag_hash` stops being evidence of anything.
* **Fragment composition** (`[INV-A06]`). Templates cite `name@version`; a stale citation
  is a compile-time refusal, not a silently different node.
* **The minimal shape that satisfies the criteria** (`D05`). A low-risk task in paid mode
  gets no planner and no reviewer; a high-risk one cannot compile without an independent
  review, whatever the template's cost tuning says.

No database, no worktree, no subprocess.
"""
from __future__ import annotations

import dataclasses

import pytest

from maestro.taskgraph import normalize_spec
from maestro.workflows import compiler
from maestro.workflows.models import WorkflowError
from maestro.workflows.policy import (
    FragmentRef,
    PolicyError,
    ProjectPolicy,
    QualityGates,
    RiskContext,
    StageSpec,
    TemplateSpec,
    default_policy,
)


@pytest.fixture
def policy() -> ProjectPolicy:
    return default_policy()


def spec_for(task_id: str = "T-001", *, self_modifying: bool = False, **fields):
    block = {"id": task_id, "title": f"{task_id} title", "status": "pending"}
    block.update(fields)
    spec = normalize_spec(block)
    return dataclasses.replace(spec, self_modifying=True) if self_modifying else spec


def nodes_of(spec, policy=None, **kwargs) -> tuple[str, ...]:
    return compiler.compile_workflow(spec, policy=policy, **kwargs).node_ids


def test_fallback_refusal_needs_a_gate_node_and_authoritative_specs():
    """P12B F5: a step-down is only honoured when the weaker model's work is checked."""
    spec = spec_for(fallback="step_down")
    revision = compiler.compile_workflow(spec)
    assert any(n.handler_ref == "maestro.handlers.verification" for n in revision.nodes)
    assert compiler.fallback_refusal(spec, revision, has_authoritative_specs=True) is None
    assert "verifications" in compiler.fallback_refusal(
        spec, revision, has_authoritative_specs=False)

    # A structurally valid gateless revision is not what the compiler emits; the check
    # only reads `nodes`, so a stand-in carrying them is enough.
    no_gate = type("Revision", (), {"nodes": tuple(
        n for n in revision.nodes if n.handler_ref != "maestro.handlers.verification")})()
    assert "verification gate" in compiler.fallback_refusal(
        spec, no_gate, has_authoritative_specs=True)

    flagless = spec_for()
    assert compiler.fallback_refusal(flagless, revision, has_authoritative_specs=False) is None
    assert compiler.compile_workflow(flagless).compiler_version == compiler.compile_workflow(
        spec).compiler_version == "p7.1", "the flag does not change compiler output"


# ── determinism ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize('escalation', [False, True])
def test_task_counted_minima_reach_every_compiled_attempt(policy, escalation):
    from maestro.scheduling import ResourceRequest

    spec = spec_for(resources={'cpu': 2, 'ram_mb': 512, 'vram_mb': 32})
    baseline = compiler.compile_workflow(spec_for(), policy=policy, escalation=escalation)
    revision = compiler.compile_workflow(spec, policy=policy, escalation=escalation)
    for node, original in zip(revision.nodes, baseline.nodes):
        request = ResourceRequest.from_node(node, task_id=spec.task_id)
        assert {key: request.counted[key] for key in spec.resources} == spec.resources
        assert {key: value for key, value in node.resource_request.items()
                if key not in spec.resources} == original.resource_request


def test_task_resources_preserve_larger_fragment_minima(policy, monkeypatch):
    original = compiler.FRAGMENTS['implementer']
    build = original.build
    monkeypatch.setitem(compiler.FRAGMENTS, 'implementer', dataclasses.replace(
        original, build=lambda spec, node_id: dataclasses.replace(
            build(spec, node_id=node_id), resource_request={'ram_mb': 1024, 'writer_lease': True})))
    revision = compiler.compile_workflow(spec_for(resources={'ram_mb': 512}), policy=policy)
    assert revision.node('implementer').resource_request == {'ram_mb': 1024, 'writer_lease': True}


def test_identical_inputs_compile_to_identical_content_hashes(policy):
    spec = spec_for(risk="high")
    first = compiler.compile_workflow(spec, workflow_id="wf_a", policy=policy)
    second = compiler.compile_workflow(spec, workflow_id="wf_a", policy=policy)
    assert first.dag_hash == second.dag_hash


def test_only_the_workflow_id_distinguishes_two_compilations(policy):
    spec = spec_for()
    a = compiler.compile_workflow(spec, workflow_id="wf_a", policy=policy)
    b = compiler.compile_workflow(spec, workflow_id="wf_b", policy=policy)
    assert a.dag_hash != b.dag_hash
    assert dataclasses.replace(b, workflow_id="wf_a").dag_hash == a.dag_hash


def test_a_changed_policy_changes_the_stamped_policy_hash(policy):
    spec = spec_for()
    relaxed = dataclasses.replace(
        policy, quality_gates=QualityGates(review_required_when=())
    )
    assert (
        compiler.compile_workflow(spec, policy=policy).policy_hash
        != compiler.compile_workflow(spec, policy=relaxed).policy_hash
    )


def test_a_plain_mapping_policy_still_hashes_the_way_it_did_before_p5():
    spec = spec_for()
    revision = compiler.compile_workflow(spec, policy={"a": 1})
    assert revision.policy_hash == compiler.policy_hash({"a": 1})
    assert revision.node_ids == compiler.compile_workflow(spec).node_ids


# ── fragment composition (`[INV-A06]`) ───────────────────────────────────────────────


def test_every_fragment_the_packaged_policy_cites_resolves(policy):
    for template in policy.templates.values():
        for stage in (*template.stages, *template.sinks):
            assert compiler.resolve_fragment(stage.fragment).name == stage.fragment.name


def test_a_template_citing_a_stale_fragment_version_refuses_to_compile(policy):
    stale = dataclasses.replace(
        policy.template("ordinary"),
        stages=tuple(
            dataclasses.replace(s, fragment=FragmentRef(s.fragment.name, "0.9.0"))
            if s.fragment.name == "gate" else s
            for s in policy.template("ordinary").stages
        ),
    )
    with pytest.raises(WorkflowError, match="version"):
        compiler.compose(spec_for(), stale)


def test_an_unknown_fragment_name_refuses_to_compile():
    template = TemplateSpec(
        execution_kind="ordinary", version="1.0.0",
        stages=(StageSpec(fragment=FragmentRef("teleporter", "1.0.0")),),
    )
    with pytest.raises(WorkflowError, match="unknown workflow fragment"):
        compiler.compose(spec_for(), template)


def test_the_baseline_lanes_are_themselves_composed_from_fragments():
    # One composer, not two: if the baseline lanes were still written out longhand there
    # would be a second code path for a lane to drift along.
    for template in compiler.BASELINE_TEMPLATES.values():
        for stage in (*template.stages, *template.sinks):
            assert stage.fragment.name in compiler.FRAGMENTS


def test_one_fragment_can_appear_under_a_different_node_id(policy):
    # `manual`'s `prep` is an implementer fragment. Composition renames the node; it does
    # not fork the shape.
    revision = compiler.compile_workflow(spec_for(dispatch="manual"), policy=policy)
    assert "prep" in revision.node_ids
    assert revision.node("prep").agent_definition_ref == "implementer"


# ── the minimal shape (`D05`) ────────────────────────────────────────────────────────


def test_a_low_risk_task_omits_planner_and_reviewer_in_paid_efficiency_mode(policy):
    ids = nodes_of(spec_for(risk="low"), policy)
    assert "planner" not in ids and "proof_review" not in ids
    assert ids == ("implementer", "gate", "merge", "accept", "park")


def test_a_high_risk_task_gets_both_a_planner_and_a_reviewer(policy):
    ids = nodes_of(spec_for(risk="high"), policy)
    assert ids == ("planner", "implementer", "gate", "proof_review", "merge", "accept", "park")


def test_a_low_risk_task_that_touches_tests_still_gets_a_reviewer(policy):
    ids = nodes_of(spec_for(risk="low", scope=["tests/test_thing.py"]), policy)
    assert "proof_review" in ids
    assert "planner" not in ids


def test_free_quality_mode_buys_planner_test_designer_and_reviewer_on_a_docs_task(policy):
    ids = nodes_of(spec_for(task_class="documentation"), policy.with_objective("free_quality"))
    assert ids == (
        "planner", "test_designer", "implementer", "gate", "proof_review",
        "merge", "accept", "park",
    )


def test_low_risk_alone_no_longer_withholds_free_candidates(policy):
    # B23: risk means real outside cost, so only the docs class skips the fan-out.
    ids = nodes_of(spec_for(risk="low"), policy.with_objective("free_quality"))
    assert "implementer_c1" in ids


def test_the_test_designer_runs_before_the_implementer(policy):
    # A medium-risk task in free mode fans the implementer out ([P7]), so the assertion is
    # against the first candidate: what must hold is that no implementer of any width sees
    # the tests before they are designed.
    ids = nodes_of(spec_for(), policy.with_objective("free_quality"))
    assert ids.index("test_designer") < min(
        index for index, node_id in enumerate(ids) if node_id.startswith("implementer")
    )


def test_the_execution_objective_defaults_to_the_policys_own(policy):
    revision = compiler.compile_workflow(spec_for(), policy=policy.with_objective("free_quality"))
    assert revision.execution_objective == "free_quality"


def test_an_explicit_objective_overrides_the_policys(policy):
    revision = compiler.compile_workflow(
        spec_for(risk="low"), policy=policy, execution_objective="free_quality"
    )
    assert revision.execution_objective == "free_quality"
    assert "test_designer" in revision.node_ids


def test_a_sink_nothing_routes_to_is_not_emitted(policy):
    # `resumable` declares two sinks. A lane that never routes to one of them should not
    # carry a wait node the runner would have to explain.
    template = dataclasses.replace(
        policy.template("resumable"),
        stages=tuple(
            dataclasses.replace(s, on_failure="park") for s in policy.template("resumable").stages
        ),
    )
    nodes, _, _ = compiler.compose(spec_for(resumable=True), template)
    assert "cooldown" not in [n.node_id for n in nodes]


def test_the_resumable_lane_still_cools_down_rather_than_parking_a_failed_gate(policy):
    revision = compiler.compile_workflow(spec_for(resumable=True), policy=policy)
    failure = {
        e.target_node_id for e in revision.edges
        if e.source_node_id == "gate" and e.guard_predicate == "on_failure"
    }
    assert failure == {"cooldown"}


# ── the floor: criteria are checked against what was built ───────────────────────────


def test_a_high_risk_task_cannot_compile_without_an_independent_review(policy):
    # The cost tuning is turned all the way down — the reviewer stage is unreachable —
    # but the gate is a floor, so the graph does not compile at all.
    ordinary = policy.template("ordinary")
    starved = dataclasses.replace(
        ordinary,
        stages=tuple(
            dataclasses.replace(s, include_when=("never",)) if s.node_id == "proof_review" else s
            for s in ordinary.stages
        ),
    )
    tuned = dataclasses.replace(policy, templates={**policy.templates, "ordinary": starved})
    assert nodes_of(spec_for(risk="low"), tuned)  # a low-risk task is unaffected
    with pytest.raises(WorkflowError, match="independent_review"):
        compiler.compile_workflow(spec_for(risk="high"), policy=tuned)


def test_a_reviewer_that_holds_the_writer_lease_does_not_satisfy_independent_review():
    reviewer = compiler._proof_review_node(spec_for())
    compromised = dataclasses.replace(
        reviewer, resource_request={"writer_lease": True, "inference_slots": 1}
    )
    assert compiler.CRITERION_CHECKS["independent_review"]([reviewer]) is True
    assert compiler.CRITERION_CHECKS["independent_review"]([compromised]) is False


def test_a_lane_that_lost_its_verification_gate_refuses_to_compile(policy):
    ordinary = policy.template("ordinary")
    gateless = dataclasses.replace(
        ordinary,
        stages=tuple(s for s in ordinary.stages if s.node_id != "gate"),
    )
    with pytest.raises(WorkflowError, match="verification_gate"):
        compiler.compile_workflow(
            spec_for(),
            policy=dataclasses.replace(
                policy, templates={**policy.templates, "ordinary": gateless}
            ),
        )


def test_the_manual_lane_cannot_reach_acceptance_without_a_human_receipt(policy):
    revision = compiler.compile_workflow(spec_for(dispatch="manual"), policy=policy)
    assert "human_action" in revision.terminal_requirements
    assert compiler.CRITERION_CHECKS["human_receipt"](list(revision.nodes)) is True


def test_the_default_policys_manual_lane_merges_after_the_gate(policy):
    # G7: the packaged policy is what a project without its own workflows/project.yaml
    # compiles against (DuetFlow). It must carry the merge the baseline lane gained.
    revision = compiler.compile_workflow(spec_for(dispatch="manual", risk="low"), policy=policy)
    order = [n for n in revision.topological_order() if n != "park"]
    assert order == ["prep", "human_action", "gate", "merge", "accept"]


def test_an_unknown_criterion_is_a_refusal_not_a_pass():
    with pytest.raises(WorkflowError, match="unknown required criterion"):
        compiler.check_criteria(spec_for(), [], ["looks_good_to_me"])


def test_an_execution_kind_the_policy_does_not_cover_is_refused(policy):
    reduced = dataclasses.replace(
        policy, templates={k: v for k, v in policy.templates.items() if k != "script"}
    )
    with pytest.raises(PolicyError, match="no template for execution_kind"):
        compiler.compile_workflow(spec_for(kind="script", run="true"), policy=reduced)


# ── permissions and roles ────────────────────────────────────────────────────────────


def test_the_policys_permission_boundary_is_stamped_onto_each_role_node(policy):
    relabelled = dataclasses.replace(
        policy, permissions={**policy.permissions, "reviewer": "sealed_reader"}
    )
    revision = compiler.compile_workflow(spec_for(risk="high"), policy=relabelled)
    assert revision.node("proof_review").permissions_ref == "sealed_reader"
    assert revision.node("implementer").permissions_ref == "task_worktree_writer"


def test_no_agent_node_carries_a_role_the_policy_cannot_describe(policy):
    for kind_fields in ({}, {"kind": "script", "run": "true"}, {"dispatch": "manual"},
                        {"dispatch": "async-job", "launch_cmd": "x"}, {"resumable": True}):
        revision = compiler.compile_workflow(
            spec_for(risk="high", **kind_fields), policy=policy.with_objective("free_quality")
        )
        for node in revision.nodes:
            if node.agent_definition_ref:
                assert policy.definition(node.agent_definition_ref)


def test_the_planner_and_the_reviewer_never_take_a_writer_lease(policy):
    revision = compiler.compile_workflow(
        spec_for(risk="high"), policy=policy.with_objective("free_quality")
    )
    reading = [
        node for node in revision.nodes
        if node.agent_definition_ref in ("planner", "reviewer")
    ]
    # One planner and one reviewer per lens ([P7]) — every one of them read-only.
    assert {node.agent_definition_ref for node in reading} == {"planner", "reviewer"}
    for node in reading:
        assert node.wants_writer_lease is False


def test_the_test_designer_does_not_contend_for_the_implementers_worktree(policy):
    revision = compiler.compile_workflow(
        spec_for(), policy=policy.with_objective("free_quality")
    )
    designer = revision.node("test_designer")
    assert designer.wants_writer_lease is False
    assert designer.permissions_ref == "scratch_workspace_writer"


def test_composition_without_a_policy_still_produces_the_baseline_lane():
    assert nodes_of(spec_for()) == ("implementer", "gate", "proof_review", "merge", "accept", "park")


def test_composing_an_empty_stage_list_is_a_refusal():
    template = TemplateSpec(
        execution_kind="ordinary", version="1.0.0",
        stages=(StageSpec(fragment=FragmentRef("gate", "1.0.0"), include_when=("never",)),),
    )
    with pytest.raises(WorkflowError, match="composed no stages"):
        compiler.compose(spec_for(), template, context=RiskContext(task_id="T-001"))


# ── P12B F7: the escalation compile ──────────────────────────────────────────────────
#
# `escalation=True` is the graph loop's own compile, and the only one that grows a
# `diagnose` stage. Everything else — replay, compatibility, tooling that re-compiles a
# spec to compare it — keeps the graph P7 emitted, which is why the version is stamped
# per-compile rather than bumped outright.


def _failure_edges(revision) -> set:
    return {
        (e.source_node_id, e.target_node_id)
        for e in revision.edges if e.guard_predicate == "on_failure"
    }


def test_an_unescalated_compile_is_byte_identical_and_still_stamped_p7_1(policy):
    for fields in ({}, {"kind": "script"}, {"kind": "resumable"},
                   {"dispatch": "async-job"}, {"dispatch": "manual"}):
        spec = spec_for(**fields)
        plain = compiler.compile_workflow(spec, workflow_id="wf_a", policy=policy)
        default = compiler.compile_workflow(
            spec, workflow_id="wf_a", policy=policy, escalation=False
        )
        assert plain.dag_hash == default.dag_hash
        assert plain.compiler_version == compiler.BASELINE_COMPILER_VERSION == "p7.1"
        assert "diagnose" not in plain.node_ids


def test_escalation_re_points_agent_and_gate_failures_at_diagnose(policy):
    revision = compiler.compile_workflow(
        spec_for(), workflow_id="wf_a", policy=policy, escalation=True
    )
    assert revision.compiler_version == compiler.COMPILER_VERSION == "p12b.1"
    assert "diagnose" in revision.node_ids
    edges = _failure_edges(revision)
    assert ("implementer", "diagnose") in edges
    assert ("gate", "diagnose") in edges
    assert ("diagnose", "park") in edges
    assert not any(target == "park" for source, target in edges if source != "diagnose")

    diagnose = revision.node("diagnose")
    assert diagnose.kind == "agent"
    assert diagnose.agent_definition_ref == "diagnoser"
    assert diagnose.permissions_ref == "frozen_snapshot_reader"
    assert diagnose.resource_request["writer_lease"] is False
    assert diagnose.cache_policy == "never", "a diagnosis is never served from a cache"
    assert diagnose.output_schema_ref == "PlanRepairProposal.v1"


def test_a_script_source_and_a_cooldown_target_keep_their_direct_park_edge():
    """A mechanical failure and a schedule are not quality verdicts.

    Read off the baseline lanes, which are the ones that still carry a `script` node and
    the `cooldown` sink — a project policy's lanes are its own shape.
    """
    script = compiler.compile_workflow(
        spec_for(kind="script"), workflow_id="wf_a", escalation=True
    )
    assert ("script", "park") in _failure_edges(script)
    assert ("gate", "diagnose") in _failure_edges(script)

    resumable = compiler.compile_workflow(
        spec_for(kind="resumable"), workflow_id="wf_a", escalation=True,
    )
    assert ("gate", "cooldown") in _failure_edges(resumable)
    assert ("implementer", "diagnose") in _failure_edges(resumable)


def test_a_lane_with_nothing_to_escalate_grows_no_diagnose_node():
    """The node is added only when at least one edge was actually re-targeted."""
    revision = compiler.compile_workflow(
        spec_for(dispatch="async-job"), workflow_id="wf_a", escalation=True
    )
    assert ("async_launch", "park") in _failure_edges(revision), \
        "an async launch that fails has failed mechanically"
    # That lane still has a gate, so it does escalate. One whose only failing sources are
    # non-agent grows no node at all.
    template = compiler.BASELINE_TEMPLATES["script"]
    script_spec = spec_for(kind="script")
    nodes, edges, _ = compiler.compose(
        script_spec, template,
        context=RiskContext(task_id="T-001", execution_kind="script"),
    )
    script_only = [e for e in edges if e.source_node_id == "script"]
    nodes_out, edges_out = compiler.escalate(script_spec, nodes, script_only)
    assert [n.node_id for n in nodes_out] == [n.node_id for n in nodes]
    assert edges_out == list(script_only)
