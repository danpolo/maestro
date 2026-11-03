"""Dan's Retry and Remove model buttons for a stopped access test (2026-11-02)."""
import fcntl
import json
from copy import deepcopy
from pathlib import Path

import pytest

from maestro import model_access, model_asks, model_sheet
from maestro.model_detection import consume
from maestro.model_policy import PolicyError, PolicyStore
from maestro.modelctl_bridge import ModelctlBridge
from tests.test_model_access import (CODEX, LUNA, AccessBridge, RoutedBridge, fixture_policy, holds, key,
                                     only_model_serving_planner, routes)
from tests.test_model_asks import detected, inbox, tap
from tests.test_model_asks_card import InPlace
from tests.test_model_sheet import GOOD, HEADER, MODEL, Drive

OWNER = Path('/home/dan/projects/modelctl')


class Owner:
    """modelctl's side of a card: its stops, the one-model test and the removal mark."""

    def __init__(self):
        super().__init__()
        self.answers, self.retests, self.removed, self.busy = [], [], [], None

    def stop(self, model, reason, access=None, at='t1', backend=CODEX):
        if access:
            self.checks.setdefault(backend, {})[model] = access
        self.stops.setdefault(backend, {})[model] = dict(reason=reason, at=at, access=access)

    def retest(self, backend, model):
        self.retests.append(model)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        outcome, detail, access = answer
        if access:
            self.checks.setdefault(backend, {})[model] = access
        if outcome == 'stopped':
            self.stops[backend][model].update(reason=detail, access=access)
        else:
            self.stops.get(backend, {}).pop(model, None)
        return dict(provider=backend, model=model, status='x', detail=detail, access=access, outcome=outcome,
                    removed=False, handed_over=True)

    def drop_from_tests(self, backend, model):
        if self.busy:
            raise PolicyError(self.busy)
        self.removed.append((backend, model))
        self.stops.get(backend, {}).pop(model, None)


class CardBridge(Owner, AccessBridge):
    pass


class PulledBridge(Owner, RoutedBridge):
    pass


@pytest.fixture
def world(tmp_path):
    store = PolicyStore(tmp_path / 'models')
    store.publish(fixture_policy(), expected=None)
    return store, CardBridge(), InPlace()


@pytest.fixture
def pulled(tmp_path, monkeypatch):
    """A model Dan approved through Pull from Drive, with the approval record that leaves behind."""
    store, bridge, transport = PolicyStore(tmp_path / 'models'), PulledBridge(), InPlace()
    store.publish(fixture_policy(), expected=None)
    detected((store, bridge, transport))
    drive = Drive(HEADER + GOOD)
    monkeypatch.setattr(model_sheet, '_download', drive.download)
    monkeypatch.setattr(model_sheet, '_upload', drive.upload)
    tap(store, bridge, transport, 'Pull from Drive')
    assert routes(store, MODEL)
    return store, bridge, transport, drive


def sync(world):
    return model_access.reconcile(*world[:3])


def cards(store):
    return [r for r in model_asks.records(store) if r['kind'] == 'access_failure']


def card(store):
    [record] = cards(store)
    return record


def readds(store):
    return [r for r in model_asks.records(store) if r.get('readd')]


def tap_data(world, data):
    store, bridge, transport = world[:3]
    inbox(store, kind='callback', data=data, message_id=transport.last()['id'])
    return model_asks.process(store, bridge, transport)


def alert(world, model=LUNA, reason='login expired', access=None, **kw):
    world[1].stop(model, reason, access, **kw)
    outcome = sync(world)
    assert not outcome['errors']
    return outcome


# ── the alert ──

def test_maestro_declares_it_owns_the_stop_alert():
    assert model_access.ACCESS_ASKS_VERSION == 1


def test_a_rejection_opens_one_card_with_both_buttons_and_takes_the_model_out(world):
    store, _, transport = world
    alert(world, reason='model not supported', access='FAILED')
    assert routes(store) == [] and key() in holds(store)
    assert len(cards(store)) == 1 and len(transport.sent) == 1
    assert set(transport.buttons()) == {'Retry', 'Remove model'}
    assert 'model not supported' in transport.last()['text']
    assert 'takes it out of routing until a test passes' in transport.last()['text']
    assert 'stopped' in transport.last()['text']


