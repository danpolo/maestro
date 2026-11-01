from copy import deepcopy
import pytest
from tests.hang_bound import HANG_SECONDS
from maestro.model_policy import PolicyStore, shipped_policy_v1 as shipped_policy, effective_roles, catalog_defaults


@pytest.fixture(autouse=True)
def schema1_bootstrap(monkeypatch):
    # Historical model-wide transitions; schema 2 successors are covered separately.
    from maestro import model_policy
    monkeypatch.setattr(model_policy, 'shipped_policy', model_policy.shipped_policy_v1)


def metadata():
    result = dict(contract=1, table_hash='table-1', models=[
        dict(backend_id='codex', model_id='gpt-6-sol', family='sol', version=[6],
             enforced_window=420000, set=True),
        dict(backend_id='codex', model_id='gpt-6.1-sol', family='sol', version=[6,1],
             enforced_window=420000, set=True),
        dict(backend_id='codex', model_id='gpt-6.2-sol', family='sol', version=[6,2],
             enforced_window=390000, set=True)])
    for row in result['models']:
        row['lifecycle'] = dict(prepare=[180000,200000],fresh=[240000,270000],ceiling=350000)
    return result


class Owner:
    def configured_models(self, backend, *, identities=()): return metadata()


def event():
    return dict(kind='MODEL_SET', event_id='switch-1', provider='codex', model_id='gpt-6.2-sol',
                family='sol', version=[6,2], predecessor_id='gpt-6.1-sol', enforced_window=390000)


def test_model_set_follows_class_and_inherits_same_backend_profile():
    from maestro import model_detection
    assert hasattr(model_detection, 'policy_for_set'), 'MODEL_SET policy use case is missing'
    policy = model_detection.policy_for_set(shipped_policy(), event(), Owner())
    effective, provenance = effective_roles({'roles': {'judge': {'models': {'codex': 'gpt-6-sol'}}}}, policy)
    assert effective['roles']['judge']['models']['codex'] == 'gpt-6.2-sol'
    assert provenance['judge']['codex'] == 'project'
    defaults = catalog_defaults(policy, {})['codex']
    profile = defaults['model_profiles']['gpt-6.2-sol']
    pair = defaults['pair_profiles']['gpt-6.2-sol', 'medium']
    previous = catalog_defaults(shipped_policy(), {})['codex']['pair_profiles']['gpt-6.1-sol', 'medium']
    assert (pair.strength, pair.relative_cost) == (previous.strength, previous.relative_cost)
    assert profile.context_limits.max_input_tokens == 390000
    assert 'gpt-6.1-sol' not in defaults['available_models']


def test_transition_after_pruning_a_dynamic_predecessor_keeps_progressing():
    from maestro.model_detection import policy_for_set
    owner = metadata()
    policy = policy_for_set(shipped_policy(), event(), None, owner=owner)
    for minor in (3, 4):
        owner = deepcopy(owner)  # each subprocess metadata response is independent
        successor = dict(owner['models'][-1], model_id=f'gpt-6.{minor}-sol', version=[6, minor])
        owner['models'].append(successor)
        if minor == 4:
            row = next(r for r in owner['models'] if r['model_id'] == 'gpt-6.2-sol')
            row.update(set=False, enforced_window=None)
            row.pop('lifecycle')
        evt = dict(event(), event_id=f'switch-{minor}', model_id=successor['model_id'],
                   version=successor['version'], predecessor_id=f'gpt-6.{minor-1}-sol')
        policy = policy_for_set(policy, evt, None, owner=owner)
    assert catalog_defaults(policy, {})['codex']['available_models'].count('gpt-6.4-sol') == 1
    assert any(r['model_id'] == 'gpt-6.2-sol' and not r['set'] for r in policy['source']['class_metadata'])


