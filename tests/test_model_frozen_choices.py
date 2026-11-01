"""Real launch and bounded-call seams preserve concrete choices over task retries."""
from types import SimpleNamespace
import importlib
import json
import sys

import pytest
import yaml

from maestro import agentcall, config, implementer, orchestrator
from maestro.model_policy import freeze_task, shipped_policy_v1 as shipped_policy
from tests.test_model_task_pins import project, _adopt  # noqa: F401
from tests.test_orchestrator_scheduling import loop_env  # noqa: F401

_REAL_LAUNCH_BACKEND = orchestrator._launch_backend
_REAL_LAUNCH_MODEL = orchestrator._launch_model
_REAL_BIND = orchestrator._legacy_bind_model_policy


STRONG = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-sol'}  # the shipped strong routes


@pytest.mark.parametrize('role', ['implementer', 'test_designer', 'reviewer', 'planner', 'diagnoser'])
@pytest.mark.parametrize('risk', ['low', 'medium', 'high'])
def test_unadopted_schema1_snapshot_keeps_historical_catalog_and_effort(project, monkeypatch, role, risk):
    from maestro import model_policy as mp
    from maestro.backends import catalog, router
    from maestro.workflows.policy import ProjectPolicy
    # Freeze with the pre-upgrade shipped inventory, then resume under today's code.
    with monkeypatch.context() as old:
        old.setattr(mp, 'shipped_policy', mp.shipped_policy_v1)
        record = mp.freeze_task(project, 'old', renew=True)
    assert record['revision'] is None and record['policy']['schema'] == 1
    monkeypatch.setattr(orchestrator, 'REPO', project)
    dispatch = orchestrator._graph_routing(task_id='old')
    live = orchestrator._graph_routing()
    per_run = orchestrator._graph_task_routing(live, 'old')
    assert all(entry.pairs_migrated for entry in per_run.catalog.entries.values())
    assert per_run.catalog.revision == dispatch.catalog.revision
    assert all(entry.pairs_migrated for entry in dispatch.catalog.entries.values())
    assert 'claude-haiku-4-5-20251001' in dispatch.catalog.entry('claude').available_models
    binding = router.resolve_agent(SimpleNamespace(node_id=role, agent_definition_ref=role),
        {'risk': risk}, catalog=dispatch.catalog, policy=ProjectPolicy.load(project),
        role_requirement=mp.shipped_policy()['roles'][role])
    assert binding.is_bound
    assert binding.requested_effort == ('high' if risk == 'high' or role in ('planner', 'diagnoser') else 'medium')


def test_unadopted_schema2_snapshot_survives_shipped_inventory_change(project, monkeypatch):
    from maestro import model_policy as mp
    old_policy = mp.shipped_policy()
    row = next(r for r in old_policy['routes'] if r['model_id'] == 'gpt-6.1-sol' and r['effort'] == 'low')
    row['intelligence'] = 39
    with monkeypatch.context() as old:
        old.setattr(mp, 'shipped_policy', lambda: old_policy)
        mp.freeze_task(project, 'old', renew=True)
    monkeypatch.setattr(orchestrator, 'REPO', project)
    live = orchestrator._graph_routing()
    frozen = orchestrator._graph_task_routing(live, 'old')
    scores = lambda dispatch: {p.effort: p.intelligence
        for p in dispatch.catalog.entry('codex').pairs('gpt-6.1-sol')}
    assert scores(live)['low'] == 42
    assert scores(frozen)['low'] == 39


def _implementer(project, strength, first):
    """Set the class; the retired harness preference cannot affect neutral routing."""
    document = yaml.safe_load((project / 'project.yaml').read_text())
    document['roles'] = {'implementer': {'strength': strength}}
    (project / 'project.yaml').write_text(yaml.safe_dump(document))


