"""A route is a `(harness, model, effort)` pair with its own class and cost (DESIGN §17.3)."""
from __future__ import annotations

import dataclasses

import pytest

from maestro.backends import catalog as cat
from maestro.backends import router
from maestro.workflows.policy import AgentDefinition

DIAL = cat.EffortControls(
    supported=True, delivery_mechanism="flag", native_values=("low", "medium", "high")
)


def entry(backend_id="dial", *, controls=DIAL, pairs=None, profiles=None, models=("m",), migrated=()):
    return cat.BackendCapabilityEntry(
        backend_id=backend_id,
        runtime_kind="agent_cli",
        installed_version="1.0",
        billing_category="subscription",
        usage_pool_id=f"{backend_id}-pool",
        available_models=tuple(models),
        effort_controls=controls,
        model_profiles=profiles or {},
        pair_profiles=cat.migrate_pairs(*migrated) if migrated else pairs or {},
        pairs_migrated=bool(migrated),
    )


def pair_table(*rows):
    return {(model, effort): cat.PairProfile(model, effort, strength, cost)
            for model, effort, strength, cost in rows}


def role(strength="balanced"):
    return AgentDefinition(
        role_id="implementer", version="1.0.0", responsibility="implement the task",
        logical_tools=("write_file", "run_command"),
        permitted_backend_capabilities={"min_reasoning_strength": strength},
    )


class Policy:
    def __init__(self, strength="balanced", objective="paid_efficiency"):
        self.objective = objective
        self.agent_definitions = {"implementer": role(strength)}


class Node:
    node_id = "n1"
    agent_definition_ref = "implementer"


def resolve(catalog, strength="balanced", **kwargs):
    return router.resolve_agent(Node(), None, kwargs.pop('resources', None), catalog,
                                Policy(strength), **kwargs)


# ── catalog ──────────────────────────────────────────────────────────────────────────


def test_a_pair_carries_its_own_class_and_cost():
    e = entry(pairs=pair_table(("m", "medium", "balanced", 0.7), ("m", "high", "strong", 1.5)))
    assert e.pair("m", "medium").strength == "balanced"
    assert e.pair("m", "high").relative_cost == 1.5
    assert [p.effort for p in e.pairs("m")] == ["medium", "high"]


def test_w2_binding_freezes_selected_pair_cost_without_repricing_old_bindings():
    catalog = cat.CapabilityCatalog.of([entry(pairs=pair_table(('m', 'medium', 'balanced', 0.7)))])
    bound = resolve(catalog)
    assert bound.to_dict().get('relative_cost') == 0.7
    replacement = cat.CapabilityCatalog.of([entry(pairs=pair_table(('m', 'medium', 'balanced', 9.0)))])
    assert resolve(replacement).to_dict()['relative_cost'] == 9.0
    assert bound.to_dict()['relative_cost'] == 0.7
    historical = dataclasses.replace(bound, relative_cost=None)
    assert 'relative_cost' not in historical.to_dict()


def test_routes_yield_entry_model_and_effort():
    e = entry(pairs=pair_table(("m", "medium", "balanced", 0.7), ("m", "high", "strong", 1.5)))
    assert [(x.backend_id, m, f) for x, m, f in cat.CapabilityCatalog.of([e]).routes()] == [
        ("dial", "m", "medium"), ("dial", "m", "high")]


def test_a_pair_whose_effort_cannot_be_delivered_is_not_a_route():
    e = entry(pairs=pair_table(("m", "medium", "balanced", 0.7), ("m", "max", "strong", 3.0)))
    assert [p.effort for p in e.pairs("m")] == ["medium"]
    assert [(p.model_id, p.effort) for p in e.undeliverable_pairs()] == [("m", "max")]