@pytest.mark.parametrize('reason', ['network unreachable', 'please log in', 'timed out', 'odd answer'])
@pytest.mark.parametrize('access', [None, 'INCONCLUSIVE', 'VERIFIED'])
def test_any_other_failure_opens_one_card_and_the_model_stays_routed(world, reason, access):
    store, _, transport = world
    before = routes(store)
    alert(world, reason=reason, access=access)
    assert routes(store) == before and holds(store) == {}
    assert len(transport.sent) == 1 and set(transport.buttons()) == {'Retry', 'Remove model'}
    assert reason in transport.last()['text'] and 'It stays in routing.' in transport.last()['text']


def test_the_rendered_alert_text_and_buttons(world):
    _, _, transport = world
    alert(world, reason='login <expired>', access='INCONCLUSIVE')
    ident = card(world[0])['ask_id']
    assert transport.last()['text'] == (
        f'⚠️ <b>{LUNA}</b> on codex failed its access test:\n<pre>login &lt;expired&gt;</pre>\n'
        'It stays in routing.\nAutomatic retests are stopped. Retry tests it once now. '
        'Remove model takes it out of routing and stops its tests.'
        f'\n\n<code>mdl:{ident}</code>')
    assert transport.last()['markup'] == dict(inline_keyboard=[[
        dict(text='Retry', callback_data=f'mdl:{ident}:retry'),
        dict(text='Remove model', callback_data=f'mdl:{ident}:remove')]])


def test_a_secret_in_the_reason_is_never_saved_or_sent(world):
    store, _, transport = world
    secret = 'sk-ant-api03-' + 'a' * 40
    alert(world, reason=f'refused for {secret}')
    assert secret not in transport.last()['text'] and secret not in json.dumps(card(store))


@pytest.mark.parametrize('character', ['x', '<'])
def test_a_long_reason_is_cut_to_fit_a_telegram_message(world, character):
    _, _, transport = world
    alert(world, reason=character * 9000)
    assert len(transport.last()['text']) < 4096


def test_a_second_consume_opens_no_second_card(world):
    store, _, transport = world
    alert(world)
    sync(world)
    sync(world)
    assert len(cards(store)) == 1 and len(transport.sent) == 1


def test_a_changed_reason_updates_and_resends_the_same_card(world):
    store, bridge, transport = world
    alert(world, reason='login expired')
    ident = card(store)['ask_id']
    bridge.stops[CODEX][LUNA]['reason'] = 'network unreachable'
    sync(world)
    assert card(store)['ask_id'] == ident and card(store)['failure'] == 'network unreachable'
    assert len(transport.sent) == 2 and 'network unreachable' in transport.last()['text']


def test_a_stop_that_has_gone_closes_the_card_with_a_reason(world):
    store, bridge, transport = world
    alert(world)
    del bridge.stops[CODEX][LUNA]
    sync(world)
    assert card(store)['status'] == 'superseded' and card(store)['reason']
    assert transport.last()['text'].startswith('⏭') and not transport.last()['markup']


def test_a_card_dan_never_saw_closes_without_a_message(world):
    store, bridge, transport = world
    bridge.stop(LUNA, 'login expired')
    model_access.reconcile(store, bridge, None)
    assert card(store)['send_pending'] and transport.sent == []
    del bridge.stops[CODEX][LUNA]
    sync(world)
    assert card(store)['status'] == 'superseded' and transport.sent == []


def test_a_new_stop_of_the_same_model_is_a_new_card(world):
    store, bridge, _ = world
    alert(world, at='t1')
    del bridge.stops[CODEX][LUNA]
    sync(world)
    alert(world, at='t2')
    assert sorted(r['status'] for r in cards(store)) == ['open', 'superseded']