def _real_launch_boundary(project, monkeypatch):
    """Retain model resolution and controller binding; stub paid execution only."""
    launched = []
    monkeypatch.setattr(orchestrator, 'REPO', project)
    monkeypatch.setattr(orchestrator, '_launch_backend', _REAL_LAUNCH_BACKEND)
    monkeypatch.setattr(orchestrator, '_launch_model', _REAL_LAUNCH_MODEL)
    monkeypatch.setattr(orchestrator, '_legacy_bind_model_policy', _REAL_BIND)
    monkeypatch.setattr(orchestrator, 'launch_implementer', implementer.launch_implementer)
    monkeypatch.setattr(orchestrator, 'operator_backend', lambda: '')
    monkeypatch.setattr(orchestrator, 'exhausted_backends', lambda: [])
    monkeypatch.setattr(implementer, '_make_brief', lambda *args: 'brief')
    monkeypatch.setattr(implementer, 'append_journal', lambda *args, **kwargs: None)
    def driver(backend, session_uuid):
        return SimpleNamespace(
            capabilities=lambda: SimpleNamespace(system_prompt_file=False),
            launch=lambda spec: launched.append((backend, spec.model)) or SimpleNamespace(window='impl-01'))
    monkeypatch.setattr(implementer, '_implementer_driver', driver)
    return launched


def test_legacy_main_records_the_frozen_backend_actually_launched(loop_env, project, monkeypatch):
    _implementer(project, 'strong', 'claude')
    _adopt(project, shipped_policy())
    launched = _real_launch_boundary(project, monkeypatch)
    loop_env.runnable = [{'id': '01', 'title': 'frozen launch'}]
    # Change the project after the controller freezes, before the real launch.
    monkeypatch.setattr(orchestrator, 'create_worktree',
                        lambda *args: _implementer(project, 'balanced', 'codex'))
    assert orchestrator.main() == 0
    entry, = loop_env.state['in_flight']
    assert launched == [('codex', 'gpt-6.1-sol')]
    assert (entry['backend'], entry['model']) == launched[0]


def test_legacy_retry_without_old_backend_keeps_frozen_project_choice(loop_env, project, monkeypatch):
    _implementer(project, 'strong', 'claude')
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True, task={'id': '01'})
    # Only the role's class is frozen. A missing backend follows the neutral pool order.
    _implementer(project, 'balanced', 'claude')
    launched = _real_launch_boundary(project, monkeypatch)
    old = {'task_id': '01', 'session_id': 'old'}
    loop_env.runnable = [{'id': '01'}]
    loop_env.state['in_flight'] = [old]
    in_flight = []
    orchestrator._do_retry('01', old, 'retry', {}, in_flight)
    entry, = in_flight
    assert launched == [('codex', 'gpt-6.1-sol')]
    assert (entry['backend'], entry['model']) == launched[0]


@pytest.mark.parametrize('boundary', ['freeze', 'prepare', 'launch'])
@pytest.mark.parametrize('change', ['operator', 'quota'])
def test_main_reserves_launches_and_records_one_backend_despite_live_drift(
        loop_env, project, monkeypatch, boundary, change):
    _implementer(project, 'strong', 'claude')
    _adopt(project, shipped_policy())
    launched = _real_launch_boundary(project, monkeypatch)
    live = {'changed': False}
    pin = lambda: 'claude' if change == 'operator' and live['changed'] else ''
    exhausted = lambda: ['codex'] if change == 'quota' and live['changed'] else []
    monkeypatch.setattr(orchestrator, 'operator_backend', pin)
    monkeypatch.setattr(implementer, 'operator_backend', pin)
    monkeypatch.setattr(orchestrator, 'exhausted_backends', exhausted)
    monkeypatch.setattr(implementer.quota, 'exhausted_backends', exhausted)
    reservations = []
    monkeypatch.setattr(orchestrator, '_reserve_host_slot',
                        lambda task, sid, backend: reservations.append(backend) or 'slot')
    def bind(*args, **kwargs):
        if boundary == 'freeze':
            live['changed'] = True
        return _REAL_BIND(*args, **kwargs)
    monkeypatch.setattr(orchestrator, '_legacy_bind_model_policy', bind)
    def prepare(*args):
        if boundary == 'prepare':
            live['changed'] = True
    monkeypatch.setattr(orchestrator, 'create_worktree', prepare)
    real_launch = implementer.launch_implementer
    def launch(*args, **kwargs):
        result = real_launch(*args, **kwargs)
        if boundary == 'launch':
            live['changed'] = True
        return result
    monkeypatch.setattr(orchestrator, 'launch_implementer', launch)
    loop_env.runnable = [{'id': '01', 'title': 'live launch drift'}]
    assert orchestrator.main() == 0
    entry, = loop_env.state['in_flight']
    expected = 'claude' if boundary == 'freeze' else 'codex'
    assert reservations == [expected]
    assert launched == [(expected, STRONG[expected])]
    assert (entry['backend'], entry['model']) == launched[0]