@pytest.mark.parametrize('role', ['codex', {'backend': 'codex', 'model': 'gpt-6-sol'}])
def test_automatic_publication_accepts_supported_role_forms(tmp_path, role):
    import yaml
    from maestro.model_detection import consume_set
    from maestro.model_inventory import register_project
    store = PolicyStore(tmp_path / 'models'); repo = tmp_path / 'project'; repo.mkdir()
    (repo / 'project.yaml').write_text(yaml.safe_dump(dict(
        project=dict(name='shorthand', repo_path=str(repo)), roles=dict(implementer=role))))
    register_project(repo, store)
    class IdentityOwner(Owner):
        def configured_models(self, backend, *, identities=()):
            if isinstance(role, dict):
                assert 'gpt-6-sol' in identities
            return metadata()
    assert consume_set(store, IdentityOwner(), event()) == 1
    assert store.current() is not None
    from maestro.model_inventory import project_report
    report = project_report(repo, store)
    if isinstance(role, dict):
        assert report['class_choices']['implementer']['codex']['choice'] == 'gpt-6-sol'


def test_unavailable_project_class_does_not_block_another_providers_transition(tmp_path):
    import yaml
    from maestro.model_detection import consume_set
    from maestro.model_inventory import register_project, preview_project
    from maestro.model_policy import replace_route
    store = PolicyStore(tmp_path / 'models')
    policy = replace_route(shipped_policy(), ('claude', 'claude-sonnet-5-5'), ('claude', 'claude-opus-5-5'))
    policy['source']['class_metadata'] = [dict(backend_id='claude', model_id='claude-sonnet-5-5',
        family='sonnet', version=[5, 5], set=True, enforced_window=385000)]
    before = store.publish(policy, expected=None)
    repo = tmp_path / 'project'; repo.mkdir()
    (repo / 'project.yaml').write_text(yaml.safe_dump(dict(project=dict(name='old-class', repo_path=str(repo)),
        roles=dict(implementer=dict(models=dict(claude='claude-sonnet-5-5'))))))
    register_project(repo, store)
    assert consume_set(store, Owner(), event()) == 1
    assert store.current() != before
    assert preview_project(repo, store.load(store.current()), store)['status'] == 'invalid'


def test_class_choices_do_not_cross_backends():
    from maestro import model_detection
    assert hasattr(model_detection, 'policy_for_set'), 'MODEL_SET policy use case is missing'
    bad = event(); bad['predecessor_id'] = 'claude-sonnet-5-5'
    with pytest.raises(ValueError):
        model_detection.policy_for_set(shipped_policy(), bad, Owner())


def test_class_following_pin_and_graph_use_successor_limits(tmp_path):
    from maestro import model_detection
    from maestro.model_policy import check_task_pin, graph_catalog_defaults
    policy = model_detection.policy_for_set(shipped_policy(), event(), Owner())
    table = tmp_path / 'limits.md'
    table.write_text('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
                     '|---|---|---|---|\n| GPT-6.2 Sol | 180K-200K | 240K-270K | 350K |\n')
    project = {'model_limits': {'codex': str(table)}}
    assert check_task_pin(project, policy, 'codex', 'gpt-6-sol') == 'gpt-6.2-sol'
    graph = graph_catalog_defaults(policy, project)['codex']
    assert 'gpt-6.2-sol' in graph['available_models']
    assert graph['model_profiles']['gpt-6.2-sol'].context_limits.max_input_tokens == 390000
    assert graph['model_profiles']['gpt-6.2-sol'].context_limits.effective_context_ceiling == 240000


class SetOutbox(Owner):
    def __init__(self): self.events = [event()]; self.acks = []
    def pending_events(self): return self.events
    def acknowledge(self, ident): self.acks.append(ident); self.events = []


def test_consume_set_publishes_once_and_retries_one_notice(tmp_path):
    from maestro.model_detection import consume
    store, owner = PolicyStore(tmp_path / 'models'), SetOutbox()
    delivered = []
    def offline(record): raise OSError('offline')
    first = consume(store, owner, notify=offline)
    assert store.current() is not None
    revision = store.current()
    assert first['switches_applied'] == 1 and first['notification_pending'] == 1
    second = consume(store, owner, notify=delivered.append)
    assert store.current() == revision and second['switches_applied'] == 0
    assert len(delivered) == 1 and delivered[0]['status'] == 'applied'
    consume(store, owner, notify=delivered.append)
    assert len(delivered) == 1


def test_unsafe_successor_lifecycle_refuses_publication(tmp_path):
    from maestro.model_detection import consume
    class Unsafe(SetOutbox):
        def configured_models(self, backend, *, identities=()):
            data = metadata()
            data['models'][-1]['lifecycle'] = dict(prepare=[180000,200000],fresh=[240000,270000],ceiling=500000)
            return data
    store = PolicyStore(tmp_path/'models')
    result = consume(store, Unsafe())
    assert store.current() is None and result['errors']


