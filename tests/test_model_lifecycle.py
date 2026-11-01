"""Rollback, owner-signalled retirement, adoption reconciliation and proposal review.

Real policy store, registry, projects and adoption; only modelctl metadata is doubled.
"""
import json

import pytest

from maestro.model_policy import PolicyStore, PolicyError, adopted_policy, digest, shipped_policy_v1 as shipped_policy
from tests.test_model_replacement import Metadata, setup  # noqa: F401 — `setup` is a fixture

SONNET, LUNA, OLD_LUNA = ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'), ('codex', 'gpt-5.6-luna')


class Owner(Metadata):
    """Adds the owner's removed-model state and durable outbox."""
    def __init__(self):
        super().__init__()
        self.removed = {'claude': [], 'codex': []}
        self.state_readable = True
        self.events = []

    def retired(self, backend):
        if not self.state_readable:
            raise PolicyError('modelctl state unavailable or incompatible (ValueError)')
        return dict(provider=backend, removed=list(self.removed[backend]),
                    state_hash=digest(self.removed))

    def pending_events(self):
        return list(self.events)

    def acknowledge(self, event_id):
        self.events = [e for e in self.events if e['event_id'] != event_id]


def _switch(store, owner):
    from maestro.model_replacement import preview_replace, apply_preview
    return apply_preview(preview_replace(store, owner, SONNET, LUNA), store, owner, confirmed=True)


def _idle(project):
    project.joinpath('.orchestrator/state.json').write_text(json.dumps({'in_flight': []}))


# ── rollback ──────────────────────────────────────────────────────────────

def test_rollback_publishes_prior_policy_as_new_revision_and_adopts_at_idle_boundary(setup):
    from maestro.model_lifecycle import preview_rollback, apply_policy_preview
    store, projects, owner = setup
    switched = _switch(store, Owner())['revision']
    preview = preview_rollback(store)
    assert preview['target'] == digest(shipped_policy())
    assert preview['expected_revision'] == switched
    outcome = apply_policy_preview(preview, store, confirmed=True)
    assert outcome['revision'] not in (switched, digest(shipped_policy()))
    rolled = store.load(outcome['revision'])
    assert rolled['source']['kind'] == 'rollback'
    assert rolled['source']['restored'] == digest(shipped_policy())
    assert rolled['roles'] == shipped_policy()['roles']
    assert [r['status'] for r in outcome['projects']] == ['updated', 'updated', 'deferred']
    # Active project keeps the revision it had; it never adopted the switch either.
    assert adopted_policy(projects[2], store)[0] is None
    assert adopted_policy(projects[0], store)[0] == outcome['revision']


def test_rollback_restores_initial_shipped_policy_after_source_upgrade(setup, monkeypatch):
    from copy import deepcopy
    from maestro import model_policy
    from maestro.model_lifecycle import preview_rollback, apply_policy_preview
    store, projects, _ = setup
    original = shipped_policy()
    _switch(store, Owner())
    upgraded = deepcopy(original)
    upgraded['source']['upgraded_release'] = 'new executing source'
    monkeypatch.setattr(model_policy, 'shipped_policy', lambda: deepcopy(upgraded))
    preview = preview_rollback(store)
    assert preview['target'] == digest(original)
    outcome = apply_policy_preview(preview, store, confirmed=True)
    assert store.load(outcome['revision'])['roles'] == original['roles']
    assert store.load_any(digest(original)) == original


@pytest.mark.parametrize('override', [
    {'models': {'codex': ['gpt-6-luna']}},
    {'models': {'codex': {'id': 'gpt-6-luna'}}},
    {'models': {'codex': ''}},
    {'backend': ['codex']},
    {'model': ['gpt-6-luna']},
])
def test_malformed_role_values_do_not_block_shared_replacement(setup, override):
    import yaml
    from maestro.model_inventory import register_project
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, owner = setup
    broken = projects[1]
    config = yaml.safe_load((broken / 'project.yaml').read_text())
    config['roles'] = {'implementer': override}
    (broken / 'project.yaml').write_text(yaml.safe_dump(config))
    with pytest.raises(PolicyError, match='configuration'):
        register_project(broken, store)
    preview = preview_replace(store, owner, SONNET, LUNA)
    rows = {row['repo']: row for row in preview['projects']}
    assert rows[str(broken)]['status'] == 'invalid'
    outcome = apply_preview(preview, store, owner, confirmed=True)
    assert outcome['revision'] == store.current()
    assert [row['status'] for row in outcome['projects']] == ['updated', 'invalid', 'deferred']
    assert adopted_policy(broken, store)[0] is None