def _roles(project, strength):
    """One class for every role the legacy loop runs; `strong` is opus on claude, `balanced`
    sonnet. The legacy `judge` reads `reviewer` (DESIGN.md §17.2)."""
    document = yaml.safe_load((project / 'project.yaml').read_text())
    document['roles'] = {role: {'strength': strength}
                         for role in ('implementer', 'reviewer', 'diagnoser')}
    (project / 'project.yaml').write_text(yaml.safe_dump(document))


def test_unfrozen_roles_follow_the_project_edit(project):
    # The contrast the frozen tests below rest on: with no task, the edit decides.
    from maestro import roles
    _adopt(project, shipped_policy())
    for strength, model in (('strong', 'claude-opus-5-5'), ('balanced', 'claude-sonnet-5-5')):
        _roles(project, strength)
        assert {roles.model_for(role, 'claude') for role in ('implementer', 'judge', 'diagnoser')} == {model}


def test_real_implementer_launch_keeps_project_family_after_config_edit(project, monkeypatch):
    _roles(project, 'strong')
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True)
    launched = []
    driver = SimpleNamespace(capabilities=lambda: SimpleNamespace(system_prompt_file=False),
                             launch=lambda spec: launched.append(spec) or SimpleNamespace(window='impl-01'))
    monkeypatch.setattr(implementer, '_implementer_driver', lambda *args: driver)
    monkeypatch.setattr(implementer, '_make_brief', lambda *args: 'brief')
    monkeypatch.setattr(implementer, 'append_journal', lambda *args, **kwargs: None)
    workspace = project / 'workspace'; workspace.mkdir()
    implementer.launch_implementer({'id': '01'}, 's1', workspace, project)
    _roles(project, 'balanced')
    implementer.launch_implementer({'id': '01'}, 's2', workspace, project)
    assert [spec.model for spec in launched] == ['gpt-6.1-sol'] * 2


def test_task_frozen_before_the_role_shape_change_still_reads_and_runs_at_shipped_strengths(project):
    from maestro import roles
    from maestro.model_policy import task_model_context, task_record
    document = yaml.safe_load((project / 'project.yaml').read_text())
    old_shape = {'implementer': {'backend': 'codex', 'models': {'claude': 'claude-opus-5-5'}}}
    (project / 'project.yaml').write_text(yaml.safe_dump(dict(document, roles=old_shape)))
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True, task={'id': '01'})
    _roles(project, 'strong')
    assert task_record(project, '01')['choices']['roles'] == old_shape
    with task_model_context(project, '01'):
        outcome = roles.resolve('implementer')
        assert (outcome.backend, outcome.model) == ('codex', 'gpt-6-luna')
        assert implementer.resolve_launch_model({'id': '01'}, 'claude')[0] == 'claude-sonnet-5-5'


def test_a_strength_set_only_in_the_global_file_is_frozen_with_the_task(project, tmp_path, monkeypatch):
    # DESIGN.md §17.2: a task freezes the merged roles (global under project).
    from maestro import roles
    from maestro.model_policy import task_model_context, task_record
    global_path = tmp_path / 'global-layer' / 'config.yaml'
    global_path.parent.mkdir()
    global_path.write_text(yaml.safe_dump({'roles': {'implementer': {'strength': 'strong'}}}))
    monkeypatch.setenv(config.GLOBAL_CONFIG_ENV, str(global_path))
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True, task={'id': '01'})
    assert task_record(project, '01')['choices']['roles']['implementer'] == {'strength': 'strong'}
    global_path.unlink()  # the task keeps the class it froze
    with task_model_context(project, '01'):
        assert roles.resolve('implementer').model == 'gpt-6.1-sol'


