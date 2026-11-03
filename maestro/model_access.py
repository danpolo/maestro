"""A model that fails modelctl's access test leaves routing until it passes (Dan, 2026-11-02).

Each `maestro models consume` compares modelctl's recorded access with the current routes, so a
missed run corrects itself on the next one. Removal is by route rows, never `retire_routes`: a
retired route cannot become eligible again.

Any failure other than a rate limit stops that model's automatic tests in modelctl. This module
alerts Dan once per stop with an `access_failure` card: Retry runs one test, Remove model drops the
model's routes, its saved routes and its tests, and the removed card keeps a Pull from Drive button
because nothing else offers one. modelctl still tells Dan about a pass. A rate limit stays silent.

A model modelctl marks as removed by its provider leaves routing the same way (Dan, 2026-11-03),
whatever its last test said, and Dan hears. It returns when it is listed again, unless a test has
since failed. `maestro models retire` remains the permanent, deliberate step.
"""
from __future__ import annotations

import fcntl
import json
import time
from copy import deepcopy

from maestro import model_asks as asks
from maestro.backends.catalog import _effort_rank
from maestro.model_inventory import registry
from maestro.model_lifecycle import preview_base, publish_and_adopt
from maestro.model_policy import (PolicyError, atomic_write, canonical, catalog_defaults, digest, role_references,
                                  validate)

# Owner capability: modelctl leaves the stopped-test notice to these cards.
ACCESS_ASKS_VERSION = 1

HOLD_FILE = 'access-holds.json'
LOCK_FILE = 'access-holds.lock'
FAILED, VERIFIED = 'FAILED', 'VERIFIED'
CARD = 'access_failure'
_REASON_CHARS = 700  # fits a Telegram message even if every character needs HTML escaping


def _source(policy: dict, kind: str) -> dict:
    source = {'kind': kind, 'previous_policy': digest(policy), 'bootstrap': policy['source']}
    for key in ('class_metadata', 'inherited_profiles'):
        if key in policy['source']:
            source[key] = deepcopy(policy['source'][key])
    return source


def _unserved(policy: dict) -> set[str]:
    """Schema 2 roles no route can serve, by the router's intelligence and effort-floor rules."""
    if policy['schema'] != 2:
        return set()
    def serves(route, need):
        return route['intelligence'] >= need['min_intelligence'] and (
            need['effort_rule'] != 'minimum' or _effort_rank(route['effort']) >= _effort_rank(need['effort']))
    return {role for role, need in policy['roles'].items()
            if not any(serves(route, need) for route in policy['routes'])}


def without_routes(policy: dict, pair: tuple[str, str], kind: str = 'access_hold') -> dict:
    """`policy` with this model's routes gone. `validate` alone would strand a role, so check that too."""
    validate(policy)
    if roles := role_references(policy, pair):
        raise PolicyError(f'{", ".join(roles)} prefer it; replace it first')
    result = deepcopy(policy)
    result['routes'] = [r for r in policy['routes'] if (r['backend_id'], r['model_id']) != pair]
    result['source'] = _source(policy, kind)
    validate(result)
    if stranded := sorted(_unserved(result) - _unserved(policy)):
        raise PolicyError(f'no other route can serve {", ".join(stranded)}')
    return result


def _owner_facts(policy: dict, pair: tuple[str, str]) -> dict:
    """What a route needs beside its rows. A later MODEL_SET re-derives these from modelctl, which
    leaves an unrouted or removed model without them, so the hold keeps a copy."""
    source, (backend, model) = policy['source'], pair
    return dict(profiles=[p for p in source.get('inherited_profiles', [])
                          if (p['backend_id'], p['model_id']) == pair],
                facts=[m for m in source.get('class_metadata', [])
                       if m['backend_id'] == backend and model in (m['model_id'], *m.get('aliases', []))])


