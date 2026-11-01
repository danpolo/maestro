import json
import pytest
from pathlib import Path
from maestro.model_policy import PolicyStore, shipped_policy_v1 as shipped_policy, freeze_task
from maestro.model_inventory import register_project, adopt
from maestro.control.store import ControlStore


def project(tmp_path, store):
    repo = tmp_path / 'project'; repo.mkdir()
    (repo/'project.yaml').write_text(f'project:\n  name: demo\n  repo_path: {repo}\n')
    root=repo/'.orchestrator';root.mkdir()
    (root/'state.json').write_text(json.dumps({'in_flight': []}))
    (root/'current').write_text(str(Path(__file__).resolve().parents[1]))
    ControlStore.open_controller(root/'control.sqlite3').close()
    register_project(repo,store)
    return repo


def test_dependency_inventory_retains_deferred_project_and_frozen_task(tmp_path):
    from maestro import model_inventory
    assert hasattr(model_inventory, 'dependency_inventory'), 'owner retention inventory missing'
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    first=store.publish(shipped_policy(),expected=None)
    assert adopt(repo,store)['status']=='updated'
    freeze_task(repo,'01',store)
    with ControlStore.open_controller(repo/'.orchestrator/control.sqlite3') as controller:
        controller.register_task('01',spec_revision=1,status='active')
    from maestro.model_policy import replace_route
    second=store.publish(replace_route(store.load(first),('codex','gpt-6.1-sol'),('codex','gpt-6-luna')),expected=first)
    inventory=model_inventory.dependency_inventory(store)
    assert inventory['complete'] is True
    old=[r for r in inventory['dependencies'] if r['backend_id']=='codex' and r['model_id']=='gpt-6.1-sol']
    assert {r['kind'] for r in old} == {'adopted_policy','frozen_task'}
    assert inventory['current_revision']==second


def test_unreadable_registered_project_refuses_dependency_release(tmp_path):
    from maestro import model_inventory
    assert hasattr(model_inventory,'dependency_inventory'), 'owner retention inventory missing'
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    (repo/'.orchestrator/state.json').write_text('broken')
    result=model_inventory.dependency_inventory(store)
    assert result['complete'] is False and result['errors']


def test_completed_task_no_longer_retains_its_old_frozen_policy(tmp_path):
    from maestro.model_inventory import dependency_inventory
    from maestro.model_policy import replace_route
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    first=store.publish(shipped_policy(),expected=None);adopt(repo,store);freeze_task(repo,'01',store)
    with ControlStore.open_controller(repo/'.orchestrator/control.sqlite3') as controller:
        controller.register_task('01',spec_revision=1,status='accepted')
    second=store.publish(replace_route(store.load(first),('codex','gpt-6.1-sol'),('codex','gpt-6-luna')),expected=first)
    assert adopt(repo,store)['status']=='updated'
    inventory=dependency_inventory(store)
    assert inventory['complete'] is True
    assert not any(r['model_id']=='gpt-6.1-sol' for r in inventory['dependencies'])


def test_legacy_inflight_pin_outside_policy_routes_is_retained(tmp_path):
    from maestro.model_inventory import dependency_inventory
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    store.publish(shipped_policy(),expected=None);adopt(repo,store);freeze_task(repo,'01',store)
    (repo/'.orchestrator/state.json').write_text(json.dumps({'in_flight':[dict(task_id='01',backend='codex',model='gpt-6-sol')]}))
    result=dependency_inventory(store)
    assert result['complete'] is True
    assert any(r['model_id']=='gpt-6-sol' and r['kind']=='legacy_binding' for r in result['dependencies'])


def test_active_attempt_concrete_pin_is_retained_even_when_task_projection_terminal(tmp_path):
    from maestro.model_inventory import dependency_inventory
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    store.publish(shipped_policy(),expected=None);adopt(repo,store);freeze_task(repo,'01',store)
    with ControlStore.open_controller(repo/'.orchestrator/control.sqlite3') as controller:
        controller.register_task('01',spec_revision=1,status='accepted')
        with controller.transaction() as conn:
            conn.execute("INSERT INTO task_runs VALUES ('run1','01','w',1,'paused','now',NULL)")
            conn.execute("INSERT INTO attempts(attempt_id,run_id,node_id,status,backend_id,model_id) VALUES ('a','run1','n','running','codex','gpt-6-sol')")
    result=dependency_inventory(store)
    assert result['complete'] is True
    assert any(r['model_id']=='gpt-6-sol' and r['kind']=='attempt_binding' for r in result['dependencies'])