def test_task_model_choice_cannot_change_on_retry(project):
    _adopt(project, shipped_policy())
    task = {'id': '01', 'model': 'claude-opus-5-5'}
    # The controller freezes the task's own choice at the lifetime boundary.
    freeze_task(project, '01', renew=True, task=task)
    assert implementer.resolve_launch_model(task, 'claude')[0] == 'claude-opus-5-5'
    assert implementer.resolve_launch_model(dict(task, model='claude-sonnet-5-5'), 'claude')[0] == 'claude-opus-5-5'


@pytest.mark.parametrize('models', [
    {'claude': None}, {'claude': {'model': [], 'warning': ''}},
    {'claude': {'model': 'claude-opus-5-5', 'warning': None}},
    {'claude': {'model': 'claude-opus-5-5'}},
    {'claude': {'model': 'claude-opus-5-5', 'warning': '', 'extra': True}},
    {'unknown-driver': {'model': 'claude-opus-5-5', 'warning': ''}},
    {'claude': {'model': '  claude-opus-5-5 ', 'warning': ''}},
])
def test_rehashed_malformed_task_model_choices_are_refused(project, models):
    from maestro.model_policy import task_record, _task_file, digest, PolicyError
    _adopt(project, shipped_policy())
    record = freeze_task(project, '01', renew=True, task={'id': '01'})
    record['choices']['task_models'] = models
    record['choices_hash'] = digest(record['choices'])
    _task_file(project, '01').write_text(json.dumps(record))
    with pytest.raises(PolicyError, match='choices'):
        task_record(project, '01')


def test_task_bounded_diagnosis_uses_frozen_family(project, monkeypatch):
    from maestro.selfheal import diagnose
    from maestro.backends.base import Completion
    _roles(project, 'strong')
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True)
    _roles(project, 'balanced')
    completed = []
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: '')
    monkeypatch.setattr(agentcall, '_driver_for', lambda backend: SimpleNamespace(
        complete=lambda spec: completed.append(spec) or Completion(text='{}', returncode=0)))
    diagnose._diagnose_failure('01', 'test failure', '', '')
    assert len(completed) == 1
    assert completed[0].model == 'gpt-6.1-sol'


def test_recovered_graph_routing_keeps_frozen_role_strengths(project, monkeypatch):
    _roles(project, 'strong')
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True)
    _roles(project, 'balanced')
    monkeypatch.setattr(orchestrator, 'REPO', project)
    frozen = {role: 'strong' for role in ('implementer', 'reviewer', 'diagnoser')}
    # A restarted controller's normal builder must use the task snapshot explicitly.
    dispatch = orchestrator._graph_routing(task_id='01')
    assert dispatch.role_strengths == frozen
    # With no task it reads today's project; the per-run copy goes back to the snapshot.
    live = orchestrator._graph_routing()
    assert live.role_strengths == {role: 'balanced' for role in frozen}
    assert orchestrator._graph_task_routing(live, '01').role_strengths == frozen


@pytest.mark.parametrize('module_name', ['maestro.selfheal.selffix', 'maestro.selfheal.redo', 'maestro.hitl.ask'])
def test_detached_task_calls_keep_frozen_choices(project, monkeypatch, module_name):
    from maestro.backends.base import Completion
    module = importlib.import_module(module_name)
    _roles(project, 'strong')
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True)
    _roles(project, 'balanced')
    request = project / 'request.json'
    request.write_text(json.dumps({'task': '01', 'fix_id': 'fix-01', 'redo_id': 'redo-01',
                                  'worktree': str(project / 'wt'), 'log': str(project / 'ask.log')}))
    monkeypatch.setattr(module, 'REPO', project)
    monkeypatch.setattr(module, 'notify_telegram', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['runner', str(request)])
    if hasattr(module, '_run_git'):
        monkeypatch.setattr(module, '_run_git', lambda *args: SimpleNamespace(returncode=0, stdout='', stderr=''))
    if hasattr(module, 'SELFFIX_WORKTREE_PREFIX'):
        prefix = str(project / 'selffix-')
        monkeypatch.setattr(module, 'SELFFIX_WORKTREE_PREFIX', prefix)
        (project / 'selffix-fix-01').mkdir()
    if hasattr(module, 'sidecar_paths'):
        monkeypatch.setattr(module, 'sidecar_paths', lambda *args: (project / 'redo.log', project / 'result.json'))
    completed = []
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: '')
    def complete(spec):
        completed.append(spec)
        if spec.log_file:
            spec.log_file.write_text('answer')
        return Completion(text='answer', returncode=0)
    monkeypatch.setattr(agentcall, '_driver_for', lambda backend: SimpleNamespace(complete=complete))
    assert module.main() == 0
    assert [spec.model for spec in completed] == ['gpt-6.1-sol']


