"""Literal task pins and implementer launches under an adopted shared policy are hard constraints."""
import json
from pathlib import Path

import pytest

from maestro import config, implementer
from maestro.control.store import ControlStore
from maestro.model_policy import PolicyError, PolicyStore, replace_route, shipped_policy_v1 as shipped_policy

TABLE = ('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
         '|---|---|---|---|\n'
         '| Claude Sonnet 5.5 | 160-180K | 220-240K | 320K |\n'
         '| Claude Opus 5.5 | 160-180K | 220-240K | 320K |\n')


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / 'project'
    repo.mkdir()
    limits = tmp_path / 'claude-limits.md'
    limits.write_text(TABLE)
    codex = tmp_path / 'codex-limits.md'
    codex.write_text('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
                     '|---|---|---|---|\n| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n'
                     '| GPT-6 Sol | 70K-80K | 110K-120K | 150K |\n'
                     # The shipped strong Codex route: without its row no strong role runs there.
                     '| GPT-6.1 Sol | 180K-200K | 240K-270K | 350K |\n')
    (repo / 'project.yaml').write_text(
        f'project:\n  name: demo\n  repo_path: {repo}\n'
        f'model_limits:\n  claude: {limits}\n  codex: {codex}\n')
    orch = repo / '.orchestrator'
    orch.mkdir()
    (orch / 'state.json').write_text(json.dumps({'in_flight': [], 'waiting_on_dan': {}}))
    (orch / 'current').write_text(str(Path(__file__).resolve().parents[1]))
    # A migrated project: adoption is a control-store event (see test_model_adoption_record).
    ControlStore.open_controller(orch / 'control.sqlite3').close()
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    monkeypatch.setattr(config, 'PROJECT_YAML', repo / 'project.yaml')
    monkeypatch.setattr(implementer, 'operator_backend', lambda: '')
    monkeypatch.setattr(implementer.quota, 'exhausted_backends', lambda: [])
    return repo


def _adopt(project, policy):
    from maestro.model_inventory import adopt, register_project
    store = PolicyStore()
    register_project(project, store)
    store.publish(policy, expected=None)
    assert adopt(project, store)['status'] == 'updated'


def test_keyword_pin_resolving_to_a_served_model_is_accepted(project):
    _adopt(project, shipped_policy())
    model, _ = implementer.resolve_launch_model({'id': '01', 'model': 'opus5'}, 'claude')
    assert model == implementer.IMPLEMENTER_MODELS['opus']


def test_literal_pin_with_ceiling_beyond_native_window_is_refused(project):
    _adopt(project, shipped_policy())
    table = project.parent / 'codex-limits.md'
    table.write_text(table.read_text().replace('150K', '500K'))
    with pytest.raises(PolicyError, match='native window'):
        implementer.resolve_launch_model({'id': '01', 'model': 'gpt-6-luna'}, 'codex')


def test_unsafe_native_window_is_reported_and_refused_by_bounded_calls(project, monkeypatch):
    from maestro import agentcall
    from maestro.model_inventory import project_report
    text = (project / 'project.yaml').read_text()
    (project / 'project.yaml').write_text(
        text + 'roles:\n  judge:\n    backend: codex\n    models:\n      codex: gpt-6-luna\n')
    _adopt(project, shipped_policy())
    table = project.parent / 'codex-limits.md'
    table.write_text(table.read_text().replace('150K', '500K'))
    report = project_report(project)
    assert 'native window' in report['invalid_routes']['judge']['codex']
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: 'codex')
    monkeypatch.setattr(agentcall.quota, 'exhausted_backends', lambda: [])
    with pytest.raises(PolicyError):
        agentcall.resolve_call('judge', model='gpt-6-luna')


def test_literal_pin_without_a_provider_lifecycle_row_is_refused(project):
    _adopt(project, shipped_policy())
    (project.parent / 'claude-limits.md').write_text(TABLE.replace('| Claude Opus 5.5 | 160-180K | 220-240K | 320K |\n', ''))
    with pytest.raises(PolicyError, match='lifecycle'):
        implementer.resolve_launch_model({'id': '01', 'model': 'claude-opus-5-5'}, 'claude')


def test_literal_pin_to_a_retired_route_is_refused(project):
    haiku = ('claude', 'claude-haiku-4-5-20251001')
    policy = shipped_policy()
    policy['routes'] = [r for r in policy['routes'] if (r['backend_id'], r['model_id']) != haiku]
    policy['retired'] = [list(haiku)]
    _adopt(project, policy)
    with pytest.raises(PolicyError, match='pin'):
        implementer.resolve_launch_model({'id': '01', 'model': haiku[1]}, 'claude')


def _strong_implementer_without_claude_opus(project):
    """A strong implementer under a policy whose only strong Claude route is retired."""
    text = (project / 'project.yaml').read_text()
    (project / 'project.yaml').write_text(text + 'roles:\n  implementer:\n    strength: strong\n')
    policy = replace_route(shipped_policy(), ('claude', 'claude-opus-5-5'), ('codex', 'gpt-6.1-sol'))
    policy['retired'] = [['claude', 'claude-opus-5-5']]
    _adopt(project, policy)


def test_implementer_backend_refuses_when_no_eligible_route(project, monkeypatch):
    _strong_implementer_without_claude_opus(project)
    monkeypatch.setattr(implementer.quota, 'exhausted_backends', lambda: ['codex'])
    with pytest.raises(PolicyError, match='implementer'):
        implementer._implementer_backend()


def test_harness_without_a_route_at_the_role_strength_is_not_handed_to_the_launch(project):
    _strong_implementer_without_claude_opus(project)
    # Claude leads `routing.harness_order`, but nothing it still serves is strong.
    backend, models = implementer._implementer_backend()
    assert backend == 'codex'
    assert 'claude' not in models
    assert models['codex'] == 'gpt-6.1-sol'
    with pytest.raises(PolicyError, match='no inherited implementer model'):
        implementer.resolve_launch_model({'id': '01'}, 'claude')


@pytest.mark.parametrize('model', ['gpt-6-sol', 'claude-haiku-4-5-20251001'])
def test_bounded_call_explicit_model_is_checked_on_the_backend_that_won(project, model, monkeypatch):
    from maestro import agentcall
    haiku = ('claude', 'claude-haiku-4-5-20251001')
    policy = shipped_policy()
    policy['routes'] = [r for r in policy['routes'] if (r['backend_id'], r['model_id']) != haiku]
    policy['retired'] = [list(haiku)]
    _adopt(project, policy)
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: '')
    monkeypatch.setattr(agentcall.quota, 'exhausted_backends', lambda: [])
    assert agentcall.resolve_call('judge', model='claude-opus-5-5') == ('claude', 'claude-opus-5-5')
    with pytest.raises(PolicyError, match='pin'):
        agentcall.resolve_call('judge', model=model)
