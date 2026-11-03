"""P6 §2: `resolve_agent()`'s pipeline, one step at a time.

`tests/test_routing.py` is the acceptance suite — the three conditions §3 names. This file
is the unit level underneath it: hard-constraint filtering, demand estimation, the joint
model/effort choice, effort delivery mapping, and admission.
"""
from __future__ import annotations

import pytest

from maestro.backends import catalog as cat
from maestro.backends import router as rt
from maestro.workflows.models import NodeSpec
from maestro.workflows.policy import AgentDefinition


def pair_catalog(*backends):
    """Approved current pairs and observed five-level harness controls."""
    from maestro import model_policy, thirdparty
    defaults = model_policy.catalog_defaults(model_policy.shipped_policy(), {})
    built = cat.catalog_from_register(thirdparty.load_register(), defaults=defaults)
    return cat.CapabilityCatalog.of([built.entry(name) for name in backends])


def resolve_role_pair(role="implementer", backend="claude", **overrides):
    from maestro.model_policy import shipped_policy
    from maestro.workflows.policy import default_policy
    return resolve(node=agent_node(node_id=role, agent_definition_ref=role),
                   catalog=pair_catalog(backend), policy=default_policy(),
                   role_requirement=shipped_policy()["roles"][role], **overrides)


@pytest.mark.parametrize("role", ["planner", "diagnoser"])
def test_pair_role_minimum_preserves_high_effort(role):
    binding = resolve_role_pair(role)
    assert binding.is_bound
    assert (binding.model_id, binding.requested_effort) == ("claude-opus-5-5", "high")
    assert any(r.reason == "effort_below_minimum" for r in binding.rejected_routes)


def test_pair_preference_uses_the_in_pool_near_cost_band():
    binding = resolve_role_pair()
    assert (binding.model_id, binding.requested_effort) == ("claude-sonnet-5-5", "medium")
    binding = resolve_role_pair(backend="codex")
    assert (binding.model_id, binding.requested_effort) == ("gpt-6.1-sol", "low")
    binding = resolve_role_pair(near_cost_margin=0.0)
    assert (binding.model_id, binding.requested_effort) == ("claude-opus-5-5", "low")


@pytest.mark.parametrize("context", [rt.RoutingContext(risk="high"),
                                     rt.RoutingContext(historical_failure_rate=0.5),
                                     rt.RoutingContext(task_class="difficult_debugging")])
def test_pair_risk_escalates_intelligence_only(context):
    binding = resolve_role_pair(context_manifest=context)
    assert (binding.model_id, binding.requested_effort) == ("claude-opus-5-5", "medium")


@pytest.mark.parametrize("opus_rate, expected", [(0.9, "claude-opus-5-5"),
                                                (0.75, "claude-sonnet-5-5")])
def test_pair_preference_yields_only_to_meaningful_measured_evidence(opus_rate, expected):
    stats = cat.OutcomeStatistics(minimum_observations=5)
    for model, rate in [("claude-opus-5-5", opus_rate), ("claude-sonnet-5-5", 0.7)]:
        effort = "low" if "opus" in model else "medium"
        shape = cat.ShapeKey(backend_id="claude", model_id=model, effort=effort,
                             role_version="1.0.0", task_class="ordinary", template_id="",
                             template_version="", active_fragment_set=())
        for i in range(20):
            stats.record(shape, success=i < round(20 * rate))
    assert resolve_role_pair(statistics=stats).model_id == expected


def test_pair_numerical_floor_and_config_strength_never_lower_requirements():
    binding = resolve_role_pair("reviewer")
    assert (binding.model_id, binding.requested_effort) == ("claude-opus-5-5", "medium")
    assert any(r.reason == "intelligence_below_demand" and "47 < 48" in r.detail
               for r in binding.rejected_routes)
    raised = resolve_role_pair(role_strength="strong")
    assert raised.model_id == "claude-opus-5-5" and raised.requested_effort == "medium"
    demand = rt.estimate_demand(agent_node(), rt.RoutingContext(risk="low"), Policy(),
                                role_strength="light",
                                role_requirement={"min_intelligence": 50, "effort": "high",
                                                  "effort_rule": "minimum"})
    assert demand.min_intelligence == 50 and demand.min_strength == "strong"


