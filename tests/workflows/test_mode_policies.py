"""`[P7]`: one task spec, three modes, and no paid token spent to get free-mode quality.

The four things `§3`'s acceptance condition asks for, each pinned once:

* **One spec, three modes.** The same `TaskSpec` compiles and validates under paid
  efficiency, free quality, and the hybrid overlay. Not "compiles" in the weak sense —
  `compile_workflow` runs `check_criteria`, so a mode whose composed graph came up short
  would raise here rather than produce a thinner graph.
* **Zero paid-token penalty.** Free mode buys extra nodes; every one of them must bind to
  free-tier capacity. The test counts nodes whose *binding* carries a paid billing category,
  and that count may not grow when the graph does. Counting nodes would prove nothing —
  the claim `D05` makes is about spend, not about topology.
* **Hybrid is an overlay, not an objective.** `EXECUTION_OBJECTIVES` still has two members;
  the named role gets paid capacity and every other role stays on the free tier.
* **Zero paid capacity in free mode never launches a subscription call.** With only a
  subscription backend catalogued, free mode returns `RouteUnavailable` — it does not fall
  back, and there is no route by which it could.
"""
from __future__ import annotations

import dataclasses

import pytest

from maestro.backends import catalog as catalog_module
from maestro.backends import router
from maestro.workflows import compiler
from maestro.workflows.models import EXECUTION_OBJECTIVES
from maestro.workflows.policy import HybridPolicy, default_policy

from .conftest import task_spec

MODES = ("paid_efficiency", "free_quality")


@pytest.fixture
def policy():
    return default_policy()


@pytest.fixture
def spec():
    """One medium-risk ordinary task — the shape free mode is supposed to explore."""
    return task_spec(risk="medium")


# ── the catalogue the three modes route against ──────────────────────────────────────


def _entry(backend_id: str, billing: str, **overrides) -> catalog_module.BackendCapabilityEntry:
    """One catalogued backend. Strong, sandboxed and roomy, so the only thing that can
    disqualify it in these tests is its billing category — which is the whole subject."""
    body = dict(
        backend_id=backend_id,
        runtime_kind=(
            "model_endpoint_harness" if billing == "free_tier_api" else "agent_cli"
        ),
        installed_version="1.0.0",
        billing_category=billing,
        usage_pool_id=f"{backend_id}-pool",
        available_models=("m-strong",),
        effort_controls=catalog_module.EffortControls(
            supported=True, delivery_mechanism="flag",
            native_values=("low", "medium", "high"),
        ),
        context_limits=catalog_module.ContextLimits(400_000, 64_000, 300_000),
        model_profiles={'m-strong': catalog_module.ModelProfile('m-strong')},
        pair_profiles=catalog_module.migrate_pairs(('m-strong', 'strong', 0.0001 if billing == 'free_tier_api' else 3.0)),
        pairs_migrated=True,
        permission_enforcement="physical_sandbox",
        concurrency_limit=8,
    )
    body.update(overrides)
    return catalog_module.BackendCapabilityEntry(**body)


@pytest.fixture
def fixture_catalog():
    """Free-tier and subscription capacity side by side, so a mode can pick wrong."""
    return catalog_module.CapabilityCatalog.of(
        [_entry("free-api", "free_tier_api"), _entry("paid-cli", "subscription")]
    )


def _binding(node, policy, catalog):
    return router.resolve_agent(
        node, {"task_class": "implementation"}, None, catalog, policy
    )


def _paid_bindings(revision, policy, catalog) -> list[str]:
    """Node ids whose resolved binding spends paid capacity."""
    paid = []
    for node in revision.nodes:
        if node.kind != "agent":
            continue
        binding = _binding(node, policy, catalog)
        if binding.is_bound and binding.billing_category != "free_tier_api":
            paid.append(node.node_id)
    return paid


# ── (a) the identical task spec succeeds across paid, free and hybrid ────────────────


@pytest.mark.parametrize("objective", MODES)
def test_the_identical_task_spec_compiles_and_validates_under_either_objective(
    spec, policy, objective
):
    revision = compiler.compile_workflow(
        spec, policy=policy.with_objective(objective), execution_objective=objective
    )
    assert revision.execution_objective == objective
    # `compile_workflow` ran `check_criteria`; reaching here means the composed graph met
    # this mode's floor, fan-out criteria included.
    assert "accept" in revision.node_ids


def test_the_identical_task_spec_compiles_under_the_hybrid_overlay(spec, policy):
    hybrid = dataclasses.replace(
        policy.with_objective("free_quality"), hybrid=HybridPolicy(paid_roles=("planner",))
    )
    revision = compiler.compile_workflow(spec, policy=hybrid)
    free = compiler.compile_workflow(spec, policy=policy.with_objective("free_quality"))
    # Hybrid is an eligibility overlay, so the *graph* is the free-mode graph unchanged.
    # Only the routing of one role differs, which is the next test.
    assert revision.node_ids == free.node_ids


