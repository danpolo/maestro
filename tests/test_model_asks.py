"""Telegram model asks: Dan approves a new class or reviews inherited limits from Telegram alone.

Fake bridge, transport and inbox files only; nothing here talks to Telegram or modelctl.
"""
import json
import os

import pytest

from maestro import model_asks
from maestro.model_detection import consume, proposals, switches
from maestro.model_policy import PolicyError, PolicyStore, catalog_defaults

UNKNOWN = dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000)


def sol_rows():
    rows = [dict(backend_id='codex', model_id=m, family='sol', version=v, aliases=[m], enforced_window=w, set=True,
                 lifecycle=dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000))
            for m, v, w in (('gpt-6.1-sol', [6, 1], 420000), ('gpt-6.2-sol', [6, 2], 390000))]
    return rows


class Bridge:
    """modelctl as Maestro sees it: outbox, class rows, the Unknown row and `add`."""

    def __init__(self, rows=(), *, add_ok=True):
        self.events, self.acked, self.added = [], set(), []
        self.rows, self.add_ok = list(rows), add_ok

    def pending_events(self):
        return [e for e in self.events if e['event_id'] not in self.acked]

    def acknowledge(self, event_id):
        self.acked.add(event_id)

    def configured_models(self, backend, *, identities=()):
        return dict(contract=1, models=[r for r in self.rows if r['backend_id'] == backend])

    def limits(self, backend):
        return dict(unknown=dict(UNKNOWN, display='Model unknown'))

    def detect(self, model, family='comet', first_seen='2026-10-08T00:00:00Z'):
        self.events.append(dict(kind='NEW_MODEL_DISCOVERED', event_id=f'new-{model}', provider='codex',
                                model_id=model, family=family, first_seen=first_seen, fingerprint='f',
                                related_model_ids=[]))

    def add(self, backend, argv):
        """Stand-in for the transactional `modelctl add`: write the row, emit MODEL_SET."""
        self.added.append(argv)
        if not self.add_ok:
            return False, 'checking limits\nFAILED: ceiling 900K exceeds the native window\n(nothing was modified)'
        opts = dict(zip(argv[0:-1:2], argv[1::2]))
        model = opts['--model']
        handoff, fresh = (model_asks.parse_cell(c, opts['--' + c]) for c in ('handoff', 'fresh'))
        row = next((r for r in self.rows if r['model_id'] == model), None)
        if row is None:
            row = dict(backend_id=backend, model_id=model, family='comet', version=[7], aliases=[model], set=True)
            self.rows.append(row)
        row.update(lifecycle=dict(prepare=handoff, fresh=fresh, ceiling=model_asks.parse_cell('ceiling', opts['--ceiling'])),
                   enforced_window=model_asks.parse_cell('window', opts['--window']))
        if '--strength' in opts:
            row['routing_profile'] = dict(strength=opts['--strength'], relative_cost=float(opts['--relative-cost']),
                                          max_output_tokens=int(opts['--max-output']))
            if '--native-window' in opts:
                row['routing_profile']['native_window_tokens'] = model_asks.parse_cell('native_window',
                                                                                       opts['--native-window'])
        self.events.append(dict(kind='MODEL_SET', event_id=f'set-{model}-{len(self.added)}', provider=backend,
                                model_id=model, family=row['family'], version=row['version'],
                                enforced_window=row['enforced_window'], predecessor_id=None))
        return True, 'Done. Maestro adopts between tasks.'


class Transport:
    chat_id = '42'

    def __init__(self):
        self.sent, self.edits = [], []

    def send(self, text, markup=None):
        self.sent.append(dict(id=100 + len(self.sent), text=text, markup=markup))
        return self.sent[-1]['id']

    def edit(self, message_id, *, markup=None):
        self.edits.append((message_id, markup))

    def edit_text(self, message_id, text, *, markup=None):
        message = next(m for m in self.sent if m['id'] == message_id)
        message.update(text=text, markup=markup)

    def last(self):
        return self.sent[-1]

    def buttons(self):
        markup = self.last()['markup'] or {'inline_keyboard': []}
        return {b['text']: b['callback_data'] for row in markup['inline_keyboard'] for b in row}


@pytest.fixture
def world(tmp_path, monkeypatch):
    # This suite covers the schema 1 role/grade walk on historical policies.
    # Schema 2 pair approval is exercised in test_model_management_schema2.
    from maestro import model_policy
    monkeypatch.setattr(model_policy, 'shipped_policy', model_policy.shipped_policy_v1)
    store = PolicyStore(tmp_path / 'models')
    return store, Bridge(), Transport()


_UPDATES = iter(range(1, 10_000))


def inbox(store, **update):
    update.setdefault('update_id', next(_UPDATES))
    update.setdefault('chat_id', 42)
    path = store.home / 'asks' / 'inbox' / f'{update["update_id"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(update))
    return update['update_id']


def tap(store, bridge, transport, label):
    data = transport.buttons()[label]
    inbox(store, kind='callback', data=data, message_id=transport.last()['id'])
    return model_asks.process(store, bridge, transport)


def reply(store, bridge, transport, text, to=None):
    message = to or transport.last()
    inbox(store, kind='message', text=text, reply_to_message_id=message['id'], reply_to_text=message['text'])
    return model_asks.process(store, bridge, transport)


def only(store):
    [record] = model_asks.records(store)
    return record


def detected(world, model='gpt-7-comet', **kw):
    store, bridge, transport = world
    bridge.detect(model, **kw)
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors']
    return result


def test_new_class_three_steps_end_with_modelctl_add_and_role_assignment(world):
    store, bridge, transport = world
    detected(world)
    assert 'gpt-7-comet' in transport.last()['text'] and 'mdl:' in transport.last()['text']
    assert set(transport.buttons()) == {'Use', 'Pull from Drive', 'Decline'}
    assert 'gpt-6.1-sol' in transport.last()['text']  # the other routes' values, for context
    tap(store, bridge, transport, 'Use')
    judge = next(label for label in transport.buttons() if 'judge' in label)
    assert 'gpt-6.1-sol' in judge
    sent = len(transport.sent)
    tap(store, bridge, transport, judge)
    assert len(transport.sent) == sent and transport.edits  # a toggle edits the keyboard in place
    assert only(store)['roles'] == ['judge']
    tap(store, bridge, transport, 'Done')
    # Strength and cost inherit from the replaced judge model; max output is always asked.
    assert only(store)['answers'] == dict(strength='strong', relative_cost=1.0)
    assert 'Max output' in transport.last()['text']
    reply(store, bridge, transport, '32K')
    tap(store, bridge, transport, 'Use suggestion 180K-200K')
    reply(store, bridge, transport, '240-270K')
    reply(store, bridge, transport, '350K')
    assert 'Use default 420K' in transport.buttons()
    tap(store, bridge, transport, 'Use default 420K')
    assert bridge.added == [['--model', 'gpt-7-comet', '--handoff', '180K-200K', '--fresh', '240K-270K',
                             '--ceiling', '350K', '--window', '420K', '--strength', 'strong',
                             '--relative-cost', '1', '--max-output', '32000', '-y']]
    record = only(store)
    assert (record['status'], record['step']) == ('done', 'closed')
    policy = store.load(store.current())
    assert policy['roles']['judge']['models']['codex'] == 'gpt-7-comet'
    assert policy['roles']['diagnoser']['models']['codex'] == 'gpt-6.1-sol'
    assert 'gpt-7-comet' in catalog_defaults(policy, {})['codex']['available_models']
    assert [p['status'] for p in proposals(store)] == ['applied']
    assert transport.last()['text'].startswith('✅') and transport.last()['markup'] is None
    # The MODEL_SET switch is covered by this ask: no second ask, no plain notice.
    assert all(not s['notification_pending'] for s in switches(store))
    assert len(model_asks.records(store)) == 1