def test_canceled_task_releases_old_frozen_routes(tmp_path):
    from maestro.model_inventory import dependency_inventory
    from maestro.model_policy import replace_route
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    first=store.publish(shipped_policy(),expected=None);adopt(repo,store);freeze_task(repo,'01',store)
    with ControlStore.open_controller(repo/'.orchestrator/control.sqlite3') as controller:
        controller.register_task('01',spec_revision=1,status='canceled')
    store.publish(replace_route(store.load(first),('codex','gpt-6.1-sol'),('codex','gpt-6-luna')),expected=first)
    assert adopt(repo,store)['status']=='updated'
    result=dependency_inventory(store)
    assert result['complete'] is True
    assert not any(r['model_id']=='gpt-6.1-sol' for r in result['dependencies'])


def test_frozen_policy_retains_possible_literal_task_pins_outside_routes(tmp_path):
    from maestro.model_inventory import dependency_inventory
    store=PolicyStore(tmp_path/'models');repo=project(tmp_path,store)
    store.publish(shipped_policy(),expected=None);adopt(repo,store);freeze_task(repo,'01',store)
    with ControlStore.open_controller(repo/'.orchestrator/control.sqlite3') as controller:
        controller.register_task('01',spec_revision=1,status='active')
    result=dependency_inventory(store)
    assert result['complete'] is True
    assert any(r['model_id']=='gpt-6-sol' and r['kind']=='frozen_possible_pin' for r in result['dependencies'])


def test_misnamed_snapshot_cannot_borrow_another_tasks_terminal_proof(tmp_path):
    from maestro.model_inventory import dependency_inventory
    from maestro.model_policy import _task_file
    store = PolicyStore(tmp_path / 'models'); repo = project(tmp_path, store)
    store.publish(shipped_policy(), expected=None); adopt(repo, store)
    freeze_task(repo, '01', store)
    with ControlStore.open_controller(repo / '.orchestrator/control.sqlite3') as control:
        control.register_task('01', status='accepted')
    _task_file(repo, '02').write_bytes(_task_file(repo, '01').read_bytes())
    result = dependency_inventory(store)
    assert result['complete'] is False
    assert 'filename' in result['errors'][0]['reason']


def test_real_script_entry_needs_no_model_binding_to_qualify_inventory(tmp_path):
    from maestro.model_inventory import dependency_inventory
    store = PolicyStore(tmp_path / 'models'); repo = project(tmp_path, store)
    store.publish(shipped_policy(), expected=None); adopt(repo, store)
    entry = dict(session_id='impl-S1-now', task_id='S1', role='script', worktree='/tmp/wt-S1',
                 window='script-S1', branch='impl-s1-now', started_at='now', status='running')
    (repo / '.orchestrator/state.json').write_text(json.dumps(dict(in_flight=[entry])))
    result = dependency_inventory(store)
    assert result['complete'] is True
    assert not any(row['kind'] == 'legacy_binding' for row in result['dependencies'])


@pytest.mark.parametrize('identity', [None, [], {}, 1, '', '  ', ' 01 '])
def test_malformed_snapshot_task_identity_retains_rows(tmp_path, identity):
    import hashlib
    from maestro.model_inventory import dependency_inventory
    from maestro.model_policy import _task_file
    store=PolicyStore(tmp_path/'models'); repo=project(tmp_path,store)
    store.publish(shipped_policy(),expected=None); adopt(repo,store)
    record=freeze_task(repo,'01',store); original=_task_file(repo,'01')
    record['task_id']=identity
    destination=original if not isinstance(identity,str) else original.with_name(hashlib.sha256(identity.encode()).hexdigest()+'.json')
    original.unlink(); destination.write_text(json.dumps(record))
    result=dependency_inventory(store)
    assert result['complete'] is False and result['errors']