def test_discovery_and_set_make_only_one_switch_notice(tmp_path):
    from maestro.model_detection import consume
    owner = SetOutbox()
    discovery = dict(event_id='discovery-1', kind='NEW_MODEL_DISCOVERED', provider='codex',
                     model_id='gpt-6.2-sol', family='sol',first_seen='2026-10-07',fingerprint='f',
                     related_model_ids=['gpt-6.1-sol'])
    owner.events = [discovery, event()]
    # A real outbox acknowledges one event, not the whole batch.
    owner.acknowledge = lambda ident: owner.acks.append(ident)
    notices=[]
    result=consume(PolicyStore(tmp_path/'models'),owner,notify=notices.append)
    assert result['switches_applied']==1 and len(notices)==1
    assert notices[0]['status']=='applied'


def test_set_new_class_adds_route_without_changing_role_preferences():
    from maestro.model_detection import policy_for_set
    class NewClass(Owner):
        def configured_models(self, backend, *, identities=()):
            result = metadata()
            result['models'].append(dict(backend_id='codex',model_id='gpt-7-comet',family='comet',version=[7],
                enforced_window=420000,set=True,lifecycle=dict(prepare=[180000,200000],fresh=[240000,270000],ceiling=350000),
                routing_profile=dict(strength='strong',relative_cost=1.3,max_output_tokens=32000)))
            return result
    new_event=dict(kind='MODEL_SET',event_id='new-class',provider='codex',model_id='gpt-7-comet',family='comet',
                   version=[7],enforced_window=420000,predecessor_id=None)
    original=shipped_policy()
    policy=policy_for_set(original,new_event,NewClass())
    assert policy['roles']==original['roles']
    assert any(r['model_id']=='gpt-7-comet' for r in policy['routes'])
    assert catalog_defaults(policy,{})['codex']['pair_profiles']['gpt-7-comet', 'medium'].strength=='strong'


def test_inferred_claude_predecessor_uses_owner_aliases():
    from maestro.model_detection import policy_for_set
    class ClaudeOwner:
        def configured_models(self, backend, *, identities=()):
            return dict(contract=1, models=[
                dict(backend_id='claude',model_id='sonnet-5',aliases=['claude-sonnet-5'],
                     family='sonnet',version=[5],enforced_window=385000,set=True),
                dict(backend_id='claude',model_id='sonnet-5.5',aliases=['claude-sonnet-5-5'],
                     family='sonnet',version=[5,5],enforced_window=385000,set=True),
                dict(backend_id='claude',model_id='claude-sonnet-6',aliases=['sonnet-6'],
                     family='sonnet',version=[6],enforced_window=385000,set=True,
                     lifecycle=dict(prepare=[160000,180000],fresh=[220000,240000],ceiling=320000))])
    evt=dict(kind='MODEL_SET',event_id='sonnet6',provider='claude',model_id='claude-sonnet-6',
             family='sonnet',version=[6],enforced_window=385000,predecessor_id=None)
    policy=policy_for_set(shipped_policy(),evt,ClaudeOwner())
    effective,_=effective_roles({'roles':{'implementer':{'models':{'claude':'claude-sonnet-5'}}}},policy)
    assert effective['roles']['implementer']['models']['claude']=='claude-sonnet-6'
    profile=catalog_defaults(policy,{})['claude']['model_profiles']['claude-sonnet-6']
    pair = catalog_defaults(policy,{})['claude']['pair_profiles']['claude-sonnet-6', 'medium']
    assert pair.strength==catalog_defaults(shipped_policy(),{})['claude']['pair_profiles']['claude-sonnet-5-5', 'medium'].strength


def test_transition_removes_every_eligible_older_route_in_same_class():
    from maestro.model_detection import policy_for_set
    original=shipped_policy()
    original['routes'].append(dict(backend_id='codex',model_id='gpt-6-sol',strength='strong',relative_cost=1.0))
    original['roles']['implementer']['models']['codex']='gpt-6-sol'
    policy=policy_for_set(original,event(),Owner())
    routes={r['model_id'] for r in policy['routes'] if r['backend_id']=='codex'}
    assert not {'gpt-6-sol','gpt-6.1-sol'} & routes
    assert policy['roles']['implementer']['models']['codex']=='gpt-6.2-sol'