def test_invalid_reply_reasks_the_same_cell_and_a_reply_to_the_error_counts(world):
    store, bridge, transport = world
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')  # no roles: strength and cost are asked too
    assert only(store)['pending_cell'] == 'strength'
    tap(store, bridge, transport, 'balanced')
    reply(store, bridge, transport, 'cheap')
    assert transport.last()['text'].startswith('❗') and only(store)['pending_cell'] == 'relative_cost'
    reply(store, bridge, transport, '0.75')
    reply(store, bridge, transport, '32768')
    assert only(store)['answers']['max_output'] == 32768
    reply(store, bridge, transport, '200K-250K')
    reply(store, bridge, transport, '240K-260K')  # fresh must start after the handoff range ends
    assert 'must end before the fresh range' in transport.last()['text']
    reply(store, bridge, transport, '260K-280K')
    reply(store, bridge, transport, '250500')  # not a whole K
    assert 'whole number of K' in transport.last()['text']
    reply(store, bridge, transport, '300K')
    assert only(store)['pending_cell'] == 'window'


def test_stale_taps_duplicate_updates_and_foreign_chats_change_nothing(world):
    store, bridge, transport = world
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')
    use = only(store)['ask_id']
    before = only(store)
    inbox(store, kind='callback', data=f'mdl:{use}:set:handoff:180K-200K')  # not the pending cell
    inbox(store, kind='callback', data=f'mdl:{use}:set:strength:light', chat_id=7)  # foreign chat
    result = model_asks.process(store, bridge, transport)
    assert result['ignored'] == 1 and result['processed'] == 1
    assert only(store)['answers'] == before['answers'] and only(store)['pending_cell'] == 'strength'
    update = inbox(store, kind='callback', data=f'mdl:{use}:set:strength:light')
    model_asks.process(store, bridge, transport)
    inbox(store, update_id=update, kind='callback', data=f'mdl:{use}:set:strength:strong')  # redelivered id
    model_asks.process(store, bridge, transport)
    assert only(store)['answers']['strength'] == 'light'
    assert not list((store.home / 'asks' / 'inbox').glob('*.json'))


def test_decline_rejects_the_proposal_and_closes_the_ask(world):
    store, bridge, transport = world
    detected(world)
    tap(store, bridge, transport, 'Decline')
    assert only(store)['status'] == 'declined'
    assert [p['status'] for p in proposals(store)] == ['rejected']
    assert transport.last()['text'].startswith('🚫') and not bridge.added
    assert store.current() is None


def test_newer_detection_of_the_same_class_supersedes_the_open_ask(world):
    store, bridge, transport = world
    detected(world)
    first = only(store)['ask_id']
    detected(world, model='gpt-7.1-comet', first_seen='2026-10-09T00:00:00Z')
    by_model = {r['model_id']: r for r in model_asks.records(store)}
    assert by_model['gpt-7-comet']['status'] == 'superseded'
    assert by_model['gpt-7.1-comet']['status'] == 'open'
    assert {p['destination'][1]: p['status'] for p in proposals(store)} == {
        'gpt-7-comet': 'superseded', 'gpt-7.1-comet': 'waiting_limits'}
    assert any(f'mdl:{first}' in m['text'] and m['text'].startswith('⏭') for m in transport.sent)


def test_failed_add_reports_modelctl_error_and_retry_completes(world):
    store, bridge, transport = world
    bridge.add_ok = False
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')
    tap(store, bridge, transport, 'strong')
    for value in ('1.4', '32K', '180K-200K', '240K-270K', '350K', '420K'):
        reply(store, bridge, transport, value)
    record = only(store)
    assert (record['status'], record['step']) == ('open', 'failed')
    assert 'FAILED: ceiling 900K exceeds the native window' in transport.last()['text']
    assert {'Retry', 'Re-enter values', 'Pull from Drive', 'Decline'} == set(transport.buttons())
    assert store.current() is None
    bridge.add_ok = True
    tap(store, bridge, transport, 'Retry')
    assert only(store)['status'] == 'done' and len(bridge.added) == 2
    assert 'gpt-7-comet' in catalog_defaults(store.load(store.current()), {})['codex']['available_models']


def test_reenter_after_failure_offers_the_entered_values_as_keep(world):
    store, bridge, transport = world
    bridge.add_ok = False
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')
    tap(store, bridge, transport, 'strong')
    for value in ('1.4', '32K', '180K-200K', '240K-270K', '350K', '420K'):
        reply(store, bridge, transport, value)
    tap(store, bridge, transport, 'Re-enter values')
    assert only(store)['pending_cell'] == 'strength' and 'strong (current)' in transport.buttons()
    tap(store, bridge, transport, 'strong (current)')
    assert 'Keep 1.4' in transport.buttons()


def same_class_switch(world):
    store, bridge, transport = world
    bridge.rows = sol_rows()
    bridge.events.append(dict(kind='MODEL_SET', event_id='switch-1', provider='codex', model_id='gpt-6.2-sol',
                              family='sol', version=[6, 2], predecessor_id='gpt-6.1-sol', enforced_window=390000))
    notices = []
    result = consume(store, bridge, notify=notices.append, asks=True, transport=transport)
    assert not result['errors'] and result['switches_applied'] == 1
    assert notices == []  # the review ask replaces the plain notice
    return only(store)


def test_same_class_switch_sends_a_non_blocking_review_and_keep_changes_nothing(world):
    store, bridge, transport = world
    record = same_class_switch(world)
    assert record['kind'] == 'limits_review' and record['step'] == 'review'
    text = transport.last()['text']
    assert 'gpt-6.1-sol' in text and 'gpt-6.2-sol' in text and '390K' in text and 'Nothing waits' in text
    assert [s['notification'] for s in switches(store)] == [f'model ask {record["ask_id"]}']
    revision = store.current()
    tap(store, bridge, transport, 'Keep')
    assert only(store)['status'] == 'kept' and not bridge.added and store.current() == revision
    # Replay creates nothing new.
    consume(store, bridge, asks=True, transport=transport)
    assert len(model_asks.records(store)) == 1


def test_review_change_of_one_lifecycle_cell_passes_limits_but_not_the_profile(world):
    store, bridge, transport = world
    same_class_switch(world)
    tap(store, bridge, transport, 'Change')
    assert 'Keep 180K-200K' in transport.buttons()
    reply(store, bridge, transport, '200K-220K')
    for _ in range(3):  # fresh, ceiling, window
        tap(store, bridge, transport, next(label for label in transport.buttons() if label.startswith('Keep')))
    tap(store, bridge, transport, 'strong (current)')
    for _ in range(2):  # relative cost, max output
        tap(store, bridge, transport, next(label for label in transport.buttons() if label.startswith('Keep')))
    assert bridge.added == [['--model', 'gpt-6.2-sol', '--handoff', '200K-220K', '--fresh', '240K-270K',
                             '--ceiling', '350K', '--window', '390K', '-y']]
    assert only(store)['status'] == 'done'


def test_review_profile_change_passes_all_three_profile_flags(world):
    store, bridge, transport = world
    same_class_switch(world)
    tap(store, bridge, transport, 'Change')
    for _ in range(4):
        tap(store, bridge, transport, next(label for label in transport.buttons() if label.startswith('Keep')))
    tap(store, bridge, transport, 'balanced')
    for _ in range(2):
        tap(store, bridge, transport, next(label for label in transport.buttons() if label.startswith('Keep')))
    assert bridge.added[0][-7:] == ['--strength', 'balanced', '--relative-cost', '1', '--max-output', '32000', '-y']