def test_effort_variant_is_per_model_and_dispatches_only_the_observed_id():
    e = entry('variants', controls=cat.EffortControls(), models=('m', 'other'),
              profiles={'m': cat.ModelProfile('m', effort_variants={'high': 'm-h'}),
                        'other': cat.ModelProfile('other', effort_variants={'low': 'other-l'})},
              pairs=pair_table(('m', 'high', 'balanced', 0.7),
                               ('m', 'low', 'balanced', 0.1),
                               ('other', 'high', 'balanced', 0.2)))
    bound = resolve(cat.CapabilityCatalog.of([e]))
    assert isinstance(bound, router.AgentBinding)
    assert (bound.model_id, bound.requested_effort) == ('m', 'high')
    plan = router.dispatch_plan(bound)
    assert plan.model == 'm-h'
    assert not plan.effort_args and not plan.effort_config
    assert {(p.model_id, p.effort) for p in e.undeliverable_pairs()} == {
        ('m', 'low'), ('other', 'high')}


def test_same_model_uses_shared_prior_until_its_own_harness_has_enough_evidence():
    native = entry('native', profiles={'m': cat.ModelProfile('m', canonical_model_id='shared')},
                   pairs=pair_table(('m', 'high', 'balanced', 1)))
    agy = entry('agy', controls=cat.EffortControls(),
                profiles={'m': cat.ModelProfile('m', canonical_model_id='shared',
                                               effort_variants={'high': 'm-high'})},
                pairs=pair_table(('m', 'high', 'balanced', 2)))
    statistics = cat.OutcomeStatistics()
    native_binding = resolve(cat.CapabilityCatalog.of([native]))
    for _ in range(5):
        statistics.record(native_binding.shape_key, success=True)
    # Native is excluded only from eligibility; its verified outcomes remain a prior.
    both = cat.CapabilityCatalog.of([native, agy])
    bound = resolve(both, exclude=frozenset({('native', 'm')}), statistics=statistics)
    assert bound.backend_id == 'agy'
    assert 'measured success=100%' in bound.selection_reason
    assert bound.shape_key.effort == 'm-high'  # raw harness observations stay distinct
    for _ in range(5):
        statistics.record(bound.shape_key, success=False)
    own = resolve(both, exclude=frozenset({('native', 'm')}), statistics=statistics)
    assert 'measured success=0%' in own.selection_reason
    assert statistics.observations(native_binding.shape_key) == 5
    assert statistics.observations(bound.shape_key) == 5


@pytest.mark.parametrize('change', ['model', 'effort', 'role_version', 'task_class',
                                  'template_id', 'template_version', 'active_fragment_set'])
def test_shared_prior_never_crosses_other_shape_dimensions(change):
    native = entry('native', profiles={'m': cat.ModelProfile('m', canonical_model_id='shared')},
                   pairs=pair_table(('m', 'high', 'balanced', 1)))
    agy = dataclasses.replace(native, backend_id='agy')
    bound = resolve(cat.CapabilityCatalog.of([native]))
    changes = {change: ('different',) if change == 'active_fragment_set' else 'different'}
    if change == 'model':
        changes = {'model_id': 'different'}
    statistics = cat.OutcomeStatistics()
    for _ in range(5):
        statistics.record(dataclasses.replace(bound.shape_key, **changes), success=True)
    routed = resolve(cat.CapabilityCatalog.of([native, agy]),
                     exclude=frozenset({('native', 'm')}), statistics=statistics)
    assert 'measured success=' not in routed.selection_reason


def test_soft_independence_prefers_a_different_family_across_harnesses():
    def model(backend, family):
        return entry(backend, profiles={'m': cat.ModelProfile('m', model_family=family)},
                     pairs=pair_table(('m', 'high', 'balanced', 1)))
    native, agy, gemini = model('native', 'claude'), model('agy', 'claude'), model('gemini', 'gemini')
    catalog = cat.CapabilityCatalog.of([native, agy, gemini])
    bound = resolve(catalog, independent_families=frozenset({'claude'}))
    assert bound.backend_id == 'gemini'
    assert bound.to_dict()['model_family'] == 'gemini'
    # Same family and unknown identities never count as independently served models.
    assert all(r.reason == 'same_or_unknown_family' for r in bound.rejected_routes)
    remaining = cat.CapabilityCatalog.of([native, agy])
    fallback = resolve(remaining, independent_families=frozenset({'claude'}))
    assert fallback.is_bound
    assert 'independence_relaxed' in fallback.selection_reason


