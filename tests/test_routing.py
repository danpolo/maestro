"""P6 acceptance (`phases/P06_CAPABILITY_ROUTING_CATALOG.md` §3), end to end.

Three conditions, checked against the *shipped* artefacts rather than fixtures — the
packaged project policy and its five role definitions, and the real verification register
`maestro doctor --thirdparty` gates:

1. one resolved `AgentBinding` is used for dispatch, timeouts, token accounting and status;
2. a routing failure is an explicit typed wait or block, never a silent fallback to an
   unapproved model or billing tier;
3. quota exhaustion pauses a pool's dispatch without killing its healthy running workers.

`tests/backends/test_router.py` covers the pipeline step by step; this file only asks
whether the thing maestro actually ships resolves.
"""
from __future__ import annotations

import pytest

from maestro.backends import catalog as cat
from maestro.backends import router as rt
from maestro.workflows.models import NodeSpec
from maestro.workflows.policy import default_policy


@pytest.fixture(scope="module")
def policy():
    return default_policy()


@pytest.fixture
def shipped_catalog():
    """A catalog derived from the real register, with this host's routing judgement.

    The register supplies every capability *fact* (version, effort controls, sandboxing,
    telemetry); `defaults` supplies only what a verification row cannot know — which
    models to offer, their relative standing, and the pool they bill to.
    """
    from maestro import thirdparty, model_policy

    return cat.catalog_from_register(
        thirdparty.load_register(),
        defaults={
            "claude": {
                "available_models": ["claude-sonnet-5", "claude-opus-5"],
                "usage_pool_id": "claude-subscription",
                "concurrency_limit": 3,
                "context_limits": {
                    "max_input_tokens": 1_000_000,
                    "max_output_tokens": 64_000,
                    "effective_context_ceiling": 200_000,
                },
                "model_profiles": {
                    "claude-sonnet-5": cat.ModelProfile("claude-sonnet-5"),
                    "claude-opus-5": cat.ModelProfile("claude-opus-5"),
                },
                "pair_profiles": cat.migrate_pairs(('claude-sonnet-5', 'balanced', 1.0),
                                                    ('claude-opus-5', 'strong', 4.0)),
                "pairs_migrated": True,
            },
            "codex": {
                "available_models": ["gpt-5.6-terra"],
                "usage_pool_id": "codex-subscription",
                "concurrency_limit": 2,
                "context_limits": {
                    "max_input_tokens": 400_000,
                    "max_output_tokens": 32_000,
                    "effective_context_ceiling": 150_000,
                },
                "model_profiles": {
                    "gpt-5.6-terra": cat.ModelProfile("gpt-5.6-terra"),
                },
                "pair_profiles": cat.migrate_pairs(('gpt-5.6-terra', 'strong', 2.0)),
                "pairs_migrated": True,
            },
            # Historical schema 1 no-dial routes, preserving the P07B acceptance
            # baseline. The current schema 2 AGY inactivity has a separate test.
            'agy': model_policy.catalog_defaults(model_policy.shipped_policy_v1(), {})['agy'],
        },
    )


def node(role: str = "implementer", node_id: str = "implementer") -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind="agent",
        handler_ref=f"agent.{role}",
        agent_definition_ref=role,
        effect_class="agent_editing_worktree",
        resource_request={"writer_lease": True},
    )


def manifest(**overrides) -> rt.RoutingContext:
    body = dict(
        estimated_input_tokens=60_000,
        task_class="ordinary",
        risk="medium",
        template_id="ordinary",
        template_version="1",
        active_fragment_set=("implementer@1.0.0", "gate@1.0.0"),
    )
    body.update(overrides)
    return rt.RoutingContext(**body)


def test_dispatch_passes_role_requirements_and_near_cost_margin():
    from maestro.model_policy import shipped_policy
    from maestro.workflows.routing import DispatchRouting, resolve
    from tests.backends.test_router import pair_catalog
    dispatch = DispatchRouting(catalog=pair_catalog("claude"), policy=default_policy(),
                               queries=None, task_spec=lambda _: None,
                               role_requirements=shipped_policy()["roles"], near_cost_margin=0.0)
    binding = resolve(dispatch, node("implementer"), manifest(), now_epoch=1000)
    assert (binding.model_id, binding.requested_effort) == ("claude-opus-5-5", "low")
    planner = resolve(dispatch, node("planner"), manifest(), now_epoch=1000)
    assert planner.requested_effort == "high"