def test_pair_unsatisfied_minimum_blocks_without_an_effort_substitution():
    from dataclasses import replace
    base = pair_catalog("claude").entry("claude")
    for effort, reason in [("medium", "effort_below_minimum"), ("ultra", "no_approved_pair")]:
        # Ultra is deliberately not in this harness's observed vocabulary.
        pair = cat.PairProfile.from_score("claude-opus-5-5", effort, 51, 0.1)
        entry = replace(base, available_models=(pair.model_id,),
                        model_profiles={pair.model_id: base.profile(pair.model_id)},
                        pair_profiles={(pair.model_id, effort): pair})
        from maestro.workflows.policy import default_policy
        outcome = resolve(node=agent_node(agent_definition_ref="planner"),
                          policy=default_policy(), catalog=catalog_of(entry),
                          role_requirement={"min_intelligence": 50, "effort": "high",
                                            "effort_rule": "minimum"})
        assert not outcome.is_bound and outcome.disposition == "block"
        assert outcome.reason == "no_eligible_route"
        assert outcome.rejected_routes[0].reason == reason


def test_pair_equal_candidates_have_a_stable_order_and_deliver_exact_effort():
    from dataclasses import replace
    base = pair_catalog("claude").entry("claude")
    pairs = [cat.PairProfile.from_score(model, "medium", 41, 0.5)
             for model in ("claude-sonnet-5-5", "claude-opus-5-5")]
    for iteration in range(50):
        ordered = pairs if iteration % 2 else list(reversed(pairs))
        entry = replace(base, available_models=tuple(pair.model_id for pair in ordered),
                        model_profiles={p.model_id: base.profile(p.model_id) for p in ordered},
                        pair_profiles={(p.model_id, p.effort): p for p in ordered})
        binding = resolve(catalog=catalog_of(entry),
                          role_requirement={"min_intelligence": 40, "effort": "medium",
                                            "effort_rule": "preference"})
        assert binding.model_id == "claude-opus-5-5"
        assert binding.requested_effort == binding.effort_native_value == "medium"


def test_pair_context_and_pool_admission_remain_hard_constraints():
    oversized = resolve_role_pair(context_manifest=rt.RoutingContext(estimated_input_tokens=2_000_000))
    assert not oversized.is_bound and oversized.disposition == "block"
    assert all(r.reason == "context_overflow" for r in oversized.rejected_routes)
    resources = cat.ResourcePoolState()
    resources.pool("claude-subscription").pause(until=2000, reason="test")
    paused = resolve_role_pair(resources=resources)
    assert not paused.is_bound and paused.disposition == "wait"


def test_pair_step_down_maps_numerical_requirement_through_class_floors():
    binding = resolve_role_pair("planner", strength_offset=-1)
    assert (binding.model_id, binding.requested_effort) == ("claude-haiku-5-5", "xhigh")


def observed_pace(resources, pool_id, delta, *, at=1000, age=0):
    from datetime import datetime, timezone
    # Halfway through the weekly window, so 50 - used = pace_delta.
    resources.pool(pool_id).observation = cat.QuotaObservation(
        pool_id, {10080: cat.QuotaWindow(10080, 50 - delta, at + 3.5 * 86400)},
        observed_at=datetime.fromtimestamp(at - age, timezone.utc).isoformat())


def global_pair_resolve(resources=None, node_id="implementer", **overrides):
    from maestro.model_policy import shipped_policy
    from maestro.workflows.policy import default_policy
    return resolve(node=agent_node(node_id=node_id),
                   catalog=pair_catalog("claude", "codex"), policy=default_policy(),
                   resources=resources or cat.ResourcePoolState(),
                   role_requirement=shipped_policy()["roles"]["implementer"], **overrides)


def test_global_pace_selects_the_pool_without_comparing_raw_costs():
    resources = cat.ResourcePoolState()
    observed_pace(resources, "claude-subscription", 10)
    observed_pace(resources, "codex-subscription", -5)
    assert global_pair_resolve(resources).backend_id == "claude"
    observed_pace(resources, "claude-subscription", -5)
    observed_pace(resources, "codex-subscription", 10)
    selected = global_pair_resolve(resources)
    assert selected.backend_id == "codex"  # 0.62 beats Claude's independently scaled 0.42.
    assert "pool=codex-subscription pace=10.0" in selected.selection_reason
    assert {r.backend_id for r in selected.rejected_routes} == {"claude", "codex"}


def test_global_neutral_pool_ties_do_not_give_a_harness_automatic_priority():
    from collections import Counter
    from maestro.model_policy import shipped_policy
    from maestro.workflows.policy import default_policy

    # Vary routing identities, with the same real packaged inputs for all samples.
    catalog = pair_catalog("claude", "codex")
    policy = default_policy()
    requirement = shipped_policy()["roles"]["implementer"]
    counts = Counter(
        resolve(node=agent_node(node_id=f"node-{i}"), catalog=catalog, policy=policy,
                resources=cat.ResourcePoolState(), role_requirement=requirement).backend_id
        for i in range(200)
    )
    assert counts["claude"] >= 60 and counts["codex"] >= 60, counts