def test_routes_without_owner_identity_are_never_claimed_independent():
    # Live shape today: modelctl master publishes no model_family, so every profile has
    # None. The router may still bind, but it must say independence was relaxed.
    def model(backend):
        return entry(backend, profiles={'m': cat.ModelProfile('m')},
                     pairs=pair_table(('m', 'high', 'balanced', 1)))
    catalog = cat.CapabilityCatalog.of([model('native'), model('agy')])
    bound = resolve(catalog, independent_families=frozenset({'claude'}))
    assert bound.is_bound
    assert bound.to_dict().get('model_family') is None
    assert 'independence_relaxed' in bound.selection_reason


def test_soft_independence_waits_for_an_eligible_alternative_with_a_paused_pool():
    same = entry('native', profiles={'m': cat.ModelProfile('m', model_family='claude')},
                 pairs=pair_table(('m', 'high', 'balanced', 1)))
    other = entry('gemini', profiles={'m': cat.ModelProfile('m', model_family='gemini')},
                  pairs=pair_table(('m', 'high', 'balanced', 1)))
    resources = cat.ResourcePoolState()
    resources.pool('gemini-pool').pause(until=2000, reason='quota')
    result = resolve(cat.CapabilityCatalog.of([same, other]), resources=resources,
                     independent_families=frozenset({'claude'}), now_epoch=1000)
    assert not result.is_bound and result.disposition == 'wait'


def test_a_harness_without_a_dial_routes_only_the_pair_that_names_no_effort():
    e = entry("nodial", controls=cat.EffortControls(),
              pairs=pair_table(("m", None, "balanced", 1.0), ("m", "high", "strong", 1.0)))
    assert [p.effort for p in e.pairs("m")] == [None]


def test_a_dial_harness_does_not_route_a_pair_that_names_no_effort():
    e = entry(pairs=pair_table(("m", None, "balanced", 1.0)))
    assert e.pairs("m") == ()


def test_a_model_with_no_pair_has_no_route():
    e = entry(pairs={})
    assert cat.CapabilityCatalog.of([e]).routes() == ()
    with pytest.raises(cat.CatalogError):
        e.pair("m", "medium")


def test_schema1_rows_are_read_under_the_historical_migration_rule():
    e = entry(migrated=(("m", "strong", 1.25),))
    assert [(p.effort, p.strength, p.relative_cost) for p in e.pairs("m")] == [
        ("medium", "strong", 1.25), ("high", "strong", 1.25)]
    nodial = entry("nodial", controls=cat.EffortControls(),
                   migrated=(("m", "light", 1.0),))
    assert [(p.effort, p.strength) for p in nodial.pairs("m")] == [(None, "light")]


def test_a_pair_for_an_unlisted_model_or_with_an_empty_effort_is_refused():
    with pytest.raises(cat.CatalogError):
        entry(pairs=pair_table(("other", "medium", "balanced", 1.0)))
    with pytest.raises(cat.CatalogError):
        cat.PairProfile("m", "", "balanced", 1.0)


@pytest.mark.parametrize("intelligence, expected", [
    (0, "light"), (39, "light"), (40, "balanced"), (47, "balanced"), (48, "strong"), (100, "strong")])
def test_the_class_is_derived_from_the_pair_intelligence(intelligence, expected):
    assert cat.class_of(intelligence) == expected
    pair = cat.PairProfile.from_score("m", "medium", intelligence, 1.0)
    assert (pair.intelligence, pair.strength) == (intelligence, expected)
    assert pair.to_dict()["intelligence"] == intelligence


@pytest.mark.parametrize("intelligence", [-1, 101, 40.0, True, "40"])
def test_an_intelligence_is_a_whole_number_from_0_to_100(intelligence):
    with pytest.raises(cat.CatalogError):
        cat.class_of(intelligence)
    with pytest.raises(cat.CatalogError):
        cat.PairProfile.from_score("m", "medium", intelligence, 1.0)