def test_hybrid_is_not_a_third_execution_objective():
    # W5 adds `pace` as a routing objective; hybrid remains an overlay, not an objective.
    assert "hybrid" not in EXECUTION_OBJECTIVES
    assert set(EXECUTION_OBJECTIVES) == {"paid_efficiency", "free_quality", "pace"}


# ── (b) free compilation has zero paid-token penalty ─────────────────────────────────


def test_free_mode_buys_more_nodes_than_paid_mode(spec, policy):
    paid = compiler.compile_workflow(spec, policy=policy.with_objective("paid_efficiency"))
    free = compiler.compile_workflow(spec, policy=policy.with_objective("free_quality"))
    assert len(free.nodes) > len(paid.nodes)
    # The extra inference is the point: candidates, a join, a planner, a test designer and
    # one reviewer per lens.
    assert {"implementer_c1", "implementer_c2", "candidate_select"} <= set(free.node_ids)
    assert len([n for n in free.node_ids if n.startswith("proof_review_")]) >= 2


def test_every_extra_node_free_mode_composes_binds_to_free_tier_capacity(
    spec, policy, fixture_catalog
):
    """`D05`: extra inference, zero token-cost penalty.

    The assertion is on the bindings, not the node count — a free-mode graph that doubled
    its nodes and routed one of them to the subscription would have spent exactly what
    free mode exists not to spend.
    """
    free_policy = policy.with_objective("free_quality")
    free = compiler.compile_workflow(spec, policy=free_policy)
    assert _paid_bindings(free, free_policy, fixture_catalog) == []


def test_free_mode_adds_no_paid_binding_to_the_paid_mode_baseline(
    spec, policy, fixture_catalog
):
    paid_policy = policy.with_objective("paid_efficiency")
    free_policy = policy.with_objective("free_quality")
    paid = compiler.compile_workflow(spec, policy=paid_policy)
    free = compiler.compile_workflow(spec, policy=free_policy)
    before = len(_paid_bindings(paid, paid_policy, fixture_catalog))
    after = len(_paid_bindings(free, free_policy, fixture_catalog))
    assert after <= before, (
        "free mode grew the count of nodes bound to paid capacity — that is the token-cost "
        "penalty D05 rules out"
    )
    assert after == 0


# ── (c) hybrid grants paid capacity to named roles only (`§3.1`) ─────────────────────


def test_the_hybrid_overlay_grants_paid_capacity_to_the_named_role_only(
    spec, policy, fixture_catalog
):
    hybrid_policy = dataclasses.replace(
        policy.with_objective("free_quality"), hybrid=HybridPolicy(paid_roles=("planner",))
    )
    revision = compiler.compile_workflow(spec, policy=hybrid_policy)
    planner = revision.node("planner")
    implementer = revision.node("implementer_c1")

    planner_billing = router._permitted_billing(hybrid_policy, {}, "planner")
    implementer_billing = router._permitted_billing(hybrid_policy, {}, "implementer")
    assert "subscription" in planner_billing
    assert "subscription" not in implementer_billing

    # And end to end: with both tiers catalogued the planner may take the subscription,
    # while the implementer candidates may not.
    assert _binding(planner, hybrid_policy, fixture_catalog).is_bound
    assert _binding(implementer, hybrid_policy, fixture_catalog).billing_category == (
        "free_tier_api"
    )


def test_pure_free_mode_grants_no_paid_capacity_to_any_role(policy):
    free_policy = policy.with_objective("free_quality")
    assert free_policy.hybrid.paid_roles == ()
    for role in ("planner", "implementer", "reviewer", "test_designer", "diagnoser"):
        assert router._permitted_billing(free_policy, {}, role) == frozenset({"free_tier_api"})


def test_the_hybrid_overlay_does_not_widen_paid_mode(policy):
    """`§3.1`: hybrid reserves paid capacity under the free objective. Paid mode has none
    to reserve, so the overlay must not narrow or widen what paid mode already permits."""
    paid = dataclasses.replace(
        policy.with_objective("paid_efficiency"), hybrid=HybridPolicy(paid_roles=("planner",))
    )
    assert router._permitted_billing(paid, {}, "planner") == router.OBJECTIVE_BILLING[
        "paid_efficiency"
    ]


# ── (d) zero paid capacity in free mode never launches a subscription call ───────────


def test_free_mode_with_only_subscription_capacity_waits_rather_than_spending(spec, policy):
    subscription_only = catalog_module.CapabilityCatalog.of([_entry("paid-cli", "subscription")])
    free_policy = policy.with_objective("free_quality")
    revision = compiler.compile_workflow(spec, policy=free_policy)
    for node in revision.nodes:
        if node.kind != "agent":
            continue
        outcome = _binding(node, free_policy, subscription_only)
        assert not outcome.is_bound, f"{node.node_id} was bound to subscription capacity"
        assert outcome.reason == "no_eligible_route"
        assert any(
            rejected.reason == "billing_not_permitted" for rejected in outcome.rejected_routes
        )
