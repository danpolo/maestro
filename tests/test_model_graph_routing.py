"""Weekly pace ranks eligible pools; capabilities and quota remain hard gates."""
from dataclasses import replace

from maestro.backends import catalog as cat
from maestro.backends import router
from tests.backends.test_router import resolve, cheap_backend, strong_backend, catalog_of, observed_pace


def test_weekly_pace_precedes_cross_pool_cost():
    resources = cat.ResourcePoolState()
    observed_pace(resources, 'strong-pool', 10)
    binding = resolve(resources=resources)
    assert binding.backend_id == 'strong_cli'
    assert binding.model_id == 'big-1'
    assert binding.effort_delivery == 'config_key'
    assert binding.effort_native_value == 'deep'
    assert 'pool=strong-pool pace=10.0' in binding.selection_reason


def test_a_paused_best_pool_falls_through_with_explanation():
    resources = cat.ResourcePoolState()
    resources.pool('strong-pool').pause(until=2000, reason='test quota')
    observed_pace(resources, 'strong-pool', 10)
    binding = resolve(resources=resources)
    assert binding.backend_id == 'cheap_cli'
    assert any(r.backend_id == 'strong_cli' and r.reason == 'pool_paused'
               for r in binding.rejected_routes)


def test_pace_cannot_override_context_or_permission_demand():
    from maestro.backends.router import RoutingContext
    resources = cat.ResourcePoolState()
    observed_pace(resources, 'cheap-pool', 10)
    binding = resolve(resources=resources,
                      context_manifest=RoutingContext(estimated_input_tokens=130000))
    assert binding.backend_id == 'strong_cli'
    assert any(r.backend_id == 'cheap_cli' for r in binding.rejected_routes)


def test_shared_catalog_replacement_keeps_destination_metadata_without_readmitting_old_role_pins():
    from maestro.model_policy import shipped_policy_v1 as shipped_policy, replace_route, catalog_defaults
    policy = replace_route(shipped_policy(), ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    base = catalog_defaults(policy, {})
    assert 'claude-sonnet-5-5' not in base['claude']['available_models']
    assert base['codex']['usage_pool_id'] == 'codex-subscription'
    assert base['codex']['model_profiles']['gpt-6-luna'].context_limits.max_input_tokens == 340000
    explicit = catalog_defaults(policy, {'roles': {'implementer': {
        'models': {'claude': 'claude-sonnet-5-5'}}}})
    assert 'claude-sonnet-5-5' not in explicit['claude']['available_models']
    assert all(key[0] != 'claude-sonnet-5-5' for key in explicit['claude']['pair_profiles'])
    policy['retired'].append(['claude', 'claude-sonnet-5-5'])
    retired = catalog_defaults(policy, {'roles': {'implementer': {
        'models': {'claude': 'claude-sonnet-5-5'}}}})
    assert 'claude-sonnet-5-5' not in retired['claude']['available_models']


def test_launch_targets_ignore_retired_routing_harness_order():
    from maestro.model_policy import harness_drivers
    from maestro.roles import default_chain
    assert harness_drivers({'routing': {'harness_order': ['codex']}}) == default_chain()


def _provider_tables(root):
    """Disposable provider lifecycle tables, so no test reads the host's installed rows."""
    head = '| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n|---|---|---|---|\n'
    claude, codex = root / 'claude-limits.md', root / 'codex-limits.md'
    claude.write_text(head + '| Claude Sonnet 5.5 | 160-180K | 220-240K | 320K |\n'
                             '| Claude Opus 5.5 | 160-180K | 220-240K | 320K |\n')
    codex.write_text(head + '| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n'
                            '| GPT-6 Sol | 70K-80K | 110K-120K | 150K |\n')
    return f'model_limits:\n  claude: {claude}\n  codex: {codex}\n'


def test_production_graph_builder_reads_adopted_policy(tmp_path, monkeypatch):
    from maestro import orchestrator, config
    from maestro.model_policy import PolicyStore, shipped_policy_v1 as shipped_policy, replace_route, atomic_write, canonical
    from maestro.workflows import routing
    from tests.backends.test_router import agent_node
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'project.yaml').write_text(_provider_tables(tmp_path))
    store = PolicyStore()
    revision = store.publish(replace_route(shipped_policy(), ('claude', 'claude-sonnet-5-5'),
                                          ('codex', 'gpt-6-luna')), expected=None)
    atomic_write(tmp_path / '.orchestrator/model_policy.json', canonical({'revision': revision}))

    def bound():
        dispatch = orchestrator._graph_routing()
        observed_pace(dispatch.resources, 'claude-subscription', 1)
        binding = routing.resolve(dispatch, agent_node(),
                                  router.RoutingContext(estimated_input_tokens=1000), now_epoch=1000)
        assert binding.is_bound
        return dispatch, binding

    # The replaced model is gone, so the first harness serves its next approved route.
    dispatch, binding = bound()
    assert 'claude-sonnet-5-5' not in dispatch.catalog.entries['claude'].available_models
    assert (binding.backend_id, binding.model_id) == ('claude', 'claude-opus-5-5')
    # A retired harness order cannot change a current route.
    (tmp_path / 'project.yaml').write_text(
        _provider_tables(tmp_path) + 'routing:\n  harness_order: [codex, claude]\n')
    dispatch, binding = bound()
    assert not hasattr(dispatch, 'harness_order')
    assert (binding.backend_id, binding.model_id) == ('claude', 'claude-opus-5-5')
    # So does a role's `strength`; a role config does not name is not in the table.
    assert dispatch.role_strengths == {}
    (tmp_path / 'project.yaml').write_text(
        _provider_tables(tmp_path) + 'routing:\n  harness_order: [codex, claude]\n'
        'roles:\n  implementer: {strength: strong}\n')
    dispatch, binding = bound()
    assert dispatch.role_strengths == {'implementer': 'strong'}
    # Codex is still first, but its strong model has no provider row in this fixture.
    assert (binding.backend_id, binding.model_id) == ('claude', 'claude-opus-5-5')


def test_role_strengths_read_the_new_shape_and_survive_the_old_one():
    from maestro.model_policy import (configured_role_strengths, effective_roles, role_strengths,
                                      shipped_role_strengths)
    shipped = shipped_role_strengths()
    assert shipped == dict(implementer='balanced', test_designer='balanced', reviewer='strong',
                           planner='strong', diagnoser='strong')
    assert role_strengths({}) == shipped
    assert configured_role_strengths({}) == {}
    # The legacy `judge` is the graph's `reviewer`; a value that is no class reads as absent.
    project = {'roles': {'judge': {'strength': 'balanced'}, 'implementer': {'strength': 'huge'},
                         'scout': {'strength': 'light'}}}
    assert configured_role_strengths(project) == {'reviewer': 'balanced', 'scout': 'light'}
    assert role_strengths(project) == {**shipped, 'reviewer': 'balanced', 'scout': 'light'}
    # A record frozen before §17.2 holds the old shape: it reads as the shipped strengths.
    old = {'roles': {'implementer': {'backend': 'codex', 'models': {'codex': 'gpt-6-luna'}},
                     'judge': 'claude'}}
    assert configured_role_strengths(old) == {}
    assert role_strengths(old) == shipped
    # The schema 1 merge keeps a role's strength, so a frozen bootstrap task still has it.
    merged, _ = effective_roles({'roles': {'implementer': {'strength': 'strong'}}}, None)
    assert merged['roles']['implementer']['strength'] == 'strong'