def test_global_stale_pace_is_neutral_and_saturated_best_pool_falls_through():
    resources = cat.ResourcePoolState()
    observed_pace(resources, "claude-subscription", 10, age=901)
    observed_pace(resources, "codex-subscription", 1)
    assert global_pair_resolve(resources).backend_id == "codex"
    resources.pool("codex-subscription").active = 2
    bound = global_pair_resolve(resources)
    assert bound.backend_id == "claude"
    assert any(r.reason == "pool_saturated" for r in bound.rejected_routes)


# ── fixtures: a small three-backend world ────────────────────────────────────────────


def _limits(ceiling=150_000):
    return cat.ContextLimits(200_000, 32_000, ceiling)


def cheap_backend(**overrides):
    body = dict(
        backend_id="cheap_cli",
        runtime_kind="agent_cli",
        installed_version="1.0.0",
        billing_category="subscription",
        usage_pool_id="cheap-pool",
        available_models=("small-1",),
        effort_controls=cat.EffortControls(
            supported=True, delivery_mechanism="flag", native_values=("low", "medium", "high")
        ),
        context_limits=_limits(100_000),
        model_profiles={'small-1': cat.ModelProfile('small-1')},
        pair_profiles=cat.migrate_pairs(('small-1', 'balanced', 0.2)),
        pairs_migrated=True,
        permission_enforcement="directory_isolation",
        concurrency_limit=2,
    )
    body.update(overrides)
    if 'pair_profiles' in overrides and 'pairs_migrated' not in overrides:
        body['pairs_migrated'] = False
    return cat.BackendCapabilityEntry(**body)


def strong_backend(**overrides):
    body = dict(
        backend_id="strong_cli",
        runtime_kind="agent_cli",
        installed_version="2.0.0",
        billing_category="subscription",
        usage_pool_id="strong-pool",
        available_models=("big-1",),
        effort_controls=cat.EffortControls(
            supported=True,
            delivery_mechanism="config_key",
            native_values=("minimal", "deep"),
            mapping={"high": "deep", "low": "minimal"},
        ),
        context_limits=_limits(200_000),
        model_profiles={'big-1': cat.ModelProfile('big-1')},
        pair_profiles=cat.migrate_pairs(('big-1', 'strong', 3.0)),
        pairs_migrated=True,
        permission_enforcement="physical_sandbox",
        concurrency_limit=1,
    )
    body.update(overrides)
    if 'pair_profiles' in overrides and 'pairs_migrated' not in overrides:
        body['pairs_migrated'] = False
    return cat.BackendCapabilityEntry(**body)


def free_backend(**overrides):
    body = dict(
        backend_id="free_api",
        runtime_kind="model_endpoint_harness",
        installed_version="v1",
        billing_category="free_tier_api",
        usage_pool_id="free-pool",
        available_models=("free-1",),
        context_limits=_limits(120_000),
        model_profiles={'free-1': cat.ModelProfile('free-1')},
        pair_profiles=cat.migrate_pairs(('free-1', 'balanced', 0.0001)),
        pairs_migrated=True,
        tool_delivery="json_schema_dispatch",
        permission_enforcement="conventional",
    )
    body.update(overrides)
    if 'pair_profiles' in overrides and 'pairs_migrated' not in overrides:
        body['pairs_migrated'] = False
    return cat.BackendCapabilityEntry(**body)


def catalog_of(*entries) -> cat.CapabilityCatalog:
    return cat.CapabilityCatalog.of(list(entries) or [cheap_backend(), strong_backend()])


class Policy:
    """The slice of `ProjectPolicy` the router reads.

    A real `ProjectPolicy` requires templates, gates and a consistent role set to
    construct; the router only ever asks it for `objective` and `agent_definitions`, so a
    stand-in keeps these tests about routing. `tests/test_routing.py` uses the real
    policy loaded from the shipped template, which is where that coupling is proven.
    """

    def __init__(self, objective="paid_efficiency", definitions=None):
        self.objective = objective
        self.agent_definitions = definitions or {"implementer": implementer_role()}


def implementer_role(**overrides) -> AgentDefinition:
    body = dict(
        role_id="implementer",
        version="1.0.0",
        responsibility="implement the task",
        logical_tools=("write_file", "run_command"),
    )
    body.update(overrides)
    return AgentDefinition(**body)


def agent_node(**overrides) -> NodeSpec:
    body = dict(
        node_id="implementer",
        kind="agent",
        handler_ref="agent.implementer",
        agent_definition_ref="implementer",
    )
    body.update(overrides)
    return NodeSpec(**body)