def test_rollback_to_named_revision_and_refuses_unknown_or_unconfirmed(setup):
    from maestro.model_lifecycle import preview_rollback, apply_policy_preview
    store, projects, owner = setup
    first = _switch(store, Owner())['revision']
    from maestro.model_replacement import preview_replace, apply_preview
    second = apply_preview(preview_replace(store, Owner(), ('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5-5')),
                           store, Owner(), confirmed=True)['revision']
    preview = preview_rollback(store, first)
    assert preview['expected_revision'] == second
    with pytest.raises(PolicyError, match='confirmation'):
        apply_policy_preview(preview, store, confirmed=False)
    assert store.current() == second
    with pytest.raises(PolicyError):
        preview_rollback(store, 'f' * 64)
    restored = store.load(apply_policy_preview(preview, store, confirmed=True)['revision'])
    assert restored['roles'] == store.load(first)['roles']
    assert restored['routes'] == store.load(first)['routes']


def test_rollback_never_resurrects_a_retired_route(setup):
    from maestro.model_lifecycle import preview_rollback, preview_retire, apply_policy_preview
    store, projects, owner = setup
    owner = Owner()
    _switch(store, owner)
    before = store.current()
    owner.removed['codex'] = ['gpt-5.6-luna']
    apply_policy_preview(preview_retire(store, owner, 'codex'), store, confirmed=True, bridge=owner)
    rolled = store.load(apply_policy_preview(preview_rollback(store, before), store, confirmed=True)['revision'])
    assert list(OLD_LUNA) in rolled['retired']
    assert OLD_LUNA not in {(r['backend_id'], r['model_id']) for r in rolled['routes']}
    # A rollback whose roles need a now-retired route is refused, not silently rewritten.
    owner.removed['codex'] = ['gpt-5.6-luna', 'gpt-6-luna']
    blocked = preview_retire(store, owner, 'codex')
    # Only the implementer: the judge's Codex column is a Sol (gpt-6.1-sol since 2026-10-01).
    assert blocked['replacement_required'] == [dict(route=list(LUNA), roles=['implementer'])]


def test_stale_rollback_preview_refuses(setup):
    from maestro.model_lifecycle import preview_rollback, apply_policy_preview
    store, projects, owner = setup
    _switch(store, Owner())
    preview = preview_rollback(store)
    store.publish(shipped_policy(), expected=store.current())
    current = store.current()
    with pytest.raises(PolicyError, match='changed since preview'):
        apply_policy_preview(preview, store, confirmed=True)
    assert store.current() == current


# ── retirement from owner metadata ────────────────────────────────────────

def test_owner_removed_unreferenced_route_is_retired_referenced_route_needs_replacement(setup):
    from maestro.model_lifecycle import preview_retire, apply_policy_preview
    store, projects, owner = setup
    owner = Owner()
    owner.removed['claude'] = ['claude-sonnet-5-5', 'claude-haiku-4-5-20251001']
    preview = preview_retire(store, owner, 'claude')
    assert preview['retire'] == [['claude', 'claude-haiku-4-5-20251001']]
    # Only the implementer: the judge defaults to Opus since 2026-09-30.
    assert preview['replacement_required'] == [dict(route=list(SONNET), roles=['implementer'])]
    policy = store.load(apply_policy_preview(preview, store, confirmed=True, bridge=owner)['revision'])
    assert ['claude', 'claude-haiku-4-5-20251001'] in policy['retired']
    assert SONNET in {(r['backend_id'], r['model_id']) for r in policy['routes']}


def test_metadata_failure_is_unknown_availability_never_retirement(setup):
    from maestro.model_lifecycle import preview_retire
    store, projects, owner = setup
    owner = Owner()
    owner.state_readable = False
    with pytest.raises(PolicyError, match='unavailable'):
        preview_retire(store, owner, 'claude')
    assert store.current() is None


def test_retire_preview_is_stale_when_owner_state_changes(setup):
    from maestro.model_lifecycle import preview_retire, apply_policy_preview
    store, projects, owner = setup
    owner = Owner()
    owner.removed['codex'] = ['gpt-5.6-luna']
    preview = preview_retire(store, owner, 'codex')
    owner.removed['codex'] = []
    with pytest.raises(PolicyError, match='changed since preview'):
        apply_policy_preview(preview, store, confirmed=True, bridge=owner)
    assert store.current() is None