@pytest.mark.parametrize("margin", [-0.1, 1.1, True, "0.2", float("nan")])
def test_doctor_refuses_an_invalid_near_cost_margin(margin):
    from maestro.model_policy import role_shape_issues
    assert any("routing.near_cost_margin" in issue
               for issue in role_shape_issues({"routing": {"near_cost_margin": margin}}))


def test_pace_refresh_reads_once_per_driver_per_300_seconds_and_preserves_failed_samples():
    from maestro.backends.base import Usage, WindowUsage
    from maestro.workflows.routing import DispatchRouting
    from tests.backends.test_router import pair_catalog
    calls = []

    def read(binding):
        calls.append(binding.backend_id)
        if len(calls) > 2:
            raise RuntimeError("reader unavailable")
        return Usage(source="fixture", updated_at="1970-01-01T00:16:40Z",
                     windows={10080: WindowUsage(used_pct=50, resets_at=303400)})

    dispatch = DispatchRouting(catalog=pair_catalog("claude", "codex"), policy=default_policy(),
                               queries=None, task_spec=lambda _: None, usage_reader=read)
    for at in range(1000, 1100, 10):
        dispatch.refresh_pace(at)
    assert sorted(calls) == ["claude", "codex"]
    previous = dispatch.resources.pool("claude-subscription").observation
    assert previous.acquisition == "pull" and previous.pace_delta(1000) == 0
    dispatch.refresh_pace(1300)
    assert len(calls) == 4
    assert dispatch.resources.pool("claude-subscription").observation is previous
    assert previous.pace_delta(1901) is None


# ── the shipped artefacts resolve at all ─────────────────────────────────────────────


def test_every_shipped_role_resolves_against_the_shipped_catalog(policy, shipped_catalog):
    for role in sorted(policy.agent_definitions):
        binding = rt.resolve_agent(
            node(role, node_id=role), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
        )
        assert binding.is_bound, f"{role} could not be routed: {binding.to_dict()}"
        assert binding.model_id in shipped_catalog.entry(binding.backend_id).available_models
        assert binding.selection_reason


def test_a_strong_only_role_is_never_given_a_balanced_model(policy, shipped_catalog):
    """`diagnoser`, `planner` and `reviewer` all declare `min_reasoning_strength: strong`."""
    for role in ("diagnoser", "planner", "reviewer"):
        binding = rt.resolve_agent(
            node(role, node_id=role), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
        )
        entry = shipped_catalog.entry(binding.backend_id)
        assert entry.pair(binding.model_id, binding.requested_effort).strength == "strong", role


def test_no_effort_flag_is_invented_for_a_backend_whose_values_were_never_observed(policy):
    """A row showing `--effort` but no native values stays unsupported ([INV-A07])."""
    entry = cat.BackendCapabilityEntry(
        backend_id="claude", runtime_kind="agent_cli", installed_version="1",
        billing_category="subscription", usage_pool_id="p", available_models=("claude-sonnet-5",),
        effort_controls=cat.effort_controls_from_findings({"controls_observed": ["--effort"]}),
        model_profiles={'claude-sonnet-5': cat.ModelProfile('claude-sonnet-5')},
        pair_profiles=cat.migrate_pairs(('claude-sonnet-5', 'balanced', 1.0)),
        pairs_migrated=True,
    )
    assert entry.effort_controls.supported is False

    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), cat.CapabilityCatalog.of([entry]), policy
    )
    assert binding.is_bound
    assert binding.effort_delivery == "unsupported"
    assert binding.effort_native_value is None
    plan = rt.dispatch_plan(binding)
    assert plan.effort_args == () and plan.effort_config == ()
    assert plan.model == binding.model_id