def resolve(**kwargs):
    params = dict(
        node=agent_node(),
        context_manifest=rt.RoutingContext(estimated_input_tokens=40_000),
        resources=cat.ResourcePoolState(),
        catalog=catalog_of(),
        policy=Policy(),
        now_epoch=1_000.0,
    )
    params.update(kwargs)
    node = params.pop("node")
    manifest = params.pop("context_manifest")
    return rt.resolve_agent(node, manifest, **params)


# ── step 1: hard constraints ─────────────────────────────────────────────────────────


def test_a_backend_outside_the_objectives_billing_categories_is_filtered_out():
    binding = resolve(catalog=catalog_of(cheap_backend(), free_backend()))
    assert binding.backend_id == "cheap_cli"
    reasons = {route.reason for route in binding.rejected_routes}
    assert "billing_not_permitted" in reasons


def test_the_free_objective_does_not_silently_spend_subscription_capacity():
    """`§3.1`: free nodes must not consume reserved paid quota. The hybrid grant is
    explicit and belongs to P7, so the default must refuse rather than borrow."""
    outcome = resolve(
        catalog=catalog_of(cheap_backend(), strong_backend()),
        policy=Policy(objective="free_quality"),
    )
    assert outcome.is_bound is False
    assert outcome.disposition == "block" and outcome.reason == "no_eligible_route"


def test_an_excluded_route_is_rejected_and_the_next_one_binds():
    """P12B F4: a route a node overflowed on is excluded for that node only."""
    both = catalog_of(cheap_backend(), strong_backend())
    unexcluded = resolve(catalog=both)
    binding = resolve(catalog=both, exclude=frozenset({(unexcluded.backend_id, unexcluded.model_id)}))
    assert binding.is_bound and binding.backend_id != unexcluded.backend_id
    assert [(r.backend_id, r.reason) for r in binding.rejected_routes] == [
        (unexcluded.backend_id, "route_excluded")]


def _strong_demand_policy(objective="paid_efficiency", **role):
    return Policy(objective=objective, definitions={"implementer": implementer_role(
        permitted_backend_capabilities={"min_reasoning_strength": "strong", **role},
    )})


def _strong_pool_paused():
    resources = cat.ResourcePoolState()
    resources.pool("strong-pool").pause(until=10**9, reason="quota")
    return resources


def test_a_strength_offset_lowers_the_demand_one_rung():
    """P12B F5: the step-down resolution binds the `balanced` route the demand refused."""
    both = catalog_of(cheap_backend(), strong_backend())
    waiting = resolve(catalog=both, policy=_strong_demand_policy(), resources=_strong_pool_paused())
    assert (waiting.disposition, waiting.reason) == ("wait", "pool_paused")

    stepped = resolve(catalog=both, policy=_strong_demand_policy(),
                      resources=_strong_pool_paused(), strength_offset=-1)
    assert stepped.is_bound and stepped.backend_id == "cheap_cli"
    assert "stepped from strong" in stepped.selection_reason
    assert stepped.stepped_down is False, "marking the binding is routing.resolve's job"


def test_a_strength_offset_is_floored_at_light():
    binding = resolve(catalog=catalog_of(cheap_backend()), strength_offset=-5)
    assert binding.is_bound and "stepped from" in binding.selection_reason


def test_a_strength_floor_raises_the_demand_and_never_lowers_it():
    """P12B F7: the diagnoser's escalation above the model that failed."""
    both = catalog_of(cheap_backend(), strong_backend())
    ordinary = resolve(catalog=both)
    assert ordinary.is_bound and ordinary.backend_id == "cheap_cli"

    raised = resolve(catalog=both, min_strength_floor="strong")
    assert raised.is_bound and raised.backend_id == "strong_cli"
    assert "raised to strong" in raised.selection_reason

    # A role whose own demand is already `strong` is not dragged back down by a floor
    # below it: a floor is a minimum, not a setting.
    unmoved = resolve(catalog=both, policy=_strong_demand_policy(),
                      min_strength_floor="balanced")
    assert unmoved.is_bound and unmoved.backend_id == "strong_cli"
    assert "raised to" not in unmoved.selection_reason


def test_a_billing_override_drops_the_hybrid_grant():
    from maestro.workflows.policy import HybridPolicy

    policy = Policy(objective="free_quality")
    policy.hybrid = HybridPolicy(paid_roles=("implementer",))
    granted = resolve(catalog=catalog_of(cheap_backend()), policy=policy)
    assert granted.is_bound, "the hybrid grant admits subscription for the implementer"

    overridden = resolve(catalog=catalog_of(cheap_backend()), policy=policy,
                         billing=rt.OBJECTIVE_BILLING["free_quality"])
    assert (overridden.disposition, overridden.reason) == ("block", "no_eligible_route")
    assert [r.reason for r in overridden.rejected_routes] == ["billing_not_permitted"]


