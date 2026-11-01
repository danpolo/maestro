"""Future `maestro init` projects inherit role models explicitly instead of pinning them.

Dan (2026-09-30): inheriting projects keep today's models — the template's former table
(Opus judge, Codex columns) became the shipped defaults, so nothing is downgraded.
"""
import json
from pathlib import Path

import pytest
import yaml

from maestro import config, roles
from maestro.cli import TEMPLATES_DIR
from maestro.model_policy import effective_roles, replace_route, shipped_policy_v1 as shipped_policy

TODAY = {
    'implementer': {'claude': 'claude-sonnet-5-5', 'codex': 'gpt-6-luna'},
    # 2026-10-01: gpt-6.1-sol replaces gpt-6-sol (test_model_sol_replacement).
    'judge': {'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-sol'},
    'diagnoser': {'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-sol'},
}

# Schema 2 uses the same role rules and near-cost preference as graph selection.
PAIRS = {'implementer': {'claude': 'claude-sonnet-5-5', 'codex': 'gpt-6.1-sol'},
         'judge': TODAY['judge'], 'diagnoser': TODAY['diagnoser']}


def _template():
    return yaml.safe_load((TEMPLATES_DIR / 'project.yaml.tmpl').read_text())


def test_template_states_only_the_shipped_role_strengths():
    # DESIGN.md §17.2: a role states its strength. The template writes out the shipped
    # ones, so a fresh project routes exactly as a project with no `roles:` block.
    from maestro.model_policy import role_shape_issues, role_strengths
    assert role_shape_issues(_template()) == []
    assert role_strengths(_template()) == role_strengths({})


def test_inherited_template_uses_the_pair_inventory_without_a_shared_policy():
    doc = _template()
    for role, table in PAIRS.items():
        settings = roles.role_config(role, doc)
        assert settings.backend == ('claude' if role == 'judge' else 'codex')
        assert {b: settings.model_for(b) for b in table} == table


def test_shipped_policy_bootstraps_from_the_same_table():
    assert {r: e['models'] for r, e in shipped_policy()['roles'].items()} == TODAY
    assert {e['backend'] for e in shipped_policy()['roles'].values()} == {'claude'}


def test_inheriting_project_follows_the_shared_policy_with_no_project_provenance():
    doc = _template()
    doc['project'] = {'name': 'demo', 'repo_path': '/nonexistent'}
    policy = replace_route(shipped_policy(), ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    effective, provenance = effective_roles(doc, policy)
    assert effective['roles']['implementer']['backend'] == 'codex'
    assert all(origin == 'shared' for table in provenance.values() for origin in table.values())


@pytest.fixture
def inherited_project(tmp_path, monkeypatch):
    repo = tmp_path / 'project'
    (repo / '.orchestrator').mkdir(parents=True)
    text = (TEMPLATES_DIR / 'project.yaml.tmpl').read_text()
    text = text.replace('{{ project_name }}', 'demo').replace('{{ repo_path }}', str(repo))
    (repo / 'project.yaml').write_text(text.replace('{{ main_branch }}', 'main'))
    (repo / '.orchestrator/state.json').write_text(json.dumps({'in_flight': []}))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    monkeypatch.setattr(config, 'PROJECT_YAML', repo / 'project.yaml')
    return repo


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_inherited_implementer_launch_gets_its_own_backends_model(inherited_project, backend):
    """Never a Claude id handed to Codex because the project stopped pinning a column."""
    from maestro import implementer
    model, _ = implementer.resolve_launch_model({'id': '01'}, backend)
    assert model == PAIRS['implementer'][backend]