@pytest.mark.parametrize("backend_id, expect_args, expect_config", [
    ("claude", ("--effort", "medium"), ()),
    ("codex", (), (("model_reasoning_effort", "medium"),)),
])
def test_the_registers_observed_effort_reaches_the_dispatch_plan(
    policy, shipped_catalog, backend_id, expect_args, expect_config
):
    """Both rows record their effort vocabulary; codex also records its config key."""
    entry = shipped_catalog.entry(backend_id)
    assert entry.effort_controls.supported is True
    assert set(entry.effort_controls.native_values) == {"low", "medium", "high", "xhigh", "max"}

    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), cat.CapabilityCatalog.of([entry]), policy
    )
    assert binding.is_bound and binding.effort_support == "native"
    plan = rt.dispatch_plan(binding)
    assert (plan.effort_args, plan.effort_config) == (expect_args, expect_config)


def test_the_binding_names_the_catalog_revision_it_was_resolved_against(
    policy, shipped_catalog
):
    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
    )
    assert binding.catalog_revision == shipped_catalog.entry(binding.backend_id).revision


# ── acceptance 1: one binding for dispatch, timeouts, accounting and status ──────────


def test_one_resolved_binding_serves_all_four_execution_phases(policy, shipped_catalog):
    spec = node()
    binding = rt.resolve_agent(
        spec, manifest(), cat.ResourcePoolState(), shipped_catalog, policy
    )

    plan = rt.dispatch_plan(binding)
    deadline = rt.execution_deadline_sec(binding, spec)
    account = rt.token_account_key(binding)
    status = rt.status_summary(binding)

    assert plan.binding is binding, "dispatch re-derived its own binding"
    assert plan.model == binding.model_id
    assert deadline == spec.timeout_policy.execution_timeout_sec
    assert account.startswith(binding.usage_pool_id)
    assert binding.model_id in account and binding.model_id in status
    assert binding.backend_id in status


def test_a_binding_is_serialisable_and_survives_a_round_trip(policy, shipped_catalog):
    """An `Attempt` stores the binding (`§2.7`); a binding that could not be written
    down would be re-resolved at each phase, which is the defect P6 exists to close."""
    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
    )
    document = binding.to_dict()
    assert document["shape_key"]["role_version"] == policy.definition("implementer").version
    assert isinstance(document["rejected_routes"], list)
    import json

    assert json.loads(json.dumps(document)) == document


def test_the_same_inputs_resolve_to_the_same_binding_id(policy, shipped_catalog):
    first = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
    )
    second = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
    )
    assert first.binding_id == second.binding_id


# ── acceptance 2: typed wait or block, never a silent fallback ──────────────────────


def test_a_saturated_frontier_produces_a_typed_wait_and_no_binding(policy, shipped_catalog):
    resources = cat.ResourcePoolState()
    for backend_id in shipped_catalog.backend_ids():
        entry = shipped_catalog.entry(backend_id)
        pool = resources.pool(entry.usage_pool_id, concurrency_limit=entry.concurrency_limit)
        pool.active = pool.concurrency_limit

    outcome = rt.resolve_agent(node(), manifest(), resources, shipped_catalog, policy)
    assert outcome.is_bound is False
    assert outcome.disposition == "wait"
    assert outcome.reason in rt.REASON_CODES
    assert outcome.rejected_routes, "a refusal that names no evaluated route explains nothing"


def test_a_task_too_large_for_every_catalogued_ceiling_blocks_rather_than_waiting(
    policy, shipped_catalog
):
    outcome = rt.resolve_agent(
        node(), manifest(estimated_input_tokens=5_000_000), cat.ResourcePoolState(),
        shipped_catalog, policy,
    )
    assert outcome.is_bound is False
    assert outcome.disposition == "block"
    assert {route.reason for route in outcome.rejected_routes} == {"context_overflow"}
    assert outcome.retry_after_sec is None, "waiting cannot make a context window larger"


def test_the_free_objective_never_silently_bills_a_subscription_pool(policy, shipped_catalog):
    """Every shipped catalog entry is `subscription`; under `free_quality` the honest
    answer is a block, not a quiet hop onto paid capacity (`§3.1`)."""
    free_policy = policy.with_objective("free_quality")
    outcome = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), shipped_catalog, free_policy
    )
    assert outcome.is_bound is False
    assert outcome.disposition == "block"
    assert all(route.reason == "billing_not_permitted" for route in outcome.rejected_routes)


