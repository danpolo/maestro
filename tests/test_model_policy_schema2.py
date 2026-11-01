"""Policy schema 2: a route row is a `(harness, model, effort)` pair (DESIGN.md §17.3)."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from maestro import model_policy, thirdparty
from maestro.backends import catalog as cat
from maestro.backends import router
from maestro.model_policy import PolicyError, catalog_defaults, shipped_policy_v1 as shipped_policy, validate
from tests.backends.test_route_pairs import role
from tests.test_model_task_pins import project, _adopt  # noqa: F401

FIXTURE = Path(__file__).parent / 'fixtures/model_policy/schema2.json'
#: The three fixture pairs no harness can run, one of each kind.
UNDELIVERABLE = {('claude', 'claude-opus-5-5', None),          # a dial harness, no effort named
                 ('codex', 'gpt-6-luna', 'unlisted'),          # a word no harness lists as an effort
                 ('antigravity_cli', 'gemini-3.8-flash-high', 'high')}  # an effort, and no dial


@pytest.fixture
def pairs():
    return json.loads(FIXTURE.read_text())


def entries(policy, document=None):
    defaults = catalog_defaults(policy, document or {})
    return cat.catalog_from_register(thirdparty.load_register(), defaults=defaults)


# ── validate ─────────────────────────────────────────────────────────────────────────


def test_both_schemas_validate(pairs):
    validate(pairs)
    validate(shipped_policy())


def test_one_model_at_two_efforts_is_two_rows_and_a_repeated_pair_is_refused(pairs):
    assert sum(r['model_id'] == 'claude-opus-5-5' for r in pairs['routes']) == 3
    pairs['routes'].append(deepcopy(pairs['routes'][0]))
    with pytest.raises(PolicyError, match='duplicate'):
        validate(pairs)


@pytest.mark.parametrize('effort', ['', ' high', 3, False])
def test_an_effort_is_a_word_or_null(pairs, effort):
    pairs['routes'][0]['effort'] = effort
    with pytest.raises(PolicyError, match='invalid route effort'):
        validate(pairs)


def test_a_schema_2_row_must_name_its_effort_and_a_schema_1_row_must_not(pairs):
    del pairs['routes'][0]['effort']
    with pytest.raises(PolicyError, match='invalid route profile'):
        validate(pairs)
    old = shipped_policy()
    old['routes'][0]['effort'] = 'medium'
    with pytest.raises(PolicyError, match='invalid route profile'):
        validate(old)


def test_a_schema_2_row_states_an_intelligence_and_never_a_class(pairs):
    pairs['routes'][0]['strength'] = 'strong'
    with pytest.raises(PolicyError, match='invalid route profile'):
        validate(pairs)
    del pairs['routes'][0]['strength']
    validate(pairs)
    for value in (-1, 101, 50.0, True, '50', None):
        pairs['routes'][0]['intelligence'] = value
        with pytest.raises(PolicyError, match='invalid intelligence/cost profile'):
            validate(pairs)


def test_a_schema_2_row_may_be_marked_provisional_with_a_boolean_only(pairs):
    assert pairs['routes'][-1]['provisional'] is True
    pairs['routes'][0]['provisional'] = False
    validate(pairs)
    pairs['routes'][0]['provisional'] = 'yes'
    with pytest.raises(PolicyError, match='provisional'):
        validate(pairs)


def test_a_schema_2_role_states_its_floor_its_effort_and_the_rule(pairs):
    assert pairs['roles']['planner'] == {
        'min_intelligence': 50, 'effort': 'high', 'effort_rule': 'minimum'}
    good = pairs['roles']['implementer']
    for bad in ({**good, 'effort_rule': 'exact'}, {**good, 'effort': 'ultra'},
                {**good, 'min_intelligence': 101}, {**good, 'min_intelligence': True},
                {**good, 'strength': 'balanced'}, {'min_intelligence': 40, 'effort': 'medium'},
                {'backend': 'claude', 'models': {'claude': 'claude-sonnet-5-5'}}, 'balanced'):
        pairs['roles']['implementer'] = bad
        with pytest.raises(PolicyError, match='roles.implementer'):
            validate(pairs)
    pairs['roles'] = {}
    validate(pairs)


def test_the_catalog_pairs_carry_the_intelligence_and_the_derived_class(pairs):
    table = catalog_defaults(pairs, {})['claude']['pair_profiles']
    assert (table['claude-opus-5-5', 'high'].intelligence, table['claude-opus-5-5', 'high'].strength) == (
        52, 'strong')
    assert (table['claude-sonnet-5-5', 'high'].intelligence,
            table['claude-sonnet-5-5', 'high'].strength) == (44, 'balanced')
    built = entries(pairs).entries['codex'].pair('gpt-6.1-sol', 'low')
    assert (built.intelligence, built.strength, built.relative_cost) == (44, 'balanced', 0.6)


def test_a_retired_model_can_have_no_pair_and_an_unknown_schema_is_refused(pairs):
    pairs['retired'] = [['codex', 'gpt-6-astra']]
    with pytest.raises(PolicyError, match='retired route cannot be eligible'):
        validate(pairs)
    pairs['retired'] = []
    pairs['schema'] = 3
    with pytest.raises(PolicyError, match='unsupported model policy schema'):
        validate(pairs)


# ── catalog ──────────────────────────────────────────────────────────────────────────


def test_schema_2_gives_scored_pairs_and_schema_1_builds_historical_pairs(pairs):
    explicit = catalog_defaults(pairs, {})
    assert set(explicit['claude']['pair_profiles']) == {
        ('claude-opus-5-5', 'medium'), ('claude-opus-5-5', 'high'), ('claude-opus-5-5', None),
        ('claude-sonnet-5-5', 'low'), ('claude-sonnet-5-5', 'medium'), ('claude-sonnet-5-5', 'high'),
        ('claude-haiku-4-5-20251001', 'medium')}
    assert explicit['claude']['pair_profiles']['claude-sonnet-5-5', 'low'].strength == 'light'
    assert explicit['claude']['available_models'] == [
        'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001']
    # A model the policy gives no pair is not served at all.
    agy, = (base for base in explicit.values() if base['backend_id'] == 'antigravity_cli')
    assert agy['available_models'] == ['gemini-3.8-flash-high']
    historical = catalog_defaults(shipped_policy(), {})
    assert all(base['pairs_migrated'] is True for base in historical.values())
    assert all(base['pairs_migrated'] is False for base in explicit.values())
    for row in shipped_policy()['routes']:
        base = next(b for b in historical.values() if b['backend_id'] == row['backend_id'])
        assert [(base['pair_profiles'][row['model_id'], effort].strength,
                 base['pair_profiles'][row['model_id'], effort].relative_cost)
                for effort in (*cat.MIGRATION_EFFORTS, None)] == [
                    (row['strength'], row['relative_cost'])] * 3


def test_the_fixture_pairs_no_harness_delivers_are_refused_as_routes(pairs):
    built = entries(pairs)
    refused = {(e.backend_id, p.model_id, p.effort)
               for e in built.entries.values() for p in e.undeliverable_pairs()}
    assert refused == UNDELIVERABLE
    assert not any(e.pairs_migrated for e in built.entries.values())
    assert [p.effort for p in built.entries['claude'].pairs('claude-opus-5-5')] == ['medium', 'high']
    assert {(e.backend_id, m, f) for e, m, f in built.routes()}.isdisjoint(UNDELIVERABLE)
    assert len(list(built.routes())) == len(pairs['routes']) - len(UNDELIVERABLE)


def test_a_retired_model_loses_every_pair(pairs):
    pairs['routes'] = [r for r in pairs['routes'] if r['model_id'] != 'gpt-6-astra']
    pairs['retired'] = [['codex', 'gpt-6-astra']]
    base = catalog_defaults(pairs, {})['codex']
    assert 'gpt-6-astra' not in base['available_models']
    assert not any(model == 'gpt-6-astra' for model, _ in base['pair_profiles'])


def test_an_old_shape_models_pin_admits_nothing_under_schema_2(pairs):
    """Under schema 1 a role's `models:` pin re-admits a model the policy dropped. A role
    states no model now (§17.2), and a model with no approved pair has no route (§17.3)."""
    pairs['routes'] = [r for r in pairs['routes'] if r['model_id'] != 'gpt-6-astra']
    pinned = {'roles': {'implementer': {'models': {'codex': 'gpt-6-astra'}}}}
    assert 'gpt-6-astra' not in catalog_defaults(pairs, pinned)['codex']['available_models']


# ── readers ──────────────────────────────────────────────────────────────────────────


class _Policy:
    objective = 'paid_efficiency'

    def __init__(self, role_id, strength):
        self.agent_definitions = {role_id: role(strength)}


def _pick(built, role_id, strength):
    class Node:
        node_id = 'n1'
        agent_definition_ref = role_id
    resources = cat.ResourcePoolState()
    from tests.backends.test_router import observed_pace
    observed_pace(resources, 'claude-subscription', 1)
    return router.resolve_agent(Node(), None, resources, built, _Policy(role_id, strength),
                                now_epoch=1000)


def test_the_router_binds_the_cheapest_pair_of_the_class_not_the_migration_effort(pairs):
    built = entries(pairs)
    planner = _pick(built, 'planner', 'strong')
    assert (planner.backend_id, planner.model_id, planner.requested_effort) == (
        'claude', 'claude-opus-5-5', 'medium')
    implementer = _pick(built, 'implementer', 'balanced')
    assert (implementer.model_id, implementer.requested_effort) == ('claude-sonnet-5-5', 'medium')
    # Under the schema 1 reading the planner keeps the effort it used before (§17.5).
    migrated = _pick(entries(shipped_policy()), 'planner', 'strong')
    assert (migrated.model_id, migrated.requested_effort) == ('claude-opus-5-5', 'high')


def test_at_equal_cost_router_prefers_higher_pair_intelligence(project, pairs):
    # Both legacy and graph use the numerical tie-break.
    for row in pairs['routes']:
        if (row['model_id'], row['effort']) == ('claude-opus-5-5', 'medium'):
            row['relative_cost'] = 0.7  # what Sonnet at medium, a balanced pair, costs
    document = yaml.safe_load((project / 'project.yaml').read_text())
    derived = model_policy.derived_roles(document, pairs)['roles']['implementer']
    assert derived['models']['claude'] == 'claude-opus-5-5'
    assert _pick(entries(pairs, document), 'implementer', 'balanced').model_id == 'claude-opus-5-5'


def test_the_legacy_loop_derives_its_models_from_the_pairs(project, pairs):
    from maestro import roles
    _adopt(project, pairs)
    # The fixture's own document: its `model_limits` tables, never the developer's.
    document = yaml.safe_load((project / 'project.yaml').read_text())
    derived = model_policy.runtime_roles(document, project)['roles']
    assert derived['implementer']['models'] == {
        'claude': 'claude-sonnet-5-5', 'codex': 'gpt-6-luna', 'antigravity': 'gemini-3.8-flash-high'}
    assert derived['implementer']['backend'] == 'codex'
    # No strong pair on agy, so the harness is skipped for a strong role.
    assert derived['reviewer']['models'] == {'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-sol'}
    assert roles.resolve('judge').model == 'claude-opus-5-5'


def test_a_task_freezes_and_runs_under_a_schema_2_policy(project, pairs):
    from maestro import roles
    from maestro.model_policy import freeze_task, task_model_context
    _adopt(project, pairs)
    record = freeze_task(project, '01', renew=True, task={'id': '01'})
    assert record['policy']['schema'] == 2
    with task_model_context(project, '01'):
        outcome = roles.resolve('implementer')
        assert (outcome.backend, outcome.model) == ('codex', 'gpt-6-luna')


def test_role_assignment_refuses_a_schema_2_policy_by_name(pairs):
    sol = ('codex', 'gpt-6.1-sol')
    with pytest.raises(PolicyError, match='schema 1'):
        model_policy.assign_roles(pairs, sol, ['planner'])