def test_a_billing_override_stays_inside_a_role_declared_set():
    policy = Policy(definitions={"implementer": implementer_role(
        permitted_backend_capabilities={"billing_categories": ["free_tier_api"]})})
    outcome = resolve(catalog=catalog_of(cheap_backend()), policy=policy,
                      billing=frozenset({"subscription"}))
    assert outcome.is_bound is False


def test_an_unstepped_historical_binding_serialises_exactly_as_before():
    import dataclasses

    binding = dataclasses.replace(resolve(), relative_cost=None)
    document = binding.to_dict()
    assert "stepped_down" not in document
    assert list(document) == [
        "node_id", "backend_id", "runtime_instance_id", "model_id", "effort_native_value",
        "effort_delivery", "effort_support", "catalog_revision", "instruction_delivery",
        "permission_enforcement", "usage_pool_id", "billing_category", "selection_reason",
        "rejected_routes", "requested_effort", "shape_key", "effort_config_key",
    ]
    stepped = dataclasses.replace(binding, stepped_down=True)
    assert stepped.to_dict() == {**document, "stepped_down": True}
    assert stepped.binding_id == binding.binding_id


def test_a_wait_does_not_know_its_reset_until_asked():
    outcome = resolve(catalog=catalog_of(strong_backend()), resources=_strong_pool_paused())
    assert outcome.reset_known is None


def test_excluding_every_route_blocks_rather_than_waits():
    outcome = resolve(
        catalog=catalog_of(cheap_backend()), exclude=frozenset({("cheap_cli", "small-1")}),
    )
    assert (outcome.disposition, outcome.reason) == ("block", "no_eligible_route")
    assert [route.reason for route in outcome.rejected_routes] == ["route_excluded"]


def test_a_task_larger_than_the_quality_ceiling_is_rejected_for_context_overflow():
    outcome = resolve(
        context_manifest=rt.RoutingContext(estimated_input_tokens=180_000),
        catalog=catalog_of(cheap_backend()),
    )
    assert outcome.is_bound is False
    assert [route.reason for route in outcome.rejected_routes] == ["context_overflow"]


def test_a_modality_the_backend_lacks_is_a_hard_filter():
    outcome = resolve(
        context_manifest=rt.RoutingContext(modalities=("text", "image")),
        catalog=catalog_of(cheap_backend()),
    )
    assert outcome.is_bound is False
    assert outcome.rejected_routes[0].reason == "modality_unsupported"


def test_a_role_requiring_a_physical_sandbox_is_not_given_directory_isolation():
    policy = Policy(
        definitions={
            "implementer": implementer_role(
                permitted_backend_capabilities={"min_permission_enforcement": "physical_sandbox"}
            )
        }
    )
    binding = resolve(policy=policy)
    assert binding.backend_id == "strong_cli"
    assert any(r.reason == "permission_insufficient" for r in binding.rejected_routes)


def test_a_role_may_narrow_the_permitted_models_and_the_router_obeys():
    policy = Policy(
        definitions={
            "implementer": implementer_role(
                permitted_backend_capabilities={"models": ["big-1"]}
            )
        }
    )
    binding = resolve(policy=policy)
    assert binding.model_id == "big-1"
    assert any(r.reason == "model_not_permitted" for r in binding.rejected_routes)


def test_a_node_whose_role_the_policy_does_not_define_blocks_rather_than_defaulting():
    outcome = resolve(node=agent_node(agent_definition_ref="ghost"))
    assert outcome.is_bound is False
    assert outcome.disposition == "block" and outcome.reason == "no_agent_definition"


def test_an_empty_catalog_blocks_explicitly():
    outcome = resolve(catalog=cat.CapabilityCatalog())
    assert outcome.is_bound is False and outcome.reason == "empty_catalog"


# ── step 2: demand estimation ────────────────────────────────────────────────────────


def test_high_risk_work_is_never_routed_to_a_low_effort_small_model():
    """`§2.1` step 2's one non-negotiable, checked at both ends of the pipeline."""
    context = rt.RoutingContext(risk="high", estimated_input_tokens=10_000)
    demand = rt.estimate_demand(agent_node(), context, Policy())
    assert demand.min_strength == "strong"

    binding = resolve(context_manifest=context)
    assert binding.backend_id == "strong_cli", "high-risk task routed to the cheap small model"
    assert binding.effort_native_value == "deep"