def test_review_keeping_every_cell_closes_without_modelctl(world):
    store, bridge, transport = world
    same_class_switch(world)
    tap(store, bridge, transport, 'Change')
    while only(store)['step'] == 'cell':
        labels = transport.buttons()
        tap(store, bridge, transport, next(l for l in labels if l.startswith('Keep') or l.endswith('(current)')))
    assert only(store)['status'] == 'kept' and not bridge.added


def test_owner_entered_switch_settles_silently(world):
    store, bridge, transport = world
    bridge.rows = sol_rows()
    bridge.events.append(dict(kind='MODEL_SET', event_id='manual', provider='codex', model_id='gpt-6.2-sol',
                              family='sol', version=[6, 2], predecessor_id=None, enforced_window=390000))
    consume(store, bridge, asks=True, transport=transport)
    assert not model_asks.records(store) and not transport.sent
    assert [s['notification_pending'] for s in switches(store)] == [False]


def test_reminders_only_for_open_new_class_asks_once_a_day(world):
    store, bridge, transport = world
    detected(world)
    sent = model_asks.records(store)[0]['last_sent']
    model_asks.flush(store, transport, bridge, now=sent + model_asks.REMIND_AFTER - 1)
    assert len(transport.sent) == 1
    model_asks.flush(store, transport, bridge, now=sent + model_asks.REMIND_AFTER)
    assert len(transport.sent) == 2 and transport.last()['text'].startswith('⏰')
    model_asks.flush(store, transport, bridge, now=sent + model_asks.REMIND_AFTER + 60)
    assert len(transport.sent) == 2


def test_review_asks_are_never_reminded(world):
    store, bridge, transport = world
    record = same_class_switch(world)
    model_asks.flush(store, transport, bridge, now=record['last_sent'] + 10 * model_asks.REMIND_AFTER)
    assert len(transport.sent) == 1


def test_without_a_transport_messages_wait_and_status_lists_the_ask(world):
    store, bridge, _ = world
    bridge.detect('gpt-7-comet')
    result = consume(store, bridge, asks=True, transport=None)
    assert result['asks']['send_pending'] == 1
    [listed] = model_asks.open_asks(store)
    assert listed['destination'] == ['codex', 'gpt-7-comet'] and listed['send_pending']
    transport = Transport()
    model_asks.flush(store, transport, bridge)
    assert len(transport.sent) == 1 and not model_asks.open_asks(store)[0]['send_pending']


def test_ask_closes_when_its_proposal_is_settled_elsewhere(world):
    store, bridge, transport = world
    detected(world)
    from maestro import model_detection
    [proposal] = proposals(store)
    proposal['status'] = 'applied'
    model_detection._save(store, proposal)
    model_asks.flush(store, transport, bridge)
    assert only(store)['status'] == 'superseded' and 'set outside Telegram' in transport.last()['text']


def test_a_message_that_is_not_a_reply_is_ignored_even_with_one_waiting_ask(world):
    store, bridge, transport = world
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')
    tap(store, bridge, transport, 'strong')
    inbox(store, kind='message', text='1.5')
    assert model_asks.process(store, bridge, transport)['ignored'] == 1
    assert 'relative_cost' not in only(store)['answers']


@pytest.mark.parametrize('cell,raw,value', [('handoff', '220-250K', [220000, 250000]), ('fresh', '260K', [260000, 260000]),
                                            ('ceiling', '450000', 450000), ('max_output', '32768', 32768),
                                            ('relative_cost', '0.5', 0.5), ('strength', 'Strong', 'strong')])
def test_cell_parsing_mirrors_modelctl(cell, raw, value):
    assert model_asks.parse_cell(cell, raw) == value


@pytest.mark.parametrize('cell,raw', [('handoff', '250K-220K'), ('ceiling', '450'), ('ceiling', '450.5K'),
                                      ('relative_cost', '-1'), ('relative_cost', 'nan'), ('strength', 'huge')])
def test_cell_parsing_refuses(cell, raw):
    with pytest.raises(PolicyError):
        model_asks.parse_cell(cell, raw)


def test_window_default_matches_modelctl():
    assert model_asks.default_window(350000) == 420000
    assert model_asks.default_window(450000) == 540000


def test_transport_config_is_read_like_send_to_me_and_never_from_tests(tmp_path, monkeypatch):
    config = tmp_path / 'telegram.env'
    config.write_text('# bot\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=-1001\n')
    config.chmod(0o600)
    assert model_asks.TelegramTransport.from_config(config) is None  # PYTEST_CURRENT_TEST is set
    monkeypatch.delenv('PYTEST_CURRENT_TEST')
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    assert model_asks.TelegramTransport.from_config(config) is None  # not rolled out yet
    (tmp_path / 'models' / 'asks').mkdir(parents=True)
    (tmp_path / 'models' / 'asks' / 'telegram.enabled').write_text('')
    transport = model_asks.TelegramTransport.from_config(config)
    assert (transport.token, transport.chat_id) == ('123:abc', '-1001')
    config.chmod(0o644)
    assert model_asks.TelegramTransport.from_config(config) is None
    assert model_asks.TelegramTransport.from_config(tmp_path / 'missing.env') is None


def test_transport_errors_never_carry_the_token(monkeypatch):
    import requests
    def boom(url, **_):
        raise requests.ConnectionError(url)
    monkeypatch.setattr(requests, 'post', boom)
    with pytest.raises(PolicyError) as caught:
        model_asks.TelegramTransport('123:secret', '42').send('hi')
    assert 'secret' not in str(caught.value) and caught.value.__cause__ is None


def test_model_asks_declares_the_owner_capability():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(model_asks.__file__).read_text())
    assert any(isinstance(node, ast.Assign) and node.targets[0].id == 'MODEL_ASKS_VERSION'
               and node.value.value == 1 for node in tree.body)


def test_values_survive_redaction_and_failure_html_stays_well_formed(world):
    store, bridge, transport = world
    record = same_class_switch(world)
    assert 'Max output: 32K' in transport.last()['text'] and '[redacted]' not in transport.last()['text']
    record.update(status='open', step='failed', error='FAILED: max_output_tokens=900000')
    text, _ = model_asks.render(record)
    assert text.count('<pre>') == text.count('</pre>') == 1


def test_reenter_after_a_registered_failure_is_not_closed_as_stale(world, monkeypatch):
    store, bridge, transport = world
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, next(label for label in transport.buttons() if 'judge' in label))
    tap(store, bridge, transport, 'Done')
    monkeypatch.setattr(model_asks, '_assign_roles', lambda *a: (_ for _ in ()).throw(PolicyError('policy changed since preview')))
    for value in ('32K', '180K-200K', '240K-270K', '350K', '420K'):
        reply(store, bridge, transport, value)
    assert only(store)['step'] == 'failed' and [p['status'] for p in proposals(store)] == ['applied']
    tap(store, bridge, transport, 'Re-enter values')
    assert (only(store)['status'], only(store)['step']) == ('open', 'cell')


def test_keep_preserves_a_precise_relative_cost():
    assert model_asks.parse_cell('relative_cost', model_asks.show('relative_cost', 0.3333333)) == 0.3333333
    assert model_asks.show('relative_cost', 1.0) == '1'


