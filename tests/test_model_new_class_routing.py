"""A model unknown to `catalog.py` becomes routable from Dan's answers plus owner data.

No catalog edit and no release: the owner's MODEL_SET row (limits, window and the
routing profile Dan supplied on Telegram) plus the role assignment are the whole input.
"""
import pytest

from maestro.backends import catalog
from maestro.model_policy import (PolicyError, assign_roles, catalog_defaults, check_task_pin,
                                  effective_roles, graph_catalog_defaults, invalid_routes,
                                  shipped_policy_v1 as shipped_policy, validate)

LIFE = dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000)
TABLE = ('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
         '|---|---|---|---|\n| GPT-7 Comet | 180K-200K | 240K-270K | 350K |\n')


class Owner:
    def configured_models(self, backend, *, identities=()):
        return dict(contract=1, models=[dict(
            backend_id='codex', model_id='gpt-7-comet', family='comet', version=[7], aliases=['GPT-7 Comet'],
            enforced_window=420000, set=True, lifecycle=dict(LIFE, display='GPT-7 Comet'),
            routing_profile=dict(strength='strong', relative_cost=1.4, max_output_tokens=32000))])


def comet_policy():
    from maestro.model_detection import policy_for_set
    event = dict(kind='MODEL_SET', event_id='comet', provider='codex', model_id='gpt-7-comet',
                 family='comet', version=[7], enforced_window=420000, predecessor_id=None)
    return policy_for_set(shipped_policy(), event, Owner())


def test_new_class_is_unknown_to_the_shipped_catalog():
    assert not any('gpt-7-comet' in base['available_models'] or 'gpt-7-comet' in base.get('model_profiles', {})
                   for base in catalog.SHIPPED_CATALOG_DEFAULTS.values())


def test_unknown_model_routes_from_owner_profile_and_role_answers(tmp_path):
    policy = assign_roles(comet_policy(), ('codex', 'gpt-7-comet'), ['judge', 'diagnoser'])
    validate(policy)
    table = tmp_path / 'codex.md'
    table.write_text(TABLE)
    project = {'model_limits': {'codex': str(table)}}
    effective, _ = effective_roles(project, policy)
    assert effective['roles']['judge']['models']['codex'] == 'gpt-7-comet'
    assert effective['roles']['diagnoser']['models']['codex'] == 'gpt-7-comet'
    assert effective['roles']['implementer']['models']['codex'] == 'gpt-6-luna'
    graph = graph_catalog_defaults(policy, project)['codex']
    assert 'gpt-7-comet' in graph['available_models']
    profile = graph['model_profiles']['gpt-7-comet']
    pair = graph['pair_profiles']['gpt-7-comet', 'medium']
    assert (pair.strength, pair.relative_cost) == ('strong', 1.4)
    assert profile.context_limits.max_output_tokens == 32000
    assert profile.context_limits.effective_context_ceiling == 240000
    assert check_task_pin(project, policy, 'codex', 'gpt-7-comet') == 'gpt-7-comet'
    assert 'judge' not in invalid_routes(project, policy)


def test_role_assignment_keeps_the_old_route_eligible_and_records_provenance():
    before = comet_policy()
    after = assign_roles(before, ('codex', 'gpt-7-comet'), ['judge'])
    assert {(r['backend_id'], r['model_id']) for r in after['routes']} == \
        {(r['backend_id'], r['model_id']) for r in before['routes']}
    assert after['roles']['diagnoser'] == before['roles']['diagnoser']
    assert after['source']['kind'] == 'role_assignment'
    assert after['source']['class_metadata'] == before['source']['class_metadata']
    assert after['source']['inherited_profiles'] == before['source']['inherited_profiles']
    assert 'gpt-7-comet' in catalog_defaults(after, {})['codex']['available_models']


@pytest.mark.parametrize('pair,roles', [(('codex', 'gpt-9-none'), ['judge']),
                                        (('codex', 'gpt-7-comet'), ['nobody'])])
def test_role_assignment_refuses_ineligible_route_or_unknown_role(pair, roles):
    with pytest.raises(PolicyError):
        assign_roles(comet_policy(), pair, roles)
