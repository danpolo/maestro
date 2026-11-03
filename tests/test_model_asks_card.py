"""Dan's Use flow (2026-11-01): one card, edited in place; every tap answers; taps never die."""
import json

import pytest

from maestro import model_asks
from maestro.model_policy import PolicyError, PolicyStore
from tests.test_model_asks import Bridge, Transport, detected, only, tap, reply
from pathlib import Path


class InPlace(Transport):
    def __init__(self):
        super().__init__()
        self.texts = []

    def edit_text(self, message_id, text, *, markup=None):
        self.texts.append((message_id, text))
        super().edit_text(message_id, text, markup=markup)


@pytest.fixture
def world(tmp_path):
    policy = json.loads((Path(__file__).parent / 'fixtures/model_policy/schema2.json').read_text())
    store, bridge, transport = PolicyStore(tmp_path / 'models'), Bridge(), InPlace()
    store.publish(policy, expected=None)
    detected((store, bridge, transport))
    return store, bridge, transport


def use(world):
    store, bridge, transport = world
    tap(store, bridge, transport, 'Use')


def tap_prefix(world, prefix):
    store, bridge, transport = world
    label = next(t for t in transport.buttons() if t.startswith(prefix))
    tap(store, bridge, transport, label)


def fill(world, prefix, *values):
    tap_prefix(world, prefix)
    for value in values:
        reply(*world, value)


def complete(world):
    fill(world, 'Prepare', '100K-120K')
    fill(world, 'Start fresh', '140K-170K')
    fill(world, 'Ceiling', '220K')
    fill(world, 'Window', '540K')
    fill(world, 'Max output', '128K')
    fill(world, 'high', '50', '1.2')


def test_use_turns_the_same_message_into_a_card_of_every_value(world):
    store, bridge, transport = world
    before = len(transport.sent)
    use(world)
    assert len(transport.sent) == before and transport.texts
    assert only(store)['step'] == 'card'
    labels = list(transport.buttons())
    for label in ('Prepare handoff: ❓', 'Start fresh: ❓', 'Ceiling: ❓', 'Window: ❓', 'Max output: ❓',
                  'low: ❓', 'medium: ❓', 'high: ❓', 'xhigh: ❓', 'max: ❓', 'Apply', 'Decline'):
        assert label in labels, labels


def test_card_starts_empty_for_an_unregistered_model_and_offers_suggestions(world):
    store, *_ = world
    use(world)
    assert only(store)['answers'] == {}
    tap_prefix(world, 'Ceiling')
    assert any(t.startswith('Use suggestion') for t in world[2].buttons())
    assert 'Back' in world[2].buttons()


def test_card_shows_known_values_editable_for_a_model_with_a_row(tmp_path):
    policy = json.loads((Path(__file__).parent / 'fixtures/model_policy/schema2.json').read_text())
    store, transport = PolicyStore(tmp_path / 'models'), InPlace()
    bridge = Bridge([dict(backend_id='codex', model_id='gpt-7-comet', family='comet', version=[7], aliases=['gpt-7-comet'],
                          enforced_window=540000, set=True,
                          lifecycle=dict(prepare=[220000, 250000], fresh=[300000, 340000], ceiling=450000))])
    store.publish(policy, expected=None)
    world = store, bridge, transport
    detected(world)
    use(world)
    assert 'Ceiling: 450K' in transport.buttons() and 'Max output: ❓' in transport.buttons()
    fill(world, 'Ceiling', '440K')
    assert 'Ceiling: 440K' in transport.buttons()


def test_a_bad_value_stays_on_its_prompt_and_says_why(world):
    use(world)
    fill(world, 'Ceiling', 'lots')
    assert '❗' in world[2].last()['text'] and only(world[0])['step'] == 'cell'


def test_apply_with_missing_values_names_them_and_changes_nothing(world):
    store, bridge, transport = world
    use(world)
    fill(world, 'Ceiling', '220K')
    tap(store, bridge, transport, 'Apply')
    text = transport.last()['text']
    assert '❗' in text and 'Prepare handoff' in text and 'effort' in text
    assert only(store)['step'] == 'card' and not bridge.added