def test_nothing_to_retire_publishes_nothing(setup):
    from maestro.model_lifecycle import preview_retire, apply_policy_preview
    store, projects, owner = setup
    preview = preview_retire(store, Owner(), 'codex')
    assert preview['status'] == 'nothing_to_retire'
    with pytest.raises(PolicyError):
        apply_policy_preview(preview, store, confirmed=True, bridge=Owner())
    assert store.current() is None


# ── truthful adoption status / retry reconciliation ───────────────────────

def test_status_reports_actual_adoption_and_retry_reconciles(setup):
    from maestro.model_inventory import adoption_status, adopt
    store, projects, owner = setup
    revision = _switch(store, Owner())['revision']
    status = {row['repo']: row for row in adoption_status(store)}
    assert status[str(projects[0].resolve())]['adoption'] == 'current'
    assert status[str(projects[2].resolve())]['adoption'] == 'not_adopted'
    assert status[str(projects[2].resolve())]['current_revision'] == revision
    _idle(projects[2])
    assert adopt(projects[2], store)['status'] == 'updated'
    assert {row['adoption'] for row in adoption_status(store)} == {'current'}
    from maestro.control.models import EventType
    from maestro.control.store import ControlStore
    with ControlStore.open_controller(projects[1] / '.orchestrator/control.sqlite3') as controller:
        controller.append_event(EventType.MODEL_POLICY_ADOPTED, 'model_policy', payload={'broken': True})
    broken = {row['repo']: row for row in adoption_status(store)}[str(projects[1].resolve())]
    assert broken['adoption'] == 'unreadable'


def test_status_cli_json_includes_adoption(setup, capsys):
    from maestro import cli
    store, projects, owner = setup
    _switch(store, Owner())
    assert cli.main(['models', 'status', '--json']) == 0
    view = json.loads(capsys.readouterr().out)
    assert sorted(row['adoption'] for row in view['adoption']) == ['current', 'current', 'not_adopted']


# ── proposal review / selection ───────────────────────────────────────────

def _older_release_policy():
    """What a store bootstrapped before 2026-10-08 holds: gpt-5.6-luna still a shared
    route (the release now ships it pin-only, superseded by gpt-6-luna)."""
    policy = shipped_policy()
    policy['routes'].append(dict(backend_id='codex', model_id='gpt-5.6-luna', strength='light', relative_cost=0.05))
    policy['source'] = {'kind': 'shipped', 'release': 'before gpt-6-luna light route'}
    return policy


def _proposal(store, owner, model='gpt-6-luna', related=('gpt-5.6-luna',)):
    from maestro.model_detection import consume, proposal_id
    if store.current() is None:
        store.publish(_older_release_policy(), expected=None)
    owner.events = [dict(event_id=f'codex:{model}', kind='NEW_MODEL_DISCOVERED', provider='codex',
                         model_id=model, family='luna', first_seen='2026-09-30T12:00:00Z',
                         fingerprint='fp1', related_model_ids=list(related))]
    consume(store, owner)
    return proposal_id('codex', model)


def _record(store, ident):
    return json.loads((store.home / 'proposals' / (ident + '.json')).read_text())


def test_approved_proposal_applies_through_replacement_use_case(setup):
    from maestro.model_detection import review_proposal, decide_proposal
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    review = review_proposal(store, owner, ident)
    assert review['candidates'] == [list(OLD_LUNA)]
    assert review['stale_policy'] is False
    assert review['preview']['status'] == 'ready'
    outcome = decide_proposal(store, owner, ident, review['preview'], approve=True)
    record = _record(store, ident)
    assert record['status'] == 'applied'
    assert record['applied_revision'] == outcome['revision'] == store.current()
    policy = store.load(store.current())
    assert OLD_LUNA not in {(r['backend_id'], r['model_id']) for r in policy['routes']}
    with pytest.raises(PolicyError, match='applied'):
        review_proposal(store, owner, ident)


def test_rejected_proposal_publishes_nothing(setup):
    from maestro.model_detection import review_proposal, decide_proposal
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    review = review_proposal(store, owner, ident)
    decide_proposal(store, owner, ident, review['preview'], approve=False)
    assert _record(store, ident)['status'] == 'rejected'
    assert store.current() == digest(_older_release_policy())


