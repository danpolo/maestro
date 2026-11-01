"""Real project state/SQLite boundaries protect active work from host switches."""
import json
from pathlib import Path

import pytest

from maestro import roles, config
from maestro.control.store import ControlStore
from maestro.model_policy import PolicyStore, adopted_policy, shipped_policy_v1 as shipped_policy, replace_route


def _provider_tables(root):
    """Disposable provider lifecycle tables, so no test reads the host's installed rows."""
    head = '| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n|---|---|---|---|\n'
    claude, codex = root / 'claude-limits.md', root / 'codex-limits.md'
    claude.write_text(head + '| Claude Sonnet 5.5 | 160-180K | 220-240K | 320K |\n'
                             '| Claude Opus 5.5 | 160-180K | 220-240K | 320K |\n')
    codex.write_text(head + '| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n'
                            '| GPT-6 Sol | 70K-80K | 110K-120K | 150K |\n')
    return f'model_limits:\n  claude: {claude}\n  codex: {codex}\n'


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / 'project'
    repo.mkdir()
    (repo / 'project.yaml').write_text(f'project:\n  name: demo\n  repo_path: {repo}\n'
                                       + _provider_tables(tmp_path))
    orch = repo / '.orchestrator'
    orch.mkdir()
    (orch / 'state.json').write_text(json.dumps({'in_flight': [], 'waiting_on_dan': {}}))
    (orch / 'current').write_text(str(Path(__file__).resolve().parents[1]))
    # A migrated project: adoption is a control-store event (see test_model_adoption_record).
    ControlStore.open_controller(orch / 'control.sqlite3').close()
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    monkeypatch.setattr(config, 'PROJECT_YAML', repo / 'project.yaml')
    return repo


