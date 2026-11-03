"""A model that fails its access retest leaves routing until it passes (Dan, 2026-11-02)."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import model_access, model_commands
from maestro.model_detection import consume
from maestro.model_policy import PolicyError, PolicyStore, catalog_defaults, digest
from tests.test_model_asks import Bridge
from tests.test_model_asks_card import InPlace
from tests.test_model_sheet import HEADER, GOOD, Drive, MODEL, SheetBridge
from tests.test_model_asks import detected, tap

CODEX, LUNA = 'codex', 'gpt-6-luna'


class AccessBridge(Bridge):
    def __init__(self):
        super().__init__()
        self.checks, self.unreadable, self.stops, self.gone = {}, set(), {}, {}

    def access(self, backend):
        if backend in self.unreadable:
            raise PolicyError(f'{backend} unreadable')
        return self.checks.get(backend, {})

    def retired(self, backend):
        if backend in self.unreadable:
            raise PolicyError(f'{backend} unreadable')
        return dict(provider=backend, removed=sorted(self.gone.get(backend, ())), state_hash='h')

    def stopped(self, backend):
        if backend in self.unreadable:
            raise PolicyError(f'{backend} unreadable')
        return deepcopy(self.stops.get(backend, {}))

    def report(self, model, status, backend=CODEX):
        self.checks.setdefault(backend, {})[model] = status


class Telegram:
    def __init__(self):
        self.texts, self.down = [], False

    def send(self, text, markup=None):
        if self.down:
            raise PolicyError('telegram sendMessage unreachable')
        self.texts.append(text)
        return len(self.texts)


def fixture_policy():
    return json.loads((Path(__file__).parent / 'fixtures/model_policy/schema2.json').read_text())


@pytest.fixture
def world(tmp_path):
    store = PolicyStore(tmp_path / 'models')
    store.publish(fixture_policy(), expected=None)
    return store, AccessBridge(), Telegram()


def run(world):
    return model_access.reconcile(*world)


def routes(store, model=LUNA, backend=CODEX):
    return [r for r in store.snapshot()[1]['routes'] if (r['backend_id'], r['model_id']) == (backend, model)]


def holds(store):
    path = store.home / model_access.HOLD_FILE
    return json.loads(path.read_text())['held'] if path.exists() else {}


def key(model=LUNA, backend=CODEX):
    return f'{backend}:{model}'


def only_model_serving_planner(store, model=LUNA):
    """Make `model` the one route able to serve the planner role."""
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    for route in changed['routes']:
        if route['model_id'] == model:
            route.update(intelligence=99)
    changed['roles']['planner'].update(min_intelligence=99)
    changed['source'] = dict(changed['source'], previous_policy=digest(policy))
    store.publish(changed, expected=revision)


# ── a model the provider removed (Dan, 2026-11-03) ──

def test_a_removed_model_leaves_routing_by_itself_and_dan_hears(world):
    store, bridge, telegram = world
    before = routes(store)
    bridge.report(LUNA, 'VERIFIED')  # its last test passed, and it has still gone
    bridge.gone[CODEX] = {LUNA}
    outcome = run(world)
    policy = store.snapshot()[1]
    assert outcome['taken_out'] == [key()] and outcome['errors'] == []
    assert routes(store) == [] and policy['retired'] == []
    assert [row for _, row in holds(store)[key()]['routes']] == before
    assert len(telegram.texts) == 1 and LUNA in telegram.texts[0]
    revision = store.current()
    assert run(world)['taken_out'] == [] and store.current() == revision  # stays out, nothing republished
    assert len(telegram.texts) == 1


def test_a_removed_model_listed_again_returns_with_its_routes(world):
    store, bridge, telegram = world
    original = store.snapshot()[1]['routes']
    bridge.report(LUNA, 'INCONCLUSIVE')  # not a failed test, so it does not wait for a pass
    bridge.gone[CODEX] = {LUNA}
    run(world)
    bridge.gone[CODEX] = set()
    outcome = run(world)
    assert outcome['restored'] == [key()]
    assert store.snapshot()[1]['routes'] == original and holds(store) == {}
    assert len(telegram.texts) == 2 and LUNA in telegram.texts[1]


def test_a_removed_model_listed_again_stays_out_if_its_test_failed(world):
    store, bridge, _ = world
    bridge.gone[CODEX] = {LUNA}
    run(world)
    bridge.gone[CODEX] = set()
    bridge.report(LUNA, 'FAILED')
    assert run(world)['restored'] == []
    assert routes(store) == [] and key() in holds(store)


def test_a_failed_model_stays_out_until_its_test_passes_even_if_it_was_also_removed(world):
    store, bridge, _ = world
    bridge.report(LUNA, 'FAILED')
    run(world)
    bridge.gone[CODEX] = {LUNA}
    run(world)
    bridge.gone[CODEX] = set()
    assert run(world)['restored'] == []
    assert routes(store) == []
    bridge.report(LUNA, 'VERIFIED')
    assert run(world)['restored'] == [key()]


def test_a_removed_class_takes_out_a_route_under_its_native_alias_and_stays_out(world):
    store, bridge, _ = world
    alias, owner_id, backend = 'gemini-3.8-flash-high', 'gemini-3-8-flash', 'antigravity_cli'
    row = dict(backend_id=backend, model_id=owner_id, family='gemini-flash', version=[3, 8],
               aliases=[owner_id, alias], set=True, enforced_window=88000)

    def set_facts(rows):
        revision, policy = store.snapshot()
        changed = deepcopy(policy)
        changed['source'] = dict(changed['source'], class_metadata=rows, previous_policy=digest(policy))
        store.publish(changed, expected=revision)

    set_facts([row])
    bridge.gone[backend] = {owner_id}
    assert run(world)['taken_out'] == [key(alias, backend)]
    set_facts([])  # with the policy no longer naming the alias, the hold alone must keep it out
    assert run(world)['restored'] == []
    bridge.gone[backend] = set()
    assert run(world)['restored'] == [key(alias, backend)]


def test_a_removed_model_a_role_needs_stays_routed_and_dan_hears_once(world):
    store, bridge, telegram = world
    only_model_serving_planner(store)
    before = routes(store)
    bridge.gone[CODEX] = {LUNA}
    outcome = run(world)
    assert outcome['taken_out'] == [] and 'planner' in outcome['refused'][0]['reason']
    assert routes(store) == before and holds(store) == {}
    run(world)
    assert len(telegram.texts) == 1 and 'removed' in telegram.texts[0] and 'failed' not in telegram.texts[0]


def test_a_removal_notice_that_fails_is_sent_on_a_later_run_and_only_once(world):
    store, bridge, telegram = world
    bridge.gone[CODEX] = {LUNA}
    telegram.down = True
    outcome = run(world)
    assert outcome['taken_out'] == [key()] and 'notice not sent' in outcome['errors'][0]
    telegram.down = False
    run(world)
    run(world)
    assert len(telegram.texts) == 1 and LUNA in telegram.texts[0]


def test_a_removal_during_a_retry_tap_is_told_on_the_next_run(world):
    store, bridge, telegram = world
    bridge.gone[CODEX] = {LUNA}
    model_access.reconcile(store, bridge, None)
    run(world)
    assert len(telegram.texts) == 1 and LUNA in telegram.texts[0]


def test_a_returned_alias_stays_out_if_its_class_test_failed(world):
    store, bridge, _ = world
    alias, owner_id, backend = 'gemini-3.8-flash-high', 'gemini-3-8-flash', 'antigravity_cli'
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    changed['source'].update(previous_policy=digest(policy), class_metadata=[dict(
        backend_id=backend, model_id=owner_id, family='gemini-flash', version=[3, 8],
        aliases=[owner_id, alias], set=True, enforced_window=88000)])
    store.publish(changed, expected=revision)
    bridge.gone[backend] = {owner_id}
    run(world)
    bridge.gone[backend] = set()
    bridge.report(owner_id, 'FAILED', backend)
    assert run(world)['restored'] == []
    assert routes(store, alias, backend) == []


def test_retiring_a_held_model_is_final_and_ends_the_hold_quietly(world):
    from maestro.model_lifecycle import apply_policy_preview, preview_retire
    store, bridge, telegram = world
    bridge.gone[CODEX] = {LUNA}
    run(world)
    preview = preview_retire(store, bridge, CODEX)
    assert preview['retire'] == [[CODEX, LUNA]]
    apply_policy_preview(preview, store, confirmed=True, bridge=bridge)
    assert [CODEX, LUNA] in store.snapshot()[1]['retired']
    told = len(telegram.texts)
    bridge.gone[CODEX] = set()
    outcome = run(world)
    assert outcome['restored'] == [] and outcome['refused'] == [] and holds(store) == {}
    assert routes(store) == [] and len(telegram.texts) == told


def test_an_unreadable_removal_state_changes_nothing(world):
    store, bridge, _ = world
    revision = store.current()
    bridge.gone[CODEX] = {LUNA}
    bridge.unreadable.add(CODEX)
    outcome = run(world)
    assert outcome['taken_out'] == [] and store.current() == revision
    assert any(CODEX in error for error in outcome['errors'])


# ── take out and put back ──

def test_a_failure_takes_every_route_out_and_holds_them(world):
    store, bridge, telegram = world
    before = routes(store)
    assert len(before) > 1
    others = len(store.snapshot()[1]['routes']) - len(before)
    bridge.report(LUNA, 'FAILED')
    outcome = run(world)
    assert outcome['taken_out'] == [key()] and outcome['errors'] == []
    policy = store.snapshot()[1]
    assert routes(store) == [] and len(policy['routes']) == others
    assert [row for _, row in holds(store)[key()]['routes']] == before
    assert policy['retired'] == []  # a retired route could never come back
    assert policy['source']['kind'] == 'access_hold'
    assert telegram.texts == []  # modelctl already told Dan about the failure


def test_taking_out_keeps_the_model_facts_a_restore_and_reconcile_rely_on(world):
    store, bridge, _ = world
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    changed['source'].update(class_metadata=[], inherited_profiles=[], previous_policy=digest(policy))
    store.publish(changed, expected=revision)
    bridge.report(LUNA, 'FAILED')
    run(world)
    source = store.snapshot()[1]['source']
    assert source['class_metadata'] == [] and source['inherited_profiles'] == []
    assert source['previous_policy'] == digest(changed)


def test_a_pass_restores_the_same_routes_in_the_same_order(world):
    store, bridge, telegram = world
    original = store.snapshot()[1]['routes']
    bridge.report(LUNA, 'FAILED')
    run(world)
    bridge.report(LUNA, 'VERIFIED')
    outcome = run(world)
    policy = store.snapshot()[1]
    assert outcome['restored'] == [key()]
    assert policy['routes'] == original and policy['source']['kind'] == 'access_restore'
    assert holds(store) == {} and telegram.texts == []


def test_a_second_pass_after_the_restore_publishes_nothing(world):
    store, bridge, _ = world
    bridge.report(LUNA, 'FAILED')
    run(world)
    bridge.report(LUNA, 'VERIFIED')
    run(world)
    current = store.current()
    run(world)
    assert store.current() == current


def test_a_routed_model_that_passes_or_is_unknown_stays_routed(world):
    store, bridge, _ = world
    current = store.current()
    bridge.report(LUNA, 'VERIFIED')
    bridge.report('gpt-6-astra', 'INCONCLUSIVE')
    assert run(world) == dict(taken_out=[], restored=[], refused=[], errors=[])
    assert store.current() == current and holds(store) == {}


def test_an_inconclusive_answer_neither_restores_nor_drops_a_held_model(world):
    store, bridge, _ = world
    bridge.report(LUNA, 'FAILED')
    run(world)
    bridge.report(LUNA, 'INCONCLUSIVE')
    current = store.current()
    run(world)
    assert store.current() == current and routes(store) == [] and key() in holds(store)


def test_a_failed_model_with_no_routes_is_left_alone(world):
    store, bridge, _ = world
    bridge.report('gpt-9-unrouted', 'FAILED')
    current = store.current()
    assert run(world)['taken_out'] == []
    assert store.current() == current and holds(store) == {}


def test_a_missed_run_corrects_itself_on_the_next_one(world):
    store, bridge, _ = world
    bridge.report(LUNA, 'FAILED')
    run(world)
    run(world)  # nothing more to do while it keeps failing
    bridge.report(LUNA, 'VERIFIED')
    run(world)
    assert routes(store) and holds(store) == {}


def test_each_backend_is_read_on_its_own(world):
    store, bridge, _ = world
    bridge.unreadable.add('claude')
    bridge.report(LUNA, 'FAILED')
    outcome = run(world)
    assert outcome['taken_out'] == [key()] and any('claude' in e for e in outcome['errors'])


# ── a routed model re-added while held ──

def test_a_restore_is_skipped_when_routes_already_exist(world):
    store, bridge, _ = world
    original = routes(store)
    bridge.report(LUNA, 'FAILED')
    run(world)
    revision, policy = store.snapshot()
    readded = deepcopy(policy)
    readded['routes'] += [dict(original[0], intelligence=44)]  # Dan's Pull, with his new score
    store.publish(readded, expected=revision)
    bridge.report(LUNA, 'VERIFIED')
    current = store.current()
    outcome = run(world)
    assert outcome['restored'] == [] and store.current() == current
    assert holds(store) == {} and [r['intelligence'] for r in routes(store)] == [44]


def test_a_model_readded_while_still_failing_goes_out_again_holding_its_new_routes(world):
    store, bridge, _ = world
    original = routes(store)
    bridge.report(LUNA, 'FAILED')
    run(world)
    revision, policy = store.snapshot()
    readded = deepcopy(policy)
    readded['routes'] += [dict(original[0], intelligence=44)]
    store.publish(readded, expected=revision)
    run(world)
    assert routes(store) == []
    assert [row['intelligence'] for _, row in holds(store)[key()]['routes']] == [44]


# ── refused publications ──

def test_a_take_out_that_would_strand_a_role_alerts_once_and_keeps_the_model(world):
    store, bridge, telegram = world
    only_model_serving_planner(store)
    current = store.current()
    bridge.report(LUNA, 'FAILED')
    outcome = run(world)
    assert outcome['refused'][0]['model'] == key() and 'planner' in outcome['refused'][0]['reason']
    assert store.current() == current and routes(store) and holds(store) == {}
    assert len(telegram.texts) == 1
    assert LUNA in telegram.texts[0] and 'planner' in telegram.texts[0] and 'stays routed' in telegram.texts[0]
    run(world)
    run(world)
    assert len(telegram.texts) == 1  # not every hour


def test_a_refusal_alerts_again_only_after_the_model_passed_in_between(world):
    store, bridge, telegram = world
    only_model_serving_planner(store)
    bridge.report(LUNA, 'FAILED')
    run(world)
    bridge.report(LUNA, 'VERIFIED')
    run(world)
    bridge.report(LUNA, 'FAILED')
    run(world)
    assert len(telegram.texts) == 2


def test_taking_out_a_role_already_unserved_is_not_refused_on_its_account(world):
    store, bridge, _ = world
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    changed['roles']['planner'].update(min_intelligence=100)  # nothing serves it before or after
    changed['source'] = dict(changed['source'], previous_policy=digest(policy))
    store.publish(changed, expected=revision)
    bridge.report(LUNA, 'FAILED')
    assert run(world)['taken_out'] == [key()]


def test_an_effort_floor_counts_when_deciding_a_role_is_still_served(world):
    store, bridge, _ = world
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    # Other models clear the intelligence bar; only luna reaches the role's effort floor.
    changed['roles']['planner'].update(min_intelligence=1, effort='max', effort_rule='minimum')
    changed['routes'] = [r for r in changed['routes'] if r['effort'] != 'max']
    changed['routes'].append(dict(routes(store)[0], effort='max'))
    changed['source'] = dict(changed['source'], previous_policy=digest(policy))
    store.publish(changed, expected=revision)
    bridge.report(LUNA, 'FAILED')
    assert run(world)['refused'][0]['model'] == key()


def test_taking_out_the_last_route_of_the_policy_is_refused(tmp_path):
    store = PolicyStore(tmp_path / 'models')
    policy = fixture_policy()
    policy['routes'] = [r for r in policy['routes'] if r['model_id'] == LUNA]
    policy['roles'] = {}
    store.publish(policy, expected=None)
    bridge, telegram = AccessBridge(), Telegram()
    bridge.report(LUNA, 'FAILED')
    outcome = model_access.reconcile(store, bridge, telegram)
    assert outcome['refused'] and routes(store) and len(telegram.texts) == 1


def test_a_refusal_alert_that_could_not_be_sent_is_tried_again(world):
    store, bridge, telegram = world
    only_model_serving_planner(store)
    bridge.report(LUNA, 'FAILED')
    telegram.down = True
    assert any('telegram' in e for e in run(world)['errors'])
    telegram.down = False
    run(world)
    run(world)
    assert len(telegram.texts) == 1


def test_a_refusal_with_no_transport_waits_for_one(world):
    store, bridge, telegram = world
    only_model_serving_planner(store)
    bridge.report(LUNA, 'FAILED')
    outcome = model_access.reconcile(store, bridge, None)
    assert outcome['refused'] and routes(store)
    run(world)
    assert len(telegram.texts) == 1


def test_a_restore_that_is_refused_alerts_once_and_stays_held(world, monkeypatch):
    store, bridge, telegram = world
    bridge.report(LUNA, 'FAILED')
    run(world)

    def refused(policy, hold):
        raise PolicyError('no profile')
    monkeypatch.setattr(model_access, 'with_routes', refused)
    bridge.report(LUNA, 'VERIFIED')
    current = store.current()
    run(world)
    run(world)
    assert store.current() == current and key() in holds(store)
    assert len(telegram.texts) == 1 and 'stays out of routing' in telegram.texts[0]


def test_a_publication_that_lost_a_race_retries_without_alerting(world, monkeypatch):
    store, bridge, telegram = world
    bridge.report(LUNA, 'FAILED')
    real = model_access.publish_and_adopt

    def lost_the_race(preview, store_, projects):
        revision, policy = store_.snapshot()
        moved = deepcopy(policy)
        moved['source'] = dict(moved['source'], previous_policy=digest(policy))
        store_.publish(moved, expected=revision)
        return real(preview, store_, projects)

    monkeypatch.setattr(model_access, 'publish_and_adopt', lost_the_race)
    outcome = run(world)
    assert outcome['errors'] and not outcome['refused'] and telegram.texts == []
    assert routes(store) and key() in holds(store)  # saved before publishing, in case it had succeeded
    monkeypatch.setattr(model_access, 'publish_and_adopt', real)
    assert run(world)['taken_out'] == [key()]
    assert routes(store) == []


def test_a_crash_after_a_restore_published_only_drops_the_entry(world, monkeypatch):
    store, bridge, _ = world
    bridge.report(LUNA, 'FAILED')
    run(world)
    bridge.report(LUNA, 'VERIFIED')
    real = model_access.publish_and_adopt

    def crash_after_publishing(preview, store_, projects):
        real(preview, store_, projects)
        raise OSError('killed')

    monkeypatch.setattr(model_access, 'publish_and_adopt', crash_after_publishing)
    assert run(world)['errors']
    monkeypatch.setattr(model_access, 'publish_and_adopt', real)
    current = store.current()
    run(world)
    assert store.current() == current and holds(store) == {} and routes(store)


# ── never fails consume ──

def test_an_unreadable_hold_file_is_reported_and_changes_nothing(world):
    store, bridge, _ = world
    (store.home / model_access.HOLD_FILE).write_text('{not json')
    bridge.report(LUNA, 'FAILED')
    current = store.current()
    outcome = run(world)
    assert outcome['errors'] and store.current() == current


def test_another_run_in_progress_is_skipped(world):
    import fcntl
    store, bridge, _ = world
    bridge.report(LUNA, 'FAILED')
    with open(store.home / model_access.LOCK_FILE, 'a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        outcome = run(world)
    assert outcome['busy'] is True and routes(store)


def consume_verb(tmp_path, monkeypatch, access_outcome):
    store = PolicyStore(tmp_path / 'models')
    store.publish(fixture_policy(), expected=None)
    order = []
    monkeypatch.setattr(model_commands, 'PolicyStore', lambda: store)
    monkeypatch.setattr(model_commands, 'ModelctlBridge', AccessBridge)
    monkeypatch.setattr(model_commands, 'consume', lambda *a, **k: order.append('consume') or dict(errors=[]))

    def access(*args):
        order.append('access')
        return access_outcome

    monkeypatch.setattr(model_commands.model_access, 'reconcile', access)
    monkeypatch.setattr(model_commands.model_sheet, 'sync_if_enabled', lambda *a: order.append('sheet'))
    return order


def test_the_consume_verb_runs_the_step_before_the_drive_table(tmp_path, monkeypatch, capsys):
    order = consume_verb(tmp_path, monkeypatch, dict(taken_out=[key()], errors=[]))
    assert model_commands.run(SimpleNamespace(verb='consume')) == 0
    # Consume again: the events a removed model's routes failed can go through now.
    assert order == ['consume', 'access', 'consume', 'sheet']
    assert json.loads(capsys.readouterr().out)['access']['taken_out'] == [key()]


def test_the_consume_verb_consumes_once_when_nothing_left_routing(tmp_path, monkeypatch, capsys):
    order = consume_verb(tmp_path, monkeypatch, dict(taken_out=[], errors=[]))
    assert model_commands.run(SimpleNamespace(verb='consume')) == 0
    assert order == ['consume', 'access', 'sheet']


def test_a_second_consume_decides_the_exit_code(tmp_path, monkeypatch, capsys):
    consume_verb(tmp_path, monkeypatch, dict(taken_out=[key()], errors=[]))
    outcomes = iter([dict(errors=['profile unavailable']), dict(errors=[])])
    monkeypatch.setattr(model_commands, 'consume', lambda *a, **k: next(outcomes))
    assert model_commands.run(SimpleNamespace(verb='consume')) == 0
    assert json.loads(capsys.readouterr().out)['first_pass']['errors'] == ['profile unavailable']


def test_a_failure_in_the_step_is_reported_but_does_not_fail_consume(tmp_path, monkeypatch, capsys):
    consume_verb(tmp_path, monkeypatch, dict(errors=['boom']))
    assert model_commands.run(SimpleNamespace(verb='consume')) == 0
    assert json.loads(capsys.readouterr().out)['access']['errors'] == ['boom']


# ── reconcile leaves a taken-out model alone ──

class RoutedBridge(SheetBridge):
    def __init__(self):
        super().__init__()
        self.checks, self.stops, self.gone = {}, {}, set()

    def access(self, backend):
        return self.checks.get(backend, {})

    def retired(self, backend):
        return dict(provider=backend, removed=sorted(self.gone) if backend == CODEX else [], state_hash='h')

    def stopped(self, backend):
        return deepcopy(self.stops.get(backend, {}))


@pytest.fixture
def routed(tmp_path, monkeypatch):
    """A model Maestro consumed from an owner SET: the only kind `_reconcile` re-derives."""
    from maestro import model_sheet
    store, bridge, transport = PolicyStore(tmp_path / 'models'), RoutedBridge(), InPlace()
    store.publish(fixture_policy(), expected=None)
    detected((store, bridge, transport))
    drive = Drive(HEADER + GOOD)
    monkeypatch.setattr(model_sheet, '_download', drive.download)
    monkeypatch.setattr(model_sheet, '_upload', drive.upload)
    tap(store, bridge, transport, 'Pull from Drive')
    assert routes(store, MODEL)
    assert MODEL in {p['model_id'] for p in store.snapshot()[1]['source']['inherited_profiles']}
    return store, bridge, transport


def raise_ceiling(monkeypatch):
    from maestro import model_detection
    rule = model_detection._admission_ceiling
    monkeypatch.setattr(model_detection, '_admission_ceiling', lambda target, cap: min(rule(target, cap) + 10000, cap))


def test_reconcile_republishes_a_routed_model_after_a_rule_change(routed, monkeypatch):
    store, bridge, transport = routed
    raise_ceiling(monkeypatch)
    assert consume(store, bridge, asks=True, transport=transport)['reconciled'] == 1


def test_reconcile_leaves_a_taken_out_model_out(routed, monkeypatch):
    store, bridge, transport = routed
    bridge.checks[CODEX] = {MODEL: 'FAILED'}
    assert model_access.reconcile(store, bridge, Telegram())['taken_out'] == [key(MODEL)]
    current = store.current()
    raise_ceiling(monkeypatch)
    result = consume(store, bridge, asks=True, transport=transport)
    assert result['reconciled'] == 0 and not result['errors']
    assert store.current() == current and routes(store, MODEL) == []
    bridge.checks[CODEX] = {MODEL: 'VERIFIED'}
    assert model_access.reconcile(store, bridge, Telegram())['restored'] == [key(MODEL)]
    assert routes(store, MODEL)


def drop_owner_facts(store, model):
    """What another model's SET does to a model that is out: its profile goes and its row reads unset."""
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    source = changed['source']
    source['inherited_profiles'] = [p for p in source['inherited_profiles'] if p['model_id'] != model]
    for row in source['class_metadata']:
        if row['model_id'] == model:
            row['set'] = False
    source['previous_policy'] = digest(policy)
    store.publish(changed, expected=revision)