def with_routes(policy: dict, hold: dict) -> dict:
    """`policy` with a hold's routes back at their old positions, and any owner facts lost meanwhile.
    Facts modelctl set again while the model was out are newer than the hold, so they stay."""
    validate(policy)
    result = deepcopy(policy)
    for index, route in sorted(hold['routes'], key=lambda item: item[0]):
        result['routes'].insert(index, deepcopy(route))
    source = result['source'] = _source(policy, 'access_restore')
    for record in hold.get('profiles', []):
        rows = source.setdefault('inherited_profiles', [])
        if not any((r['backend_id'], r['model_id']) == (record['backend_id'], record['model_id']) for r in rows):
            rows.append(deepcopy(record))
    for record in hold.get('facts', []):
        rows = source.setdefault('class_metadata', [])
        same = [r for r in rows if (r['backend_id'], r['model_id']) == (record['backend_id'], record['model_id'])]
        if same and same[0].get('set') is not False:
            continue
        rows[:] = [r for r in rows if r not in same] + [deepcopy(record)]
    validate(result)
    catalog_defaults(result, {})  # a route with no profile would fail every later consume
    return result


def _load(store) -> dict:
    path = store.home / HOLD_FILE
    if not path.exists():
        return dict(held={}, alerted={})
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PolicyError(f'unreadable {HOLD_FILE}; repair it before retrying') from exc


def _save(store, holds: dict) -> None:
    atomic_write(store.home / HOLD_FILE, canonical(holds))


def _refused(store, holds, key, text, reason, transport, result) -> None:
    """Dan hears once per refusal; a send that fails is tried on the next run."""
    result['refused'].append(dict(model=key, reason=reason))
    if key in holds['alerted'] or transport is None:
        return
    try:
        transport.send(f'⚠️ {asks._e(text)}')
    except (PolicyError, OSError) as exc:
        result['errors'].append(f'{key}: alert not sent: {exc}')
        return
    holds['alerted'][key] = True
    _save(store, holds)


def _publish(store, holds, key, build, describe, transport, result, *, before=None) -> bool:
    """Publish `build(policy)`. A refused change alerts; a lost race only waits for the next run."""
    revision, policy = store.snapshot()
    try:
        changed = build(policy)
        preview = dict(preview_base(store, revision), policy=changed)
    except PolicyError as exc:
        _refused(store, holds, key, describe(exc), str(exc), transport, result)
        return False
    try:
        if before is not None:
            before()
        publish_and_adopt(preview, store, registry(store))
    except (PolicyError, OSError) as exc:
        if store.current() != revision:
            result['errors'].append(f'{key}: {exc}; next run retries')
        else:
            _refused(store, holds, key, describe(exc), str(exc), transport, result)
        return False
    return True


def _tell(transport, key, text, result) -> bool:
    """A notice about something already done, so a failed send never undoes it."""
    if transport is None:
        return False
    try:
        transport.send(f'ℹ️ {asks._e(text)}')
    except (PolicyError, OSError) as exc:
        result['errors'].append(f'{key}: notice not sent: {exc}')
        return False
    return True


def _settle(store, holds, backend, model, status, transport, result, removed=()) -> None:
    key, pair = f'{backend}:{model}', (backend, model)
    rows = [(i, r) for i, r in enumerate(store.snapshot()[1]['routes']) if (r['backend_id'], r['model_id']) == pair]
    if status == FAILED and rows:
        def hold():  # saved first, so a crash before publication loses nothing
            holds['held'][key] = dict(backend=backend, model_id=model, routes=[list(row) for row in rows],
                                      **_owner_facts(store.snapshot()[1], pair))
            if removed:
                holds['held'][key]['removed'] = list(removed)
            _save(store, holds)
        why = 'was removed by its provider' if removed else 'failed its access test'
        if _publish(store, holds, key, lambda policy: without_routes(policy, pair),
                    lambda exc: f'{backend} {model} {why}, but routing refused to take '
                                f'it out: {exc}. It stays routed.', transport, result, before=hold):
            holds['alerted'].pop(key, None)
            _save(store, holds)
            result['taken_out'].append(key)
        return
    if status == VERIFIED and key in holds['held'] and not rows:
        was_removed = 'removed' in holds['held'][key]
        why = 'is listed again' if was_removed else 'passed its access test'
        if _publish(store, holds, key, lambda policy: with_routes(policy, holds['held'][key]),
                    lambda exc: f'{backend} {model} {why}, but its routes could not be '
                                f'restored: {exc}. It stays out of routing; the next run tries again.',
                    transport, result):
            del holds['held'][key]
            holds['alerted'].pop(key, None)
            _save(store, holds)
            result['restored'].append(key)
            if was_removed:
                _tell(transport, key, f'{backend} {model} is listed again, so its routes are back.', result)
        return
    # Nothing to publish: routes came back another way (Dan's Pull), or a past refusal no longer applies.
    stale = status == VERIFIED and holds['held'].pop(key, None) is not None
    if holds['alerted'].pop(key, None) or stale:
        _save(store, holds)