def test_every_refusal_carries_a_reason_from_the_closed_vocabulary(policy, shipped_catalog):
    outcomes = [
        rt.resolve_agent(node(), manifest(estimated_input_tokens=5_000_000),
                         cat.ResourcePoolState(), shipped_catalog, policy),
        rt.resolve_agent(node(), manifest(), cat.ResourcePoolState(),
                         cat.CapabilityCatalog(), policy),
    ]
    for outcome in outcomes:
        assert outcome.reason in rt.REASON_CODES
        assert outcome.disposition in ("wait", "block")
        assert outcome.detail


# ── acceptance 3: exhaustion pauses dispatch, not running workers ───────────────────


def test_quota_exhaustion_pauses_the_pool_and_leaves_running_workers_alone(
    policy, shipped_catalog
):
    resources = cat.ResourcePoolState()
    claude = shipped_catalog.entry("claude")
    pool = resources.pool(claude.usage_pool_id, concurrency_limit=claude.concurrency_limit)
    pool.reserve()
    pool.reserve()

    resources.observe(
        cat.QuotaObservation(
            pool_id=claude.usage_pool_id,
            windows={300: cat.QuotaWindow(300, used_pct=97.0, resets_at=5_000)},
            observed_at=cat.utc_now_iso(),
            written_at=cat.utc_now_iso(),
            acquisition="push",
        )
    )
    assert claude.usage_pool_id in resources.pause_exhausted_pools()
    assert pool.active == 2, "pausing an exhausted pool killed its healthy workers"

    outcome = rt.resolve_agent(
        node(), manifest(), resources,
        cat.CapabilityCatalog.of([claude]), policy, now_epoch=1_000.0,
    )
    assert outcome.is_bound is False
    assert outcome.disposition == "wait"
    assert outcome.retry_after_sec == 4_000.0

    pool.release()
    pool.release()
    assert pool.active == 0, "a paused pool refused to let its workers finish"


def test_an_exhausted_pool_does_not_block_a_healthy_one(policy, shipped_catalog):
    """`05_SCHEDULING…` §1: a saturated pool is not a dependency edge for other work."""
    resources = cat.ResourcePoolState()
    claude = shipped_catalog.entry("claude")
    resources.pool(claude.usage_pool_id, concurrency_limit=claude.concurrency_limit)
    resources.observe(
        cat.QuotaObservation(
            pool_id=claude.usage_pool_id,
            windows={300: cat.QuotaWindow(300, used_pct=99.0, resets_at=5_000)},
            observed_at=cat.utc_now_iso(),
            written_at=cat.utc_now_iso(),
        )
    )
    resources.pause_exhausted_pools()

    binding = rt.resolve_agent(
        node("diagnoser", node_id="diagnoser"), manifest(), resources,
        shipped_catalog, policy, now_epoch=1_000.0,
    )
    assert binding.is_bound, "a paused pool turned another backend's work into a wait"
    assert binding.backend_id != "claude"
    assert resources.pool(claude.usage_pool_id).paused_until is not None, (
        "the exhausted pool quietly un-paused itself while other work was routed"
    )


def test_a_carried_forward_reading_is_not_evidence_of_headroom():
    """`[INV-A07]`: a re-dated sample must not read as a fresh measurement."""
    observation = cat.QuotaObservation(
        pool_id="p",
        windows={300: cat.QuotaWindow(300, used_pct=10.0)},
        observed_at=cat.utc_now_iso(),
        written_at=cat.utc_now_iso(),
    )
    assert observation.fresh_enough()
    assert not observation.carry_forward().fresh_enough()


# ── outcome statistics feed back into routing ───────────────────────────────────────


def test_verified_outcomes_accumulate_against_the_bindings_own_shape(policy, shipped_catalog):
    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), shipped_catalog, policy
    )
    stats = cat.OutcomeStatistics(minimum_observations=2)
    rt.record_outcome(stats, binding, success=True)
    rt.record_outcome(stats, binding, success=False)
    assert stats.success_rate(binding.shape_key) == 0.5

    # A different template version is a different shape, not more evidence for this one.
    other = rt.resolve_agent(
        node(), manifest(template_version="2"), cat.ResourcePoolState(),
        shipped_catalog, policy,
    )
    assert stats.observations(other.shape_key) == 0


# ── P07B: Antigravity as a light / balanced backend ─────────────────────────────────


AGY = "antigravity_cli"


