"""Where a project's adopted policy revision lives: one authority per project.

A migrated project (it has `.orchestrator/control.sqlite3`) records adoption as a control
store event committed in the same transaction as its idle proof. An un-migrated project has
no controller lock another process could share, so only its own running controller records
adoption, in the legacy pointer file; a CLI never migrates it by creating the database.
"""
import json
from pathlib import Path

import pytest

from maestro import config, orchestrator
from maestro.control.models import EventType
from maestro.control.store import ControlStore
from maestro.model_policy import (MODEL_POLICY_READER_VERSION, POLICY_SCHEMAS, PolicyError, PolicyStore, adopted_policy,
                                  atomic_write, canonical, digest, replace_route, shipped_policy_v1 as shipped_policy)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / 'project'
    (repo / '.orchestrator').mkdir(parents=True)
    (repo / 'project.yaml').write_text(f'project:\n  name: demo\n  repo_path: {repo}\n')
    (repo / '.orchestrator/state.json').write_text(json.dumps({'in_flight': [], 'waiting_on_dan': {}}))
    (repo / '.orchestrator/current').write_text(str(ROOT))
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    monkeypatch.setattr(config, 'PROJECT_YAML', repo / 'project.yaml')
    return repo


def _published(project):
    from maestro.model_inventory import register_project
    store = PolicyStore()
    register_project(project, store)
    return store, store.publish(shipped_policy(), expected=None)


def _migrate(project):
    ControlStore.open_controller(project / '.orchestrator/control.sqlite3').close()


def _adoption_events(project):
    with ControlStore.open_client(project / '.orchestrator/control.sqlite3') as client:
        return [e for e in client.events(aggregate_id='model_policy')]


def test_migrated_project_records_adoption_as_control_event_not_pointer_file(project):
    from maestro.model_inventory import adopt
    _migrate(project)
    store, first = _published(project)
    assert adopt(project, store)['status'] == 'updated'
    second = store.publish(replace_route(store.load(first), ('claude', 'claude-sonnet-5-5'),
                                         ('codex', 'gpt-6-luna')), expected=first)
    assert adopt(project, store)['status'] == 'updated'
    events = _adoption_events(project)
    assert [e.type for e in events] == [EventType.MODEL_POLICY_ADOPTED] * 2
    assert [e.payload for e in events] == [dict(revision=first, previous=None),
                                           dict(revision=second, previous=first)]
    assert not (project / '.orchestrator/model_policy.json').exists()
    assert adopted_policy(project, store)[0] == second


def test_adoption_event_outranks_a_stray_pointer_file(project):
    from maestro.model_inventory import adopt
    _migrate(project)
    store, first = _published(project)
    assert adopt(project, store)['status'] == 'updated'
    second = store.publish(replace_route(store.load(first), ('claude', 'claude-sonnet-5-5'),
                                         ('codex', 'gpt-6-luna')), expected=first)
    (project / '.orchestrator/model_policy.json').write_text(json.dumps({'schema': 1, 'revision': second}))
    assert adopted_policy(project, store)[0] == first


def test_pointer_file_adoption_carries_over_until_first_adoption_event(project):
    """Migrating a project does not lose what its own controller adopted before."""
    _migrate(project)
    store, first = _published(project)
    (project / '.orchestrator/model_policy.json').write_text(json.dumps({'schema': 1, 'revision': first}))
    assert adopted_policy(project, store)[0] == first


def test_cli_adoption_never_migrates_an_unmigrated_project(project):
    from maestro.model_inventory import adopt
    store, _ = _published(project)
    outcome = adopt(project, store)
    assert outcome['status'] == 'deferred'
    assert 'control store' in outcome['reason']
    assert not (project / '.orchestrator/control.sqlite3').exists()
    assert not (project / '.orchestrator/model_policy.json').exists()


def test_unmigrated_legacy_controller_adopts_in_process_without_migrating(project, monkeypatch):
    store, first = _published(project)
    monkeypatch.setattr(orchestrator, 'REPO', project)
    monkeypatch.setattr(orchestrator._state_module, 'control_store', lambda: None)
    assert orchestrator._model_policy_boundary()['status'] == 'updated'
    assert not (project / '.orchestrator/control.sqlite3').exists()
    assert adopted_policy(project, store)[0] == first


def test_unmigrated_controller_still_needs_idle_proof(project, monkeypatch):
    store, _ = _published(project)
    (project / '.orchestrator/state.json').write_text(json.dumps({'in_flight': [{'task_id': '01'}]}))
    monkeypatch.setattr(orchestrator, 'REPO', project)
    monkeypatch.setattr(orchestrator._state_module, 'control_store', lambda: None)
    assert orchestrator._model_policy_boundary()['status'] == 'deferred'
    assert adopted_policy(project, store)[0] is None


def test_malformed_adoption_event_is_unreadable_not_a_moving_default(project):
    from maestro.model_inventory import adopt, adoption_status
    _migrate(project)
    store, _ = _published(project)
    assert adopt(project, store)['status'] == 'updated'
    with ControlStore.open_controller(project / '.orchestrator/control.sqlite3') as controller:
        controller.append_event(EventType.MODEL_POLICY_ADOPTED, 'model_policy', payload={'bogus': 1})
    with pytest.raises(PolicyError, match='unreadable adopted model policy'):
        adopted_policy(project, store)
    assert adoption_status(store)[0]['adoption'] == 'unreadable'


