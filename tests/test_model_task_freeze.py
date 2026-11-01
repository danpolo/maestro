"""A task keeps the model policy revision it started with, across runs, retries and recovery."""
import json
from pathlib import Path

import pytest

from maestro import config, orchestrator
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import ControlStore
from maestro.model_policy import PolicyError, PolicyStore, replace_route, shipped_policy_v1 as shipped_policy


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / 'project'
    repo.mkdir()
    (repo / 'project.yaml').write_text(f'project:\n  name: demo\n  repo_path: {repo}\n')
    orch = repo / '.orchestrator'
    orch.mkdir()
    (orch / 'state.json').write_text(json.dumps({'in_flight': [], 'waiting_on_dan': {}}))
    (orch / 'current').write_text(str(Path(__file__).resolve().parents[1]))
    # A migrated project: adoption is a control-store event (see test_model_adoption_record).
    ControlStore.open_controller(orch / 'control.sqlite3').close()
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    monkeypatch.setattr(config, 'PROJECT_YAML', repo / 'project.yaml')
    monkeypatch.setattr(orchestrator, 'REPO', repo)
    return repo


def _adopted(project):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    first = store.publish(shipped_policy(), expected=None)
    assert adopt(project, store)['status'] == 'updated'
    return store, first


def _move_pointer(project, store, first):
    """An out-of-band pointer change: adoption itself defers while the task is active."""
    second = store.publish(replace_route(store.load(first), ('claude', 'claude-sonnet-5-5'),
                                         ('codex', 'gpt-6-luna')), expected=first)
    from maestro.control.models import EventType
    with ControlStore.open_controller(project / '.orchestrator/control.sqlite3') as controller:
        controller.append_event(EventType.MODEL_POLICY_ADOPTED, 'model_policy',
                                payload={'revision': second, 'previous': first})
    return second


def test_continuing_task_refuses_a_moved_adopted_revision(project):
    from maestro.model_policy import freeze_task
    store, first = _adopted(project)
    assert freeze_task(project, '01', store, renew=True)['revision'] == first
    assert freeze_task(project, '01', store)['revision'] == first
    _move_pointer(project, store, first)
    with pytest.raises(PolicyError, match='mid-task'):
        freeze_task(project, '01', store)


def test_new_task_lifetime_renews_to_current_adoption(project):
    from maestro.model_policy import freeze_task
    store, first = _adopted(project)
    freeze_task(project, '01', store, renew=True)
    second = _move_pointer(project, store, first)
    assert freeze_task(project, '01', store, renew=True)['revision'] == second


def test_run_started_event_records_model_policy_revision(project):
    store = ControlStore.open_controller(project / '.orchestrator/control.sqlite3')
    try:
        store.register_task('01', spec_revision=1, status='active', at='2026-09-30T00:00:00Z')
        event = Lifecycle(store).start_run('01', 'r1', workflow_id='wf', workflow_revision=1,
                                           model_policy_revision='abc')
        assert event.payload['model_policy_revision'] == 'abc'
        plain = Lifecycle(store).start_run('01', 'r2', workflow_id='wf', workflow_revision=1)
        assert 'model_policy_revision' not in plain.payload
    finally:
        store.close()


@pytest.mark.parametrize('status,renew', [(None, True), ('planned', True), ('accepted', True),
                                          ('active', False), ('blocked', False)])
def test_graph_binding_renews_only_at_task_lifetime_start(project, status, renew, monkeypatch):
    seen = {}

    def fake_freeze(repo, task_id, store=None, *, renew=False, task=None):
        seen['renew'] = renew
        return {'revision': 'rev'}
    monkeypatch.setattr('maestro.model_policy.freeze_task', fake_freeze)
    store = ControlStore.open_controller(project / '.orchestrator/control.sqlite3')
    try:
        if status is not None:
            store.register_task('01', spec_revision=1, status=status, at='2026-09-30T00:00:00Z')
        assert orchestrator._graph_bind_model_policy(store, '01', 'rev') == 'rev'
        assert seen['renew'] is renew
    finally:
        store.close()


def test_graph_binding_refuses_routing_built_from_another_revision(project):
    store, first = _adopted(project)
    control = ControlStore.open_controller(project / '.orchestrator/control.sqlite3')
    try:
        assert orchestrator._graph_bind_model_policy(control, '01', first) == first
        control.register_task('01', spec_revision=1, status='active', at='2026-09-30T00:00:00Z')
        with pytest.raises(PolicyError, match='routing'):
            orchestrator._graph_bind_model_policy(control, '01', 'other')
        _move_pointer(project, store, first)
        with pytest.raises(PolicyError, match='mid-task'):
            orchestrator._graph_bind_model_policy(control, '01', first)
    finally:
        control.close()


def test_legacy_retry_keeps_frozen_revision_and_refuses_a_moved_one(project):
    store, first = _adopted(project)
    assert orchestrator._legacy_bind_model_policy('01', renew=True) == first
    assert orchestrator._legacy_bind_model_policy('01') == first
    _move_pointer(project, store, first)
    with pytest.raises(PolicyError, match='mid-task'):
        orchestrator._legacy_bind_model_policy('01')


def test_binding_without_shared_policy_records_no_revision(project):
    assert orchestrator._legacy_bind_model_policy('01', renew=True) is None
    assert orchestrator._legacy_bind_model_policy('01') is None
