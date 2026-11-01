"""W4 owner facts become immutable, scored, exactly deliverable routes."""
from copy import deepcopy
import pytest

from maestro import model_detection, model_policy, thirdparty
from maestro.backends import catalog
from maestro.backends.router import AgentBinding, dispatch_plan


def flash_owner():
    return dict(contract=1, table_hash='fixture', models=[dict(
        backend_id='antigravity_cli', model_id='gemini-3-8-flash',
        aliases=['gemini-3.8-flash', 'agy/Gemini 3.8 Flash',
                 'gemini-3.8-flash-low', 'gemini-3.8-flash-high'],
        family='gemini-flash', version=[3, 8], set=True, enforced_window=176000,
        canonical_model_id='gemini-3-8-flash', model_family='gemini',
        effort_variants={'low': 'gemini-3.8-flash-low', 'high': 'gemini-3.8-flash-high'},
        lifecycle=dict(display='agy/Gemini 3.8 Flash', prepare=[110000, 120000],
                       fresh=[125000, 130000], ceiling=140000))])


def flash_event():
    return dict(kind='MODEL_SET', provider='antigravity_cli', model_id='gemini-3-8-flash',
                event_id='owner-event-kept', family='gemini-flash', version=[3, 8],
                enforced_window=176000, predecessor_id=None)


def test_owner_class_alias_adopts_existing_scores_and_exact_variants_without_regrading():
    before = model_policy.shipped_policy()
    frozen = deepcopy(before)
    changed = model_detection.policy_for_set(before, flash_event(), None, owner=flash_owner())
    assert before == frozen
    old = [r for r in before['routes'] if r['backend_id'] == 'antigravity_cli'
           and r['model_id'] == 'gemini-3.8-flash']
    new = [r for r in changed['routes'] if r['backend_id'] == 'antigravity_cli'
           and r['model_id'] == 'gemini-3-8-flash']
    assert new == [dict(r, model_id='gemini-3-8-flash') for r in old]
    assert not any(r['model_id'] == 'gemini-3.8-flash' for r in changed['routes'])
    built = catalog.catalog_from_register(thirdparty.load_register(),
                                         defaults=model_policy.catalog_defaults(changed, {}))
    entry = built.entry('antigravity_cli')
    profile = entry.profile('gemini-3-8-flash')
    assert profile.canonical_model_id == 'gemini-3-8-flash'
    assert profile.model_family == 'gemini'
    assert profile.context_limits.effective_context_ceiling == 125000
    assert [p.effort for p in entry.pairs('gemini-3-8-flash')] == ['low', 'high']
    # Metadata absent for medium means no delivery; its approved score stays intact.
    assert changed['routes'] != frozen['routes']
    assert model_detection.policy_for_set(changed, flash_event(), None, owner=flash_owner()) == changed


def test_current_owner_inventory_reconciles_flash_alias_with_older_versions_present():
    # Current owner shape: SET 3.8 plus UNSET 3.7/3.6, all already shipped routes.
    # A one-row owner misses the predecessor path taken by this inventory.
    owner = flash_owner()
    current = owner['models'][0]
    current['aliases'].append('gemini-3.8-flash-medium')
    current['effort_variants']['medium'] = 'gemini-3.8-flash-medium'
    for minor in (7, 6):
        older = deepcopy(current)
        older.update(model_id=f'gemini-3-{minor}-flash', version=[3, minor], set=False,
                     canonical_model_id=f'gemini-3-{minor}-flash',
                     aliases=[f'gemini-3.{minor}-flash'],
                     effort_variants={e: f'gemini-3.{minor}-flash-{e}'
                                      for e in ('low', 'medium', 'high')})
        owner['models'].append(older)
    before = model_policy.shipped_policy()
    frozen = deepcopy(before)
    expected = [dict(r, model_id=current['model_id']) for r in before['routes']
                if r['backend_id'] == 'antigravity_cli' and r['model_id'] == 'gemini-3.8-flash']
    changed = model_detection.policy_for_set(before, flash_event(), None, owner=owner)
    flash_ids = {ident for row in owner['models']
                 for ident in [row['model_id'], *row['aliases']]}
    flash_routes = [r for r in changed['routes']
                    if r['backend_id'] == 'antigravity_cli' and r['model_id'] in flash_ids]
    assert flash_routes == expected
    assert before == frozen
    assert [r for r in changed['routes'] if r['backend_id'] != 'antigravity_cli'
            or r['model_id'] not in flash_ids] == [
                r for r in before['routes'] if r['backend_id'] != 'antigravity_cli'
                or r['model_id'] not in flash_ids]
    assert model_detection.policy_for_set(changed, flash_event(), None, owner=owner) == changed
    entry = catalog.catalog_from_register(thirdparty.load_register(),
        defaults=model_policy.catalog_defaults(changed, {})).entry('antigravity_cli')
    assert {m for m in entry.available_models if m in flash_ids} == {current['model_id']}
    assert {p.effort: p.intelligence for p in entry.pairs(current['model_id'])} == {
        r['effort']: r['intelligence'] for r in expected}