def test_a_superseded_ask_never_sent_closes_without_a_message(world):
    store, bridge, transport = world
    bridge.detect('gpt-7-comet', first_seen='2026-10-08T00:00:00Z')
    bridge.detect('gpt-7.1-comet', first_seen='2026-10-09T00:00:00Z')
    consume(store, bridge, asks=True, transport=transport)
    assert len(transport.sent) == 1 and 'gpt-7.1-comet' in transport.last()['text']


def test_without_a_transport_the_inbox_waits(world):
    store, bridge, transport = world
    detected(world)
    inbox(store, kind='callback', data=transport.buttons()['Use'], chat_id=7)
    result = model_asks.process(store, bridge, None)
    assert result['errors'] and only(store)['step'] == 'use'
    assert len(list((store.home / 'asks' / 'inbox').glob('*.json'))) == 1


def test_a_second_process_run_does_not_apply_concurrently(world):
    import fcntl
    store, bridge, transport = world
    (store.home / 'asks').mkdir(parents=True, exist_ok=True)
    with open(store.home / 'asks' / 'process.lock', 'a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert model_asks.process(store, bridge, transport).get('busy') is True


# ── live rollout gaps (2026-10-08) ──────────────────────────────────────────────────────

def stale_detection(bridge, model='gpt-6.1-sol'):
    bridge.events.append(dict(kind='NEW_MODEL_DISCOVERED', event_id=f'old-{model}', provider='codex',
                              model_id=model, family='sol', first_seen='2026-09-30T08:44:37Z',
                              fingerprint='f', related_model_ids=[]))


def test_an_old_detection_whose_class_already_has_a_newer_member_never_asks(world):
    # Live: haiku-4-5's 09-30 detection and haiku-5-5's switch landed in one consume.
    store, bridge, transport = world
    stale_detection(bridge)
    record = same_class_switch(world)  # asserts no errors and the review is the only ask
    assert record['kind'] == 'limits_review' and len(transport.sent) == 1
    [proposal] = proposals(store)
    assert proposal['status'] == 'superseded'


def test_an_open_ask_for_an_older_member_closes_once_a_newer_member_is_set(world):
    store, bridge, transport = world
    bridge.detect('gpt-6.1-sol', family='sol')
    consume(store, bridge, asks=True, transport=transport)
    [old] = model_asks.records(store)
    assert old['step'] == 'use' and len(transport.sent) == 1
    same_switch = dict(kind='MODEL_SET', event_id='switch-1', provider='codex', model_id='gpt-6.2-sol',
                       family='sol', version=[6, 2], predecessor_id='gpt-6.1-sol', enforced_window=390000)
    bridge.rows = sol_rows()
    bridge.events.append(same_switch)
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors']
    old = model_asks._load(store, old['ask_id'])
    assert old['status'] == 'superseded'
    closing = next(m['text'] for m in transport.sent if f'mdl:{old["ask_id"]}' in m['text'] and m['text'].startswith('⏭'))
    assert 'gpt-6.2-sol' in closing
    assert [p['status'] for p in proposals(store)] == ['superseded']


def test_a_newer_unset_detection_is_not_superseded_by_an_older_set_member(world):
    store, bridge, transport = world
    bridge.rows = sol_rows()
    detected(world, model='gpt-6.3-sol', family='sol')
    assert only(store)['status'] == 'open' and [p['status'] for p in proposals(store)] == ['waiting_limits']


def profileless_row(model='gpt-7-comet'):
    return dict(backend_id='codex', model_id=model, family='comet', version=[7], aliases=[model], set=True,
                enforced_window=420000, routing_profile=None,
                lifecycle=dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000))


def profileless_set(bridge, model='gpt-7-comet'):
    bridge.rows = [profileless_row(model)]
    bridge.events.append(dict(kind='MODEL_SET', event_id=f'set-{model}', provider='codex', model_id=model,
                              family='comet', version=[7], predecessor_id=None, enforced_window=420000))


def test_a_set_row_without_a_profile_becomes_a_telegram_ask_not_a_consume_error(world):
    # Live: codex gpt-5.5 and gpt-5.6-terra failed every consume with a CLI hint only.
    store, bridge, transport = world
    profileless_set(bridge)
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and bridge.acked == {'set-gpt-7-comet'}
    record = only(store)
    assert (record['kind'], record['step'], record['needs']) == ('new_class', 'use', 'profile')
    assert 'no routing profile' in transport.last()['text']
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')
    # Only the profile is asked: modelctl already holds the limits.
    assert only(store)['walk'] == ['strength', 'relative_cost', 'max_output']
    tap(store, bridge, transport, 'balanced')
    reply(store, bridge, transport, '0.8')
    reply(store, bridge, transport, '32K')
    assert bridge.added == [['--model', 'gpt-7-comet', '--handoff', '180K-200K', '--fresh', '240K-270K',
                             '--ceiling', '350K', '--window', '420K', '--strength', 'balanced',
                             '--relative-cost', '0.8', '--max-output', '32000', '-y']]
    assert only(store)['status'] == 'done'
    assert 'gpt-7-comet' in catalog_defaults(store.load(store.current()), {})['codex']['available_models']
    assert [p['status'] for p in proposals(store)] == ['applied']


def test_a_declined_profile_ask_stays_declined_on_replay(world):
    store, bridge, transport = world
    profileless_set(bridge)
    consume(store, bridge, asks=True, transport=transport)
    tap(store, bridge, transport, 'Decline')
    bridge.acked.clear()  # the same event delivered again
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and bridge.acked == {'set-gpt-7-comet'}
    assert only(store)['status'] == 'declined' and transport.last()['text'].startswith('🚫')


def test_without_asks_a_set_row_without_a_profile_is_still_a_consume_error(world):
    store, bridge, _ = world
    profileless_set(bridge)
    result = consume(store, bridge)
    assert 'explicit profile' in result['errors'][0] and not bridge.acked and not proposals(store)


def test_a_profile_ask_for_an_older_member_leaves_the_newer_detection_open(world):
    store, bridge, transport = world
    bridge.detect('gpt-7.1-comet', family='comet')  # no owner row: its version is unknown
    consume(store, bridge, asks=True, transport=transport)
    profileless_set(bridge)
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors']
    assert {r['model_id']: r['status'] for r in model_asks.records(store)} == {
        'gpt-7.1-comet': 'open', 'gpt-7-comet': 'open'}
    assert {p['destination'][1]: p['status'] for p in proposals(store)} == {
        'gpt-7.1-comet': 'waiting_limits', 'gpt-7-comet': 'waiting_limits'}


def test_a_profile_ask_for_a_newer_member_supersedes_the_older_ask_by_owner_version(world):
    store, bridge, transport = world
    older = dict(profileless_row('gpt-6-comet'), version=[6], set=False)
    bridge.rows = [older]
    bridge.detect('gpt-6-comet', family='comet')
    consume(store, bridge, asks=True, transport=transport)
    profileless_set(bridge)
    bridge.rows.append(older)
    consume(store, bridge, asks=True, transport=transport)
    assert {r['model_id']: r['status'] for r in model_asks.records(store)} == {
        'gpt-6-comet': 'superseded', 'gpt-7-comet': 'open'}


def test_a_newer_member_routed_under_an_alias_still_supersedes_an_old_detection(world):
    store, bridge, transport = world
    same_class_switch(world)
    newest = next(r for r in bridge.rows if r['model_id'] == 'gpt-6.2-sol')
    newest.update(model_id='sol-6.2', aliases=['gpt-6.2-sol'])  # owner id differs from the routed id
    stale_detection(bridge)
    consume(store, bridge, asks=True, transport=transport)
    assert [p['status'] for p in proposals(store)] == ['superseded']
    assert [r['kind'] for r in model_asks.records(store)] == ['limits_review']