def test_proposal_from_older_policy_is_flagged_and_candidates_recomputed(setup):
    from maestro.model_detection import review_proposal
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner, related=('gpt-5.6-luna', 'gpt-6.1-sol'))
    # A policy change after detection removes one candidate from shared eligibility.
    from maestro.model_replacement import preview_replace, apply_preview
    apply_preview(preview_replace(store, owner, ('codex', 'gpt-6.1-sol'), ('claude', 'claude-opus-5-5')),
                  store, owner, confirmed=True)
    review = review_proposal(store, owner, ident)
    assert review['stale_policy'] is True
    assert review['candidates'] == [list(OLD_LUNA)]
    assert sorted(_record(store, ident)['replacement_candidates']) == [list(OLD_LUNA), ['codex', 'gpt-6.1-sol']]


def test_multiple_candidates_need_an_explicit_choice(setup):
    from maestro.model_detection import review_proposal
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner, related=('gpt-5.6-luna', 'gpt-6.1-sol'))
    review = review_proposal(store, owner, ident)
    assert review['preview'] is None
    chosen = review_proposal(store, owner, ident, old=('codex', 'gpt-6.1-sol'))
    assert chosen['preview']['old'] == ['codex', 'gpt-6.1-sol']
    with pytest.raises(PolicyError):
        review_proposal(store, owner, ident, old=('claude', 'claude-haiku-4-5-20251001'))


def test_unregistered_destination_is_reported_registration_required_not_selected(setup):
    from maestro.model_detection import review_proposal, decide_proposal
    store, projects, owner = setup
    owner = Owner()
    owner.registered = False
    ident = _proposal(store, owner)
    review = review_proposal(store, owner, ident)
    assert review['preview']['status'] == 'registration_required'
    record = _record(store, ident)
    assert record['status'] == 'registration_required'
    assert record['suggested_limits']['prepare'] == [70000, 70000]
    with pytest.raises(PolicyError, match='owner'):
        decide_proposal(store, owner, ident, review['preview'], approve=True)
    assert _record(store, ident)['status'] == 'registration_required'
    assert store.current() == digest(_older_release_policy())


def test_failed_selection_returns_proposal_to_pending_with_error(setup):
    from maestro.model_detection import review_proposal, decide_proposal
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    review = review_proposal(store, owner, ident)
    owner.table_hash = 'table-2'  # provider table moved after the preview
    with pytest.raises(PolicyError, match='changed since preview'):
        decide_proposal(store, owner, ident, review['preview'], approve=True)
    record = _record(store, ident)
    assert record['status'] == 'pending_approval'
    assert 'changed since preview' in record['last_error']
    assert store.current() == digest(_older_release_policy())


@pytest.mark.parametrize('published', [True, False])
def test_crash_after_approval_reconciles_from_actual_publication(setup, published):
    from maestro.model_detection import review_proposal, reconcile_proposals
    from maestro.model_policy import atomic_write, canonical
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    preview = review_proposal(store, owner, ident)['preview']
    record = _record(store, ident)
    record.update(status='approved', approved_expected_revision=preview['expected_revision'],
                  approved_policy_revision=digest(preview['policy']))
    atomic_write(store.home / 'proposals' / (ident + '.json'), canonical(record))
    if published:
        store.publish(preview['policy'], expected=preview['expected_revision'])
    reconcile_proposals(store)
    record = _record(store, ident)
    if published:
        assert (record['status'], record['applied_revision']) == ('applied', store.current())
    else:
        assert record['status'] == 'pending_approval'
        assert 'not published' in record['last_error']


def test_review_cli_applies_selected_proposal(setup, monkeypatch, capsys):
    from maestro import cli, model_commands
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    monkeypatch.setattr(model_commands, 'ModelctlBridge', lambda: owner)
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert cli.main(['models', 'review', '--proposal', ident]) == 0
    assert _record(store, ident)['status'] == 'applied'


def test_review_cli_reports_registered_but_not_selected_when_publication_fails(setup, monkeypatch, capsys):
    from maestro import cli, model_commands
    store, _, _ = setup
    owner = Owner()
    ident = _proposal(store, owner)
    monkeypatch.setattr(model_commands, 'ModelctlBridge', lambda: owner)
    def confirm(_):
        owner.table_hash = 'edited-after-review'
        return 'yes'
    monkeypatch.setattr('builtins.input', confirm)
    assert cli.main(['models', 'review', '--proposal', ident]) == 1
    assert 'Registered: yes. Selected: no.' in capsys.readouterr().out
    assert owner.registered and store.current() == digest(_older_release_policy())
    assert _record(store, ident)['status'] == 'pending_approval'