def test_legacy_role_uses_the_same_observed_variant_as_graph_dispatch():
    policy = model_detection.policy_for_set(model_policy.shipped_policy(), flash_event(),
                                           None, owner=flash_owner())
    binding = model_policy.legacy_binding({}, policy, 'implementer', driver='antigravity')
    assert isinstance(binding, AgentBinding)
    plan = dispatch_plan(binding)
    entry = model_policy.derived_roles({}, policy)['roles']['implementer']
    assert entry['models']['antigravity'] == plan.model == 'gemini-3.8-flash-high'
    assert entry['effort_controls']['antigravity'] == ((), ())


@pytest.mark.parametrize('pin,expected', [
    ('gemini-3-8-flash', 'gemini-3.8-flash-high'),
    ('gemini-3.8-flash-high', 'gemini-3.8-flash-high'),
    ('gemini-3.8-flash-low', 'gemini-3.8-flash-low'),
])
def test_manual_pin_delivers_exact_variant_to_completion(pin, expected, tmp_path, monkeypatch):
    from maestro import agentcall, config
    from maestro.backends.antigravity import AntigravityBackend, completion_argv
    from maestro.backends.base import Completion

    policy = model_detection.policy_for_set(model_policy.shipped_policy(), flash_event(),
                                           None, owner=flash_owner())
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    from pathlib import Path
    project = {'model_limits': {'antigravity': str(
        Path(__file__).parent / 'fixtures/model_limits/antigravity.md')}}
    monkeypatch.setattr(config, 'load_project_yaml', lambda: project)
    monkeypatch.setattr(model_policy, 'adopted_policy', lambda repo: (None, policy))
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: 'antigravity')
    seen = []
    def complete(self, spec):
        seen.append(completion_argv(spec))
        return Completion('ok')
    monkeypatch.setattr(AntigravityBackend, 'complete', complete)
    assert agentcall.resolve_call('implementer', model=pin) == ('antigravity', expected)
    assert agentcall.ask('implementer', 'implement', model=pin).text == 'ok'
    assert seen[0][seen[0].index('--model') + 1] == expected
    assert '--effort' not in seen[0]


@pytest.mark.parametrize('pin,expected', [
    ('gemini-3-8-flash', 'gemini-3.8-flash-high'),
    ('gemini-3.8-flash-high', 'gemini-3.8-flash-high'),
    ('gemini-3.8-flash-low', 'gemini-3.8-flash-low'),
])
def test_frozen_manual_pin_keeps_exact_variant_after_task_and_role_edits(
    pin, expected, tmp_path, monkeypatch,
):
    import json
    from pathlib import Path
    from maestro import config, implementer
    from maestro.modelctl_bridge import ModelctlBridge

    policy = model_detection.policy_for_set(model_policy.shipped_policy(), flash_event(),
                                           None, owner=flash_owner())
    before = deepcopy(policy)
    project = {'model_limits': {'antigravity': str(
        Path(__file__).parent / 'fixtures/model_limits/antigravity.md')}}
    (tmp_path / 'project.yaml').write_text(json.dumps(project))
    (tmp_path / 'global.yaml').write_text('{}')
    monkeypatch.setenv('MAESTRO_GLOBAL_CONFIG', str(tmp_path / 'global.yaml'))
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    monkeypatch.setattr(config, 'load_project_yaml', lambda: project)
    monkeypatch.setattr(model_policy, 'adopted_policy',
                        lambda *a, **kw: (model_policy.digest(policy), policy))
    # Unrelated harnesses cannot lend authority to this agy pin. No live owner reads.
    monkeypatch.setattr(ModelctlBridge, 'configured_models',
                        lambda self, backend, **kw: dict(contract=1, models=[]))
    task = dict(id='pinned', model=pin)
    store = model_policy.PolicyStore(tmp_path / 'models')
    record = model_policy.freeze_task(tmp_path, 'pinned', store=store, task=task)
    task['model'] = 'gemini-3.8-flash-high' if pin.endswith('-low') else 'gemini-3.8-flash-low'
    project['roles'] = {'implementer': {'strength': 'strong'}}
    assert model_policy.freeze_task(tmp_path, 'pinned', store=store, task=task) == record
    assert implementer.resolve_launch_model(task, 'antigravity') == (expected, '')
    assert model_policy.task_record(tmp_path, 'pinned') == record
    assert policy == before


