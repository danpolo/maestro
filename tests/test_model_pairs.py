"""Dan's initial pair inventory, including deliberately nonmonotonic scores.

CLI verification, 2026-10-11: Claude 2.1.296 help lists low/medium/high/xhigh/max.
Codex 0.162.1 cache lists these for Luna and Astra. Sol has no own cache entry;
each level was checked with codex exec --skip-git-repo-check --ephemeral -s read-only
-m gpt-6.1-sol -c model_reasoning_effort=<level> "Reply with the single word ok."
All five exited 0, printed their requested effort and ok. A made-up level failed
with 400 and listed none/minimal/low/medium/high/xhigh/max. No effort was substituted.
"""
import json
from pathlib import Path

from maestro import model_policy
from maestro.backends import catalog


EXPECTED = {
    'claude': {
        'claude-haiku-5-5': [(29, .09), (34, .20), (38, .32), (41, .50), (43, .90)],
        'claude-sonnet-5-5': [(36, .36), (41, .50), (47, .90), (52, 2.05), (56, 5.70)],
        'claude-opus-5-5': [(42, .42), (51, 1.00), (54, 1.36), (56, 2.60), (58, 4.50)],
    },
    'codex': {
        'gpt-6-luna': [(22, .04), (30, .12), (33, .18), (35, .29), (38, .46)],
        'gpt-6.1-sol': [(42, .62), (48, 1.00), (50, 1.52), (51, 1.86), (52, 3.43)],
        'gpt-6-astra': [(46, 2.10), (50, 4.00), (51, 4.50), (52, 6.00), (53, 8.50)],
    },
    'antigravity_cli': {
        'gemini-3.8-flash': [(33, .65), (40, 1.00), (41, 1.33)],
        'gemini-3.7-flash': [(37, .60), (40, .90), (39, 1.00)],
        'gemini-3.6-flash': [(27, .70), (31, 1.15), (34, 1.70)],
        'gemini-3.1-pro': [(27, .95), (30, 1.40)],
        'claude-sonnet-5-5': [(36, .40), (41, .70), (47, 1.00)],
        'claude-opus-5-5': [(42, .65), (51, 1.30), (54, 1.80)],
    },
}
ROLES = {
    'implementer': dict(min_intelligence=40, effort='medium', effort_rule='preference'),
    'test_designer': dict(min_intelligence=40, effort='medium', effort_rule='preference'),
    'reviewer': dict(min_intelligence=48, effort='medium', effort_rule='preference'),
    'planner': dict(min_intelligence=50, effort='high', effort_rule='minimum'),
    'diagnoser': dict(min_intelligence=50, effort='high', effort_rule='minimum'),
}


def test_shipped_policy_is_exactly_dans_47_pairs_and_role_requirements():
    policy = model_policy.shipped_policy()
    assert policy['schema'] == 2
    expected = []
    for backend, models in EXPECTED.items():
        for model, cells in models.items():
            efforts = ('low', 'high') if model == 'gemini-3.1-pro' else catalog.CANONICAL_EFFORTS
            for effort, (score, cost) in zip(efforts, cells):
                row = dict(backend_id=backend, model_id=model, effort=effort,
                           intelligence=score, relative_cost=cost)
                if backend == 'antigravity_cli' and (model.startswith('claude-') or
                        model == 'gemini-3.1-pro' or
                        (model == 'gemini-3.6-flash' and effort != 'high')):
                    row['provisional'] = True
                expected.append(row)
    assert policy['routes'] == expected
    assert policy['roles'] == ROLES
    assert len(policy['routes']) == 47
    model_policy.validate(policy)
    built = model_policy.catalog_defaults(policy, {})
    assert built['claude']['model_profiles']['claude-haiku-5-5'].context_limits == (
        catalog.ContextLimits(1_000_000, 32_000, 190_000))
    assert built['agy']['pair_profiles']['gemini-3.7-flash', 'high'].intelligence == 39
    # Native historical profiles are present, but none of them becomes a new pair.
    assert 'claude-opus-4-6-thinking' in catalog.PIN_ONLY_MODELS['antigravity_cli']
    assert 'claude-opus-4-6-thinking' not in built['agy']['available_models']
    assert 'claude-haiku-4-5-20251001' in catalog.PIN_ONLY_MODELS['claude']


def test_pair_file_is_the_source_and_each_call_is_independent():
    path = Path(model_policy.__file__).with_name('model_pairs.json')
    raw = json.loads(path.read_text())
    policy = model_policy.shipped_policy()
    assert policy['routes'] == raw['routes']
    assert policy['roles'] == raw['roles']
    policy['routes'][0]['intelligence'] = 0
    assert model_policy.shipped_policy()['routes'][0]['intelligence'] == 29


def test_real_42e771ed_frozen_policy_keeps_every_native_profile():
    policy = json.loads((Path(__file__).parent / 'fixtures/model_policy/42e771ed.json').read_text())
    assert model_policy.digest(policy) == '42e771edcb2195f4a10de4ef7c321d3b9d4536354219938e5a021bf315661374'
    built = model_policy.catalog_defaults(policy, {})
    assert {(base['backend_id'], model) for base in built.values()
            for model in base['available_models']} == {
                (r['backend_id'], r['model_id']) for r in policy['routes']}


def test_schema1_source_policy_can_still_be_loaded_and_preserved(tmp_path):
    legacy = model_policy.shipped_policy_v1()
    model_policy.validate(legacy)
    store = model_policy.PolicyStore(tmp_path)
    revision = model_policy.digest(legacy)
    assert store.load_any(revision) == legacy
    changed = model_policy.retire_routes(legacy, [('codex', 'gpt-6-astra')])
    store.publish(changed, expected=None)
    assert store.load(revision) == legacy


def test_packaged_register_delivers_all_claude_codex_pairs_and_no_agy_pair():
    from maestro import thirdparty
    built = catalog.catalog_from_register(thirdparty.load_register(),
        defaults=model_policy.catalog_defaults(model_policy.shipped_policy(), {}))
    assert len(list(built.routes())) == 30
    for entry, model, effort in built.routes():
        assert entry.backend_id != 'antigravity_cli'
        assert entry.effort_controls.resolve(effort) == (effort, 'native')
    assert len(built.entry('antigravity_cli').undeliverable_pairs()) == 17