def test_a_running_action_owns_its_card(world):
    store, bridge, transport = world
    alert(world)
    record = card(store)
    record.update(status='applying', step='retry')
    model_asks._save(store, record)
    bridge.stops[CODEX][LUNA]['reason'] = 'something else'
    sync(world)
    assert card(store) == record
    del bridge.stops[CODEX][LUNA]
    sync(world)
    assert card(store) == record and len(transport.sent) == 1


def test_an_unreadable_backend_leaves_its_cards_alone(world):
    store, bridge, _ = world
    alert(world)
    bridge.unreadable.add(CODEX)
    assert any(CODEX in e for e in sync(world)['errors'])
    assert card(store)['status'] == 'open'


def test_a_card_that_could_not_be_sent_goes_on_the_next_run_and_consume_never_fails(world):
    store, bridge, transport = world
    bridge.stop(LUNA, 'login expired')
    real = transport.send

    def down(text, markup=None):
        raise PolicyError('telegram sendMessage unreachable')

    transport.send = down
    assert any('telegram' in e for e in sync(world)['errors'])
    transport.send = real
    sync(world)
    assert len(transport.sent) == 1


def test_open_cards_are_listed_for_status(world):
    alert(world)
    assert [a['kind'] for a in model_asks.open_asks(world[0])] == ['access_failure']


# ── Retry ──

def test_retry_that_passes_restores_the_routes_and_clears_the_stop(world):
    store, bridge, transport = world
    before = routes(store)
    alert(world, access='FAILED')
    assert routes(store) == []
    message = transport.last()['id']
    bridge.answers.append(('passed', 'ok', 'VERIFIED'))
    assert tap(store, bridge, transport, 'Retry')['applied'] == 1
    assert bridge.retests == [LUNA]
    assert any(i == message and '⏳' in t and 'up to 3 minutes' in t for i, t in transport.texts)
    assert routes(store) == before and holds(store) == {} and bridge.stops[CODEX] == {}
    assert card(store)['status'] == 'done'
    assert transport.last()['id'] != message and 'is in routing' in transport.last()['text']
    assert not transport.last()['markup']
    sync(world)
    assert len(cards(store)) == 1 and card(store)['status'] == 'done'


def test_retry_that_passes_while_its_routes_cannot_return_says_the_next_run_tries(world, monkeypatch):
    store, bridge, transport = world
    alert(world, access='FAILED')

    def refused(policy, hold):
        raise PolicyError('no profile')
    monkeypatch.setattr(model_access, 'with_routes', refused)
    bridge.answers.append(('passed', 'ok', 'VERIFIED'))
    tap(store, bridge, transport, 'Retry')
    assert card(store)['status'] == 'done' and key() in holds(store)
    assert 'next run tries again' in [m['text'] for m in transport.sent if '✅' in m['text']][0]


def test_retry_that_passes_on_a_model_that_had_no_routes_does_not_claim_any(world):
    store, bridge, transport = world
    alert(world, model='gpt-9-unrouted')
    bridge.answers.append(('passed', 'ok', 'VERIFIED'))
    tap(store, bridge, transport, 'Retry')
    assert 'no routes' in transport.last()['text']


def test_retry_that_hits_a_rate_limit_closes_the_card_and_promises_the_hourly_test(world):
    store, bridge, transport = world
    alert(world, access='INCONCLUSIVE')
    bridge.answers.append(('rate_limited', 'quota', 'INCONCLUSIVE'))
    assert tap(store, bridge, transport, 'Retry')['applied'] == 1
    assert card(store)['status'] == 'done' and bridge.stops[CODEX] == {}
    assert 'hourly' in transport.last()['text'] and 'when it passes' in transport.last()['text']
    assert not transport.last()['markup']


def test_a_rate_limit_on_a_verified_model_does_not_promise_a_test_that_never_runs(world):
    store, bridge, transport = world
    alert(world, access='VERIFIED')
    bridge.answers.append(('rate_limited', 'quota', 'VERIFIED'))
    tap(store, bridge, transport, 'Retry')
    assert card(store)['status'] == 'done'
    assert 'hourly' not in transport.last()['text'] and 'verified' in transport.last()['text'].lower()