@pytest.mark.parametrize('field,value', [
    ('canonical_model_id', ''), ('canonical_model_id', 1),
    ('model_family', ' claude '), ('model_family', None),
    ('effort_variants', []), ('effort_variants', {'turbo': 'unverified'}),
    ('effort_variants', {'high': ' '}),
    ('effort_variants', {'low': 'same', 'high': 'same'}),
])
def test_invalid_optional_owner_facts_refuse_publication(field, value):
    owner = flash_owner()
    owner['models'][0][field] = value
    with pytest.raises(model_policy.PolicyError, match='owner'):
        model_detection.policy_for_set(model_policy.shipped_policy(), flash_event(),
                                       None, owner=owner)


def test_effort_removal_keeps_scores_but_removes_delivery():
    before = model_detection.policy_for_set(model_policy.shipped_policy(), flash_event(),
                                           None, owner=flash_owner())
    owner = flash_owner()
    owner['models'][0]['effort_variants'] = {}
    changed = model_detection.policy_for_set(before, flash_event(), None, owner=owner)
    assert changed['routes'] == before['routes']
    binding = model_policy.legacy_binding({}, changed, 'implementer', driver='antigravity')
    assert not isinstance(binding, AgentBinding)


@pytest.mark.parametrize('removed', ['gemini-3-8-flash', 'gemini-3.8-flash'])
def test_retirement_resolves_owner_canonical_id_to_policy_alias_without_changing_frozen_policy(tmp_path, removed):
    from maestro.model_lifecycle import preview_retire, apply_policy_preview
    from maestro.model_policy import PolicyStore

    owner = flash_owner()
    owner['models'][0].update(set=False)
    before = model_policy.shipped_policy()
    before['source']['class_metadata'] = owner['models']
    frozen = deepcopy(before)
    store = PolicyStore(tmp_path / 'models')
    previous = store.publish(before, expected=None)

    class Removed:
        def retired(self, backend):
            assert backend == 'antigravity_cli'
            return dict(removed=[removed], state_hash='stable')

    bridge = Removed()
    preview = preview_retire(store, bridge, 'antigravity_cli')
    # The legacy shipped catalog also retains exact low/high pin identities.
    assert preview['retire'] == [
        ['antigravity_cli', 'gemini-3.8-flash'],
        ['antigravity_cli', 'gemini-3.8-flash-high'],
        ['antigravity_cli', 'gemini-3.8-flash-low']]
    assert store.current() == previous  # Preview never publishes.
    outcome = apply_policy_preview(preview, store, confirmed=True, bridge=bridge)
    changed = store.load(outcome['revision'])
    assert not any(r['backend_id'] == 'antigravity_cli' and r['model_id'] == 'gemini-3.8-flash'
                   for r in changed['routes'])
    assert [r for r in changed['routes'] if r['model_id'] != 'gemini-3.8-flash'] == [
        r for r in frozen['routes'] if r['backend_id'] != 'antigravity_cli'
        or r['model_id'] != 'gemini-3.8-flash']
    assert store.load(previous) == before == frozen


def test_retirement_aliases_do_not_cross_backend(tmp_path):
    from maestro.model_lifecycle import preview_retire
    from maestro.model_policy import PolicyStore

    before = model_policy.shipped_policy()
    row = flash_owner()['models'][0]
    row['backend_id'] = 'codex'  # A foreign owner's facts cannot grant alias authority.
    before['source']['class_metadata'] = [row]
    store = PolicyStore(tmp_path / 'models')
    revision = store.publish(before, expected=None)

    class Removed:
        def retired(self, backend):
            return dict(removed=['gemini-3-8-flash'], state_hash='stable')

    preview = preview_retire(store, Removed(), 'antigravity_cli')
    assert preview['retire'] == [] and preview['status'] == 'nothing_to_retire'
    assert store.current() == revision