def test_task_deny_list_recommendation_keeps_frozen_diagnoser(project, monkeypatch):
    from maestro.workflows import deny_list
    from maestro.backends.base import Completion
    _roles(project, 'strong')
    _adopt(project, shipped_policy())
    freeze_task(project, '01', renew=True)
    _roles(project, 'balanced')
    completed = []
    monkeypatch.setattr(deny_list, '_agent', deny_list._default_agent)
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: '')
    monkeypatch.setattr(agentcall, '_driver_for', lambda backend: SimpleNamespace(
        complete=lambda spec: completed.append(spec) or Completion(
            text='{"hits":[{"key":"hit","recommendation":"allow","reason":"safe"}]}', returncode=0)))
    reply = deny_list.recommend(project, {'id': '01'}, [
        {'key': 'hit', 'file': 'app.py', 'pattern': 'sudo', 'kind': 'line', 'line': 'sudo'}], '')
    assert reply['hit']['recommendation'] == 'allow'
    assert [spec.model for spec in completed] == ['gpt-6.1-sol']


@pytest.mark.parametrize('alias', ['current-sol-alias', 'GPT-6.1-SOL'])
def test_fresh_task_alias_on_existing_fact_follows_adopted_version(project, monkeypatch, alias):
    from copy import deepcopy
    from maestro.model_detection import policy_for_set
    from maestro.model_policy import task_model_context
    from maestro.modelctl_bridge import ModelctlBridge
    from tests.test_model_class_transitions import metadata, event
    policy = policy_for_set(shipped_policy(), event(), None, owner=metadata())
    table = project.parent / 'codex-limits.md'
    table.write_text(table.read_text() + '| GPT-6.2 Sol | 180K-200K | 240K-270K | 350K |\n')
    _adopt(project, policy)
    moving = metadata()
    moving['models'][1]['aliases'] = [alias]
    # Owner has moved on, but the task must use the adopted 6.2 route.
    moving['models'].append(dict(deepcopy(moving['models'][-1]), model_id='gpt-6.3-sol', version=[6,3]))
    monkeypatch.setattr(ModelctlBridge, 'configured_models',
                        lambda self, backend, **k: moving if backend=='codex' else dict(contract=1, models=[]))
    task = dict(id='alias-task', model=alias)
    record = freeze_task(project, task['id'], renew=True, task=task)
    assert record['choices']['task_models']['codex']['model'] == 'gpt-6.2-sol'
    monkeypatch.setattr(ModelctlBridge, 'configured_models', lambda *a, **k: pytest.fail('retry queried owner'))
    with task_model_context(project, task['id']):
        assert implementer.resolve_launch_model(task, 'codex')[0] == 'gpt-6.2-sol'


@pytest.mark.parametrize('edit', [
    lambda c: c.update(roles=None), lambda c: c.update(roles=[]),
    lambda c: c.update(roles=''), lambda c: c['task_models'].pop('codex')])
def test_rehashed_invalid_choice_collections_are_refused(project, edit):
    from maestro.model_policy import task_record, _task_file, digest, PolicyError
    _adopt(project, shipped_policy())
    record = freeze_task(project, '01', renew=True, task=dict(id='01'))
    edit(record['choices']); record['choices_hash'] = digest(record['choices'])
    _task_file(project, '01').write_text(json.dumps(record))
    with pytest.raises(PolicyError, match='choices'):
        task_record(project, '01')