def test_retry_that_fails_again_stays_open_and_shows_the_new_reason(world):
    store, bridge, transport = world
    alert(world, reason='login expired', access='INCONCLUSIVE')
    ident = card(store)['ask_id']
    bridge.answers.append(('stopped', 'network unreachable', 'INCONCLUSIVE'))
    assert tap(store, bridge, transport, 'Retry')['applied'] == 0
    assert card(store)['ask_id'] == ident and card(store)['status'] == 'open' and card(store)['step'] == 'stopped'
    text = transport.last()['text']
    assert 'Retry failed' in text and 'network unreachable' in text and 'login expired' not in text
    assert set(transport.buttons()) == {'Retry', 'Remove model'}
    sent = len(transport.sent)
    sync(world)
    assert len(transport.sent) == sent  # the new reason is already on the card


def test_retry_that_could_not_run_keeps_the_card_and_says_why(world):
    store, bridge, transport = world
    alert(world)
    bridge.answers.append(PolicyError('modelctl scan already running'))
    assert tap(store, bridge, transport, 'Retry')['applied'] == 0
    assert card(store)['status'] == 'open'
    assert 'already running' in transport.last()['text'] and set(transport.buttons()) == {'Retry', 'Remove model'}


def test_a_tap_on_a_closed_card_does_nothing(world):
    store, bridge, transport = world
    alert(world)
    old = transport.buttons()['Retry']
    bridge.answers.append(('passed', 'ok', 'VERIFIED'))
    tap(store, bridge, transport, 'Retry')
    tap_data(world, old)
    assert bridge.retests == [LUNA]


# ── Remove model ──

def test_remove_on_a_routed_model_drops_its_routes_and_marks_it(world):
    store, bridge, transport = world
    alert(world)
    others = len(store.snapshot()[1]['routes']) - len(routes(store))
    assert tap(store, bridge, transport, 'Remove model')['applied'] == 1
    policy = store.snapshot()[1]
    assert routes(store) == [] and len(policy['routes']) == others
    assert policy['source']['kind'] == 'access_remove' and policy['retired'] == []
    assert bridge.removed == [(CODEX, LUNA)] and holds(store) == {}
    assert card(store)['status'] == 'done' and set(transport.buttons()) == {'Pull from Drive'}
    assert 'Removed' in transport.last()['text']


def test_remove_on_a_held_model_drops_its_saved_routes_without_publishing(world):
    store, bridge, transport = world
    alert(world, access='FAILED')
    assert key() in holds(store)
    current = store.current()
    tap(store, bridge, transport, 'Remove model')
    assert store.current() == current and holds(store) == {}
    assert bridge.removed == [(CODEX, LUNA)]


def test_a_refused_remove_changes_nothing_and_shows_why(world):
    store, bridge, transport = world
    only_model_serving_planner(store)
    alert(world)
    current = store.current()
    assert tap(store, bridge, transport, 'Remove model')['applied'] == 0
    assert store.current() == current and bridge.removed == []
    assert card(store)['status'] == 'open' and card(store)['step'] == 'stopped'
    assert 'planner' in transport.last()['text'] and set(transport.buttons()) == {'Retry', 'Remove model'}


def test_a_remove_interrupted_by_a_busy_modelctl_finishes_on_a_second_tap(world):
    store, bridge, transport = world
    alert(world)
    bridge.busy = 'modelctl is busy with a test or scan, tap again in a few minutes'
    assert tap(store, bridge, transport, 'Remove model')['applied'] == 0
    assert routes(store) == [] and card(store)['status'] == 'open'
    assert 'tap again' in transport.last()['text']
    current = store.current()
    bridge.busy = None
    assert tap(store, bridge, transport, 'Remove model')['applied'] == 1
    assert store.current() == current and bridge.removed == [(CODEX, LUNA)]


def test_remove_waits_for_a_running_reconcile_rather_than_racing_it(world, monkeypatch):
    store, bridge, transport = world
    alert(world)
    seen = []
    real = fcntl.flock

    def spy(handle, flags):
        seen.append((Path(handle.name).name, flags))
        return real(handle, flags)

    monkeypatch.setattr(model_access.fcntl, 'flock', spy)
    tap(store, bridge, transport, 'Remove model')
    assert (model_access.LOCK_FILE, fcntl.LOCK_EX) in seen  # blocking, unlike reconcile's