def _managed_backends() -> list[str]:
    from maestro.backends import registry as backends
    return sorted({ref.backend_id for ref in backends.BACKENDS.values() if ref.modelctl_provider is not None})


def _naming_removed(policy, backend, removed: set) -> dict:
    """Model id -> the owner's removed ids that name it. The owner reports class ids, and an older
    policy can still route a native alias of one."""
    from maestro.model_classes import facts
    named = {model: [model] for model in removed}
    for row in facts(policy):
        identities = {row['model_id'], *row.get('aliases', [])}
        if row['backend_id'] == backend and (hit := sorted(identities & removed)):
            named.update((identity, hit) for identity in identities)
    return named


def _announce_removals(store, holds, transport, result) -> None:
    """Durable, so a take-out during a Retry tap (no transport) or a failed send is told on a later run."""
    routed = {(r['backend_id'], r['model_id']) for r in store.snapshot()[1]['routes']}
    for key, hold in holds['held'].items():
        if ('removed' in hold and not hold.get('told') and (hold['backend'], hold['model_id']) not in routed
                and _tell(transport, key, f'{hold["backend"]} {hold["model_id"]} was removed by its provider, so '
                                          'it left routing. It returns by itself if it is listed again.', result)):
            hold['told'] = True
            _save(store, holds)


def _pass(store, bridge, transport, result) -> None:
    holds = _load(store)
    for backend in _managed_backends():
        try:  # an unreadable state is unknown availability, never a removal or a return
            access = bridge.access(backend)
            removed = set(bridge.retired(backend)['removed'])
        except PolicyError as exc:
            result['errors'].append(f'{backend}: {exc}')
            continue
        policy = store.snapshot()[1]
        named = _naming_removed(policy, backend, removed)
        routed = {r['model_id'] for r in policy['routes'] if r['backend_id'] == backend}
        held = {h['model_id'] for h in holds['held'].values() if h['backend'] == backend}
        retired = {tuple(pair) for pair in policy['retired']}
        for model in sorted(routed | held):
            key = f'{backend}:{model}'
            if (backend, model) in retired:  # retirement is final, so a hold on it has nothing left to do
                holds['alerted'].pop(key, None)
                if holds['held'].pop(key, None) is not None:
                    _save(store, holds)
                continue
            hold = holds['held'].get(key, {})
            status = access.get(model)
            if model in named or removed.intersection(hold.get('removed', ())):
                status = FAILED
            elif 'removed' in hold:  # listed again; a removal never asked it to pass a test
                # modelctl keys access by class id, and the hold may be under a native alias.
                checks = {status, *(access.get(owner_id) for owner_id in hold['removed'])}
                status = FAILED if FAILED in checks else VERIFIED
            _settle(store, holds, backend, model, status, transport, result,
                    removed=named.get(model) or hold.get('removed', ()))
        _announce_removals(store, holds, transport, result)