def test_a_stated_class_must_be_the_one_the_intelligence_gives():
    assert cat.PairProfile("m", "medium", "balanced", 1.0, intelligence=47).strength == "balanced"
    with pytest.raises(cat.CatalogError, match="strong"):
        cat.PairProfile("m", "medium", "balanced", 1.0, intelligence=48)
    # A migrated pair has a class and no score.
    assert cat.PairProfile("m", "medium", "strong", 1.0).intelligence is None


def test_xhigh_and_max_are_ordinary_efforts_above_high():
    assert cat.CANONICAL_EFFORTS == ("low", "medium", "high", "xhigh", "max")
    ranks = [cat._effort_rank(effort) for effort in (None, *cat.CANONICAL_EFFORTS, "ultra")]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
    five = cat.EffortControls(supported=True, delivery_mechanism="flag",
                              native_values=cat.CANONICAL_EFFORTS)
    e = entry(controls=five, pairs=pair_table(("m", "xhigh", "strong", 2.0), ("m", "max", "strong", 3.0)))
    assert [p.effort for p in e.pairs("m")] == ["xhigh", "max"]


def test_models_lists_every_available_model_once():
    e = entry(models=("a", "b"), pairs=pair_table(("a", "medium", "balanced", 1.0),
                                                  ("a", "high", "strong", 1.5)))
    assert [(x.backend_id, m) for x, m in cat.CapabilityCatalog.of([e]).models()] == [
        ("dial", "a"), ("dial", "b")]


# ── router ───────────────────────────────────────────────────────────────────────────


def test_the_router_binds_the_cheapest_pair_at_or_above_the_role_strength():
    e = entry(pairs=pair_table(("m", "low", "light", 0.4), ("m", "medium", "balanced", 0.7),
                               ("m", "high", "balanced", 1.0)))
    binding = resolve(cat.CapabilityCatalog.of([e]))
    assert (binding.model_id, binding.effort_native_value, binding.requested_effort) == (
        "m", "medium", "medium")
    assert router.dispatch_plan(binding).effort_args == ("--effort", "medium")


def test_one_model_serves_two_classes_through_two_efforts():
    e = entry(pairs=pair_table(("m", "medium", "balanced", 0.7), ("m", "high", "strong", 1.5)))
    assert resolve(cat.CapabilityCatalog.of([e]), "strong").effort_native_value == "high"


def test_harness_order_is_no_longer_a_routing_input():
    first = entry("aaa", pairs=pair_table(("m", "medium", "balanced", 0.1)))
    second = entry("zzz", pairs=pair_table(("m", "medium", "balanced", 0.9)))
    catalog = cat.CapabilityCatalog.of([first, second])
    with pytest.raises(TypeError, match="harness_order"):
        resolve(catalog, harness_order=("zzz", "aaa"))


def test_an_unapproved_pair_is_never_a_candidate():
    e = entry(pairs=pair_table(("m", "low", "light", 0.4)))
    unavailable = resolve(cat.CapabilityCatalog.of([e]), "strong")
    assert not unavailable.is_bound
    assert unavailable.reason == "no_eligible_route"


def test_a_no_dial_pair_binds_with_no_effort_sent():
    e = entry("nodial", controls=cat.EffortControls(), pairs=pair_table(("m", None, "balanced", 1.0)))
    binding = resolve(cat.CapabilityCatalog.of([e]))
    assert binding.effort_support == "unsupported"
    assert router.dispatch_plan(binding).effort_args == ()


def test_a_migrated_model_keeps_the_effort_each_role_used():
    e = entry(migrated=(("m", "strong", 1.0),))
    catalog = cat.CapabilityCatalog.of([e])

    def bind(role_id, objective="paid_efficiency", target=catalog):
        node = Node()
        node.agent_definition_ref = role_id
        policy = Policy("strong", objective)
        policy.agent_definitions = {role_id: role("strong")}
        return router.resolve_agent(node, None, None, target, policy)

    assert bind("implementer").effort_native_value == "medium"
    assert bind("planner").effort_native_value == "high"
    assert bind("diagnoser").effort_native_value == "high"
    # The free objective never rationed effort. The copy stays a migrated entry.
    free = dataclasses.replace(e, billing_category="free_tier_api")
    assert free.pairs_migrated
    assert bind("implementer", "free_quality",
                cat.CapabilityCatalog.of([free])).effort_native_value == "high"
    nodial = entry("nodial", controls=cat.EffortControls(),
                   migrated=(("m", "strong", 1.0),))
    assert bind("planner", target=cat.CapabilityCatalog.of([nodial])).is_bound


