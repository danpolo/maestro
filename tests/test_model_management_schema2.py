"""Schema 2 management retains effort rows and reports requirements, not role pins."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import model_commands, model_policy
from maestro.model_lifecycle import preview_retire, preview_rollback
from maestro.model_policy import PolicyError, PolicyStore, catalog_defaults
from tests.test_model_task_pins import project, _adopt  # noqa: F401
from tests.test_model_asks import Bridge, Transport, detected, only, tap, reply, sol_rows


@pytest.fixture
def policy():
    return json.loads((Path(__file__).parent / 'fixtures/model_policy/schema2.json').read_text())


def test_replacement_moves_all_efforts_with_their_scores(policy):
    old, new = ('claude', 'claude-opus-5-5'), ('claude', 'claude-opus-6')
    policy['source']['class_metadata'] = [dict(backend_id='claude', model_id=model, family='opus',
        version=version, set=True, enforced_window=1000000)
        for model, version in [(old[1], [5, 5]), (new[1], [6])]]
    before = [r for r in policy['routes'] if (r['backend_id'], r['model_id']) == old]
    result = model_policy.replace_route(policy, old, new)
    assert [r for r in result['routes'] if r['model_id'] == new[1]] == [dict(r, model_id=new[1]) for r in before]
    assert not any(r['model_id'] == old[1] for r in result['routes'])
    assert result['roles'] == policy['roles']
    assert model_policy.role_references(result, new) == []
    with pytest.raises(PolicyError, match='schema 1'):
        model_policy.assign_roles(result, new, ['planner'])


def test_retirement_and_rollback_preserve_model_wide_retirement(policy, tmp_path):
    store = PolicyStore(tmp_path / 'models')
    revision = store.publish(policy, expected=None)
    owner = SimpleNamespace(retired=lambda backend: dict(removed=['claude-opus-5-5'], state_hash='owner'))
    preview = preview_retire(store, owner, 'claude')
    assert preview['status'] == 'ready' and preview['replacement_required'] == []
    store.publish(preview['policy'], expected=revision)
    rolled = preview_rollback(store, revision)['policy']
    assert ['claude', 'claude-opus-5-5'] in rolled['retired']
    assert not any(r['model_id'] == 'claude-opus-5-5' for r in rolled['routes'])
    assert rolled['roles'] == policy['roles']


def test_status_prints_each_role_requirement(policy, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    PolicyStore().publish(policy, expected=None)
    assert model_commands.run(SimpleNamespace(verb='status', as_json=False)) == 0
    text = capsys.readouterr().out
    assert 'planner' in text and '50' in text and 'high' in text and 'minimum' in text
    assert 'implementer' in text and '40' in text and 'medium' in text and 'preference' in text


def test_effective_schema2_roles_do_not_expose_obsolete_model_overrides(policy):
    project = {'roles': {'implementer': {'strength': 'strong', 'models': {'codex': 'obsolete'}}}}
    effective, provenance = model_policy.effective_roles(project, policy)
    assert effective['roles']['implementer'] == dict(policy['roles']['implementer'], strength='strong')
    assert provenance['implementer'] == {}
    assert model_policy.invalid_routes(project, policy) == {}


def test_replacing_with_existing_destination_keeps_its_own_pairs(policy):
    before = [deepcopy(r) for r in policy['routes'] if r['model_id'] == 'claude-sonnet-5-5']
    changed = model_policy.replace_route(policy, ('claude', 'claude-opus-5-5'), ('claude', 'claude-sonnet-5-5'))
    assert [r for r in changed['routes'] if r['model_id'] == 'claude-sonnet-5-5'] == before


def test_project_reports_requirements_without_model_overrides(policy, project, monkeypatch):
    from maestro import model_inventory
    _adopt(project, policy)
    monkeypatch.setattr(model_inventory, 'supports_reader', lambda repo: True)
    report = model_inventory.project_report(project)
    assert report['config'] == 'valid'
    assert report['roles']['planner'] == policy['roles']['planner']
    assert report['class_choices'] == {}
    assert model_inventory.dependency_inventory()['complete'] is True


def test_doctor_reports_schema2_role_requirements(policy, project):
    from maestro.cli import _check_model_policy
    _adopt(project, policy)
    check = _check_model_policy(project)
    assert check.ok
    assert 'planner' in check.detail and '50' in check.detail and 'minimum' in check.detail


@pytest.mark.parametrize('cell,good,bad', [('intelligence', 48, ['-1', '101', '1.5', 'True']),
                                        ('cost', 0.42, ['0', '-1', 'nan', 'inf'])])
def test_pair_cells_validate_scores_and_costs(cell, good, bad):
    from maestro.model_asks import parse_cell
    assert parse_cell(cell, str(good)) == good
    for value in bad:
        with pytest.raises(PolicyError):
            parse_cell(cell, value)


def test_new_class_approves_each_deliverable_effort_and_skips_roles(policy, tmp_path):
    from maestro import model_asks
    store, bridge, transport = PolicyStore(tmp_path / 'models'), Bridge(), Transport()
    store.publish(policy, expected=None)
    detected((store, bridge, transport))
    tap(store, bridge, transport, 'Use')
    assert only(store)['step'] == 'card' and only(store)['roles'] == []
    def fill(prefix, *values):
        label = next(t for t in transport.buttons() if t.startswith(prefix))
        tap(store, bridge, transport, label)
        for value in values:
            reply(store, bridge, transport, value)
    # New effort rows start as proposals; none becomes eligible before Apply.
    for effort, score, cost in [('low', 40, 0.6), ('medium', 48, 1.0), ('high', 52, 2.0),
                                ('xhigh', 54, 3.0), ('max', 55, 4.0)]:
        fill(effort, str(score), str(cost))
        assert not any(r['model_id'] == 'gpt-7-comet' for r in store.snapshot()[1]['routes'])
    fill('Max output', '32K')
    fill('Prepare', '180K-200K')
    fill('Start fresh', '240K-270K')
    fill('Ceiling', '350K')
    fill('Window')
    tap(store, bridge, transport, 'Use default 420K')
    tap(store, bridge, transport, 'Apply')
    assert only(store)['status'] == 'done'
    result = store.snapshot()[1]
    assert result['roles'] == policy['roles']
    assert [(r['effort'], r['intelligence'], r['relative_cost']) for r in result['routes']
            if r['model_id'] == 'gpt-7-comet'] == [
                ('low', 40, 0.6), ('medium', 48, 1.0), ('high', 52, 2.0), ('xhigh', 54, 3.0), ('max', 55, 4.0)]
    assert catalog_defaults(result, {})['codex']['model_profiles']['gpt-7-comet'].context_limits.max_output_tokens == 32000
    assert model_asks.process(store, bridge, transport)['applied'] == 0


def test_owner_model_profile_cannot_approve_a_new_class_pair(policy, tmp_path):
    from maestro.model_detection import consume
    store, bridge = PolicyStore(tmp_path / 'models'), Bridge()
    revision = store.publish(policy, expected=None)
    ok, _ = bridge.add('codex', ['--model', 'gpt-7-comet', '--handoff', '180K-200K',
                               '--fresh', '240K-270K', '--ceiling', '350K', '--window', '420K',
                               '--strength', 'strong', '--relative-cost', '1', '--max-output', '32000', '-y'])
    assert ok
    result = consume(store, bridge, asks=True, transport=Transport())
    assert not result['errors'] and store.current() == revision
    assert only(store)['step'] == 'use'


def test_same_class_successor_inherits_each_pair_and_only_reviews_limits(policy, tmp_path):
    from maestro import model_asks
    from maestro.model_detection import consume, policy_for_set
    store, transport = PolicyStore(tmp_path / 'models'), Transport()
    rows = sol_rows()
    # A model-wide owner grade is deliberately different from the pair table.
    rows[1]['routing_profile'] = dict(strength='light', relative_cost=99, max_output_tokens=32000)
    bridge = Bridge(rows)
    event = dict(kind='MODEL_SET', event_id='successor', provider='codex', model_id='gpt-6.2-sol',
                 family='sol', version=[6, 2], enforced_window=390000, predecessor_id='gpt-6.1-sol')
    bridge.events.append(event)
    revision = store.publish(policy, expected=None)
    before = [r for r in policy['routes'] if r['model_id'] == 'gpt-6.1-sol']
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and store.current() != revision
    changed = store.snapshot()[1]
    assert [r for r in changed['routes'] if r['model_id'] == 'gpt-6.2-sol'] == [
        dict(r, model_id='gpt-6.2-sol') for r in before]
    assert only(store)['kind'] == 'limits_review' and only(store)['step'] == 'review'
    assert policy_for_set(changed, event, bridge) == changed
    tap(store, bridge, transport, 'Keep')
    assert only(store)['status'] == 'kept'


def test_declining_a_partial_card_does_not_publish_the_model(policy, tmp_path):
    store, bridge, transport = PolicyStore(tmp_path / 'models'), Bridge(), Transport()
    revision = store.publish(policy, expected=None)
    detected((store, bridge, transport))
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'low: ❓')
    reply(store, bridge, transport, '40')
    reply(store, bridge, transport, '0.6')
    tap(store, bridge, transport, 'Decline')
    assert only(store)['status'] == 'declined' and store.current() == revision
    assert not bridge.added


def test_replacement_preview_reads_a_schema2_shipped_table(policy, tmp_path, monkeypatch):
    from maestro import model_replacement
    from tests.test_model_replacement import Metadata
    store = PolicyStore(tmp_path / 'models')
    store.publish(policy, expected=None)
    monkeypatch.setattr(model_replacement, 'shipped_policy', lambda: deepcopy(policy))
    preview = model_replacement.preview_replace(store, Metadata(),
        ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert preview['status'] == 'ready'
    assert preview['policy']['roles'] == policy['roles']


def test_replacement_of_an_absent_shipped_destination_has_valid_previous_revision(policy, tmp_path, monkeypatch):
    from maestro import model_replacement
    from tests.test_model_replacement import Metadata
    store = PolicyStore(tmp_path / 'models')
    shipped = deepcopy(policy)
    policy['routes'] = [r for r in policy['routes'] if r['model_id'] != 'gpt-6-luna']
    revision = store.publish(policy, expected=None)
    monkeypatch.setattr(model_replacement, 'shipped_policy', lambda: shipped)
    preview = model_replacement.preview_replace(store, Metadata(),
        ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert preview['policy']['source']['previous_policy'] == revision
    assert preview['policy']['source']['bootstrap'] == policy['source']


def test_a_card_with_no_effort_filled_cannot_apply(policy, tmp_path):
    from maestro import model_asks
    store, bridge, transport = PolicyStore(tmp_path / 'models'), Bridge(), Transport()
    revision = store.publish(policy, expected=None)
    detected((store, bridge, transport))
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'low: ❓')
    reply(store, bridge, transport, '40')
    tap(store, bridge, transport, 'Not routed')
    tap(store, bridge, transport, 'Apply')
    assert only(store)['approved_pairs'] == [] and model_asks.approved_pairs(store, 'codex', 'gpt-7-comet') == []
    assert only(store)['step'] == 'card' and store.current() == revision and not bridge.added


def test_replacing_a_model_with_itself_preserves_all_pairs(policy):
    result = model_policy.replace_route(policy, ('codex', 'gpt-6.1-sol'), ('codex', 'gpt-6.1-sol'))
    assert result == policy


@pytest.mark.parametrize('new', [('claude', 'unknown-class'), ('codex', 'unknown-class')])
def test_replacement_cannot_invent_pairs_for_an_unapproved_class(policy, new):
    with pytest.raises(PolicyError, match='approved effort pairs'):
        model_policy.replace_route(policy, ('claude', 'claude-opus-5-5'), new)