def test_rollback_and_retire_cli_require_yes(setup, monkeypatch):
    from maestro import cli, model_commands
    store, projects, owner = setup
    owner = Owner()
    _switch(store, owner)
    switched = store.current()
    monkeypatch.setattr(model_commands, 'ModelctlBridge', lambda: owner)
    monkeypatch.setattr('builtins.input', lambda _: 'no')
    assert cli.main(['models', 'rollback']) == 0
    assert store.current() == switched
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert cli.main(['models', 'rollback']) == 0
    assert store.load(store.current())['source']['kind'] == 'rollback'
    owner.removed['codex'] = ['gpt-5.6-luna']
    assert cli.main(['models', 'retire', '--backend', 'codex']) == 0
    assert list(OLD_LUNA) in store.load(store.current())['retired']


def test_crash_between_revision_write_and_pointer_move_is_not_applied(setup):
    """`publish` writes the revision file, then moves `current`. A crash in between leaves
    a revision file that was never the shared policy; the approval must not count as applied."""
    from maestro.model_detection import review_proposal, reconcile_proposals
    from maestro.model_policy import atomic_write, canonical
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    preview = review_proposal(store, owner, ident)['preview']
    before = store.current()
    record = _record(store, ident)
    record.update(status='approved', approved_expected_revision=preview['expected_revision'],
                  approved_policy_revision=digest(preview['policy']))
    atomic_write(store.home / 'proposals' / (ident + '.json'), canonical(record))
    atomic_write(store.home / 'revisions' / (digest(preview['policy']) + '.json'), canonical(preview['policy']))
    reconcile_proposals(store)
    assert store.current() == before
    record = _record(store, ident)
    assert record['status'] == 'pending_approval'
    assert 'not published' in record['last_error']


def test_approval_stays_applied_after_a_later_rollback(setup):
    from maestro.model_detection import review_proposal, decide_proposal, reconcile_proposals
    from maestro.model_lifecycle import preview_rollback, apply_policy_preview
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    preview = review_proposal(store, owner, ident)['preview']
    decide_proposal(store, owner, ident, preview, approve=True)
    applied = _record(store, ident)['applied_revision']
    apply_policy_preview(preview_rollback(store), store, confirmed=True)
    assert store.current() != applied
    record = _record(store, ident)
    record['status'] = 'approved'
    record['approved_policy_revision'] = applied
    from maestro.model_policy import atomic_write, canonical
    atomic_write(store.home / 'proposals' / (ident + '.json'), canonical(record))
    reconcile_proposals(store)
    assert (_record(store, ident)['status'], _record(store, ident)['applied_revision']) == ('applied', applied)


def test_history_starting_at_an_older_releases_shipped_policy_reconciles(setup):
    """The first publication points at the shipped policy of the release that made it.
    Shipped policies are never stored, so after an upgrade that digest is simply absent."""
    import copy
    from maestro.model_detection import review_proposal, reconcile_proposals
    from maestro.model_policy import atomic_write, canonical
    store, projects, owner = setup
    owner = Owner()
    ident = _proposal(store, owner)
    preview = review_proposal(store, owner, ident)['preview']
    policy = copy.deepcopy(preview['policy'])
    policy['source']['previous_policy'] = 'f' * 64  # an older release's shipped digest
    store.publish(policy, expected=store.current())
    record = _record(store, ident)
    record.update(status='approved', approved_policy_revision=digest(policy))
    atomic_write(store.home / 'proposals' / (ident + '.json'), canonical(record))
    reconcile_proposals(store)
    assert (_record(store, ident)['status'], _record(store, ident)['applied_revision']) == ('applied', digest(policy))


# ── malformed project configuration ───────────────────────────────────────

@pytest.mark.parametrize('broken', ['roles: [unclosed\n', 'project: x\nroles: [judge]\n'])
def test_malformed_project_is_reported_invalid_and_never_blocks_or_adopts(setup, broken):
    from maestro.model_inventory import project_report
    store, projects, owner = setup
    projects[0].joinpath('project.yaml').write_text(broken)
    outcome = _switch(store, Owner())
    assert store.current() == outcome['revision']
    by_repo = {r['repo']: r for r in outcome['projects']}
    bad = by_repo[str(projects[0].resolve())]
    assert bad['status'] == 'invalid'
    assert 'invalid' in bad['reason']
    assert adopted_policy(projects[0], store)[0] is None
    assert by_repo[str(projects[1].resolve())]['status'] == 'updated'
    report = project_report(projects[0], store)
    # The adoption record is fine; the configuration is what is broken.
    assert report['adoption'] == 'not_adopted'
    assert report['config'] == 'invalid'
    assert 'invalid project identity/configuration' in report['reason']