def test_a_successor_without_a_verified_profile_skips_the_role_step(world):
    # gpt-6-astra's shipped profile is not native-verified, so its successor needs Dan's profile.
    store, bridge, transport = world
    life = dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000)
    bridge.rows = [dict(backend_id='codex', model_id=m, family='astra', version=v, aliases=[m], set=True,
                        enforced_window=420000, lifecycle=life) for m, v in (('gpt-6-astra', [6]), ('gpt-6.1-astra', [6, 1]))]
    bridge.events.append(dict(kind='MODEL_SET', event_id='astra-61', provider='codex', model_id='gpt-6.1-astra',
                              family='astra', version=[6, 1], predecessor_id='gpt-6-astra', enforced_window=420000))
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and only(store)['predecessor'] == 'gpt-6-astra'
    assert 'succeeds gpt-6-astra' in transport.last()['text']
    tap(store, bridge, transport, 'Use')
    assert only(store)['step'] == 'cell' and only(store)['walk'] == ['strength', 'relative_cost', 'max_output']
    tap(store, bridge, transport, 'balanced')
    reply(store, bridge, transport, '0.5')
    reply(store, bridge, transport, '32K')
    assert only(store)['status'] == 'done'
    routes = catalog_defaults(store.load(store.current()), {})['codex']['available_models']
    assert 'gpt-6.1-astra' in routes and 'gpt-6-astra' not in routes


def test_a_value_prompt_shows_only_that_cell_on_every_provider(world):
    # Dan, 2026-10-08: context for the metric being decided, Claude and Codex alike.
    store, bridge, transport = world
    bridge.rows = sol_rows() + [dict(backend_id='claude', model_id='claude-opus-5-5', family='opus', version=[5, 5],
                                     aliases=['claude-opus-5-5'], set=True, enforced_window=420000,
                                     lifecycle=dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000))]
    detected(world)
    tap(store, bridge, transport, 'Use')
    tap(store, bridge, transport, 'Done')
    tap(store, bridge, transport, 'strong')
    reply(store, bridge, transport, '1.4')
    text = transport.last()['text']
    assert 'Max output on the other models' in text
    table = text[text.index('<pre>'):]
    assert 'claude-opus-5-5' in table and 'gpt-6.1-sol' in table and '32K' in table
    assert 'handoff' not in table and '180K-200K' not in table  # no other cells
    reply(store, bridge, transport, '32K')
    tap(store, bridge, transport, 'Use suggestion 180K-200K')
    reply(store, bridge, transport, '240-270K')
    text = transport.last()['text']
    assert 'Exception ceiling on the other models' in text
    assert 'claude  claude-opus-5-5  350K' in text and 'codex   gpt-6.1-sol' in text
    reply(store, bridge, transport, '350K')
    text = transport.last()['text']  # the window is Codex-only
    assert 'Enforced window on the other models' in text
    assert 'claude-opus-5-5' not in text[text.index('<pre>'):]


# ── the /models editor (Dan, 2026-10-08) ────────────────────────────────────────────────

def luna_rows():
    return [dict(backend_id='codex', model_id='gpt-6-luna', family='luna', version=[6], aliases=['gpt-6-luna'],
                 set=True, enforced_window=340000,
                 lifecycle=dict(prepare=[120000, 140000], fresh=[180000, 220000], ceiling=280000)),
            dict(backend_id='claude', model_id='claude-opus-5-5', family='opus', version=[5, 5],
                 aliases=['claude-opus-5-5'], set=True, enforced_window=420000,
                 lifecycle=dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000))]


def models(world, pick='gpt-6-luna'):
    store, bridge, transport = world
    bridge.rows = luna_rows()
    inbox(store, kind='command', text='/models')
    model_asks.process(store, bridge, transport)
    if pick:
        tap(store, bridge, transport, pick)
    return store, bridge, transport


def cell(transport, prefix):
    return next(label for label in transport.buttons() if label.lstrip('• ').startswith(prefix))


def edit_record(store):
    [record] = [r for r in model_asks.records(store) if r['kind'] == 'edit']
    return record


def test_models_lists_every_routed_model_and_a_tap_opens_its_card(world):
    store, bridge, transport = models(world, pick=None)
    text = transport.last()['text']
    assert set(transport.buttons()) == {'gpt-6-luna', 'claude-opus-5-5'}
    assert 'claude now' in text and 'codex now' in text and '280K' in text  # every value, both providers
    assert 'not editable here' in text and 'gpt-6-astra' in text  # no limits row: listed, read-only
    assert model_asks.open_asks(store) == []  # the list itself waits on nothing
    tap(store, bridge, transport, 'gpt-6-luna')
    labels = set(transport.buttons())
    assert {'Strength: balanced', 'Relative cost: 0.5', 'Max output: 32K', 'Native window: ?',
            'Prepare handoff: 120K-140K', 'Start fresh: 180K-220K', 'Ceiling: 280K', 'Window: 340K',
            'Apply', 'Cancel'} == labels
    assert 'Maestro currently allows up to 340K' in transport.last()['text']


def test_an_edited_cell_is_marked_and_apply_sends_the_lifecycle_only(world):
    store, bridge, transport = models(world)
    tap(store, bridge, transport, 'Ceiling: 280K')
    text = transport.last()['text']
    assert 'Exception ceiling on the other models' in text and 'claude-opus-5-5  350K' in text
    assert {'Keep 280K', 'Back'} <= set(transport.buttons())
    reply(store, bridge, transport, '300K')
    assert '• Ceiling: 300K' in transport.buttons() and 'Apply (1)' in transport.buttons()
    tap(store, bridge, transport, 'Apply (1)')
    assert bridge.added == [['--model', 'gpt-6-luna', '--handoff', '120K-140K', '--fresh', '180K-220K',
                             '--ceiling', '300K', '--window', '340K', '-y']]
    assert edit_record(store)['status'] == 'done'
    assert 'Exception ceiling: 280K → 300K' in transport.last()['text']


def test_values_are_checked_against_the_whole_card_on_entry(world):
    store, bridge, transport = models(world)
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '350K')  # above the 340K window
    assert 'must exceed the exception ceiling' in transport.last()['text']
    assert edit_record(store)['step'] == 'cell'
    tap(store, bridge, transport, 'Back')
    tap(store, bridge, transport, 'Native window: ?')
    reply(store, bridge, transport, '300K')  # below the 340K window already set
    assert 'enforced window 340K exceeds the native window 300K' in transport.last()['text']
    reply(store, bridge, transport, '350K')
    tap(store, bridge, transport, 'Window: 340K')
    reply(store, bridge, transport, '360K')
    assert 'enforced window 360K exceeds the native window 350K' in transport.last()['text']


def test_a_native_window_lets_the_ceiling_pass_the_catalog_window(world):
    store, bridge, transport = models(world)
    for label, value in (('Native window', '500K'), ('Window', '450K'), ('Ceiling', '400K')):
        tap(store, bridge, transport, cell(transport, label))
        reply(store, bridge, transport, value)
    tap(store, bridge, transport, 'Apply (3)')
    assert bridge.added[-1][-9:] == ['--strength', 'balanced', '--relative-cost', '0.5', '--max-output', '32000',
                                     '--native-window', '500K', '-y']
    assert edit_record(store)['status'] == 'done'
    profile = catalog_defaults(store.load(store.current()), {})['codex']['model_profiles']['gpt-6-luna']
    assert profile.context_limits.max_input_tokens == 450000  # the 340K catalog window no longer caps it