def test_current_model_new_event_identity_does_not_publish_another_switch(tmp_path):
    from maestro.model_detection import consume
    owner=SetOutbox(); store=PolicyStore(tmp_path/'models'); notices=[]
    consume(store,owner,notify=notices.append)
    revision=store.current()
    owner.events=[dict(event(),event_id='re-register')]
    result=consume(store,owner,notify=notices.append)
    assert not result['errors']
    assert store.current()==revision and result['switches_applied']==0
    assert 're-register' in owner.acks and len(notices)==1


def test_superseded_set_is_acknowledged_without_switch_or_notice(tmp_path):
    from maestro.model_detection import consume
    owner=SetOutbox()
    owner.events=[dict(event(),event_id='old-set',model_id='gpt-6.1-sol',version=[6,1],
                       predecessor_id='gpt-6-sol',enforced_window=420000),event()]
    owner.acknowledge=lambda ident:owner.acks.append(ident)
    notices=[]
    result=consume(PolicyStore(tmp_path/'models'),owner,notify=notices.append)
    assert not result['errors'] and set(owner.acks)=={'old-set','switch-1'}
    assert result['switches_applied']==1 and len(notices)==1


def test_replay_after_publication_reconciles_idle_project_adoption(tmp_path,monkeypatch):
    from maestro import model_detection, model_lifecycle
    from maestro.model_policy import adopted_revision
    from tests.test_model_dependencies import project
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store);owner=SetOutbox()
    real=model_lifecycle.publish_and_adopt
    def crash(preview, store, projects, *, check=None):
        store.publish(preview['policy'],expected=preview['expected_revision'],check=check)
        raise RuntimeError('crash after publication')
    monkeypatch.setattr(model_lifecycle,'publish_and_adopt',crash)
    with pytest.raises(RuntimeError,match='crash'):
        model_detection.consume(store,owner)
    revision=store.current(); assert adopted_revision(repo) is None
    monkeypatch.setattr(model_lifecycle,'publish_and_adopt',real)
    result=model_detection.consume(store,owner)
    assert not result['errors'] and store.current()==revision
    assert adopted_revision(repo)==revision and owner.acks==['switch-1']


def test_rollback_restores_immutable_class_facts_and_successor_profile():
    from maestro.model_policy import rollback_policy,digest
    from maestro.model_detection import policy_for_set
    target=policy_for_set(shipped_policy(),event(),Owner())
    restored=rollback_policy(shipped_policy(),target,digest(target))
    effective,_=effective_roles({'roles':{'judge':{'models':{'codex':'gpt-6-sol'}}}},restored)
    assert effective['roles']['judge']['models']['codex']=='gpt-6.2-sol'
    assert catalog_defaults(restored,{})['codex']['model_profiles']['gpt-6.2-sol'].context_limits.max_input_tokens==390000


@pytest.mark.parametrize('edit', [
    lambda e:e.pop('model_id'),lambda e:e.update(version='6.2'),
    lambda e:e.update(enforced_window=True),lambda e:e.update(event_id=[]),
    lambda e:e.update(predecessor_id={})])
def test_invalid_set_events_report_policy_errors_without_publication(tmp_path,edit):
    from maestro.model_detection import consume
    from maestro.model_policy import PolicyError
    owner=SetOutbox();edit(owner.events[0]);store=PolicyStore(tmp_path/'models')
    try:
        result=consume(store,owner)
        assert result['errors']
    except PolicyError:
        pass
    assert store.current() is None and not owner.acks


@pytest.mark.parametrize('edit', [
    lambda d:d.update(models=None),lambda d:d['models'][-1].update(aliases='bad'),
    lambda d:d['models'][-1].update(lifecycle=[]),
    lambda d:d['models'][-1].update(version=[True])])
def test_invalid_owner_class_metadata_is_a_policy_error(tmp_path,edit):
    from maestro.model_detection import consume
    from maestro.model_policy import PolicyError
    class Broken(SetOutbox):
        def configured_models(self,backend,*,identities=()):
            data=metadata();edit(data);return data
    store=PolicyStore(tmp_path/'models')
    try:
        result=consume(store,Broken())
        assert result['errors']
    except PolicyError:
        pass
    assert store.current() is None