def _codex_only_policy():
    """Shipped, minus every Claude route a `balanced` role may use: the implementer's only
    approved route is then Codex's (a role states a class, never a model: DESIGN.md §17.2)."""
    policy = replace_route(shipped_policy(), ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    return replace_route(policy, ('claude', 'claude-opus-5-5'), ('codex', 'gpt-6.1-sol'))


def test_idle_adoption_drives_real_role_consumer_and_keeps_halt(project):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    revision = store.publish(_codex_only_policy(), expected=None)
    (project / '.orchestrator/HALT').write_text('keep')
    register_project(project, store)
    assert roles.resolve('implementer').backend == 'codex'
    assert adopt(project, store)['status'] == 'updated'
    assert roles.resolve('implementer').backend == 'codex'
    assert roles.resolve('implementer').model == 'gpt-6-luna'
    assert (project / '.orchestrator/HALT').read_text() == 'keep'
    assert adopted_policy(project, store)[0] == revision


@pytest.mark.parametrize('field,value', [
    ('in_flight', [{'task_id': '01'}]), ('waiting_on_dan', {'D1': {'task_id': '01'}}),
    ('awaiting_dan_verification', {'01': {}}), ('phase_status', 'in_progress'),
])
def test_active_or_uncertain_legacy_state_defers_adoption(project, field, value):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    store.publish(shipped_policy(), expected=None)
    (project / '.orchestrator/state.json').write_text(json.dumps({field: value}))
    assert adopt(project, store)['status'] == 'deferred'
    assert adopted_policy(project, store)[0] is None


def test_live_controller_lock_defers_stopped_project_adoption(project):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    store.publish(shipped_policy(), expected=None)
    with ControlStore.open_controller(project / '.orchestrator/control.sqlite3'):
        assert adopt(project, store)['status'] == 'deferred'


def test_unreadable_state_and_old_reader_are_reported_truthfully(project, tmp_path):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    store.publish(shipped_policy(), expected=None)
    (project / '.orchestrator/state.json').write_text('{broken')
    assert adopt(project, store)['status'] == 'deferred'
    old = tmp_path / 'old'
    (old / 'maestro').mkdir(parents=True)
    (old / 'maestro/__init__.py').touch()
    (project / '.orchestrator/current').write_text(str(old))
    assert adopt(project, store)['status'] == 'upgrade_required'


def test_frozen_task_survives_host_change_and_restart(project):
    from maestro.model_inventory import adopt, register_project
    from maestro.model_policy import freeze_task, task_policy
    store = PolicyStore()
    register_project(project, store)
    first = store.publish(shipped_policy(), expected=None)
    adopt(project, store)
    assert freeze_task(project, '01', store)['revision'] == first
    second = store.publish(replace_route(store.load(first), ('claude', 'claude-sonnet-5-5'),
                                        ('codex', 'gpt-6-luna')), expected=first)
    (project / '.orchestrator/state.json').write_text(json.dumps({'in_flight': [{'task_id': '01'}]}))
    assert adopt(project, store)['status'] == 'deferred'
    assert task_policy(project, '01', store)['roles']['implementer']['backend'] == 'claude'
    assert store.current() == second


def test_registry_validates_project_identity_and_excludes_evidence_directory(project, tmp_path):
    from maestro.model_inventory import register_project
    from maestro.model_policy import PolicyError
    evidence = tmp_path / 'evidence'
    (evidence / '.orchestrator').mkdir(parents=True)
    with pytest.raises(PolicyError, match='project'):
        register_project(evidence, PolicyStore())


def test_removed_shared_route_is_not_resurrected_by_legacy_quota_fallback(project):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    store.publish(_codex_only_policy(), expected=None)
    adopt(project, store)
    outcome = roles.resolve('implementer', exhausted=['codex'])
    assert outcome.usable is False
    assert 'claude no eligible role model' in outcome.reason
    assert outcome.backend == 'codex'  # still the adopted route's harness, waiting on quota


def test_provider_limit_changes_affect_graph_ceiling_without_policy_lifecycle_copy(project, tmp_path):
    from maestro.model_policy import catalog_defaults, apply_provider_limits
    table = tmp_path / 'codex.md'
    table.write_text('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
                     '|---|---|---|---|\n| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n')
    defaults = catalog_defaults(shipped_policy(), {})
    updated = apply_provider_limits(defaults, {'model_limits': {'codex': str(table)}})
    assert updated['codex']['model_profiles']['gpt-6-luna'].context_limits.effective_context_ceiling == 110000
    assert updated['codex']['model_profiles']['gpt-6-luna'].context_limits.max_input_tokens == 340000


def test_project_phase_in_progress_is_not_idle_just_because_in_flight_is_empty(project):
    from maestro.model_inventory import idle_reason
    (project / '.orchestrator/state.json').write_text(json.dumps({'phase': {'id': 'P10', 'status': 'in_progress'}, 'in_flight': []}))
    assert idle_reason(project)


def test_bounded_call_refuses_when_shared_role_has_no_eligible_fallback(project, monkeypatch):
    from maestro import agentcall
    from maestro.model_inventory import adopt, register_project
    from maestro.model_policy import PolicyError
    store = PolicyStore()
    register_project(project, store)
    store.publish(_codex_only_policy(), expected=None)
    adopt(project, store)
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: '')
    monkeypatch.setattr(agentcall.quota, 'exhausted_backends', lambda: ['codex'])
    with pytest.raises(PolicyError):
        agentcall.resolve_call('implementer')


def test_literal_task_pin_is_refused_on_wrong_harness(project):
    from maestro.implementer import resolve_launch_model
    from maestro.model_policy import PolicyError
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    store.publish(shipped_policy(), expected=None)
    adopt(project, store)
    with pytest.raises(PolicyError, match='pin'):
        resolve_launch_model({'id': '01', 'model': 'gpt-6-sol'}, 'claude')


def test_controller_boundary_adopts_while_holding_existing_lock(project, monkeypatch):
    from maestro import orchestrator
    from maestro.model_inventory import register_project
    store = PolicyStore()
    register_project(project, store)
    revision = store.publish(shipped_policy(), expected=None)
    monkeypatch.setattr(orchestrator, 'REPO', project)
    with ControlStore.open_controller(project / '.orchestrator/control.sqlite3') as controller:
        monkeypatch.setattr(orchestrator._state_module, 'control_store', lambda: controller)
        assert orchestrator._model_policy_boundary()['status'] == 'updated'
    assert adopted_policy(project, store)[0] == revision