@pytest.mark.parametrize('status', ['FAILED', 'VERIFIED', 'INCONCLUSIVE', None])
def test_the_routing_step_does_not_treat_a_removed_model_as_held(world, status):
    store, bridge, transport = world
    alert(world, access='FAILED')
    tap(store, bridge, transport, 'Remove model')
    if status:
        bridge.report(LUNA, status)
    else:
        bridge.checks[CODEX].pop(LUNA)
    current = store.current()
    outcome = sync(world)
    assert outcome['taken_out'] == [] and outcome['restored'] == [] and outcome['refused'] == []
    assert store.current() == current and routes(store) == [] and holds(store) == {}


def test_the_removed_card_is_not_closed_by_a_later_run(world):
    store, bridge, transport = world
    alert(world)
    tap(store, bridge, transport, 'Remove model')
    sent = len(transport.sent)
    sync(world)
    assert card(store)['status'] == 'done' and len(transport.sent) == sent


# ── Pull after a removal ──

def stopped_and_removed(pulled):
    store, bridge, transport, _ = pulled
    bridge.stop(MODEL, 'login expired')
    model_access.reconcile(store, bridge, transport)
    tap(store, bridge, transport, 'Remove model')
    assert routes(store, MODEL) == []


def test_remove_forgets_the_old_approval(pulled):
    store, _, transport, _ = pulled
    sent = len(transport.sent)
    stopped_and_removed(pulled)
    assert model_asks.approved_pairs(store, CODEX, MODEL) == []
    old = next(r for r in model_asks.records(store) if r['kind'] == 'new_class')
    assert old['status'] == 'superseded' and not old['send_pending']
    assert len(transport.sent) == sent + 2  # the card and its result; nothing about the old approval


def test_an_unrelated_model_set_does_not_put_a_removed_model_back(pulled):
    store, bridge, transport, _ = pulled
    stopped_and_removed(pulled)
    row = next(r for r in bridge.rows if r['model_id'] == MODEL)
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-later', provider=CODEX, model_id=MODEL, family=row['family'],
                              version=row['version'], enforced_window=row['enforced_window'], predecessor_id=None))
    consume(store, bridge, asks=True, transport=transport)
    assert routes(store, MODEL) == []


def test_pull_after_a_removal_routes_the_model_again_without_duplicate_pairs(pulled):
    store, bridge, transport, _ = pulled
    before = routes(store, MODEL)
    stopped_and_removed(pulled)
    added = len(bridge.added)
    tap(store, bridge, transport, 'Pull from Drive')
    assert len(bridge.added) == added + 1
    assert routes(store, MODEL) == before
    assert [r['effort'] for r in routes(store, MODEL)] == ['medium', 'high']
    assert len(model_asks.approved_pairs(store, CODEX, MODEL)) == len(before)
    assert readds(store)[0]['status'] == 'done'
    assert '✅' in transport.last()['text']
    assert model_access.reconcile(store, bridge, transport)['taken_out'] == []


DRIVE_VALUES = [('medium', 45, 0.6), ('high', 50, 1.2)]


def values(store, model=MODEL):
    return [(r['effort'], r['intelligence'], r['relative_cost']) for r in routes(store, model)]


def route_an_older_member(store, bridge):
    """Make routed Luna an older model of Comet's class, returning the scores a successor would inherit."""
    bridge.rows.append(dict(backend_id=CODEX, model_id=LUNA, family='comet', version=[6], aliases=[LUNA], set=True,
                            enforced_window=272000,
                            lifecycle=dict(prepare=[100000, 120000], fresh=[140000, 170000], ceiling=220000)))
    inherited = values(store, LUNA)
    assert inherited and inherited != DRIVE_VALUES
    return inherited


def test_pull_after_a_removal_uses_the_drive_values_while_an_older_member_is_routed(pulled):
    store, bridge, transport, _ = pulled
    stopped_and_removed(pulled)
    route_an_older_member(store, bridge)
    tap(store, bridge, transport, 'Pull from Drive')
    assert readds(store)[0]['status'] == 'done'
    assert values(store) == DRIVE_VALUES