@pytest.mark.parametrize('field,value', [('relative_cost',True),('relative_cost',float('inf')),
                                       ('context_limits',{'max_input_tokens':-1}),
                                       ('context_limits',None)])
def test_serialized_inherited_profiles_refuse_invalid_shapes(field,value):
    from maestro.model_detection import policy_for_set
    from maestro.model_policy import validate,PolicyError
    policy=policy_for_set(shipped_policy(),event(),Owner())
    policy['source']['inherited_profiles'][0][field]=value
    with pytest.raises(PolicyError):
        validate(policy)


def test_current_model_limits_and_supplied_profile_edit_publish_new_revision(tmp_path):
    from maestro.model_detection import consume
    store=PolicyStore(tmp_path/'models');owner=SetOutbox()
    consume(store,owner)
    first=store.current()
    class Edited(Owner):
        def configured_models(self,backend,*,identities=()):
            data=metadata();data['models'][-1].update(enforced_window=380000,
                routing_profile=dict(strength='balanced',relative_cost=0.7,max_output_tokens=28000))
            return data
    owner.configured_models=Edited().configured_models
    owner.events=[dict(event(),event_id='limits-edit',enforced_window=380000)]
    result=consume(store,owner)
    assert not result['errors'] and result['switches_applied']==1 and store.current()!=first
    profile=catalog_defaults(store.load(store.current()),{})['codex']['model_profiles']['gpt-6.2-sol']
    assert profile.context_limits.max_input_tokens==380000
    pair = catalog_defaults(store.load(store.current()),{})['codex']['pair_profiles']['gpt-6.2-sol', 'medium']
    assert (pair.strength,pair.relative_cost,profile.context_limits.max_output_tokens)==('balanced',0.7,28000)


def test_shipped_current_model_owner_edit_controls_actual_native_profile(tmp_path):
    from maestro.model_detection import consume
    class ShippedOwner(SetOutbox):
        def configured_models(self, backend, *, identities=()):
            data = metadata(); data['models'] = data['models'][:2]
            data['models'][-1].update(enforced_window=380000, routing_profile=dict(
                strength='balanced', relative_cost=0.7, max_output_tokens=28000))
            return data
    owner = ShippedOwner()
    owner.events = [dict(event(), event_id='shipped-edit', model_id='gpt-6.1-sol',
                        version=[6, 1], predecessor_id='gpt-6-sol', enforced_window=380000)]
    store = PolicyStore(tmp_path / 'models')
    result = consume(store, owner, notify=lambda record: None)
    assert not result['errors'] and result['switches_applied'] == 1
    profile = catalog_defaults(store.load(store.current()), {})['codex']['model_profiles']['gpt-6.1-sol']
    assert profile.context_limits.max_input_tokens == 380000
    pair = catalog_defaults(store.load(store.current()), {})['codex']['pair_profiles']['gpt-6.1-sol', 'medium']
    assert (pair.strength, pair.relative_cost, profile.context_limits.max_output_tokens) == ('balanced', 0.7, 28000)


@pytest.mark.parametrize('record',[None,{},dict(status='applied'),[]])
def test_malformed_switch_journal_refuses_with_policy_error(tmp_path,record):
    import json
    from maestro.model_detection import consume
    from maestro.model_policy import PolicyError
    store=PolicyStore(tmp_path/'models');directory=store.home/'switches';directory.mkdir(parents=True)
    (directory/'bad.json').write_text(json.dumps(record))
    owner=SetOutbox();owner.events=[]
    with pytest.raises(PolicyError,match='switch journal'):
        consume(store,owner,notify=lambda r:None)


def test_ack_failure_replays_applied_switch_without_another_revision_or_notice(tmp_path):
    from maestro.model_detection import consume
    from maestro.model_policy import PolicyError
    class AckRetry(SetOutbox):
        fail = True
        def acknowledge(self, ident):
            if self.fail:
                self.fail = False
                raise PolicyError('owner ack unavailable')
            super().acknowledge(ident)
    store = PolicyStore(tmp_path / 'models'); owner = AckRetry(); notices = []
    first = consume(store, owner, notify=notices.append)
    revision = store.current()
    assert first['errors'] == ['owner ack unavailable'] and first['switches_applied'] == 1
    second = consume(store, owner, notify=notices.append)
    assert not second['errors'] and second['acknowledged'] == 1
    assert second['switches_applied'] == 0 and store.current() == revision
    assert len(notices) == 1