def test_apply_without_changes_and_cancel_call_nothing(world):
    store, bridge, transport = models(world)
    tap(store, bridge, transport, 'Ceiling: 280K')
    tap(store, bridge, transport, 'Keep 280K')
    assert 'Apply' in transport.buttons()  # keeping the value is no change
    tap(store, bridge, transport, 'Apply')
    assert edit_record(store)['status'] == 'kept' and not bridge.added


def test_a_second_tap_on_the_same_model_reuses_its_open_card(world):
    store, bridge, transport = models(world)
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')
    menu = next(m for m in transport.sent if 'Models' in m['text'])
    inbox(store, kind='callback', data={b['text']: b['callback_data'] for row in menu['markup']['inline_keyboard']
                                        for b in row}['gpt-6-luna'], message_id=menu['id'])
    model_asks.process(store, bridge, transport)
    assert '• Ceiling: 300K' in transport.buttons()  # the same ask, its change intact
    assert len([r for r in model_asks.records(store) if r['kind'] == 'edit']) == 1


def test_a_new_models_list_replaces_the_old_one(world):
    store, bridge, transport = models(world, pick=None)
    old = transport.buttons()['gpt-6-luna']
    inbox(store, kind='command', text='/models@SendToMeBot')
    model_asks.process(store, bridge, transport)
    assert len([r for r in model_asks.records(store) if r['kind'] == 'menu']) == 1
    inbox(store, kind='callback', data=old, message_id=1)
    assert model_asks.process(store, bridge, transport)['ignored'] == 1
    inbox(store, kind='command', text='/start')
    assert model_asks.process(store, bridge, transport)['ignored'] == 1


def test_a_failed_add_returns_to_the_card_with_modelctl_error(world):
    store, bridge, transport = models(world)
    bridge.add_ok = False
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')
    tap(store, bridge, transport, 'Apply (1)')
    assert 'exceeds the native window' in transport.last()['text'] and 'Apply (1)' in transport.buttons()
    bridge.add_ok = True
    tap(store, bridge, transport, 'Apply (1)')
    assert edit_record(store)['status'] == 'done'


def test_maestro_refusing_the_set_is_a_failure_not_done(world, monkeypatch):
    # Live 2026-10-08: modelctl held Haiku 5.5's new values, Maestro refused them, the ask said done.
    from maestro import model_detection
    store, bridge, transport = models(world)
    def refuse(*a, **k):
        raise PolicyError('unsafe or missing provider lifecycle row/native window')
    monkeypatch.setattr(model_detection, 'consume_set', refuse)
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')
    tap(store, bridge, transport, 'Apply (1)')
    record = edit_record(store)
    assert record['status'] == 'open' and record['step'] == 'pick' and record['registered']
    assert 'Maestro refused them' in transport.last()['text'] and 'Set Native window' in transport.last()['text']
    monkeypatch.undo()
    tap(store, bridge, transport, 'Apply (1)')  # no new modelctl add: only the publication is retried
    assert edit_record(store)['status'] == 'done' and len(bridge.added) == 1


def test_a_review_whose_set_maestro_refuses_fails_instead_of_done(world, monkeypatch):
    from maestro import model_detection
    store, bridge, transport = world
    same_class_switch(world)
    monkeypatch.setattr(model_detection, 'consume_set',
                        lambda *a, **k: (_ for _ in ()).throw(PolicyError('unsafe lifecycle')))
    tap(store, bridge, transport, 'Change')
    reply(store, bridge, transport, '200K-220K')
    while only(store)['step'] == 'cell':
        tap(store, bridge, transport, next(l for l in transport.buttons() if l.startswith('Keep') or l.endswith('(current)')))
    assert only(store)['step'] == 'failed' and 'Maestro refused them' in transport.last()['text']


def test_an_old_refused_set_for_the_model_does_not_fail_a_new_apply(world):
    # Review 2026-10-08: only the SET this apply made counts. The stale one is acknowledged as
    # superseded by the current owner row (follow-up fixed 2026-10-08).
    store, bridge, transport = models(world)
    bridge.events.append(dict(kind='MODEL_SET', event_id='stale-luna', provider='codex', model_id='gpt-6-luna',
                              family='luna', version=[6], enforced_window=999000, predecessor_id=None))
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')
    tap(store, bridge, transport, 'Apply (1)')
    assert edit_record(store)['status'] == 'done'
    assert 'stale-luna' in bridge.acked


def test_after_a_refused_apply_back_to_saved_still_runs_modelctl(world, monkeypatch):
    from maestro import model_detection
    store, bridge, transport = models(world)
    monkeypatch.setattr(model_detection, 'consume_set',
                        lambda *a, **k: (_ for _ in ()).throw(PolicyError('unsafe lifecycle')))
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')
    tap(store, bridge, transport, 'Apply (1)')
    monkeypatch.undo()
    tap(store, bridge, transport, '• Ceiling: 300K')
    tap(store, bridge, transport, 'Back to saved 280K')
    tap(store, bridge, transport, 'Apply')
    assert len(bridge.added) == 2 and '280K' in bridge.added[-1]
    assert '--strength' in bridge.added[-1]  # the profile is restated too
    assert edit_record(store)['status'] == 'done'


def test_a_reopened_card_rebases_on_what_modelctl_holds_now(world):
    store, bridge, transport = models(world)
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')
    bridge.rows[0]['lifecycle']['prepare'] = [110000, 130000]  # a CLI add meanwhile
    menu = next(m for m in transport.sent if 'Models' in m['text'])
    inbox(store, kind='callback', data={b['text']: b['callback_data'] for row in menu['markup']['inline_keyboard']
                                        for b in row}['gpt-6-luna'], message_id=menu['id'])
    model_asks.process(store, bridge, transport)
    assert {'Prepare handoff: 110K-130K', '• Ceiling: 300K'} <= set(transport.buttons())
    tap(store, bridge, transport, 'Apply (1)')
    assert '110K-130K' in bridge.added[-1]


# ── provider-sourced native window and max output (Dan, 2026-10-08) ─────────────────────

REPORTED = dict(native_window_tokens=1_000_000, max_output_tokens=128_000)


def haiku_rows(reported=REPORTED, profile=None):
    life = dict(prepare=[120000, 140000], fresh=[160000, 190000], ceiling=280000)
    old = dict(backend_id='claude', model_id='claude-haiku-4-5-20251001', family='haiku', version=[4, 5],
               aliases=['claude-haiku-4-5-20251001'], set=True, enforced_window=215000,
               lifecycle=dict(prepare=[90000, 100000], fresh=[120000, 140000], ceiling=180000))
    new = dict(backend_id='claude', model_id='claude-haiku-5-5', family='haiku', version=[5, 5],
               aliases=['claude-haiku-5-5'], set=True, enforced_window=335000, lifecycle=life,
               routing_profile=profile, **({'provider_limits': reported} if reported else {}))
    return [old, new]


def haiku_set(world, **kw):
    store, bridge, transport = world
    bridge.rows = haiku_rows(**kw)
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-haiku', provider='claude',
                              model_id='claude-haiku-5-5', family='haiku', version=[5, 5],
                              enforced_window=335000, predecessor_id='claude-haiku-4-5-20251001'))
    return consume(store, bridge, asks=True, transport=transport)


def haiku_limits(store):
    return catalog_defaults(store.load(store.current()), {})['claude']['model_profiles']['claude-haiku-5-5'].context_limits


