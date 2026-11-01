"""Telegram model asks (Dan, 2026-10-08): Dan approves a model from Telegram alone.

Kinds of ask:
- `new_class`: a model of a class Maestro has never routed. Three steps: use it? which roles
  does it take over? then the missing limits/profile cells one at a time.
- `limits_review`: a same-class successor already switched automatically with inherited
  values. Non-blocking: Keep, or Change the cells one at a time.
- `menu` and `edit`: Dan's `/models` command (codex-bridge writes it as `kind: command`) lists
  every routed Claude and Codex model; a tap opens that model's card, one button per value, and
  Apply runs one `modelctl add` with the changed values.

Records live under the policy store (`asks/<ask_id>.json`) and are written atomically under its
lock. codex-bridge, the single listener on the send-to-me bot, writes button taps and replies
into `asks/inbox/<update_id>.json`; `process` drains them. Values are only ever written by
`modelctl add` (through `ModelctlBridge.add`); Maestro never edits the owner's tables.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import time
from pathlib import Path

from maestro.model_policy import PolicyError, atomic_write, canonical


# Owner capability: modelctl leaves the new-model "Run: modelctl add" notice to these asks.
MODEL_ASKS_VERSION = 1

REMIND_AFTER = 24 * 3600
EDIT_IDLE_AFTER = 24 * 3600  # an untouched /models edit card closes after this (Dan's ask, 2026-10-08)
_UPDATES_KEPT = 100
_MESSAGES_KEPT = 50
_ERROR_CHARS = 1500
_WINDOWED = ('codex',)  # providers whose enforced window is its own table cell
_STRENGTHS = ('light', 'balanced', 'strong')
_LIFECYCLE = ('handoff', 'fresh', 'ceiling', 'window')
_PROFILE = ('strength', 'relative_cost', 'max_output')
# The editor's cells, in card order. The native window is Dan's verified owner fact (modelctl
# `--native-window`), sent with the three profile fields.
_EDIT = (*_PROFILE, 'native_window', *_LIFECYCLE)
_LABELS = dict(handoff='Prepare handoff', fresh='Normally start fresh', ceiling='Exception ceiling',
               window='Enforced window', strength='Strength', relative_cost='Relative cost',
               max_output='Max output', native_window='Native window', intelligence='Intelligence', cost='Relative cost')
_SHORT = dict(_LABELS, fresh='Start fresh', ceiling='Ceiling', window='Window')
_EXAMPLES = dict(handoff='220K-250K', fresh='260-290K', ceiling='450K', window='540K',
                 strength='balanced', relative_cost='0.75', max_output='32K', native_window='400K',
                 intelligence='48', cost='0.75')


# ── values ──────────────────────────────────────────────────────────────────────────────
# Mirrors modelctl `core/tokens.py`; modelctl's own validation stays the final authority.

def _tokens(raw: str, label: str) -> int:
    text = raw.strip()
    if match := re.fullmatch(r'(\d+)\s*[kK]', text):
        value = int(match.group(1)) * 1000
    elif re.fullmatch(r'\d{4,}', text):
        value = int(text)
    else:
        raise PolicyError(f'{label}: cannot parse {raw!r}; use e.g. 180K or 180000')
    if value <= 0:
        raise PolicyError(f'{label}: must be a positive token count')
    if value % 1000:
        raise PolicyError(f'{label}: {raw!r} is not a whole number of K')
    return value


def _range(raw: str, label: str) -> list[int]:
    parts = [p.strip() for p in raw.strip().replace('–', '-').replace('—', '-').split('-')]
    if len(parts) > 2 or not all(parts):
        raise PolicyError(f'{label}: use 180K or 180K-200K, got {raw!r}')
    if len(parts) == 2 and re.fullmatch(r'\d{1,3}', parts[0]) and parts[1].lower().endswith('k'):
        parts[0] += 'K'
    low, high = _tokens(parts[0], label), _tokens(parts[-1], label)
    if low > high:
        raise PolicyError(f'{label}: low end {parts[0]} exceeds high end {parts[-1]}')
    return [low, high]


def parse_cell(cell: str, raw) -> object:
    label, text = _LABELS[cell], str(raw).strip()
    if cell in ('handoff', 'fresh'):
        return _range(text, label)
    if cell in ('ceiling', 'window', 'native_window'):
        return _tokens(text, label)
    if cell == 'strength':
        if text.lower() not in _STRENGTHS:
            raise PolicyError(f'{label}: choose light, balanced or strong')
        return text.lower()
    if cell == 'intelligence':
        if not re.fullmatch(r'\d{1,3}', text) or not 0 <= int(text) <= 100:
            raise PolicyError(f'{label}: use a whole number from 0 to 100')
        return int(text)
    if cell in ('relative_cost', 'cost'):
        try:
            value = float(text)
        except ValueError:
            raise PolicyError(f'{label}: use a positive number such as 0.75') from None
        if not math.isfinite(value) or value <= 0:
            raise PolicyError(f'{label}: use a positive number such as 0.75')
        return value
    if match := re.fullmatch(r'(\d+)\s*[kK]', text):
        value = int(match.group(1)) * 1000
    elif re.fullmatch(r'\d+', text):
        value = int(text)
    else:
        raise PolicyError(f'{label}: use a token count such as 32K or 32000')
    if value <= 0:
        raise PolicyError(f'{label}: must be positive')
    return value


def _k(tokens: int) -> str:
    return f'{tokens / 1000:g}K'


def show(cell: str, value) -> str:
    if value is None:
        return '?'
    if cell in ('handoff', 'fresh'):
        return _k(value[0]) if value[0] == value[1] else f'{_k(value[0])}-{_k(value[1])}'
    if cell in ('ceiling', 'window', 'native_window'):
        return _k(value)
    if cell == 'max_output':
        return _k(value) if value % 1000 == 0 else str(value)
    if cell in ('relative_cost', 'cost'):
        return f'{value:g}' if float(f'{value:g}') == value else repr(value)
    return str(value)


_ORDER = (('handoff', 'fresh', lambda a, b: a[1] < b[0], 'prepare range {a} must end before the fresh range {b} starts'),
          ('fresh', 'ceiling', lambda a, b: a[1] < b, 'fresh range {a} must end below the exception ceiling {b}'),
          ('ceiling', 'window', lambda a, b: a < b, 'the enforced window {b} must exceed the exception ceiling {a}'))


def _check_order(values: dict) -> None:
    for first, second, ok, message in _ORDER:
        if first in values and second in values and not ok(values[first], values[second]):
            raise PolicyError(message.format(a=show(first, values[first]), b=show(second, values[second])))
    native = values.get('native_window')
    for cell in ('ceiling', 'window'):
        if native is not None and values.get(cell) is not None and values[cell] > native:
            raise PolicyError(f'{_LABELS[cell].lower()} {show(cell, values[cell])} exceeds the native window '
                              f'{show("native_window", native)}')


def default_window(ceiling: int) -> int:
    """modelctl's Codex default: ceiling x 1.2, rounded to 10K."""
    return (ceiling * 6 // 5 + 5_000) // 10_000 * 10_000


# ── records ─────────────────────────────────────────────────────────────────────────────

def ask_id(kind: str, subject: str) -> str:
    return hashlib.sha256(f'{kind}:{subject}'.encode()).hexdigest()[:8]


def _dir(store) -> Path:
    return store.home / 'asks'


def _path(store, ident: str) -> Path:
    if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{8}', ident):
        raise PolicyError('invalid model ask id')
    return _dir(store) / (ident + '.json')


def records(store) -> list[dict]:
    try:
        return [json.loads(p.read_text()) for p in sorted(_dir(store).glob('*.json'))]
    except (OSError, ValueError) as exc:
        raise PolicyError('unreadable model ask; repair before retrying') from exc


def _load(store, ident: str) -> dict:
    try:
        return json.loads(_path(store, ident).read_text())
    except (OSError, ValueError) as exc:
        raise PolicyError(f'no readable model ask {ident}') from exc


def _save(store, record: dict) -> None:
    atomic_write(_path(store, record['ask_id']), canonical(record))


def open_asks(store) -> list[dict]:
    """Asks still waiting on Dan or on their own application; read-only (status/doctor)."""
    return [dict(ask_id=r['ask_id'], kind=r['kind'], destination=[r['backend'], r['model_id']],
                 status=r['status'], step=r['step'], send_pending=r['send_pending'])
            for r in records(store) if r['status'] in ('open', 'applying') and r['kind'] != 'menu']


def _new(kind, subject, backend, model, family, step, now, **extra) -> dict:
    return dict(schema=1, ask_id=ask_id(kind, subject), kind=kind, subject=subject, backend=backend,
                model_id=model, family=family, status='open', step=step, order=[], walk=[], cells=[],
                pending_cell=None, answers={}, current={}, baseline={}, suggestions={}, roles=[],
                role_options=[], role_models={}, message_ids=[], current_message=None, send_pending=True,
                edit_pending=False, reminder=False, last_sent=None, created=now, applied_updates=[],
                error=None, registered=False, reason=None, **extra)


def has_new_class_ask(store, destination) -> bool:
    return any(r['kind'] == 'new_class' and [r['backend'], r['model_id']] == list(destination)
               for r in records(store))


def _supersedes(proposal, other, versions) -> bool:
    """Owner versions decide when both are known. Otherwise a detection is newer by arrival,
    but a profile ask (from a set row) may be about an older member, so it never assumes."""
    backend, model = proposal['destination']
    mine, theirs = versions.get((backend, model)), versions.get((backend, other['model_id']))
    if mine is not None and theirs is not None:
        return mine > theirs
    return proposal.get('needs') != 'profile'


def open_new_class(store, proposal: dict, now=None, versions=None, access=None) -> str | None:
    """Caller holds the policy lock. Idempotent per proposal; the newest member of a class wins."""
    from maestro import model_detection
    now = time.time() if now is None else now
    ident = ask_id('new_class', proposal['proposal_id'])
    if _path(store, ident).exists():
        return None
    backend, model = proposal['destination']
    family = proposal['detection']['family']
    for other in records(store):
        if (other['kind'] == 'new_class' and other['status'] == 'open' and other['backend'] == backend
                and other['family'] == family and other['model_id'] != model
                and _supersedes(proposal, other, versions or {})):
            other.update(status='superseded', step='closed', send_pending=bool(other['message_ids']),
                         reason=f'a newer {family} model ({model}) was detected')
            _save(store, other)
            older = model_detection._load(store, other['subject'])
            if older['status'] == 'waiting_limits':
                older['status'] = 'superseded'
                model_detection._save(store, older)
    extra = dict(needs=proposal['needs']) if proposal.get('needs') else {}
    if access == 'INCONCLUSIVE':
        extra['access'] = access
    if (proposal.get('needs') and proposal['detection'].get('predecessor_id')
            and not proposal.get('unrouted_predecessor')):
        # A same-class successor: modelctl's next SET moves its predecessor's roles anyway.
        extra['predecessor'] = proposal['detection']['predecessor_id']
    _save(store, _new('new_class', proposal['proposal_id'], backend, model, family, 'use', now, **extra))
    return ident


def _owner_row(bridge, backend, model) -> dict | None:
    rows = bridge.configured_models(backend, identities=[model]).get('models') or []
    return next((m for m in rows if m.get('set') is True
                 and (m['model_id'] == model or model in m.get('aliases', []))), None)


def _profile_values(policy, backend, model) -> dict:
    from maestro.model_policy import catalog_defaults
    base = next(b for b in catalog_defaults(policy, {}).values() if b['backend_id'] == backend)
    profile = base['model_profiles'].get(model)
    if profile is None:
        return {}
    limits = profile.context_limits or base['context_limits']
    values = dict(max_output=limits.max_output_tokens if limits else None)
    if policy['schema'] == 1:
        pair = base['pair_profiles'][model, 'medium']
        values.update(strength=pair.strength, relative_cost=pair.relative_cost)
    return values


def open_limits_review(store, bridge, switch: dict, now=None) -> str:
    """Caller holds the policy lock. One non-blocking review per applied switch record."""
    now = time.time() if now is None else now
    event = switch['event']
    backend, model = switch['destination']
    ident = ask_id('limits_review', switch['switch_id'])
    if _path(store, ident).exists():
        return ident
    row = _owner_row(bridge, backend, model)
    if row is None:
        raise PolicyError(f'{backend}:{model}: owner limits row unavailable for the review ask')
    life = row['lifecycle']
    current = dict(handoff=list(life['prepare']), fresh=list(life['fresh']), ceiling=life['ceiling'])
    if backend in _WINDOWED:
        current['window'] = row['enforced_window']
    current.update(_profile_values(store.load(switch['applied_revision']), backend, model))
    order = [c for c in (*_LIFECYCLE, *_PROFILE) if c in current and current[c] is not None]
    record = _new('limits_review', switch['switch_id'], backend, model, row['family'], 'review', now,
                  predecessor=event.get('predecessor_id'))
    record.update(order=order, walk=list(order), current=current, baseline=dict(current))
    _save(store, record)
    return ident


def _open_menu(store, bridge, update, now) -> dict:
    """Caller holds the lock. `/models`: a fresh list replaces the older ones, whose taps then
    find nothing. Each button carries an index into this record's snapshot, not a model id
    (callback data is 64 bytes)."""
    reference = _reference(store, bridge)  # a failure leaves the old list working
    for old in records(store):
        if old['kind'] == 'menu':
            _path(store, old['ask_id']).unlink(missing_ok=True)
    record = _new('menu', str(update['update_id']), '', '', '', 'menu', now)
    record.update(entries=[[r['backend'], r['model']] for r in reference if r['handoff'] is not None],
                  read_only=[[r['backend'], r['model']] for r in reference if r['handoff'] is None])
    return record


def _open_edit(store, bridge, menu, index, now) -> None:
    """Caller holds the lock. One edit ask per model: a second tap brings its card back."""
    if index >= len(menu['entries']):
        return
    backend, model = menu['entries'][index]
    snapshot = _edit_values(store, bridge, backend, model)
    if snapshot is None:
        menu.update(error=f'{model} no longer has a modelctl limits row', send_pending=True)
        return
    row, current, extra = snapshot
    for other in records(store):
        if other['kind'] == 'edit' and [other['backend'], other['model_id']] == [backend, model] \
                and other['status'] in ('open', 'applying'):
            if other['status'] == 'applying':
                other['send_pending'] = True  # the tap gets the ⏳ status back
                _save(store, other)
            else:
                # Rebase on what modelctl holds now; Dan's pending changes stay.
                answers = {c: v for c, v in other['answers'].items() if v != current.get(c)}
                other.update(extra, current=current, baseline=dict(current), answers=answers, step='pick',
                             pending_cell=None, send_pending=True)
                _save(store, other)
            return
    record = _new('edit', f'{backend}:{model}:{time.time_ns()}', backend, model, row['family'], 'pick', now,
                  **extra)
    order = [c for c in _EDIT if c in current]
    record.update(order=order, current=current, baseline=dict(current))
    _save(store, record)


def _edit_values(store, bridge, backend, model):
    """The card's values from the owner row and the current revision, or None without a row."""
    row = _owner_row(bridge, backend, model)
    if row is None:
        return None
    _, policy = store.snapshot()
    life = row['lifecycle']
    current = dict(handoff=list(life['prepare']), fresh=list(life['fresh']), ceiling=life['ceiling'])
    if backend in _WINDOWED:
        current['window'] = row['enforced_window']
    routing = _profile_values(policy, backend, model)
    grade = row.get('routing_profile') if isinstance(row.get('routing_profile'), dict) else {}
    # Dan's last owner entry is shown (and kept); a different value still in routing is flagged.
    current.update(routing)
    if grade:
        current.update(strength=grade['strength'], relative_cost=grade['relative_cost'],
                       max_output=grade['max_output_tokens'])
    current['native_window'] = grade.get('native_window_tokens')  # Dan's override only
    reported = row.get('provider_limits') if isinstance(row.get('provider_limits'), dict) else {}
    from maestro.model_policy import catalog_defaults
    base = next(b for b in catalog_defaults(policy, {}).values() if b['backend_id'] == backend)
    profile = base['model_profiles'].get(model)
    limits = (profile.context_limits if profile else None) or base['context_limits']
    return row, current, dict(owner_model=row['model_id'],
                              routing={c: v for c, v in routing.items() if v != current.get(c)},
                              assumed_native=limits.max_input_tokens if limits else None,
                              provider_native=reported.get('native_window_tokens'),
                              provider_output=reported.get('max_output_tokens'))


# ── state transitions (caller holds the lock) ───────────────────────────────────────────

def _driver(backend):
    from maestro.backends.registry import driver_name_for_backend_id
    return driver_name_for_backend_id(backend)


def _start_roles(store, record) -> None:
    _, policy = store.snapshot()
    driver = _driver(record['backend'])
    record.update(step='roles', roles=[], role_options=sorted(policy['roles']),
                  role_models={r: e['models'].get(driver) for r, e in sorted(policy['roles'].items())},
                  send_pending=True)


def _start_pairs(store, bridge, record) -> None:
    """Only register-listed efforts are proposed; every value still needs Dan's answer."""
    from maestro import thirdparty
    from maestro.backends import catalog
    entry = catalog.catalog_from_register(thirdparty.load_register(),
                                           defaults=catalog.SHIPPED_CATALOG_DEFAULTS).entries.get(record['backend'])
    efforts = [effort for effort in catalog.CANONICAL_EFFORTS
               if entry is not None and entry.delivers(effort)]
    if not efforts:
        raise PolicyError('no deliverable effort levels for this model; pair approval unavailable')
    record.update(pair_approval=True, pair_efforts=efforts, pair_index=0, approved_pairs=[], roles=[], role_models={})
    _next_pair(store, record)


def _next_pair(store, record) -> None:
    _, policy = store.snapshot()
    effort = record['pair_efforts'][record['pair_index']]
    proposed = next((r for r in policy['routes'] if r['backend_id'] == record['backend']
                     and r['effort'] == effort and r['model_id'] == record.get('predecessor')), None)
    current = dict(intelligence=proposed['intelligence'], cost=proposed['relative_cost']) if proposed else {}
    record.update(pair_effort=effort, order=['intelligence', 'cost'], walk=['intelligence', 'cost'],
                  cells=['intelligence', 'cost'], answers={}, current=current)
    _advance(record)


def _finish_pair(store, bridge, record) -> None:
    if not (record.get('pair_effort') and record['status'] == 'applying'):
        return
    values = _merged(record)
    record['approved_pairs'].append(dict(backend_id=record['backend'], model_id=record['model_id'],
                                         effort=record['pair_effort'], intelligence=values['intelligence'],
                                         relative_cost=values['cost']))
    _advance_pair(store, bridge, record)


def _advance_pair(store, bridge, record) -> None:
    record.update(pair_index=record['pair_index'] + 1, status='open')
    if record['pair_index'] < len(record['pair_efforts']):
        _next_pair(store, record)
    elif not record['approved_pairs']:
        _decline(store, record)
    else:
        _start_values(store, bridge, record)


def _start_values(store, bridge, record) -> None:
    from maestro.model_policy import catalog_defaults
    backend, model = record['backend'], record['model_id']
    _, policy = store.snapshot()
    answers = {}
    base = next(b for b in catalog_defaults(policy, {}).values() if b['backend_id'] == backend)
    profiles = base['model_profiles']
    replaced = {record['role_models'].get(r) for r in record['roles']} - {None}
    if replaced and replaced <= set(profiles):
        # Inherit from the replaced model(s) only where they all agree.
        for cell, attr in (('strength', 'strength'), ('relative_cost', 'relative_cost')):
            values = {getattr(base['pair_profiles'][m, 'medium'], attr) for m in replaced}
            if len(values) == 1:
                answers[cell] = values.pop()
    row = _owner_row(bridge, backend, model)  # a bridge failure leaves the update for retry
    if row is not None:
        life = row['lifecycle']
        answers.update(handoff=list(life['prepare']), fresh=list(life['fresh']), ceiling=life['ceiling'])
        if backend in _WINDOWED and row.get('enforced_window'):
            answers['window'] = row['enforced_window']
        grade = row.get('routing_profile')
        if isinstance(grade, dict):
            answers.update(strength=grade['strength'], relative_cost=grade['relative_cost'],
                           max_output=grade['max_output_tokens'])
    try:
        unknown = bridge.limits(backend).get('unknown')
    except PolicyError:
        unknown = None
    if unknown:
        record['suggestions'] = dict(handoff=list(unknown['prepare']), fresh=list(unknown['fresh']),
                                     ceiling=unknown['ceiling'])
    if record.get('pair_approval'):
        # modelctl's model profile remains an owner compatibility input. It never grades
        # schema 2 pairs; only the explicitly approved rows below do that.
        from maestro.backends.catalog import class_of
        first = record['approved_pairs'][0]
        answers.update(strength=class_of(first['intelligence']), relative_cost=first['relative_cost'])
        record.update(pair_effort=None, current={})
    order = ['strength', 'relative_cost', 'max_output', 'handoff', 'fresh', 'ceiling']
    order += ['window'] if backend in _WINDOWED else []
    walk = [c for c in order if c not in answers]
    record.update(order=order, walk=walk, cells=list(walk), answers=answers)
    _advance(record)


def _merged(record) -> dict:
    return {**record['current'], **record['answers']}


def _advance(record) -> None:
    record.update(error=None, send_pending=True)
    if record['cells']:
        record.update(step='cell', pending_cell=record['cells'].pop(0))
        return
    record['pending_cell'] = None
    if record['kind'] == 'limits_review' and _merged(record) == record['baseline']:
        record.update(status='kept', step='closed')
    else:
        record.update(status='applying', step='apply')


def _accept(record, cell, raw) -> None:
    try:
        value = parse_cell(cell, raw)
        merged = _merged(record)
        # The editor checks the whole card; a walk checks against the cells already decided.
        earlier = record['order'] if record['kind'] == 'edit' else record['order'][:record['order'].index(cell)]
        values = {c: merged[c] for c in earlier if merged.get(c) is not None}
        values[cell] = value
        if values.get('native_window') is None and record.get('provider_native'):
            values['native_window'] = record['provider_native']
        _check_order(values)
    except PolicyError as exc:
        record.update(error=str(exc), send_pending=True)
        return
    if record['kind'] == 'edit':
        if value == record['baseline'].get(cell):
            record['answers'].pop(cell, None)  # back to the saved value: no longer a change
        else:
            record['answers'][cell] = value
        record.update(step='pick', pending_cell=None, error=None, registered=False, send_pending=True)
        return
    record['answers'][cell] = value
    _advance(record)


def approved_pairs(store, backend, model) -> list[dict]:
    """Completed Telegram approvals only; partial, declined and failed asks admit nothing."""
    from copy import deepcopy
    return [deepcopy(pair) for record in records(store)
            if record.get('pair_approval') and record['status'] in ('applying', 'done')
            and (record['backend'], record['model_id']) == (backend, model)
            for pair in record['approved_pairs']]


def _rewalk(record) -> None:
    """Walk the asked cells again; what was entered becomes each cell's Keep value."""
    for cell in record['walk']:
        if cell in record['answers']:
            record['current'][cell] = record['answers'].pop(cell)
    record.update(cells=list(record['walk']), registered=False)
    _advance(record)


def _decline(store, record) -> None:
    from maestro import model_detection
    if record['kind'] == 'new_class':
        proposal = model_detection._load(store, record['subject'])
        if proposal['status'] == 'waiting_limits':
            proposal['status'] = 'rejected'
            model_detection._save(store, proposal)
        record.update(status='declined', step='closed', send_pending=True)
    else:
        record.update(status='kept', step='closed', send_pending=True)


def _edit_apply(record) -> None:
    values = _merged(record)
    if _profile_changed(record) and any(values.get(c) is None for c in _PROFILE):
        missing = ', '.join(_LABELS[c].lower() for c in _PROFILE if values.get(c) is None)
        record.update(error=f'modelctl needs the whole profile: set {missing} too', send_pending=True)
    elif values == record['baseline'] and not record.get('attempted'):
        # After an Apply reached modelctl, even "back to saved" must run modelctl again.
        record.update(status='kept', step='closed', error=None, send_pending=True)
    else:
        record.update(status='applying', step='apply', error=None, send_pending=True)


def _on_callback(store, bridge, record, action: str, now=None) -> None:
    step = record['step']
    if record['status'] != 'open':
        return
    if record['kind'] == 'menu':
        if re.fullmatch(r'm\d{1,3}', action):
            _open_edit(store, bridge, record, int(action[1:]), time.time() if now is None else now)
        return
    if record['kind'] == 'edit':
        if action.startswith('e:') and step == 'pick' and action[2:] in record['order']:
            record.update(step='cell', pending_cell=action[2:], error=None, send_pending=True)
        elif action == 'apply' and step == 'pick':
            _edit_apply(record)
        elif action == 'cancel' and step in ('pick', 'cell'):
            record.update(status='kept', step='closed', error=None, send_pending=True)
        elif action == 'back' and step == 'cell':
            record.update(step='pick', pending_cell=None, error=None, send_pending=True)
        elif action.startswith('set:') and step == 'cell':
            cell, _, value = action[4:].partition(':')
            if cell == record['pending_cell']:
                _accept(record, cell, value)
        return
    if action == 'use' and step == 'use' and store.snapshot()[1]['schema'] == 2:
        _start_pairs(store, bridge, record)
    elif action == 'use' and step == 'use' and record.get('predecessor'):
        _start_values(store, bridge, record)  # no role step: the successor takes its predecessor's roles
    elif action == 'use' and step == 'use':
        _start_roles(store, record)
    elif action == 'skip_pair' and step == 'cell' and record.get('pair_effort'):
        _advance_pair(store, bridge, record)
    elif action == 'decline' and step in ('use', 'roles', 'cell', 'failed') and record['kind'] == 'new_class':
        _decline(store, record)
    elif action == 'keep' and (step == 'review' or step == 'failed' and record['kind'] == 'limits_review'):
        _decline(store, record)
    elif re.fullmatch(r'r\d{1,2}', action) and step == 'roles':
        index = int(action[1:])
        if index < len(record['role_options']):
            role = record['role_options'][index]
            roles = set(record['roles']) ^ {role}
            record.update(roles=[r for r in record['role_options'] if r in roles], edit_pending=True)
    elif action == 'done' and step == 'roles':
        _start_values(store, bridge, record)
    elif action == 'change' and step == 'review':
        record['cells'] = list(record['walk'])
        _advance(record)
    elif action == 'change' and step == 'failed':
        _rewalk(record)
    elif action == 'retry' and step == 'failed':
        record.update(status='applying', step='apply', error=None, send_pending=True)
    elif action.startswith('set:') and step == 'cell':
        cell, _, value = action[4:].partition(':')
        if cell == record['pending_cell']:  # a tap on an older message never fills another cell
            _accept(record, cell, value)


# ── inbound ─────────────────────────────────────────────────────────────────────────────

_REF = re.compile(r'mdl:([0-9a-f]{8})')


def _find(all_records, update) -> tuple[dict | None, str | None]:
    by_id = {r['ask_id']: r for r in all_records}
    if update['kind'] == 'callback':
        match = re.fullmatch(r'mdl:([0-9a-f]{8}):(.{1,52})', update.get('data') or '')
        return (by_id.get(match.group(1)), match.group(2)) if match else (None, None)
    # Only a reply reaches us: codex-bridge leaves plain non-reply text to Codex.
    reply = update.get('reply_to_message_id')
    if reply is None:
        return None, None
    found = next((r for r in all_records if reply in r['message_ids']), None)
    if found is None and (match := _REF.search(update.get('reply_to_text') or '')):
        found = by_id.get(match.group(1))
    return found, None


def _valid_update(update) -> bool:
    return (isinstance(update, dict) and type(update.get('update_id')) is int
            and update.get('kind') in ('callback', 'message', 'command')
            and (update['kind'] != 'callback' or isinstance(update.get('data'), str))
            and (update['kind'] == 'callback' or isinstance(update.get('text'), str)))


def _command(text: str) -> str:
    """`/models` or `/models@SomeBot`, lower-cased; anything else is not Maestro's."""
    word = text.strip().split(maxsplit=1)[0] if text.strip() else ''
    return word.split('@', 1)[0].lower()


def _handle(store, bridge, transport, update) -> bool:
    if transport is not None and str(update.get('chat_id')) != str(transport.chat_id):
        return False
    all_records = records(store)
    if any(update['update_id'] in r['applied_updates'] for r in all_records):
        return False
    if update['kind'] == 'command':
        if _command(update['text']) != '/models':
            return False
        record = _open_menu(store, bridge, update, time.time())
        record['applied_updates'] = [update['update_id']]
        _save(store, record)
        return True
    record, action = _find(all_records, update)
    if record is None:
        return False
    if update['kind'] == 'callback':
        _on_callback(store, bridge, record, action)
    elif record['status'] == 'open' and record['step'] == 'cell':
        _accept(record, record['pending_cell'], update['text'])
    _finish_pair(store, bridge, record)
    record['applied_updates'] = (record['applied_updates'] + [update['update_id']])[-_UPDATES_KEPT:]
    _save(store, record)
    return True


def _update_key(path: Path):
    return (0, int(path.stem)) if path.stem.isdecimal() else (1, path.stem)


def process(store, bridge, transport, now=None) -> dict:
    """Drain the inbox, apply finished asks, then send what changed. Safe to rerun."""
    import fcntl
    result = dict(processed=0, ignored=0, applied=0, errors=[])
    _dir(store).mkdir(parents=True, exist_ok=True)
    with open(_dir(store) / 'process.lock', 'a') as held:
        try:
            # Not the policy lock: `_apply` runs modelctl add, whose handoff takes that one.
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(result, busy=True)
        return _process(store, bridge, transport, now, result)


def _process(store, bridge, transport, now, result) -> dict:
    inbox = sorted((_dir(store) / 'inbox').glob('*.json'), key=_update_key)
    if transport is None and inbox:
        result['errors'].append('no Telegram config: inbox left until the sender chat can be checked')
        inbox = []
    for path in inbox:
        try:
            update = json.loads(path.read_text())
        except (OSError, ValueError):
            update = None
        if not _valid_update(update):
            path.unlink(missing_ok=True)
            result['errors'].append(f'invalid inbox update {path.name} dropped')
            continue
        try:
            with store.locked():
                handled = _handle(store, bridge, transport, update)
        except PolicyError as exc:
            result['errors'].append(f'update {update["update_id"]} kept for retry: {exc}')
            continue
        path.unlink(missing_ok=True)
        result['processed' if handled else 'ignored'] += 1
    for record in records(store):
        if record['status'] == 'applying':
            result['applied'] += _apply(store, bridge, record['ask_id'])
    result.update(flush(store, transport, bridge, now=now))
    return result


# ── application (never under the lock: modelctl's handoff runs `maestro models consume`) ──

def _profile_changed(record) -> bool:
    values = _merged(record)
    return any(values.get(c) != record['baseline'].get(c) for c in (*_PROFILE, 'native_window'))


def _argv(record) -> list[str]:
    values = _merged(record)
    argv = ['--model', record['model_id'], '--handoff', show('handoff', values['handoff']),
            '--fresh', show('fresh', values['fresh']), '--ceiling', show('ceiling', values['ceiling'])]
    if record['backend'] in _WINDOWED and values.get('window'):
        argv += ['--window', show('window', values['window'])]
    restate = record['kind'] == 'edit' and record.get('attempted') and all(values.get(c) is not None for c in _PROFILE)
    if record['kind'] == 'new_class' or _profile_changed(record) or restate:
        # modelctl requires the three profile fields together; the native window rides with them.
        argv += ['--strength', values['strength'], '--relative-cost', show('relative_cost', values['relative_cost']),
                 '--max-output', str(values['max_output'])]
        if values.get('native_window'):
            argv += ['--native-window', show('native_window', values['native_window'])]
    return argv + ['-y']


def _pending_sets(bridge, record) -> list[str]:
    owner = record.get('owner_model', record['model_id'])
    return sorted(e['event_id'] for e in bridge.pending_events()
                  if isinstance(e, dict) and e.get('kind') == 'MODEL_SET' and e.get('provider') == record['backend']
                  and e.get('model_id') == owner and isinstance(e.get('event_id'), str))


def failure_text(output: str) -> str:
    from maestro.status import redact_secrets
    lines = [line for line in (output or '').splitlines() if line.strip()]
    failed = [line for line in lines if line.startswith(('FAILED', 'error:', 'ROLLBACK'))]
    text = '\n'.join(failed or lines[-12:]) or 'modelctl add failed without output'
    text = redact_secrets(text)
    return text if len(text) <= _ERROR_CHARS else text[:_ERROR_CHARS] + '…'


def _apply(store, bridge, ident) -> int:
    from maestro.model_detection import consume
    with store.locked():
        record = _load(store, ident)
        if record['status'] != 'applying':
            return 0
    try:
        if not record['registered']:
            with store.locked():
                record = _load(store, ident)
                argv = _argv(record)  # before `attempted`: only a later attempt restates the profile
                record['attempted'] = True
                # Older SETs for this model may stay pending for good (refused, superseded); only a new one counts.
                record['prior_sets'] = _pending_sets(bridge, record)
                _save(store, record)
            ok, output = bridge.add(record['backend'], argv)
            if not ok:
                raise PolicyError(failure_text(output))
            with store.locked():
                record = _load(store, ident)
                record['registered'] = True
                _save(store, record)
        consumed = consume(store, bridge, asks=True, transport=None)
        if record['kind'] != 'new_class':
            # modelctl holding the values is not enough: Maestro must have published them.
            if set(_pending_sets(bridge, record)) - set(record.get('prior_sets', [])):
                errors = '; '.join(consumed['errors']) or 'its MODEL_SET is still pending'
                hint = (' Set Native window to the verified value if the model is larger.'
                        if 'native window' in errors else '')
                raise PolicyError(f'modelctl saved the values but Maestro refused them: {errors}.{hint}')
        if record['kind'] == 'new_class':
            _, policy = store.snapshot()
            pair = (record['backend'], record['model_id'])
            if not any((r['backend_id'], r['model_id']) == pair for r in policy['routes']):
                raise PolicyError('modelctl registered the model but Maestro has not published it yet: '
                                  + ('; '.join(consumed['errors']) or 'no MODEL_SET consumed'))
            if record['roles']:
                _assign_roles(store, record)
    except PolicyError as exc:
        with store.locked():
            record = _load(store, ident)
            # The editor shows a failure on its card: fix a value and Apply again, or Cancel.
            record.update(status='open', step='pick' if record['kind'] == 'edit' else 'failed',
                          error=failure_text(str(exc)), send_pending=True)
            _save(store, record)
        return 0
    with store.locked():
        record = _load(store, ident)
        record.update(status='done', step='closed', error=None, send_pending=True)
        _save(store, record)
    return 1


def _assign_roles(store, record) -> None:
    from maestro.model_inventory import registry
    from maestro.model_lifecycle import preview_base, publish_and_adopt
    from maestro.model_policy import assign_roles
    revision, policy = store.snapshot()
    driver = _driver(record['backend'])
    todo = [r for r in record['roles'] if policy['roles'].get(r, {}).get('models', {}).get(driver) != record['model_id']]
    if not todo:
        return  # already assigned (a retry after publication)
    preview = preview_base(store, revision)
    preview['policy'] = assign_roles(policy, (record['backend'], record['model_id']), todo)
    publish_and_adopt(preview, store, registry(store))


# ── outbound ────────────────────────────────────────────────────────────────────────────

def _close_stale(store, record) -> bool:
    """A new-class ask whose proposal was settled elsewhere (e.g. a manual modelctl add)."""
    from maestro import model_detection
    if record['kind'] != 'new_class' or record['status'] != 'open' or record['step'] not in ('use', 'roles', 'cell'):
        return False
    if record.get('attempted'):
        return False  # its own apply settled the proposal (e.g. Re-enter values after a failure)
    try:
        proposal = model_detection._load(store, record['subject'])
    except PolicyError:
        return False  # an unreadable proposal is reported by status/doctor, not guessed at
    status = proposal['status']
    if status == 'waiting_limits':
        return False
    reason = ('it was set outside Telegram' if status == 'applied'
              else proposal.get('reason') or f'its proposal is now {status}')
    # Dan hears about the close only if he saw the ask.
    record.update(status='superseded', step='closed', send_pending=bool(record['message_ids']), reason=reason)
    return True


def _close_idle_edit(record, now) -> bool:
    """An edit card nobody touched for a day closes, so status does not list it forever. Dan
    hears about it only if he saw the card; its unsaved changes are dropped, never applied."""
    if (record['kind'] != 'edit' or record['status'] != 'open'
            or now - max(record['created'], record['last_sent'] or 0) < EDIT_IDLE_AFTER):
        return False
    record.update(status='superseded', step='closed', pending_cell=None, send_pending=bool(record['message_ids']),
                  reason=f'idle for {EDIT_IDLE_AFTER // 3600} h, nothing was applied. Send /models to edit again')
    return True


def flush(store, transport, bridge=None, now=None) -> dict:
    """Send each changed ask as a new message (so replies thread); toggles edit in place."""
    now = time.time() if now is None else now
    result = dict(sent=0, send_pending=0, send_errors=[])
    with store.locked():
        for record in records(store):
            changed = _close_stale(store, record) or _close_idle_edit(record, now)
            if (record['kind'] == 'new_class' and record['status'] == 'open' and not record['send_pending']
                    and record['last_sent'] is not None and now - record['last_sent'] >= REMIND_AFTER):
                # Reminders only for the blocking ask; a daily limits_review nudge would be spam.
                record.update(send_pending=True, reminder=True)
                changed = True
            if not (record['send_pending'] or record['edit_pending']):
                if changed:
                    _save(store, record)
                continue
            if transport is None:
                result['send_pending'] += 1
                if changed:
                    _save(store, record)
                continue
            try:
                text, markup = render(record, _table(store, bridge, record))
                if record['edit_pending'] and not record['send_pending'] and record['current_message'] is not None:
                    transport.edit(record['current_message'], markup=markup or {'inline_keyboard': []})
                else:
                    if record['current_message'] is not None:
                        try:
                            transport.edit(record['current_message'], markup={'inline_keyboard': []})
                        except Exception:
                            pass  # best effort: an old keyboard is harmless, its taps are ignored
                    sent = transport.send(text, markup)
                    record.update(current_message=sent, last_sent=now, reminder=False, error=None,
                                  message_ids=(record['message_ids'] + [sent])[-_MESSAGES_KEPT:])
                    result['sent'] += 1
                record.update(send_pending=False, edit_pending=False)
            except PolicyError as exc:
                result['send_errors'].append(f'{record["ask_id"]}: {exc}')
                result['send_pending'] += 1
            _save(store, record)
    return result


# ── rendering ───────────────────────────────────────────────────────────────────────────

def _e(value) -> str:
    """Redact the raw value, then escape it: redacting finished HTML can eat markup."""
    from maestro.status import redact_secrets
    return html.escape(redact_secrets(str(value)), quote=False)


def _reference(store, bridge) -> list[dict]:
    """Every routed model on every modelctl provider, with each cell's current value (None if unknown)."""
    from maestro.backends.registry import BACKENDS
    from maestro.model_policy import catalog_defaults
    managed = {ref.modelctl_provider for ref in BACKENDS.values() if ref.modelctl_provider is not None}
    _, policy = store.snapshot()
    out = []
    for base in catalog_defaults(policy, {}).values():
        backend = base['backend_id']
        if backend not in managed:
            continue
        try:
            rows = (bridge.configured_models(backend).get('models') or []) if bridge is not None else []
        except PolicyError:
            rows = []
        for model, profile in sorted(base['model_profiles'].items()):
            row = next((m for m in rows if m.get('set') and (m['model_id'] == model or model in m.get('aliases', []))), None)
            limits = profile.context_limits or base['context_limits']
            life = row['lifecycle'] if row else {}
            values = dict(backend=backend, model=model,
                            max_output=limits.max_output_tokens if limits and limits.max_output_tokens else None,
                            handoff=life.get('prepare'), fresh=life.get('fresh'), ceiling=life.get('ceiling'),
                            window=(row or {}).get('enforced_window') if backend in _WINDOWED else None,
                            native_window=((row or {}).get('routing_profile') or {}).get('native_window_tokens')
                            or ((row or {}).get('provider_limits') or {}).get('native_window_tokens'))
            pairs = [p for p in base['pair_profiles'].values() if p.model_id == model
                     and (policy['schema'] == 2 or p.effort == 'medium')]
            for pair in sorted(pairs, key=lambda p: p.effort or ''):
                out.append(dict(values, strength=pair.strength, relative_cost=pair.relative_cost,
                                effort=pair.effort if policy['schema'] == 2 else None,
                                intelligence=pair.intelligence))
    return out


def _pre(lines) -> str:
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    body = '\n'.join('  '.join(c.ljust(w) for c, w in zip(line, widths)).rstrip() for line in lines)
    return f'<pre>{_e(body)}</pre>'


def _rows(reference, backend) -> list[list[str]]:
    windowed = backend in _WINDOWED
    lines = [['model', 'S', 'cost', 'out', 'handoff', 'fresh', 'ceil'] + (['win'] if windowed else [])]
    for r in reference:
        if r['backend'] != backend:
            continue
        label = r['model'] + (f'@{r["effort"]}' if r.get('effort') else '')
        cells = [label, r['strength'][0], f'{r["relative_cost"]:g}']
        cells += [show(c, r[c]) if r[c] is not None else '?' for c in ('max_output', 'handoff', 'fresh', 'ceiling')]
        if windowed:
            cells.append(show('window', r['window']) if r['window'] else '?')
        lines.append(cells)
    return lines


def _table(store, bridge, record) -> str:
    """Context for an ask. A value prompt shows the one cell being decided on every other model,
    across all providers (Dan, 2026-10-08); Use and review show this backend's full rows; the
    `/models` list shows every provider's."""
    if record['status'] != 'open' or record['step'] not in ('use', 'cell', 'review', 'menu'):
        return ''
    try:
        reference = [r for r in _reference(store, bridge)
                     if (r['backend'], r['model']) != (record['backend'], record['model_id'])]
    except (PolicyError, StopIteration):
        return ''
    if record['step'] == 'menu':
        tables = [(backend, _rows(reference, backend)) for backend in sorted({r['backend'] for r in reference})]
        return ''.join(f'\n\n{_e(backend)} now:\n{_pre(lines)}' for backend, lines in tables if len(lines) > 1)
    if record['step'] == 'cell':
        cell = record['pending_cell']
        if cell in ('intelligence', 'cost'):
            from maestro.backends.registry import BACKENDS
            managed = {ref.backend_id for ref in BACKENDS.values() if ref.modelctl_provider is not None}
            _, policy = store.snapshot()
            key = 'intelligence' if cell == 'intelligence' else 'relative_cost'
            lines = [[r['backend_id'], r['model_id'], str(r[key])] for r in policy['routes']
                     if r['backend_id'] in managed and r.get('effort') == record['pair_effort']]
            return f'\n\n{_LABELS[cell]} at {_e(record["pair_effort"])} on the other models:\n{_pre(lines)}' if lines else ''
        lines = [[r['backend'], r['model'] + (f'@{r["effort"]}' if r.get('effort') else ''),
                  show(cell, r[cell])] for r in reference if r.get(cell) is not None]
        return f'\n\n{_LABELS[cell]} on the other models:\n{_pre(lines)}' if lines else ''
    backend = record['backend']
    lines = _rows(reference, backend)
    return f'\n\nOther {_e(backend)} models now:\n{_pre(lines)}' if len(lines) > 1 else ''


def _button(text, record, action) -> dict:
    data = f'mdl:{record["ask_id"]}:{action}'
    if len(data.encode()) > 64:
        raise PolicyError('model ask callback data exceeds 64 bytes')
    return dict(text=text, callback_data=data)


def _cell_buttons(record, cell) -> list[list[dict]]:
    if record['kind'] == 'edit':
        keep, saved = _merged(record).get(cell), record['baseline'].get(cell)
        if cell == 'strength':
            rows = [[_button(s + (' (current)' if s == keep else ''), record, f'set:strength:{s}') for s in _STRENGTHS]]
        else:
            rows = [[_button(f'Keep {show(cell, keep)}', record, f'set:{cell}:{show(cell, keep)}')]] if keep is not None else []
            if saved is not None and saved != keep:
                rows.append([_button(f'Back to saved {show(cell, saved)}', record, f'set:{cell}:{show(cell, saved)}')])
        return rows + [[_button('Back', record, 'back')]]
    keep = record['current'].get(cell)
    if cell == 'strength':
        return [[_button(s + (' (current)' if s == keep else ''), record, f'set:strength:{s}') for s in _STRENGTHS]]
    rows = []
    if keep is not None:
        rows.append([_button(f'Keep {show(cell, keep)}', record, f'set:{cell}:{show(cell, keep)}')])
    elif cell == 'window' and 'ceiling' in _merged(record):
        value = show('window', default_window(_merged(record)['ceiling']))
        rows.append([_button(f'Use default {value}', record, f'set:window:{value}')])
    elif cell in record['suggestions']:
        value = show(cell, record['suggestions'][cell])
        rows.append([_button(f'Use suggestion {value}', record, f'set:{cell}:{value}')])
    if record.get('pair_effort'):
        rows.append([_button('Skip this effort', record, 'skip_pair'), _button('Decline', record, 'decline')])
    return rows


def _cell_label(record, values, cell) -> str:
    if cell == 'native_window' and record.get('provider_native'):
        # Provider-sourced; the cell is only an override (Dan, 2026-10-08).
        if values.get(cell) is None:
            return f'Native window: {show(cell, record["provider_native"])} (provider)'
        return f'Native window: {show(cell, values[cell])} (override)'
    return f'{_SHORT[cell]}: {show(cell, values.get(cell))}'


def render(record, table: str = '') -> tuple[str, dict | None]:
    name = f'<b>{_e(record["model_id"])}</b>'
    backend = _e(record['backend'])
    footer = f'\n\n<code>mdl:{record["ask_id"]}</code>'
    status, step, buttons = record['status'], record['step'], []
    changes = '\n'.join(f'{_LABELS[c]}: {_e(show(c, record["baseline"].get(c)))} → {_e(show(c, v))}'
                        for c, v in record['answers'].items()) if record['kind'] == 'edit' else ''
    if record['kind'] == 'menu':
        error = f'❗ {_e(record["error"])}\n\n' if record['error'] else ''
        locked = (f'\nNo modelctl limits row, so not editable here: '
                  f'{_e(", ".join(m for _, m in record["read_only"]))}.' if record['read_only'] else '')
        text = f'{error}🎛 <b>Models</b>: tap one to see and edit its values.{locked}' + table
        buttons = [[_button(model, record, f'm{i}') for i, (_, model) in enumerate(record['entries'][j:j + 3], j)]
                   for j in range(0, len(record['entries']), 3)]
    elif record['kind'] == 'edit' and status == 'done':
        text = f'✅ Saved {name} on {backend}. Projects adopt it between tasks.\n{changes}'
    elif record['kind'] == 'edit' and status == 'kept':
        text = (f'👍 Stopped. modelctl may hold the values of the last Apply for {name}; '
                'Maestro has not published them.' if record.get('attempted') else f'👍 No changes to {name}.')
    elif record['kind'] == 'edit' and status == 'superseded':
        text = f'⏭ Closed the edit of {name}: {_e(record["reason"])}.'
    elif record['kind'] == 'edit' and status == 'applying':
        text = f'⏳ Saving {name}: modelctl add, then Maestro publishes the policy.\n{changes}'
    elif record['kind'] == 'edit' and step == 'pick':
        error = f'❗ {_e(record["error"])}\n\n' if record['error'] else ''
        values = _merged(record)
        notes = []
        reported = [f'{label} {_e(show(c, record[key]))}' for c, label, key in (
            ('native_window', 'native window', 'provider_native'), ('max_output', 'max output', 'provider_output'))
            if record.get(key)]
        if reported:
            notes.append('From the provider (read-only): ' + ', '.join(reported) + '.'
                         + (' Your native window override replaces it.' if values.get('native_window')
                            and record.get('provider_native') else ''))
        elif values.get('native_window') is None and record.get('assumed_native'):
            notes.append(f'Native window not set by you; Maestro currently allows up to '
                         f'{_e(show("native_window", record["assumed_native"]))}.')
        if record.get('routing'):
            notes.append('⚠️ Maestro still routes with ' + ', '.join(
                f'{_LABELS[c].lower()} {_e(show(c, v))}' for c, v in record['routing'].items())
                + ': modelctl\'s last change for this model has not been published.')
        count = len(record['answers'])
        text = (f'{error}✏️ {name} ({backend}): tap a value to change it, then Apply. '
                + (f'{count} change{"s" if count != 1 else ""} (•).' if count else 'Nothing changed yet.')
                + ''.join('\n' + n for n in notes))
        cells = [_button(('• ' if c in record['answers'] else '') + _cell_label(record, values, c),
                         record, f'e:{c}') for c in record['order']]
        buttons = [cells[i:i + 2] for i in range(0, len(cells), 2)]
        buttons.append([_button(f'Apply ({count})' if count else 'Apply', record, 'apply'),
                        _button('Cancel', record, 'cancel')])
    elif status == 'done':
        text = (f'✅ {name} is set on {backend}. Projects adopt it between tasks.'
                + (f'\nRoles now on it: {_e(", ".join(record["roles"]))}.' if record['roles'] else ''))
    elif status == 'declined':
        text = (f'🚫 Stopped. {name} is registered with modelctl; its role choices were not applied.'
                if record['registered'] else f'🚫 Declined: Maestro will not use {name}.')
    elif status == 'kept':
        text = (f'👍 Stopped. modelctl already holds the entered values for {name}.' if record['registered']
                else f'👍 Kept the current values for {name}.')
    elif status == 'superseded':
        text = f'⏭ Closed the ask about {name}: {_e(record["reason"] or "superseded")}.'
    elif status == 'applying':
        text = f'⏳ Applying {name}: modelctl add, then Maestro publishes the policy.'
    elif step == 'use' and record.get('needs') == 'profile':
        successor = (f'\nIt succeeds {_e(record["predecessor"])} and takes over its roles.'
                     if record.get('predecessor') else '')
        text = (f'🆕 {backend} model {name} (class {_e(record["family"])}) has limits in modelctl but no '
                f'routing profile, so Maestro cannot route it yet.{successor}\nUse it in Maestro?' + table)
        buttons = [[_button('Use', record, 'use'), _button('Decline', record, 'decline')]]
    elif step == 'use':
        access = ('\n⚠️ modelctl could not confirm access yet (its check was inconclusive); '
                  'the final add checks again.' if record.get('access') == 'INCONCLUSIVE' else '')
        text = (f'🆕 New {backend} model detected: {name} (class {_e(record["family"])}).{access}\n'
                'Use it in Maestro?' + table)
        buttons = [[_button('Use', record, 'use'), _button('Decline', record, 'decline')]]
    elif step == 'roles':
        text = (f'Which roles should {name} take over on {backend}? Tap to toggle, then Done.\n'
                'Untouched roles keep their model; the new model is an eligible route either way.')
        buttons = [[_button(('✅ ' if role in record['roles'] else '▫️ ')
                            + f'{role} (now {record["role_models"].get(role) or "none"})', record, f'r{i}')]
                   for i, role in enumerate(record['role_options'])]
        buttons.append([_button('Done', record, 'done')])
    elif step == 'review':
        lines = '\n'.join(f'{_LABELS[c]}: {_e(show(c, record["current"][c]))}' for c in record['order'])
        text = (f'🔁 {backend} switched {_e(record.get("predecessor") or "its predecessor")} → {name} '
                f'automatically, with these inherited values:\n{lines}\n'
                'Nothing waits on this. Keep them, or change them one at a time.' + table)
        buttons = [[_button('Keep', record, 'keep'), _button('Change', record, 'change')]]
    elif step == 'cell':
        cell = record['pending_cell']
        entered = '\n'.join(f'{_LABELS[c]}: {_e(show(c, record["answers"][c]))}'
                            for c in record['order'] if c in record['answers'])
        error = f'❗ {_e(record["error"])} — reply again.\n\n' if record['error'] else ''
        assumed = ''
        if cell == 'native_window' and record.get('provider_native'):
            assumed = (f'\nThe provider reports {_e(show(cell, record["provider_native"]))}. '
                       'Reply only to override a value the provider reports wrongly.')
        elif cell == 'native_window' and _merged(record).get(cell) is None and record.get('assumed_native'):
            assumed = (f'\nNot set by you yet; Maestro currently allows up to '
                       f'{_e(show(cell, record["assumed_native"]))}.')
        effort = f' @{_e(record["pair_effort"])}' if record.get('pair_effort') else ''
        text = (f'{error}{name}{effort} — <b>{_LABELS[cell]}</b>{assumed}\n'
                f'Reply to this message with a value (e.g. {_EXAMPLES[cell]})'
                + (' or tap a button.' if cell == 'strength' or _cell_buttons(record, cell) else '.')
                + (f'\n\n{"Changed" if record["kind"] == "edit" else "So far"}:\n{entered}'
                   if entered and not record.get('pair_approval') else '')
                + table)
        buttons = _cell_buttons(record, cell)
    elif step == 'failed':
        text = (f'❌ Applying {name} failed:\n<pre>{_e(record["error"] or "unknown error")}</pre>\n'
                'Nothing changed in routing. Retry, re-enter the values, or stop.')
        buttons = [[_button('Retry', record, 'retry'), _button('Re-enter values', record, 'change')],
                   [_button('Decline', record, 'decline') if record['kind'] == 'new_class'
                    else _button('Keep as is', record, 'keep')]]
    else:
        text = f'Model ask about {name} is {_e(status)}.'
    if record.get('reminder'):
        text = '⏰ Reminder — still waiting on you.\n' + text
    return text + footer, (dict(inline_keyboard=buttons) if buttons else None)


# ── transport ───────────────────────────────────────────────────────────────────────────

SEND_TO_ME_CONFIG = Path('~/.config/agent-send/telegram.env')
ENABLED_MARKER = 'telegram.enabled'


class TelegramTransport:
    """Bot API over the send-to-me bot; codex-bridge stays its only listener."""

    def __init__(self, token: str, chat_id: str, *, timeout: int = 10):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout

    @classmethod
    def from_config(cls, path: Path | None = None) -> 'TelegramTransport | None':
        # Subprocesses spawned by a test inherit the real HOME; never send live from a test.
        if os.environ.get('PYTEST_CURRENT_TEST'):
            return None
        # Rollout switch (Dan's go-ahead): until it exists, asks are held (`send_pending`), never sent.
        from maestro.model_policy import policy_home
        if not (policy_home() / 'asks' / ENABLED_MARKER).exists():
            return None
        path = Path(path or SEND_TO_ME_CONFIG).expanduser()
        try:
            if path.stat().st_mode & 0o077:
                return None  # send-to-me refuses a config readable by others; so do we
            lines = path.read_text().splitlines()
        except OSError:
            return None
        values = {}
        for line in lines:
            key, sep, value = line.partition('=')
            if sep and key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
                values[key] = value
        token, chat = values.get('TELEGRAM_BOT_TOKEN'), values.get('TELEGRAM_CHAT_ID', '')
        if not token or not re.fullmatch(r'-?\d+', chat):
            return None
        return cls(token, chat)

    def _call(self, method: str, payload: dict) -> dict:
        import requests
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/{method}',
                                     data=payload, timeout=self.timeout)
            body = response.json()
        except (requests.RequestException, ValueError):
            raise PolicyError(f'telegram {method} unreachable') from None  # the URL holds the token
        if not body.get('ok'):
            raise PolicyError(f'telegram {method} refused ({_e(str(body.get("description", "")))[:200]})')
        return body.get('result')

    def send(self, text: str, markup: dict | None = None) -> int:
        # `render` redacts each interpolated value (`_e`); the HTML itself is left intact.
        payload = dict(chat_id=self.chat_id, text=text, parse_mode='HTML',
                       disable_web_page_preview='true')
        if markup:
            payload['reply_markup'] = json.dumps(markup)
        result = self._call('sendMessage', payload)
        if not isinstance(result, dict) or type(result.get('message_id')) is not int:
            raise PolicyError('telegram sendMessage returned no message id')
        return result['message_id']

    def edit(self, message_id: int, *, markup: dict | None = None) -> None:
        self._call('editMessageReplyMarkup', dict(chat_id=self.chat_id, message_id=message_id,
                                                  reply_markup=json.dumps(markup or {'inline_keyboard': []})))