def test_concurrent_consumers_publish_one_revision_and_deliver_one_notice(tmp_path, monkeypatch):
    import threading
    from maestro.model_detection import consume
    from maestro import model_lifecycle
    store = PolicyStore(tmp_path / 'models'); owner = SetOutbox(); notices = []; results = []
    barrier = threading.Barrier(2)
    original = model_lifecycle.publish_and_adopt
    def simultaneous_publish(*args, **kwargs):
        barrier.wait(HANG_SECONDS)
        return original(*args, **kwargs)
    monkeypatch.setattr(model_lifecycle, 'publish_and_adopt', simultaneous_publish)
    def worker():
        try:
            results.append(consume(PolicyStore(store.home), owner, notify=notices.append))
        except BaseException as exc:
            results.append(exc)
    workers = [threading.Thread(target=worker) for _ in range(2)]
    for worker_thread in workers: worker_thread.start()
    for worker_thread in workers: worker_thread.join(HANG_SECONDS)
    assert not any(worker_thread.is_alive() for worker_thread in workers)
    assert all(isinstance(result, dict) for result in results), results
    assert sum(result['switches_applied'] for result in results) == 1
    assert len(notices) == 1
    monkeypatch.setattr(model_lifecycle, 'publish_and_adopt', original)
    replay = consume(store, owner, notify=notices.append)
    assert not replay['errors'] and replay['switches_applied'] == 0 and len(notices) == 1


def test_owner_change_after_preparation_refuses_automatic_publication(tmp_path, monkeypatch):
    from maestro.model_detection import consume
    from maestro import model_lifecycle
    owner = SetOutbox(); store = PolicyStore(tmp_path / 'models')
    original = model_lifecycle.publish_and_adopt
    def changed_owner(*args, **kwargs):
        def changed(backend, *, identities=()):
            data = metadata(); data['table_hash'] = 'edited-during-lock-wait'; return data
        owner.configured_models = changed
        return original(*args, **kwargs)
    monkeypatch.setattr(model_lifecycle, 'publish_and_adopt', changed_owner)
    result = consume(store, owner, notify=lambda record: pytest.fail('refused switch announced'))
    assert result['errors'] == ['owner limits changed before automatic publication']
    assert store.current() is None and not owner.acks


@pytest.mark.parametrize('status,pending', [('prepared', False), ('unchanged', True), ('superseded', True)])
def test_switch_journal_notice_state_must_match_transition_state(tmp_path, status, pending):
    import hashlib
    from maestro.model_detection import switches
    from maestro.model_policy import canonical, PolicyError
    store = PolicyStore(tmp_path / 'models'); directory = store.home / 'switches'; directory.mkdir(parents=True)
    ident = hashlib.sha256(canonical(event()['event_id'])).hexdigest()
    record = dict(event=event(), switch_id=ident, destination=['codex', 'gpt-6.2-sol'],
                  status=status, notification_pending=pending, planned_revision='a' * 64)
    (directory / (ident + '.json')).write_bytes(canonical(record))
    with pytest.raises(PolicyError, match='switch journal'):
        switches(store)


def test_owner_alias_cannot_identify_two_different_families(tmp_path):
    from maestro.model_detection import consume
    class Ambiguous(SetOutbox):
        def configured_models(self, backend, *, identities=()):
            data = metadata()
            data['models'][0]['aliases'] = ['shared-alias']
            data['models'].append(dict(backend_id='codex', model_id='gpt-6-luna', family='luna',
                                      version=[6], set=True, enforced_window=420000, aliases=['shared-alias']))
            return data
    store = PolicyStore(tmp_path / 'models')
    result = consume(store, Ambiguous(), notify=lambda record: None)
    assert any('ambiguous' in error for error in result['errors'])
    assert store.current() is None