def test_corrupt_control_database_is_unreadable(project):
    _migrate(project)
    store, _ = _published(project)
    (project / '.orchestrator/control.sqlite3').write_bytes(b'not a database')
    with pytest.raises(PolicyError, match='unreadable adopted model policy'):
        adopted_policy(project, store)


# ── a shared policy this reader cannot read (P13 W8) ─────────────────────

def _publish_unreadable(store, ahead=1):
    """What a newer release's `publish` leaves behind: a shape this reader does not know."""
    doc = dict(shipped_policy(), schema=max(POLICY_SCHEMAS) + ahead)
    revision = digest(doc)
    atomic_write(store.home / 'revisions' / (revision + '.json'), canonical(doc))
    atomic_write(store.home / 'current', (revision + '\n').encode())
    return revision


def _unmigrated_controller(project, monkeypatch):
    """This process as the project's own controller; returns what it journals."""
    journal = []
    monkeypatch.setattr(orchestrator, 'REPO', project)
    monkeypatch.setattr(orchestrator._state_module, 'control_store', lambda: None)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda event, detail: journal.append((event, detail)))
    monkeypatch.setattr(orchestrator, '_unreadable_policies_journaled', set())
    return journal


def test_an_unreadable_shared_policy_is_deferred_and_the_adopted_one_stays(project):
    from maestro.model_inventory import adopt
    _migrate(project)
    store, first = _published(project)
    assert adopt(project, store)['status'] == 'updated'
    newer = _publish_unreadable(store)
    outcome = adopt(project, store)
    assert outcome['status'] == 'deferred'
    assert newer in outcome['reason']
    assert f'reader version {MODEL_POLICY_READER_VERSION}' in outcome['reason']
    assert 'self-update' in outcome['reason']
    assert adopted_policy(project, store)[0] == first
    assert [e.payload['revision'] for e in _adoption_events(project)] == [first]


def test_an_old_reader_starts_under_a_newer_shared_policy(project, monkeypatch):
    """The slice 3 incident: publication ran before this project's release moved."""
    store = PolicyStore()
    newer = _publish_unreadable(store)
    journal = _unmigrated_controller(project, monkeypatch)
    assert orchestrator._model_policy_boundary()['status'] == 'deferred'
    assert orchestrator._model_policy_boundary()['status'] == 'deferred'
    assert [event for event, _ in journal] == ['model_policy_unreadable']
    assert newer in journal[0][1]
    assert adopted_policy(project, store) == (None, None)
    assert not (project / '.orchestrator/model_policy.json').exists()


def _half_written(pointer):
    pointer.write_text('half a wri')


def _not_text(pointer):
    pointer.write_bytes(b'\xff\xfe\x00 not utf-8')


def _a_directory(pointer):
    pointer.unlink()
    pointer.mkdir()


@pytest.mark.parametrize('damage', [_half_written, _not_text, _a_directory])
def test_a_shared_pointer_that_is_not_a_revision_is_deferred_too(project, monkeypatch, damage):
    store, first = _published(project)
    journal = _unmigrated_controller(project, monkeypatch)
    assert orchestrator._model_policy_boundary()['status'] == 'updated'
    damage(store.home / 'current')
    outcome = orchestrator._model_policy_boundary()
    assert outcome['status'] == 'deferred' and 'pointer' in outcome['reason']
    assert [event for event, _ in journal] == ['model_policy_unreadable']
    assert adopted_policy(project, store)[0] == first


def test_each_unreadable_revision_is_journaled_once(project, monkeypatch):
    store = PolicyStore()
    journal = _unmigrated_controller(project, monkeypatch)
    newer = _publish_unreadable(store)
    orchestrator._model_policy_boundary()
    newest = _publish_unreadable(store, ahead=2)
    orchestrator._model_policy_boundary()
    orchestrator._model_policy_boundary()
    assert [event for event, _ in journal] == ['model_policy_unreadable'] * 2
    assert newer in journal[0][1] and newest in journal[1][1]


# ── installed source reader compatibility ────────────────────────────────

def _installed_copy(tmp_path, version):
    source = tmp_path / f'installed-{version}'
    (source / 'maestro').mkdir(parents=True)
    (source / 'maestro/__init__.py').write_text('')
    text = (ROOT / 'maestro/model_policy.py').read_text()
    marker = 'MODEL_POLICY_READER_VERSION = 5'
    assert marker in text
    (source / 'maestro/model_policy.py').write_text(text.replace(marker, f'MODEL_POLICY_READER_VERSION = {version}'))
    return source


@pytest.mark.parametrize('version,supported', [(4, False), (5, True), (6, False)])
def test_reader_marker_names_the_event_backed_record_format(project, tmp_path, version, supported):
    """Version 1 read only the pointer file; it would miss event-backed adoption."""
    from maestro.model_inventory import supports_reader
    from maestro import bootstrap
    installed = _installed_copy(tmp_path, version)
    (project / '.orchestrator/current').write_text(str(installed))
    assert bootstrap.adopted_root(repo=project) == installed
    assert supports_reader(project) is supported