def reconcile(store, bridge, transport=None) -> dict:
    """Bring routing in line with modelctl's access results. Never raises: routing stays as it
    was, the problem is reported in the command's output, and the next run tries again."""
    result = dict(taken_out=[], restored=[], refused=[], errors=[])
    try:
        store.home.mkdir(parents=True, exist_ok=True)
        with open(store.home / LOCK_FILE, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(result, busy=True)
            _pass(store, bridge, transport, result)
            _sync_cards(store, bridge, result)
            if transport is not None:
                result['errors'] += asks.flush(store, transport, bridge)['send_errors']
    except Exception as exc:  # noqa: BLE001 - this step must not fail `consume`
        result['errors'].append(str(exc) or type(exc).__name__)
    return result


# ── Dan's card for a stopped test (records and transport live in `model_asks`) ──

def _reason(raw) -> str:
    """Redacted before it is saved, and short enough for a Telegram message."""
    from maestro.status import redact_secrets
    text = redact_secrets(str(raw)).strip() or 'no detail recorded'
    return text if len(text) <= _REASON_CHARS else text[:_REASON_CHARS] + '…'


def _sync_cards(store, bridge, result) -> None:
    """One card per stop. A backend that cannot be read leaves its cards as they are, and a card
    whose action is running belongs to that action."""
    stops = {}
    for backend in _managed_backends():
        try:
            stops[backend] = bridge.stopped(backend)
        except PolicyError as exc:
            result['errors'].append(f'{backend}: {exc}')
    with store.locked():
        cards = {record['subject']: record for record in asks.records(store) if record['kind'] == CARD}
        live = set()
        for backend, models in stops.items():
            for model, stop in sorted(models.items()):
                subject = f'{backend}:{model}:{stop["at"]}'
                live.add(subject)
                reason, card = _reason(stop['reason']), cards.get(subject)
                if card is None:
                    asks._save(store, asks._new(CARD, subject, backend, model, '', 'stopped', time.time(),
                                                failure=reason, access=stop['access']))
                elif card['status'] == 'open' and (card['failure'], card['access']) != (reason, stop['access']):
                    card.update(failure=reason, access=stop['access'], retried=False, send_pending=True)
                    asks._save(store, card)
        for subject, card in cards.items():
            if card['status'] == 'open' and card['backend'] in stops and subject not in live:
                # Dan hears about the close only if he saw the card.
                card.update(status='superseded', step='closed', send_pending=bool(card['message_ids']),
                            reason='a later result or a re-add cleared this stop')
                asks._save(store, card)


def on_tap(store, bridge, record, action, now) -> None:
    """Caller holds the policy lock and saves `record`. The action itself runs in `apply_card`."""
    if record['status'] == 'open' and record['step'] == 'stopped' and action in ('retry', 'remove'):
        record.update(status='applying', step=action, error=None, send_pending=True)
    elif record['status'] == 'done' and record.get('removed') and action == 'pull':
        _readd(store, bridge, record, now)


def _readd(store, bridge, record, now) -> None:
    """Pull from Drive for a removed model. A refusal raises and saves nothing."""
    backend, model = record['backend'], record['model_id']
    if any((r['backend_id'], r['model_id']) == (backend, model) for r in store.snapshot()[1]['routes']):
        raise PolicyError(f'{model} already has routes, so there is nothing to bring back')
    for other in asks.records(store):
        if (other.get('readd') and (other['backend'], other['model_id']) == (backend, model)
                and other['status'] in ('open', 'applying')):
            other['send_pending'] = True
            asks._save(store, other)
            return
    family = (asks._any_row(bridge, backend, model) or {}).get('family', '')
    readd = asks._new('new_class', f'readd:{backend}:{model}:{time.time_ns()}', backend, model, family, 'use', now,
                      readd=True)
    asks._pull(store, bridge, readd)
    asks._save(store, readd)


def _routing_now(store, backend, model) -> str:
    if any((r['backend_id'], r['model_id']) == (backend, model) for r in store.snapshot()[1]['routes']):
        return 'routed'
    return 'held' if f'{backend}:{model}' in _load(store)['held'] else 'unrouted'


def _retry(store, bridge, record) -> dict:
    backend, model = record['backend'], record['model_id']
    answer = bridge.retest(backend, model)
    reconcile(store, bridge, None)  # modelctl's own handover may have done this already; repeating is safe
    if answer['outcome'] == 'stopped':
        return dict(status='open', step='stopped', failure=_reason(answer.get('detail')),
                    access=answer.get('access'), retried=True)
    return dict(status='done', step='closed', outcome=answer['outcome'], access=answer.get('access'),
                routing=_routing_now(store, backend, model))


def _remove(store, bridge, record) -> dict:
    """Each step is safe to repeat, so a second tap finishes what a refusal or a crash interrupted."""
    backend, model = record['backend'], record['model_id']
    pair, key = (backend, model), f'{backend}:{model}'
    with open(store.home / LOCK_FILE, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        revision, policy = store.snapshot()
        if any((r['backend_id'], r['model_id']) == pair for r in policy['routes']):
            preview = dict(preview_base(store, revision), policy=without_routes(policy, pair, kind='access_remove'))
            publish_and_adopt(preview, store, registry(store))
        holds = _load(store)
        dropped = [holds[name].pop(key, None) for name in ('held', 'alerted')]
        if any(value is not None for value in dropped):
            _save(store, holds)
        with store.locked():
            # `approved_pairs` would add these to a later Pull's pairs, and an unrelated MODEL_SET
            # could put the model back on them.
            for old in asks.records(store):
                if (old['kind'] == 'new_class' and old.get('pair_approval') and old['status'] == 'done'
                        and (old['backend'], old['model_id']) == pair):
                    old.update(status='superseded', send_pending=False, reason='Dan removed this model')
                    asks._save(store, old)
    bridge.drop_from_tests(backend, model)  # takes modelctl's lock, so not while holding ours
    return dict(status='done', step='closed', removed=True)


def apply_card(store, bridge, ident) -> int:
    """Never under the policy lock: the test takes minutes and its handover runs `consume`."""
    with store.locked():
        record = asks._load(store, ident)
    try:
        change = (_retry if record['step'] == 'retry' else _remove)(store, bridge, record)
        change['error'] = None
    except (PolicyError, OSError) as exc:
        change = dict(status='open', step='stopped', error=_reason(exc))
    with store.locked():
        record = asks._load(store, ident)
        record.update(change, send_pending=True)
        asks._save(store, record)
    return 1 if record['status'] == 'done' else 0


def _result_text(record, name) -> str:
    if record['outcome'] == 'rate_limited':
        if record.get('access') == VERIFIED:
            return (f'⏸ {name} hit a rate limit, so it was not judged. Its last test verified it, '
                    'so no further test is scheduled.')
        return (f'⏸ {name} hit a rate limit, so it was not judged. It is back in the hourly retests '
                'and you will hear when it passes.')
    return {'routed': f'✅ {name} passed its test and is in routing.',
            'held': f'✅ {name} passed its test, but its routes are not back yet. The next run tries again.',
            'unrouted': f'✅ {name} passed its test. It has no routes to restore.'}[record['routing']]


def card_view(record) -> tuple[str, list]:
    """The card's text and button rows."""
    name, backend = f'<b>{asks._e(record["model_id"])}</b>', asks._e(record['backend'])
    if record['status'] == 'applying':
        return (f'⏳ Testing {name} again. This can take up to 3 minutes.' if record['step'] == 'retry'
                else f'⏳ Removing {name} from routing and from automatic tests.'), []
    if record['status'] == 'superseded':
        return f'⏭ Closed the alert about {name}: {asks._e(record["reason"])}.', []
    if record.get('removed'):
        return (f'🗑 Removed {name} on {backend}: its routes are gone and it is no longer tested. Its limits '
                'row stays, and Pull from Drive brings it back.'), [[asks._button('Pull from Drive', record, 'pull')]]
    if record['status'] == 'done':
        return _result_text(record, name), []
    error = f'❗ {asks._e(record["error"])}\n\n' if record['error'] else ''
    lead = (f'❌ Retry failed. {name} on {backend} still fails its access test:' if record.get('retried')
            else f'⚠️ {name} on {backend} failed its access test:')
    routing = ('Maestro takes it out of routing until a test passes.' if record['access'] == FAILED
               else 'It stays in routing.')
    return (f'{error}{lead}\n<pre>{asks._e(record["failure"])}</pre>\n{routing}\nAutomatic retests are stopped. '
            'Retry tests it once now. Remove model takes it out of routing and stops its tests.',
            [[asks._button('Retry', record, 'retry'), asks._button('Remove model', record, 'remove')]])