def test_expanded_dynamic_window_remains_bounded_by_inherited_native_profile():
    from maestro.model_detection import policy_for_set
    owner=metadata()
    first=policy_for_set(shipped_policy(), event(), None, owner=owner)
    changed=deepcopy(owner); changed['models'][-1]['enforced_window']=410000
    second=policy_for_set(first, dict(event(),event_id='expanded',enforced_window=410000), None, owner=changed)
    assert catalog_defaults(second,{})['codex']['model_profiles']['gpt-6.2-sol'].context_limits.max_input_tokens == 390000
    changed['models'].append(dict(deepcopy(changed['models'][-1]),model_id='gpt-6.3-sol',version=[6,3]))
    third=policy_for_set(second,dict(event(),event_id='next',model_id='gpt-6.3-sol',version=[6,3],
        predecessor_id='gpt-6.2-sol',enforced_window=410000),None,owner=changed)
    assert catalog_defaults(third,{})['codex']['model_profiles']['gpt-6.3-sol'].context_limits.max_input_tokens == 390000


def test_published_bootstrap_replay_settles_historical_waiting_proposal(tmp_path, monkeypatch):
    from maestro import model_detection, model_lifecycle
    from maestro.model_detection import consume, open_proposals
    store=PolicyStore(tmp_path/'models'); owner=SetOutbox()
    owner.events=[dict(event_id='discovery',kind='NEW_MODEL_DISCOVERED',provider='codex',model_id='gpt-6.2-sol',
        family='sol',first_seen='2026-10-07',fingerprint='fp',related_model_ids=['gpt-6.1-sol'])]
    consume(store,owner)
    assert open_proposals(store)
    owner.events=[event()]
    real=model_lifecycle.publish_and_adopt
    def crash(preview, store, projects, *, check=None):
        store.publish(preview['policy'],expected=preview['expected_revision'],check=check)
        raise RuntimeError('crash after publication')
    monkeypatch.setattr(model_lifecycle,'publish_and_adopt',crash)
    with pytest.raises(RuntimeError): consume(store,owner)
    revision=store.current()
    monkeypatch.setattr(model_lifecycle,'publish_and_adopt',real)
    result=consume(store,owner)
    assert not result['errors'] and store.current()==revision
    assert not open_proposals(store)


def test_current_dynamic_profile_edit_without_predecessor_preserves_inherited_profile(tmp_path):
    from maestro.model_detection import consume
    store=PolicyStore(tmp_path/'models');owner=SetOutbox()
    consume(store,owner)
    first=store.current()
    changed=metadata();changed['models'][-1]['enforced_window']=380000
    owner.configured_models=lambda *a,**k: changed
    owner.events=[dict(event(),event_id='scan-limits-edit',predecessor_id=None,enforced_window=380000)]
    result=consume(store,owner)
    assert not result['errors'] and result['switches_applied']==1 and store.current()!=first
    profile=catalog_defaults(store.load(store.current()),{})['codex']['model_profiles']['gpt-6.2-sol']
    assert profile.context_limits.max_input_tokens==380000
    pair = catalog_defaults(store.load(store.current()),{})['codex']['pair_profiles']['gpt-6.2-sol', 'medium']
    assert pair.strength==catalog_defaults(store.load(first),{})['codex']['pair_profiles']['gpt-6.2-sol', 'medium'].strength


@pytest.mark.parametrize('successor', [False, True])
def test_unverified_native_profile_is_not_borrowed_for_set_class(successor, tmp_path):
    from maestro.model_detection import consume
    rows=[dict(backend_id='codex',model_id='gpt-6-astra',family='astra',version=[6],set=True,
        enforced_window=390000,lifecycle=dict(prepare=[180000,200000],fresh=[240000,270000],ceiling=350000))]
    if successor: rows.append(dict(deepcopy(rows[0]),model_id='gpt-6.1-astra',version=[6,1]))
    target=rows[-1]
    owner=SetOutbox(); owner.configured_models=lambda *a,**k: dict(contract=1,models=rows)
    owner.events=[dict(kind='MODEL_SET',event_id='unknown-native',provider='codex',model_id=target['model_id'],
        family='astra',version=target['version'],enforced_window=390000,
        predecessor_id='gpt-6-astra' if successor else None)]
    store=PolicyStore(tmp_path/'models')
    result=consume(store,owner)
    assert any('native' in e for e in result['errors'])
    assert store.current() is None and not owner.acks