def test_a_provider_native_window_lets_a_ceiling_pass_the_inherited_window(world):
    result = haiku_set(world)
    assert not result['errors']
    limits = haiku_limits(world[0])
    assert (limits.max_input_tokens, limits.max_output_tokens) == (335000, 128000)  # not Haiku 4.5's 200K / 32K


def test_without_provider_facts_the_verified_shipped_native_window_admits_the_ceiling(world):
    result = haiku_set(world, reported=None)
    assert not result['errors'] and 'set-haiku' in world[1].acked
    assert haiku_limits(world[0]).max_input_tokens == 335_000


def test_dans_profile_wins_over_the_provider_facts(world):
    profile = dict(strength='light', relative_cost=0.05, max_output_tokens=64000, native_window_tokens=300000)
    assert not haiku_set(world, profile=profile)['errors']
    limits = haiku_limits(world[0])
    assert (limits.max_input_tokens, limits.max_output_tokens) == (300000, 64000)


def test_invalid_provider_limits_are_refused(world):
    assert haiku_set(world, reported=dict(native_window_tokens='1M'))['errors']


def test_the_card_shows_provider_values_read_only_and_the_cell_as_an_override(world):
    store, bridge, transport = world
    haiku_set(world)
    inbox(store, kind='command', text='/models')
    model_asks.process(store, bridge, transport)
    tap(store, bridge, transport, 'claude-haiku-5-5')
    text = transport.last()['text']
    assert 'From the provider (read-only): native window 1000K, max output 128K.' in text
    assert 'Maestro currently allows' not in text
    tap(store, bridge, transport, 'Native window: 1000K (provider)')
    assert 'Reply only to override' in transport.last()['text']
    reply(store, bridge, transport, '300K')
    assert '• Native window: 300K (override)' in transport.buttons()
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '320K')  # the override, not the provider's 1M, bounds it
    assert 'exceeds the native window 300K' in transport.last()['text']


def test_two_sets_with_provider_facts_in_one_consume_both_apply(world):
    store, bridge, transport = world
    haiku_set(world, profile=dict(strength='light', relative_cost=0.05, max_output_tokens=64000))
    opus = dict(backend_id='claude', model_id='claude-opus-5-5', family='opus', version=[5, 5],
                aliases=['claude-opus-5-5'], set=True, enforced_window=420000, provider_limits=REPORTED,
                lifecycle=dict(prepare=[180000, 200000], fresh=[240000, 270000], ceiling=350000))
    bridge.rows = [*haiku_rows(profile=dict(strength='light', relative_cost=0.05, max_output_tokens=96000)), opus]
    for model, fam, version, window in (('claude-haiku-5-5', 'haiku', [5, 5], 335000),
                                        ('claude-opus-5-5', 'opus', [5, 5], 420000)):
        bridge.events.append(dict(kind='MODEL_SET', event_id=f'set2-{model}', provider='claude', model_id=model,
                                  family=fam, version=version, enforced_window=window, predecessor_id=None))
    assert not consume(store, bridge, asks=True, transport=transport)['errors']
    profiles = catalog_defaults(store.load(store.current()), {})['claude']['model_profiles']
    assert profiles['claude-haiku-5-5'].context_limits.max_output_tokens == 96000
    # The first SET stores every row's facts; the second must not read them as already adopted.
    assert profiles['claude-opus-5-5'].context_limits.max_output_tokens == 128000


def test_codex_native_window_from_the_provider_routes_the_enforced_window(world):
    store, bridge, transport = world
    bridge.rows = [dict(backend_id='codex', model_id='gpt-6-astra', family='astra', version=[6], aliases=['gpt-6-astra'],
                        set=True, enforced_window=540000, provider_limits=dict(native_window_tokens=872000),
                        lifecycle=dict(prepare=[220000, 250000], fresh=[300000, 350000], ceiling=450000))]
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-astra', provider='codex', model_id='gpt-6-astra',
                              family='astra', version=[6], enforced_window=540000, predecessor_id=None))
    # Astra's shipped profile is unverified; the provider window alone is enough, without an ask.
    assert not consume(store, bridge, asks=True, transport=transport)['errors'] and not transport.sent
    limits = catalog_defaults(store.load(store.current()), {})['codex']['model_profiles']['gpt-6-astra'].context_limits
    assert (limits.max_input_tokens, limits.max_output_tokens) == (540000, 32000)  # no Codex max output fact


def test_an_inherited_profile_takes_its_effective_ceiling_from_its_own_lifecycle(world):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    # Haiku 4.5's catalog ceiling is 120K; Haiku 5.5's own lifecycle starts fresh at 160K.
    assert haiku_limits(store).effective_context_ceiling == 160000
    bridge.rows[1]['lifecycle'] = dict(prepare=[120000, 140000], fresh=[170000, 190000], ceiling=280000)
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-haiku-2', provider='claude', model_id='claude-haiku-5-5',
                              family='haiku', version=[5, 5], enforced_window=335000, predecessor_id=None))
    assert not consume(store, bridge, asks=True, transport=transport)['errors']
    assert haiku_limits(store).effective_context_ceiling == 170000  # a lifecycle edit moves it too


# ── automatic reconcile: a Maestro rule change reaches consumed models (Dan, 2026-10-08) ──

def raise_ceiling_rule(monkeypatch):
    from maestro import model_detection
    rule = model_detection._admission_ceiling
    monkeypatch.setattr(model_detection, '_admission_ceiling', lambda target, cap: min(rule(target, cap) + 10000, cap))


def test_a_rule_change_republishes_a_consumed_model_once_without_a_new_set(world, monkeypatch):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    before = store.current()
    assert consume(store, bridge, asks=True, transport=transport)['reconciled'] == 0
    assert store.current() == before
    raise_ceiling_rule(monkeypatch)
    sent = len(transport.sent)
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and result['reconciled'] == 1 and result['switches_applied'] == 0
    assert store.current() != before and haiku_limits(store).effective_context_ceiling == 170000
    assert len(transport.sent) == sent  # Maestro's own rule change opens no ask
    after = store.current()
    again = consume(store, bridge, asks=True, transport=transport)
    assert (again['reconciled'], again['switches_applied'], again['errors']) == (0, 0, [])
    assert store.current() == after


def test_reconcile_without_asks_sends_no_switch_notice(world, monkeypatch):
    store, bridge, _ = world
    bridge.rows = haiku_rows()
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-haiku', provider='claude', model_id='claude-haiku-5-5',
                              family='haiku', version=[5, 5], enforced_window=335000, predecessor_id=None))
    notices = []
    consume(store, bridge, notify=notices.append)
    raise_ceiling_rule(monkeypatch)
    notices.clear()
    result = consume(store, bridge, notify=notices.append)
    assert result['reconciled'] == 1 and not notices


def test_reconcile_skips_unrouted_rows_and_opens_no_ask(world, monkeypatch):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    # A set class Maestro never routed (Dan declined it): no profile, no route, no ask.
    bridge.rows.append(dict(backend_id='claude', model_id='claude-comet-1', family='comet', version=[1],
                            aliases=['claude-comet-1'], set=True, enforced_window=300000,
                            lifecycle=dict(prepare=[120000, 140000], fresh=[160000, 190000], ceiling=280000)))
    sent = len(transport.sent)
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and len(transport.sent) == sent and not proposals(store)
    profiles = catalog_defaults(store.load(store.current()), {})['claude']['model_profiles']
    assert 'claude-comet-1' not in profiles  # stored as an owner fact at most, never routed
    before = store.current()
    assert consume(store, bridge, asks=True, transport=transport)['reconciled'] == 0
    assert store.current() == before