def test_unfilled_efforts_are_not_routed_and_filled_ones_are(world):
    store, bridge, transport = world
    use(world)
    complete(world)
    assert 'high: 50 · 1.2' in transport.buttons()
    tap(store, bridge, transport, 'Apply')
    assert only(store)['status'] == 'done'
    routes = [r for r in store.snapshot()[1]['routes'] if r['model_id'] == 'gpt-7-comet']
    assert [(r['effort'], r['intelligence'], r['relative_cost']) for r in routes] == [('high', 50, 1.2)]


def test_not_routed_clears_an_effort(world):
    store, bridge, transport = world
    use(world)
    fill(world, 'high', '50', '1.2')
    tap_prefix(world, 'high')
    tap(store, bridge, transport, 'Not routed')
    assert 'high: ❓' in transport.buttons() and only(store)['pair_values'] == {}


def test_half_filled_effort_blocks_apply(world):
    store, bridge, transport = world
    use(world)
    complete(world)
    fill(world, 'low', '40')
    tap(store, bridge, transport, 'Back')
    tap(store, bridge, transport, 'Apply')
    assert '❗' in transport.last()['text'] and 'low' in transport.last()['text'] and not bridge.added


def test_decline_from_the_alert_edits_it_and_never_asks_again(world):
    store, bridge, transport = world
    before = len(transport.sent)
    tap(store, bridge, transport, 'Decline')
    assert len(transport.sent) == before and 'Declined' in transport.last()['text']
    assert only(store)['status'] == 'declined' and not transport.last()['markup']['inline_keyboard']
    model_asks.flush(store, transport, bridge, now=only(store)['created'] + 10 * 86400)
    assert len(transport.sent) == before


def test_decline_from_the_card_works(world):
    store, bridge, transport = world
    use(world)
    tap(store, bridge, transport, 'Decline')
    assert only(store)['status'] == 'declined' and not bridge.added


def test_failed_apply_returns_to_the_card_with_its_values(world):
    store, bridge, transport = world
    bridge.add_ok = False
    use(world)
    complete(world)
    tap(store, bridge, transport, 'Apply')
    assert only(store)['step'] == 'failed'
    bridge.add_ok = True
    tap(store, bridge, transport, 'Re-enter values')
    assert only(store)['step'] == 'card' and 'Ceiling: 220K' in transport.buttons()


def test_a_refused_tap_is_answered_on_telegram_and_leaves_the_inbox(world, monkeypatch):
    store, bridge, transport = world
    monkeypatch.setattr(model_asks, '_start_card', lambda *a: (_ for _ in ()).throw(PolicyError('cannot start')))
    before = transport.last()['markup']
    (store.home / 'asks/inbox').mkdir(exist_ok=True)
    data = before['inline_keyboard'][0][0]['callback_data']
    (store.home / 'asks/inbox/7.json').write_text(json.dumps(dict(update_id=7, kind='callback', data=data, chat_id=42)))
    result = model_asks.process(store, bridge, transport)
    assert not list((store.home / 'asks/inbox').iterdir())
    assert 'cannot start' in transport.last()['text'] and 'Use' in transport.buttons()
    assert result['errors']


@pytest.mark.parametrize('error', [StopIteration(), KeyError('x')])
def test_an_unexpected_error_cannot_jam_the_inbox(world, monkeypatch, error):
    store, bridge, transport = world
    monkeypatch.setattr(model_asks, '_handle', lambda *a: (_ for _ in ()).throw(error))
    (store.home / 'asks/inbox').mkdir(exist_ok=True)
    (store.home / 'asks/inbox/8.json').write_text(json.dumps(dict(update_id=8, kind='command', text='/models', chat_id=42)))
    model_asks.process(store, bridge, transport)
    assert not list((store.home / 'asks/inbox').iterdir())
    assert '❗' in transport.last()['text']