def test_a_migrated_harness_that_cannot_deliver_the_roles_effort_keeps_its_route():
    """The migration must not take a route away: the implementer ran at `medium`, this
    harness maps only `high`, so the model binds at what the harness does deliver."""
    high_only = cat.EffortControls(supported=True, delivery_mechanism="config_key",
                                   native_values=("deep",), mapping={"high": "deep"})
    e = entry(controls=high_only, migrated=(("m", "balanced", 1.0),))
    binding = resolve(cat.CapabilityCatalog.of([e]))
    assert (binding.requested_effort, binding.effort_native_value) == ("high", "deep")


def test_approved_pairs_are_not_filtered_by_the_migration_effort():
    e = entry(pairs=pair_table(("m", "high", "strong", 1.5)))
    assert resolve(cat.CapabilityCatalog.of([e]), "strong").effort_native_value == "high"


# Decision 6 (Dan, 2026-10-10): until the first schema 2 policy, a task that risk, its
# class or its failure history raised to `strong` still binds at `high`, as the old
# router's `min_effort` did.
ESCALATIONS = [
    router.RoutingContext(risk="high"),
    router.RoutingContext(task_class="difficult_debugging"),
    router.RoutingContext(historical_failure_rate=0.5),
]


def bind_in(context, catalog, **kwargs):
    return router.resolve_agent(Node(), context, None, catalog, Policy(), **kwargs)


@pytest.mark.parametrize("context", ESCALATIONS)
def test_a_migrated_model_binds_high_when_risk_or_failure_raised_the_demand(context):
    e = entry(migrated=(("m", "strong", 1.0),))
    assert router.estimate_demand(Node(), context, Policy()).escalated
    assert bind_in(context, cat.CapabilityCatalog.of([e])).effort_native_value == "high"


def test_a_role_floor_alone_does_not_raise_the_migrated_effort():
    """Only risk and failure raised the effort before; a `strong` role ran at its own."""
    demand = router.estimate_demand(Node(), router.RoutingContext(), Policy("strong"))
    assert (demand.min_strength, demand.escalated) == ("strong", False)


def test_a_stepped_down_escalated_task_keeps_high():
    """The step-down moves the strength a rung; the old router left the effort alone."""
    e = entry(migrated=(("m", "balanced", 1.0),))
    catalog = cat.CapabilityCatalog.of([e])
    assert not bind_in(ESCALATIONS[0], catalog).is_bound
    assert bind_in(ESCALATIONS[0], catalog, strength_offset=-1).effort_native_value == "high"


def test_an_escalated_task_keeps_a_migrated_route_that_cannot_deliver_high():
    medium_only = cat.EffortControls(supported=True, delivery_mechanism="flag",
                                     native_values=("medium",))
    e = entry(controls=medium_only, migrated=(("m", "strong", 1.0),))
    assert bind_in(ESCALATIONS[0], cat.CapabilityCatalog.of([e])).effort_native_value == "medium"


def test_approved_pairs_are_not_filtered_by_the_escalation_effort():
    e = entry(pairs=pair_table(("m", "medium", "strong", 1.0), ("m", "high", "strong", 1.5)))
    assert bind_in(ESCALATIONS[0], cat.CapabilityCatalog.of([e])).effort_native_value == "medium"


def test_excluding_a_model_excludes_every_pair_of_it():
    e = entry(pairs=pair_table(("m", "medium", "balanced", 0.7), ("m", "high", "strong", 1.5)))
    unavailable = resolve(cat.CapabilityCatalog.of([e]), exclude=frozenset({("dial", "m")}))
    assert not unavailable.is_bound