def test_reconcile_leaves_a_model_whose_edit_is_applying(world, monkeypatch):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    edit = model_asks._new('edit', 'claude:claude-haiku-5-5:1', 'claude', 'claude-haiku-5-5', 'haiku', 'apply', 0)
    edit['status'] = 'applying'
    model_asks._save(store, edit)
    raise_ceiling_rule(monkeypatch)
    before = store.current()
    result = consume(store, bridge, asks=False)
    assert result['reconciled'] == 0 and store.current() == before


def test_a_reconcile_interrupted_after_publication_settles_as_applied(world, monkeypatch):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    raise_ceiling_rule(monkeypatch)
    assert consume(store, bridge, asks=True, transport=transport)['reconciled'] == 1
    [record] = [r for r in switches(store) if r['event']['event_id'].startswith('reconcile:')]
    path = store.home / 'switches' / (record['switch_id'] + '.json')
    crashed = dict(record, status='prepared', planned_revision=record['applied_revision'], notification_pending=True)
    for key in ('applied_revision', 'notification'):
        crashed.pop(key)
    path.write_text(json.dumps(crashed))
    published = store.current()
    result = consume(store, bridge, asks=True, transport=transport)
    assert (result['reconciled'], result['errors']) == (0, []) and store.current() == published
    settled = json.loads(path.read_text())
    assert settled['status'] == 'applied' and not settled['notification_pending']


def test_a_set_older_than_the_current_owner_row_is_acknowledged_as_superseded(world):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-haiku-stale', provider='claude',
                              model_id='claude-haiku-5-5', family='haiku', version=[5, 5],
                              enforced_window=300000, predecessor_id=None))  # the row says 335K now
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and 'set-haiku-stale' in bridge.acked
    [record] = [r for r in switches(store) if r['event']['event_id'] == 'set-haiku-stale']
    assert record['status'] == 'superseded' and 'current owner row' in record['reason']


def test_a_set_without_any_current_owner_row_stays_pending(world):
    store, bridge, transport = world
    assert not haiku_set(world)['errors']
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-gone', provider='claude', model_id='claude-gone-1',
                              family='gone', version=[1], enforced_window=300000, predecessor_id=None))
    result = consume(store, bridge, asks=True, transport=transport)
    assert result['errors'] and 'set-gone' not in bridge.acked


def tap_in(store, bridge, transport, message, label):
    data = {b['text']: b['callback_data'] for row in message['markup']['inline_keyboard'] for b in row}[label]
    inbox(store, kind='callback', data=data, message_id=message['id'])
    return model_asks.process(store, bridge, transport)


def test_a_tap_on_a_model_whose_edit_is_applying_resends_its_saving_status(world, monkeypatch):
    store, bridge, transport = models(world)
    menu = transport.sent[0]
    record = edit_record(store)
    record.update(status='applying', step='apply')
    model_asks._save(store, record)
    monkeypatch.setattr(model_asks, '_apply', lambda *a: 0)  # still in modelctl add
    sent = len(transport.sent)
    tap_in(store, bridge, transport, menu, 'gpt-6-luna')
    assert len(transport.sent) == sent + 1 and transport.last()['text'].startswith('⏳ Saving')


def test_an_edit_idle_for_a_day_is_closed_with_a_visible_note(world):
    store, bridge, transport = models(world)
    tap(store, bridge, transport, 'Ceiling: 280K')
    reply(store, bridge, transport, '300K')  # an unsaved change
    last = edit_record(store)['last_sent']
    model_asks.flush(store, transport, bridge, now=last + model_asks.EDIT_IDLE_AFTER - 60)
    assert edit_record(store)['status'] == 'open'
    model_asks.flush(store, transport, bridge, now=last + model_asks.EDIT_IDLE_AFTER)
    record = edit_record(store)
    assert record['status'] == 'superseded' and model_asks.open_asks(store) == []
    text = transport.last()['text']
    assert text.startswith('⏭ Closed the edit of') and 'nothing was applied' in text and '/models' in text
    assert not transport.buttons()


def test_an_unseen_idle_edit_closes_silently(world):
    store, bridge, transport = models(world)
    record = edit_record(store)
    record.update(message_ids=[], current_message=None, last_sent=None)
    model_asks._save(store, record)
    sent = len(transport.sent)
    model_asks.flush(store, transport, bridge, now=record['created'] + model_asks.EDIT_IDLE_AFTER)
    assert edit_record(store)['status'] == 'superseded' and len(transport.sent) == sent


# ── access status on a detected model (Dan, 2026-10-08: hold on failed) ──

def test_a_new_class_ask_waits_while_modelctl_access_check_fails(world):
    store, bridge, transport = world
    bridge.access = lambda backend: {'gpt-7-comet': 'FAILED'}
    detected(world)
    assert not transport.sent and model_asks.records(store) == []
    assert [p['status'] for p in proposals(store)] == ['waiting_limits']
    bridge.access = lambda backend: {'gpt-7-comet': 'VERIFIED'}  # a later scan's smoke passed
    assert not consume(store, bridge, asks=True, transport=transport)['errors']
    assert transport.last()['text'].startswith('🆕 New codex model detected') and '⚠️' not in transport.last()['text']


def test_an_inconclusive_access_check_asks_with_a_warning(world):
    store, bridge, transport = world
    bridge.access = lambda backend: {'gpt-7-comet': 'INCONCLUSIVE'}
    detected(world)
    assert '⚠️ modelctl could not confirm access' in transport.last()['text']


def test_an_unreadable_access_status_still_asks(world):
    store, bridge, transport = world
    def broken(backend):
        raise PolicyError('modelctl access unavailable')
    bridge.access = broken
    detected(world)
    assert transport.last()['text'].startswith('🆕 New codex model detected')


def comet_rows():
    life = dict(prepare=[120000, 140000], fresh=[160000, 190000], ceiling=280000)
    return [dict(backend_id='codex', model_id=m, family='comet', version=v, aliases=[m], set=True,
                 enforced_window=340000, lifecycle=life) for m, v in (('gpt-7-comet', [7]), ('gpt-7.1-comet', [7, 1]))]


def test_a_successor_of_an_unrouted_profileless_predecessor_gets_the_profile_ask(world):
    store, bridge, transport = world
    bridge.rows = sol_rows() + comet_rows()  # Dan never routed gpt-7-comet
    bridge.events.append(dict(kind='MODEL_SET', event_id='set-comet-7.1', provider='codex', model_id='gpt-7.1-comet',
                              family='comet', version=[7, 1], enforced_window=340000, predecessor_id='gpt-7-comet'))
    result = consume(store, bridge, asks=True, transport=transport)
    assert not result['errors'] and 'set-comet-7.1' in bridge.acked
    assert 'has limits in modelctl but no routing profile' in transport.last()['text']
    assert 'takes over its roles' not in transport.last()['text']  # the predecessor has none
    tap(store, bridge, transport, 'Use')
    assert transport.last()['text'].startswith('Which roles should')


def test_reconcile_leaves_a_shipped_route_no_set_derived(world):
    store, bridge, transport = world
    # A fresh install routes the shipped gpt-6-astra; its owner row has no profile and no SET came.
    bridge.rows = [dict(backend_id='codex', model_id='gpt-6-astra', family='astra', version=[6], aliases=['gpt-6-astra'],
                        set=True, enforced_window=400000,
                        lifecycle=dict(prepare=[220000, 250000], fresh=[300000, 350000], ceiling=450000))]
    before = store.current()
    result = consume(store, bridge, asks=True, transport=transport)
    assert (result['reconciled'], result['errors']) == (0, []) and store.current() == before and not transport.sent