def test_a_history_of_failure_escalates_demand_even_at_low_risk():
    context = rt.RoutingContext(risk="low", historical_failure_rate=0.6)
    demand = rt.estimate_demand(agent_node(), context, Policy())
    assert demand.min_strength == "strong"


def test_the_demand_names_a_strength_and_no_effort():
    """DESIGN §17.2: effort belongs to the route. Risk moves the strength floor only."""
    demand = rt.estimate_demand(agent_node(), rt.RoutingContext(risk="high"), Policy())
    assert not hasattr(demand, "min_effort")


def test_a_role_floor_may_raise_the_demand_and_never_lower_it():
    role = implementer_role(
        permitted_backend_capabilities={"min_reasoning_strength": "strong"},
    )
    low_risk = rt.RoutingContext(risk="low")
    raised = rt.estimate_demand(agent_node(), low_risk, Policy(definitions={"implementer": role}))
    assert raised.min_strength == "strong"

    weak_role = implementer_role(
        permitted_backend_capabilities={"min_reasoning_strength": "light"},
    )
    high_risk = rt.RoutingContext(risk="high")
    kept = rt.estimate_demand(
        agent_node(), high_risk, Policy(definitions={"implementer": weak_role})
    )
    assert kept.min_strength == "strong", "a role floor lowered a risk-derived floor"


def test_a_configured_role_strength_is_the_role_floor_in_place_of_the_templates():
    # DESIGN.md §17.2: the template's `min_reasoning_strength` is only the shipped default
    # of the role's `strength`.
    low_risk = rt.RoutingContext(risk="low")
    strong_role = implementer_role(
        permitted_backend_capabilities={"min_reasoning_strength": "strong"},
    )
    policy = Policy(definitions={"implementer": strong_role})

    raised = rt.estimate_demand(agent_node(), low_risk, Policy(), role_strength="strong")
    assert raised.min_strength == "strong"
    assert "role floor strength=strong" in raised.reason

    replaced = rt.estimate_demand(agent_node(), low_risk, policy, role_strength="light")
    assert replaced.min_strength == "light", "the template floor outlived the configured one"

    kept = rt.estimate_demand(
        agent_node(), rt.RoutingContext(risk="high"), policy, role_strength="light"
    )
    assert kept.min_strength == "strong", "a configured strength lowered a risk-derived floor"

    unreadable = rt.estimate_demand(agent_node(), low_risk, policy, role_strength="huge")
    assert unreadable.min_strength == "strong", "a value that is no class must read as absent"


def test_resolve_agent_routes_at_the_configured_role_strength():
    low_risk = rt.RoutingContext(estimated_input_tokens=40_000, risk="low")
    assert resolve(context_manifest=low_risk).backend_id == "cheap_cli"
    binding = resolve(context_manifest=low_risk, role_strength="strong")
    assert (binding.backend_id, binding.model_id) == ("strong_cli", "big-1")


def test_a_manifest_may_be_a_mapping_or_an_object_and_reads_the_same_either_way():
    as_mapping = rt.RoutingContext.from_manifest(
        {"estimated_input_tokens": 100, "task_class": "security", "modalities": ["text"]}
    )
    assert as_mapping.estimated_input_tokens == 100 and as_mapping.task_class == "security"

    class Manifest:
        estimated_input_tokens = 100
        task_class = "security"

    assert rt.RoutingContext.from_manifest(Manifest()).task_class == "security"
    assert rt.RoutingContext.from_manifest(None) == rt.RoutingContext()


# ── steps 3 & 4: joint selection and effort delivery ─────────────────────────────────


def test_paid_efficiency_takes_the_cheapest_route_that_clears_the_floor():
    binding = resolve()
    assert (binding.backend_id, binding.model_id) == ("cheap_cli", "small-1")
    assert binding.effort_delivery == "flag" and binding.effort_support == "native"


def test_free_quality_ranks_by_capability_and_ignores_cost():
    expensive_free = free_backend(
        backend_id="free_strong",
        usage_pool_id="free-strong",
        available_models=("free-big",),
        model_profiles={'free-big': cat.ModelProfile('free-big')},
        pair_profiles=cat.migrate_pairs(('free-big', 'strong', 99.0)),
        pairs_migrated=True,
    )
    binding = resolve(
        catalog=catalog_of(free_backend(), expensive_free),
        policy=Policy(objective="free_quality"),
    )
    assert binding.backend_id == "free_strong"


def test_a_mapped_effort_uses_the_backends_own_word_and_says_it_was_mapped():
    binding = resolve(context_manifest=rt.RoutingContext(risk="high"))
    assert binding.effort_native_value == "deep" and binding.effort_support == "mapped"
    assert binding.effort_delivery == "config_key"


