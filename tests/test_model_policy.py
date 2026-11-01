"""Shared models exercised through CLI and real role/catalog consumers.

These tests catch bypassed revision checks, lost override provenance, and a
picker that reports a policy without its consumers actually using it.
"""
import json
from pathlib import Path

import pytest

from maestro import cli, roles


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    home = tmp_path / 'models'
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(home))
    monkeypatch.setenv('MAESTRO_REPO', str(tmp_path / 'project'))
    return home


def test_public_models_status_shows_qualified_bindings_without_writing(policy_home, capsys):
    assert cli.main(['models', 'status', '--json']) == 0
    view = json.loads(capsys.readouterr().out)
    assert view['revision'] is None
    assert view['roles']['implementer'] == {
        'min_intelligence': 40, 'effort': 'medium', 'effort_rule': 'preference'}
    assert view['source'] == 'shipped'
    assert not policy_home.exists()


def test_argument_free_picker_can_cancel_without_writing(policy_home, monkeypatch, capsys):
    monkeypatch.setattr('builtins.input', lambda _: 'q')
    assert cli.main(['models']) == 0
    assert 'implementer: intelligence >= 40; effort medium (preference)' in capsys.readouterr().out
    assert not policy_home.exists()


def test_published_policy_changes_inherited_roles_preserves_explicit_equal_default(policy_home, tmp_path):
    from maestro.model_policy import (PolicyStore, shipped_policy_v1 as shipped_policy, replace_route, effective_roles,
                                      derived_roles)

    store = PolicyStore(policy_home)
    initial = shipped_policy()
    replacement = replace_route(initial, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    revision = store.publish(replacement, expected=None)
    policy = store.load(revision)
    # Disposable provider tables: a published policy is read with lifecycle limits applied.
    head = '| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n|---|---|---|---|\n'
    (tmp_path / 'claude.md').write_text(head + '| Claude Sonnet 5.5 | 160-180K | 220-240K | 320K |\n'
                                               '| Claude Opus 5.5 | 160-180K | 220-240K | 320K |\n')
    (tmp_path / 'codex.md').write_text(head + '| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n')
    project = {'model_limits': {'claude': str(tmp_path / 'claude.md'), 'codex': str(tmp_path / 'codex.md')}}
    before = derived_roles(project, initial)
    assert roles.resolve('implementer', config=before, preferred='claude').model == 'claude-sonnet-5-5'
    # A role states a class, not a model: with sonnet gone the balanced implementer takes
    # Claude's next approved route, and the replacement is its Codex column.
    inherited = derived_roles(project, policy)
    assert roles.resolve('implementer', config=inherited).backend == 'codex'
    assert roles.resolve('implementer', config=inherited, preferred='claude').model == 'claude-opus-5-5'
    assert roles.resolve('implementer', config=inherited, preferred='codex').model == 'gpt-6-luna'
    explicit, provenance = effective_roles({'roles': {'implementer': {
        'backend': 'claude', 'models': {'claude': 'claude-sonnet-5-5'}}}}, policy)
    assert explicit['roles']['implementer']['models']['claude'] == 'claude-sonnet-5-5'
    assert provenance['implementer']['claude'] == 'project'
    assert ('claude', 'claude-sonnet-5-5') not in {
        (r['backend_id'], r['model_id']) for r in policy['routes']}


def test_stale_publish_and_corrupt_revision_refuse_without_moving_current(policy_home):
    from maestro.model_policy import PolicyError, PolicyStore, shipped_policy_v1 as shipped_policy, replace_route

    store = PolicyStore(policy_home)
    first = store.publish(shipped_policy(), expected=None)
    changed = replace_route(store.load(first), ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    with pytest.raises(PolicyError, match='changed'):
        store.publish(changed, expected=None)
    assert store.current() == first
    path = policy_home / 'revisions' / (first + '.json')
    path.write_text('{}')
    with pytest.raises(PolicyError, match='integrity'):
        store.load(first)


def test_unknown_destination_needs_its_own_strength_and_cost(policy_home):
    from maestro.model_policy import PolicyError, shipped_policy_v1 as shipped_policy, replace_route

    with pytest.raises(PolicyError, match='profile'):
        replace_route(shipped_policy(), ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-new'))


def test_qualified_identity_does_not_remove_other_harness_same_model(policy_home):
    from maestro.model_policy import shipped_policy_v1 as shipped_policy, replace_route

    policy = shipped_policy()
    policy['routes'].append({'backend_id': 'codex', 'model_id': 'claude-sonnet-5-5',
                             'strength': 'balanced', 'relative_cost': 1.0})
    changed = replace_route(policy, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert any(r['backend_id'] == 'codex' and r['model_id'] == 'claude-sonnet-5-5'
               for r in changed['routes'])
