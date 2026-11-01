"""Characterize historical routing before removing model-wide grades."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import model_policy, thirdparty
from maestro.backends import catalog, router
from maestro.workflows.policy import default_policy


def historical_catalog():
    policy = json.loads((Path(__file__).parent / 'fixtures/model_policy/42e771ed.json').read_text())
    assert model_policy.digest(policy) == '42e771edcb2195f4a10de4ef7c321d3b9d4536354219938e5a021bf315661374'
    built = catalog.catalog_from_register(thirdparty.load_register(),
        defaults=model_policy.catalog_defaults(policy, {}))
    return policy, built


@pytest.mark.parametrize('role,backend,model', [
    ('implementer', 'codex', 'gpt-6.1-sol'),
    ('test_designer', 'codex', 'gpt-6.1-sol'),
    ('reviewer', 'claude', 'claude-opus-5-5'),
    ('planner', 'codex', 'gpt-6.1-sol'),
    ('diagnoser', 'codex', 'gpt-6.1-sol'),
])
@pytest.mark.parametrize('risk', ['low', 'high'])
def test_real_frozen_revision_keeps_five_role_routes_with_current_requirements(role, backend, model, risk):
    _, built = historical_catalog()
    binding = router.resolve_agent(SimpleNamespace(node_id=role, agent_definition_ref=role),
        {'risk': risk}, catalog=built, policy=default_policy(),
        role_requirement=model_policy.shipped_policy()['roles'][role])
    effort = 'high' if risk == 'high' or role in ('planner', 'diagnoser') else 'medium'
    assert (binding.backend_id, binding.model_id, binding.requested_effort) == (backend, model, effort)


def test_real_revision_preserves_native_facts_and_each_historical_pair_grade():
    policy, built = historical_catalog()
    for row in policy['routes']:
        entry = built.entry(row['backend_id'])
        assert entry.pairs_migrated is True
        efforts = catalog.MIGRATION_EFFORTS if entry.effort_controls.supported else (None,)
        assert [(p.effort, p.strength, p.relative_cost, p.intelligence)
                for p in entry.pairs(row['model_id'])] == [
            (effort, row['strength'], row['relative_cost'], None) for effort in efforts]
    # These observed facts include historical AGY pools/windows and capacity flags;
    # removing grades must not change any of them.
    facts = {e.backend_id: {m: dict(limits=e.limits_for(m).to_dict(), pool=e.pool_for(m),
        verified=e.profile(m).native_context_verified, pairs=[p.to_dict() for p in e.pairs(m)])
        for m in e.available_models} for e in built.entries.values()}
    assert model_policy.digest(facts) == 'e8d34eeebda51e6643e6fbb438966ba41d91f8eb0317497ba2544ab7956d271e'