def test_an_unmappable_level_never_invents_a_value():
    """`§2.1` step 4: no flag, no value, and the reason travels with the binding."""
    silent = cheap_backend(
        backend_id="silent_cli",
        usage_pool_id="silent",
        effort_controls=cat.EffortControls(unsupported_reason="values never observed"),
    )
    binding = resolve(catalog=catalog_of(silent))
    assert binding.effort_native_value is None
    assert binding.effort_support == "unsupported"
    assert binding.effort_delivery == "unsupported"
    assert "values never observed" in binding.selection_reason


def test_an_unsupported_effort_backend_does_not_win_over_an_equal_one_that_delivers_it():
    """The compensation `§2.1` step 4 asks for: when effort cannot be asked for, the
    route that can deliver it is preferred at equal strength and cost."""
    mute = cheap_backend(
        backend_id="mute_cli",
        usage_pool_id="cheap-pool",
        available_models=("small-1",),
        effort_controls=cat.EffortControls(),
    )
    binding = resolve(catalog=catalog_of(cheap_backend(), mute))
    assert binding.backend_id == "cheap_cli"


def test_measured_success_breaks_a_tie_and_a_single_sample_does_not():
    twin = cheap_backend(backend_id="twin_cli", usage_pool_id="cheap-pool")
    stats = cat.OutcomeStatistics(minimum_observations=5)

    unmeasured = resolve(catalog=catalog_of(cheap_backend(), twin), statistics=stats)
    assert unmeasured.backend_id == "cheap_cli", "expected the deterministic id tie-break"

    shape = unmeasured.shape_key
    for _ in range(5):
        stats.record(shape, success=False)
    rerouted = resolve(catalog=catalog_of(cheap_backend(), twin), statistics=stats)
    assert rerouted.backend_id == "twin_cli", "a measured 0% route was still preferred"


def test_resolution_is_deterministic_across_repeated_calls():
    first, second = resolve(), resolve()
    assert first.binding_id == second.binding_id
    assert first.to_dict() == second.to_dict()


# ── step 5: admission ────────────────────────────────────────────────────────────────


def test_a_saturated_pool_routes_to_an_equally_qualified_alternate():
    resources = cat.ResourcePoolState()
    resources.pool("cheap-pool", concurrency_limit=2).active = 2
    binding = resolve(resources=resources, catalog=catalog_of(cheap_backend(), strong_backend()))
    assert binding.backend_id == "strong_cli"
    assert any(r.reason == "pool_saturated" for r in binding.rejected_routes)


def _reseller():
    """One backend whose cheapest model bills against somebody else's quota."""
    return cheap_backend(
        available_models=("small-1", "resold-1"),
        model_profiles={'small-1': cat.ModelProfile('small-1'), 'resold-1': cat.ModelProfile('resold-1', usage_pool_id='resold-pool')},
        pair_profiles=cat.migrate_pairs(('small-1', 'balanced', 0.2), ('resold-1', 'balanced', 0.1)),
        pairs_migrated=True,
    )


def test_a_models_own_pool_is_what_admission_and_the_binding_use():
    """Per-model pools: exhausting the resold quota must not stop the host's own."""
    binding = resolve(catalog=catalog_of(_reseller()))
    assert (binding.model_id, binding.usage_pool_id) == ("resold-1", "resold-pool")

    exhausted = cat.ResourcePoolState()
    exhausted.pool("resold-pool", concurrency_limit=2)
    exhausted.observe(
        cat.QuotaObservation(
            pool_id="resold-pool",
            windows={300: cat.QuotaWindow(300, used_pct=100.0, resets_at=5_000)},
            observed_at=cat.utc_now_iso(),
            written_at=cat.utc_now_iso(),
            acquisition="push",
        )
    )
    assert "resold-pool" in exhausted.pause_exhausted_pools()

    fallback = resolve(catalog=catalog_of(_reseller()), resources=exhausted)
    assert (fallback.model_id, fallback.usage_pool_id) == ("small-1", "cheap-pool"), (
        "admission read the entry's pool, not the model's"
    )


def test_a_fully_saturated_frontier_waits_with_the_soonest_reset():
    resources = cat.ResourcePoolState()
    resources.pool("cheap-pool", concurrency_limit=2).active = 2
    strong = resources.pool("strong-pool", concurrency_limit=1)
    strong.observation = cat.QuotaObservation(
        pool_id="strong-pool",
        windows={300: cat.QuotaWindow(300, used_pct=99.0, resets_at=1_600)},
        observed_at=cat.utc_now_iso(),
        written_at=cat.utc_now_iso(),
    )
    outcome = resolve(resources=resources)
    assert outcome.is_bound is False
    assert outcome.disposition == "wait"
    assert outcome.retry_after_sec == 600.0
    assert outcome.reason in ("pool_saturated", "quota_exhausted")