def test_agy_efforts_come_from_the_models_own_variants():
    bridge = Bridge([dict(backend_id='antigravity_cli', model_id='gpt-oss-120b', family='gpt-oss', version=[120],
                          aliases=['gpt-oss-120b', 'gpt-oss-120b-medium'], set=False, enforced_window=None)])
    record = dict(backend='antigravity_cli', model_id='gpt-oss-120b')
    assert model_asks._deliverable_efforts(bridge, record) == ['medium']


def test_a_stuck_inbox_warns_dan_once_a_day(world):
    store, bridge, transport = world
    inbox = store.home / 'asks/inbox'
    inbox.mkdir(exist_ok=True)
    stale = inbox / '9.json'
    stale.write_text('{}')
    import os
    os.utime(stale, (1000, 1000))
    now = 1000 + 3 * 3600
    count = len(transport.sent)
    model_asks.flush(store, transport, bridge, now=now)
    assert len(transport.sent) == count + 1 and 'not being processed' in transport.sent[-1]['text']
    model_asks.flush(store, transport, bridge, now=now + 60)
    assert len(transport.sent) == count + 1


def agy_card(**row):
    """A card for an agy model, as modelctl reports it; the ceiling and native are Dan's Argon values."""
    bridge = Bridge([dict(backend_id='antigravity_cli', model_id='gemini-4-argon', family='gemini-argon', version=[4],
                          aliases=['gemini-4-argon', 'gemini-4-argon-high'], set=True, enforced_window=540000,
                          lifecycle=dict(prepare=[220000, 250000], fresh=[300000, 340000], ceiling=450000), **row)])
    record = dict(kind='new_class', ask_id='a1', backend='antigravity_cli', model_id='gemini-4-argon',
                  suggestions={}, current={}, pair_effort=None)
    model_asks._start_card(None, bridge, record)
    record.update(status='open', pair_values=dict(high=dict(intelligence=50, cost=10.6)))
    record['answers'].update(max_output=64000, ceiling=165000, handoff=[130000, 140000], fresh=[145000, 155000],
                             native_window=198000)
    return record


def texts(rows):
    return [b['text'] for row in rows for b in row]


def test_agy_card_shows_the_enforced_window_it_will_save():
    record = agy_card()
    assert 'window' in record['order'] and record['answers']['window'] == 540000
    assert 'Window: 540K' in texts(model_asks._card_buttons(record))


def test_agy_apply_sends_the_window_dan_chose():
    record = agy_card()
    record['answers']['window'] = 198000
    model_asks._card_apply(record)
    assert record['status'] == 'applying' and record['error'] is None
    argv = model_asks._argv(record)
    assert argv[argv.index('--window') + 1] == '198K'


def test_a_window_over_the_native_is_stopped_on_the_card_not_by_modelctl():
    record = agy_card()
    model_asks._card_apply(record)
    assert record['status'] == 'open' and record['step'] == 'card'
    assert 'enforced window 540K exceeds the native window 198K' in record['error']


def test_window_default_stays_within_the_native_and_replaces_an_impossible_value():
    record = agy_card()
    record['current'] = dict(window=540000)
    assert 'Use default 198K' in texts(model_asks._cell_buttons(record, 'window'))  # 165K x 1.2 rounds to 200K


def test_no_window_default_when_the_native_leaves_no_room_above_the_ceiling():
    record = agy_card()
    record['answers']['native_window'] = 165000
    assert not [t for t in texts(model_asks._cell_buttons(record, 'window')) if t.startswith('Use default')]


def test_only_codex_is_warned_that_it_may_save_a_higher_window():
    record = agy_card()
    record.update(step='cell', pending_cell='window', current={})
    text, _ = model_asks.render(record)
    assert 'Codex' not in text and 'compact' in text
    record['backend'] = 'codex'
    text, _ = model_asks.render(record)
    assert 'Codex never goes below' in text and 'compact' not in text