def test_a_first_pull_uses_the_drive_values_while_an_older_member_is_routed(tmp_path, monkeypatch):
    store, bridge, transport = PolicyStore(tmp_path / 'models'), PulledBridge(), InPlace()
    store.publish(fixture_policy(), expected=None)
    drive = Drive(HEADER + GOOD)
    monkeypatch.setattr(model_sheet, '_download', drive.download)
    monkeypatch.setattr(model_sheet, '_upload', drive.upload)
    detected((store, bridge, transport))
    route_an_older_member(store, bridge)
    tap(store, bridge, transport, 'Pull from Drive')
    assert values(store) == DRIVE_VALUES


def test_a_set_without_an_approval_still_inherits_from_the_older_member(pulled):
    store, bridge, transport, _ = pulled
    stopped_and_removed(pulled)  # its approval is gone
    inherited = route_an_older_member(store, bridge)
    row = next(r for r in bridge.rows if r['model_id'] == MODEL)
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-successor', provider=CODEX, model_id=MODEL,
                              family=row['family'], version=row['version'], enforced_window=row['enforced_window'],
                              predecessor_id=LUNA))
    consume(store, bridge, asks=True, transport=transport)
    assert values(store) == inherited


def test_a_refused_pull_saves_nothing_and_tells_dan(pulled):
    store, bridge, transport, drive = pulled
    stopped_and_removed(pulled)
    data = transport.buttons()['Pull from Drive']
    drive.fail = True
    outcome = tap_data(pulled, data)
    assert outcome['errors'] and readds(store) == []
    assert 'That did not work' in transport.last()['text'] and 'network down' in transport.last()['text']
    drive.fail = False
    tap_data(pulled, data)
    assert routes(store, MODEL)


def test_pull_on_a_model_that_already_has_routes_is_refused_with_that_reason(pulled):
    store, bridge, transport, _ = pulled
    stopped_and_removed(pulled)
    data = transport.buttons()['Pull from Drive']
    tap_data(pulled, data)
    added = len(bridge.added)
    tap_data(pulled, data)
    assert len(bridge.added) == added and len(readds(store)) == 1
    assert 'already has routes' in transport.last()['text']


def test_an_open_readd_is_resent_rather_than_doubled_and_declining_it_needs_no_proposal(pulled):
    store, bridge, transport, _ = pulled
    stopped_and_removed(pulled)
    data = transport.buttons()['Pull from Drive']
    bridge.add_ok = False
    tap_data(pulled, data)
    assert readds(store)[0]['status'] == 'open' and readds(store)[0]['step'] == 'failed'
    sent = len(transport.sent)
    tap_data(pulled, data)
    assert len(readds(store)) == 1 and len(transport.sent) == sent + 1
    tap(store, bridge, transport, 'Decline')
    assert readds(store)[0]['status'] == 'declined' and routes(store, MODEL) == []


def test_a_readd_is_not_closed_as_stale_and_a_new_detection_does_not_trip_on_it(pulled):
    store, bridge, transport, _ = pulled
    stopped_and_removed(pulled)
    bridge.add_ok = False
    tap(store, bridge, transport, 'Pull from Drive')
    readd = readds(store)[0]
    record = dict(readd, step='card', attempted=False)
    model_asks._save(store, record)
    model_asks.flush(store, transport, bridge)
    assert readds(store)[0]['status'] == 'open'
    bridge.detect('gpt-8-comet', family=readd['family'])
    assert not consume(store, bridge, asks=True, transport=transport)['errors']
    assert readds(store)[0]['status'] == 'open'


# ── the bridge against the real owner ──

def state_home(tmp_path, models):
    state = tmp_path / '.local/state/modelctl/state.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(dict(providers=dict(codex=dict(models=models)))))
    return state


@pytest.fixture
def owner():
    if not (OWNER / 'modelctl/paths.py').exists():
        pytest.skip('external owner checkout unavailable')
    return OWNER