def test_admission_never_relaxes_a_hard_constraint_to_find_a_free_slot():
    """A saturated qualified pool must not promote a route that failed step 1."""
    resources = cat.ResourcePoolState()
    resources.pool("cheap-pool", concurrency_limit=2).active = 2
    outcome = resolve(resources=resources, catalog=catalog_of(cheap_backend(), free_backend()))
    assert outcome.is_bound is False, "fell back to a billing tier the objective forbids"
    assert outcome.disposition == "wait"


def test_resolving_does_not_itself_consume_a_slot():
    """Reservation belongs to dispatch: a resolution that reserved would leak a slot
    every time a caller resolved and then decided not to dispatch."""
    resources = cat.ResourcePoolState()
    resolve(resources=resources)
    assert resources.pool("cheap-pool").active == 0


# ── the binding, and its four consumers ──────────────────────────────────────────────


def test_the_binding_records_every_field_the_spec_names():
    document = resolve().to_dict()
    for key in (
        "backend_id",
        "runtime_instance_id",
        "model_id",
        "effort_native_value",
        "effort_delivery",
        "effort_support",
        "catalog_revision",
        "instruction_delivery",
        "permission_enforcement",
        "selection_reason",
        "rejected_routes",
    ):
        assert key in document
    assert document["selection_reason"], "a binding was made without stating why"


def test_dispatch_plan_emits_a_flag_a_config_key_or_nothing_at_all():
    flagged = rt.dispatch_plan(resolve())
    assert flagged.effort_args == ("--effort", "medium") and flagged.effort_config == ()

    configured = rt.dispatch_plan(resolve(context_manifest=rt.RoutingContext(risk="high")))
    assert configured.effort_config == (("reasoning_effort", "deep"),)
    assert configured.effort_args == ()

    mute = cheap_backend(
        backend_id="mute_cli", usage_pool_id="mute", effort_controls=cat.EffortControls()
    )
    silent = rt.dispatch_plan(resolve(catalog=catalog_of(mute)))
    assert silent.effort_args == () and silent.effort_config == ()


def test_the_four_consumers_all_read_one_binding():
    binding = resolve()
    node = agent_node()
    assert rt.dispatch_plan(binding).binding is binding
    assert rt.execution_deadline_sec(binding, node) == node.timeout_policy.execution_timeout_sec
    assert rt.token_account_key(binding) == "cheap-pool/cheap_cli/small-1"
    assert "cheap_cli/small-1" in rt.status_summary(binding)
    assert binding.usage_pool_id in rt.status_summary(binding)


def test_an_unsupported_effort_is_reported_as_such_in_the_status_line():
    mute = cheap_backend(
        backend_id="mute_cli", usage_pool_id="mute", effort_controls=cat.EffortControls()
    )
    assert "effort unsupported" in rt.status_summary(resolve(catalog=catalog_of(mute)))


def test_a_binding_carries_the_shape_its_outcome_is_recorded_against():
    binding = resolve(
        context_manifest=rt.RoutingContext(
            estimated_input_tokens=1_000,
            task_class="ordinary",
            template_id="ordinary",
            template_version="2",
            active_fragment_set=("implementer@1.0.0",),
        )
    )
    shape = binding.shape_key
    assert shape.role_version == "1.0.0", "the resolved role version did not reach the shape"
    assert shape.template_id == "ordinary" and shape.active_fragment_set == ("implementer@1.0.0",)

    stats = cat.OutcomeStatistics(minimum_observations=1)
    rt.record_outcome(stats, binding, success=True)
    assert stats.success_rate(shape) == 1.0


def test_rejection_and_refusal_reasons_come_from_a_closed_vocabulary():
    with pytest.raises(ValueError):
        rt.RejectedRoute("b", "m", "because")
    with pytest.raises(ValueError):
        rt.RouteUnavailable("n", "maybe", "pool_saturated")
    with pytest.raises(ValueError):
        rt.RouteUnavailable("n", "wait", "vibes")


def test_a_routing_decision_is_recordable_on_a_baseline_trace():
    from maestro import metrics

    trace = metrics.BaselineTrace(lane="routing")
    first = resolve(trace=trace)
    resolve(trace=trace)
    assert [event.kind for event in trace.events] == [
        metrics.ROUTE_RESOLVED,
        metrics.ROUTE_RESOLVED,
    ]
    assert trace.events[0].identity == metrics.binding_identity(first)
    assert trace.events[0].evidence == metrics.OBSERVED