def test_a_returning_model_gets_back_the_profile_a_later_set_dropped(routed):
    store, bridge, transport = routed
    source = store.snapshot()[1]['source']
    profile = [p for p in source['inherited_profiles'] if p['model_id'] == MODEL]
    row = [m for m in source['class_metadata'] if m['model_id'] == MODEL]
    assert profile and row and row[0]['set'] is True
    bridge.gone = {MODEL}
    assert model_access.reconcile(store, bridge, Telegram())['taken_out'] == [key(MODEL)]
    drop_owner_facts(store, MODEL)
    bridge.gone = set()
    assert model_access.reconcile(store, bridge, Telegram())['restored'] == [key(MODEL)]
    source = store.snapshot()[1]['source']
    assert [p for p in source['inherited_profiles'] if p['model_id'] == MODEL] == profile
    assert [m for m in source['class_metadata'] if m['model_id'] == MODEL] == row
    catalog_defaults(store.snapshot()[1], {})  # what fails consume for a route with no profile


def test_a_hold_taken_after_the_facts_were_lost_does_not_restore_a_route_with_no_profile(routed):
    store, bridge, transport = routed
    drop_owner_facts(store, MODEL)  # a SET ran before the hold, or the model stayed routed for a role
    with pytest.raises(PolicyError, match='profile unavailable'):
        catalog_defaults(store.snapshot()[1], {})
    bridge.gone = {MODEL}
    telegram = Telegram()
    assert model_access.reconcile(store, bridge, telegram)['taken_out'] == [key(MODEL)]
    catalog_defaults(store.snapshot()[1], {})
    bridge.gone = set()
    outcome = model_access.reconcile(store, bridge, telegram)
    assert outcome['restored'] == [] and 'profile unavailable' in outcome['refused'][0]['reason']
    assert routes(store, MODEL) == [] and any('listed again' in text for text in telegram.texts)


def test_a_returning_model_keeps_facts_newer_than_its_hold(routed):
    store, bridge, _ = routed
    bridge.gone = {MODEL}
    model_access.reconcile(store, bridge, Telegram())
    revision, policy = store.snapshot()
    changed = deepcopy(policy)
    newer = next(m for m in changed['source']['class_metadata'] if m['model_id'] == MODEL)
    newer['enforced_window'] += 1000
    changed['source']['previous_policy'] = digest(policy)
    store.publish(changed, expected=revision)
    bridge.gone = set()
    model_access.reconcile(store, bridge, Telegram())
    assert next(m for m in store.snapshot()[1]['source']['class_metadata'] if m['model_id'] == MODEL) == newer