def test_the_bridge_reads_stops_and_leaves_out_removed_models(tmp_path, owner):
    state_home(tmp_path, {
        'a': dict(status='configured', access='FAILED', retest_stopped=dict(reason='rejected', at='t1')),
        'b': dict(status='configured', access='INCONCLUSIVE', retest_stopped=dict(reason='x', at='t2'), removed_by_dan='t3'),
        'c': dict(status='configured', access='VERIFIED')})
    assert ModelctlBridge(owner, home=tmp_path).stopped(CODEX) == {
        'a': dict(reason='rejected', at='t1', access='FAILED')}


def test_a_real_owner_stop_reaches_a_card_and_the_routing_step(tmp_path, owner):
    state_home(tmp_path, {LUNA: dict(status='configured', access='FAILED',
                                     retest_stopped=dict(reason='model not supported', at='t1'))})
    store = PolicyStore(tmp_path / 'models')
    store.publish(fixture_policy(), expected=None)
    transport = InPlace()
    model_access.reconcile(store, ModelctlBridge(owner, home=tmp_path), transport)
    assert routes(store) == [] and card(store)['failure'] == 'model not supported'
    assert set(transport.buttons()) == {'Retry', 'Remove model'}


def test_the_bridge_marks_a_removal_through_the_owner(tmp_path, owner):
    state = state_home(tmp_path, {'a': dict(status='configured', access='FAILED', retest_stopped=dict(reason='x', at='t'))})
    ModelctlBridge(owner, home=tmp_path).drop_from_tests(CODEX, 'a')
    entry = json.loads(state.read_text())['providers']['codex']['models']['a']
    assert entry['removed_by_dan'] and 'retest_stopped' not in entry


def test_the_bridge_explains_a_busy_owner_in_plain_words(tmp_path, owner):
    state_home(tmp_path, {'a': dict(status='configured')})
    lock = tmp_path / '.local/state/modelctl/modelctl.lock'
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(PolicyError, match='busy with a test or scan.*tap again'):
            ModelctlBridge(owner, home=tmp_path).drop_from_tests(CODEX, 'a')


def test_the_bridge_reports_the_owners_refusal_of_a_retest(tmp_path, owner):
    state_home(tmp_path, {})
    with pytest.raises(PolicyError, match='no configured model'):
        ModelctlBridge(owner, home=tmp_path).retest(CODEX, 'nope')


def fake_owner(tmp_path, body):
    package = tmp_path / 'source/modelctl'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'paths.py').write_text('')
    (package / 'cli.py').write_text(f'def main(argv):\n{body}\n')
    return ModelctlBridge(tmp_path / 'source', home=tmp_path)


def test_the_bridge_returns_the_retest_answer_and_asks_for_the_one_model(tmp_path):
    bridge = fake_owner(tmp_path, '    import json\n    print(json.dumps(dict(argv=argv, outcome="passed", detail="", access="VERIFIED")))\n    return 0')
    answer = bridge.retest(CODEX, 'gpt-x')
    assert answer['argv'] == ['retest', '--provider', 'codex', '--model', 'gpt-x', '--json']
    assert answer['outcome'] == 'passed'


@pytest.mark.parametrize('body, match', [
    ('    print(\'{"error": "lock held"}\')\n    return 1', 'lock held'),
    ('    return 3', 'retest'),
    ('    print("not json")\n    return 0', 'retest'),
    ('    print(\'{"outcome": "weird"}\')\n    return 0', 'retest'),
    ('    import time\n    time.sleep(5)\n    return 0', 'did not finish'),
])
def test_the_bridge_turns_every_failed_retest_into_a_policy_error(tmp_path, body, match):
    with pytest.raises(PolicyError, match=match):
        fake_owner(tmp_path, body).retest(CODEX, 'gpt-x', timeout=1)


def test_the_bridge_add_still_returns_its_output(tmp_path):
    bridge = fake_owner(tmp_path, '    print("done", argv)\n    return 0')
    ok, output = bridge.add(CODEX, ['--model', 'm'])
    assert ok and "['add', 'codex', '--model', 'm']" in output