def agy_only(shipped_catalog) -> cat.CapabilityCatalog:
    return cat.CapabilityCatalog.of([shipped_catalog.entry(AGY)])


def light_policy(policy):
    """A test-local role whose floor is `light`: no shipped role yields light work."""
    from dataclasses import replace

    base = policy.definition("implementer")
    role = replace(
        base,
        role_id="light_fixer",
        permitted_backend_capabilities={
            **dict(base.permitted_backend_capabilities),
            "min_reasoning_strength": "light",
        },
    )
    return replace(policy, agent_definitions={**dict(policy.agent_definitions), "light_fixer": role})


def test_balanced_work_binds_only_gemini_3_8_flash_high_on_agy(policy, shipped_catalog):
    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), agy_only(shipped_catalog), policy
    )
    assert binding.is_bound, binding.to_dict()
    assert (binding.backend_id, binding.model_id) == (AGY, "gemini-3.8-flash-high")
    rejected = {route.model_id for route in binding.rejected_routes}
    assert rejected == set(shipped_catalog.entry(AGY).available_models) - {"gemini-3.8-flash-high"}


def test_strong_work_never_binds_agy(policy, shipped_catalog):
    for role in ("diagnoser", "planner", "reviewer"):
        binding = rt.resolve_agent(
            node(role, node_id=role), manifest(), cat.ResourcePoolState(),
            shipped_catalog, policy,
        )
        assert binding.is_bound and binding.backend_id != AGY, role
        alone = rt.resolve_agent(
            node(role, node_id=role), manifest(), cat.ResourcePoolState(),
            agy_only(shipped_catalog), policy,
        )
        assert alone.is_bound is False, f"{role} bound to agy: {alone.to_dict()}"
    high_risk = rt.resolve_agent(
        node(), manifest(risk="high"), cat.ResourcePoolState(), shipped_catalog, policy
    )
    assert high_risk.is_bound and high_risk.backend_id != AGY


def test_an_exhausted_agy_pool_sends_balanced_work_to_claude_or_codex(policy, shipped_catalog):
    resources = cat.ResourcePoolState()
    agy = shipped_catalog.entry(AGY)
    resources.pool(agy.usage_pool_id, concurrency_limit=agy.concurrency_limit)
    resources.observe(
        cat.QuotaObservation(
            pool_id=agy.usage_pool_id,
            windows={300: cat.QuotaWindow(300, used_pct=100.0, resets_at=5_000)},
            observed_at=cat.utc_now_iso(),
            written_at=cat.utc_now_iso(),
            acquisition="push",
        )
    )
    assert agy.usage_pool_id in resources.pause_exhausted_pools()

    binding = rt.resolve_agent(
        node(), manifest(), resources, shipped_catalog, policy, now_epoch=1_000.0
    )
    assert binding.is_bound, "a paused agy pool turned routable work into a wait"
    assert binding.backend_id in ("claude", "codex")

    alone = rt.resolve_agent(
        node(), manifest(), resources, agy_only(shipped_catalog), policy, now_epoch=1_000.0
    )
    assert alone.is_bound is False and alone.disposition == "wait"


def test_a_light_task_can_bind_an_agy_light_model(policy, shipped_catalog):
    local = light_policy(policy)
    binding = rt.resolve_agent(
        node("light_fixer", node_id="light_fixer"), manifest(risk="low"),
        cat.ResourcePoolState(), agy_only(shipped_catalog), local,
    )
    assert binding.is_bound, binding.to_dict()
    assert binding.backend_id == AGY
    assert shipped_catalog.entry(AGY).pair(binding.model_id, binding.requested_effort or None).strength == "light"


def test_no_effort_value_is_sent_to_agy(policy, shipped_catalog):
    entry = shipped_catalog.entry(AGY)
    assert entry.effort_controls.supported is False

    binding = rt.resolve_agent(
        node(), manifest(), cat.ResourcePoolState(), agy_only(shipped_catalog), policy
    )
    assert binding.is_bound
    assert binding.effort_delivery == "unsupported"
    assert binding.effort_native_value is None
    plan = rt.dispatch_plan(binding)
    assert plan.effort_args == () and plan.effort_config == ()
    assert plan.model == "gemini-3.8-flash-high"
