"""Modelctl limits-set events switch classes; older owners keep approval proposals."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

from maestro.model_policy import PolicyError, atomic_write, canonical, digest


# Owner capability: the switch journal delivers the combined limits/profile review.
CLASS_TRANSITION_NOTICE_VERSION = 1


def proposal_id(provider: str, model: str) -> str:
    return hashlib.sha256(canonical([provider, model])).hexdigest()


def proposals(store) -> list[dict]:
    try:
        return [json.loads(p.read_text()) for p in sorted((store.home / 'proposals').glob('*.json'))]
    except (OSError, ValueError) as exc:
        raise PolicyError('unreadable detection proposal; repair before retrying') from exc


def open_proposals(store) -> list[dict]:
    """Proposals still awaiting a decision, as reported by doctor/status; read-only."""
    return [dict(proposal_id=p['proposal_id'], destination=p['destination'], status=p['status'])
            for p in proposals(store) if p['status'] in _OPEN + ('waiting_limits',)]


def validate_set_event(event):
    if (not isinstance(event, dict) or event.get('kind') != 'MODEL_SET'
        or not all(isinstance(event.get(k), str) and event[k].strip() == event[k] and event[k]
                   for k in ('event_id', 'provider', 'model_id', 'family'))
        or not isinstance(event.get('version'), list) or not event['version']
        or any(type(v) is not int or v < 0 for v in event['version'])
        or type(event.get('enforced_window')) is not int or event['enforced_window'] <= 0
        or event.get('predecessor_id') is not None and
           (not isinstance(event['predecessor_id'], str) or not event['predecessor_id'].strip())):
        raise PolicyError('invalid modelctl set event')


def consume(store, bridge, *, notify=None, asks=False, transport=None) -> dict:
    """Consume modelctl's outbox. With `asks`, Dan decides on Telegram (`maestro.model_asks`):
    a `waiting_limits` proposal opens a new-class ask, and an automatic same-class switch opens
    a non-blocking limits review in place of the plain notice."""
    from maestro.backends import registry
    supported_providers = {ref.backend_id for ref in registry.BACKENDS.values()
                           if ref.modelctl_provider is not None}
    result = dict(proposals_created=0, switches_applied=0, reconciled=0, acknowledged=0, notification_pending=0,
                  errors=[])
    switches(store)  # validate durable recovery records before any publication
    events = bridge.pending_events()
    if not isinstance(events, list):
        raise PolicyError('invalid modelctl outbox')
    for event in events:
        if isinstance(event, dict) and event.get('kind') == 'MODEL_SET':
            validate_set_event(event)
    setting = {(e.get('provider'),e.get('model_id')) for e in events
               if isinstance(e,dict) and e.get('kind') == 'MODEL_SET'}
    for event in events:
        if isinstance(event, dict) and event.get('kind') == 'MODEL_SET':
            if event.get('provider') not in supported_providers:
                raise PolicyError('invalid modelctl set backend')
            try:
                result['switches_applied'] += consume_set(store, bridge, event)
                bridge.acknowledge(event['event_id'])
                result['acknowledged'] += 1
            except ProfileRequired as exc:
                if not asks:
                    result['errors'].append(str(exc))
                    continue
                try:
                    _await_profile(store, event, exc)
                    bridge.acknowledge(event['event_id'])  # proposal committed first
                    result['acknowledged'] += 1
                except PolicyError as failure:
                    result['errors'].append(str(failure))
            except PolicyError as exc:
                result['errors'].append(str(exc))
            continue
        if (not isinstance(event, dict) or event.get('kind') not in ('NEW_MODEL_DISCOVERED', 'MODEL_NOW_AVAILABLE')
                or event.get('provider') not in supported_providers
                or not all(isinstance(event.get(k), str) and event[k].strip()
                           for k in ('event_id', 'model_id', 'family', 'first_seen', 'fingerprint'))
                or not isinstance(event.get('related_model_ids'), list)
                or not all(isinstance(m, str) for m in event['related_model_ids'])):
            raise PolicyError('invalid modelctl detection outbox event')
        if (event['provider'],event['model_id']) in setting:
            try:
                bridge.acknowledge(event['event_id'])
                result['acknowledged'] += 1
            except PolicyError as exc:
                result['errors'].append(str(exc))
            continue
        class_contract = False
        if hasattr(bridge, 'configured_models'):
            try:
                class_contract = bridge.configured_models(event['provider']).get('contract') == 1
            except PolicyError:
                pass  # legacy owner can still deliver historical proposals
        ident = proposal_id(event['provider'], event['model_id'])
        with store.locked():
            path = store.home / 'proposals' / (ident + '.json')
            if not path.exists():
                revision, policy = store.snapshot()
                previous = [list(pair) for pair in sorted({(r['backend_id'], r['model_id']) for r in policy['routes']
                            if r['backend_id'] == event['provider']
                            and r['model_id'] in event['related_model_ids']
                            and r['model_id'] != event['model_id']})]
                record = dict(schema=1, proposal_id=ident, detection=event,
                              policy_revision=revision, status='waiting_limits' if class_contract else 'pending_approval',
                              destination=[event['provider'], event['model_id']],
                              replacement_candidates=previous, notification_pending=not class_contract)
                atomic_write(path, canonical(record))
                result['proposals_created'] += 1
            else:
                record = json.loads(path.read_text())
                if record.get('destination') != [event['provider'], event['model_id']]:
                    raise PolicyError('detection proposal identity mismatch')
        # Commit proposal before ack. A crash here replays the same identity.
        try:
            bridge.acknowledge(event['event_id'])
            result['acknowledged'] += 1
        except PolicyError as exc:
            result['errors'].append(str(exc))
    _reconcile(store, bridge, supported_providers, result)
    if notify is not None and proposals(store):
        # Serialized delivery; failure keeps the durable pending flag. Delivery may
        # repeat after a crash, so transports should use proposal_id as their key.
        with store.locked():
            for record in proposals(store):
                if not record.get('notification_pending'):
                    continue
                if record['status'] not in _OPEN:
                    # Announcing an already decided proposal would ask Dan to review it.
                    record.update(notification_pending=False, notification='not sent: decided before delivery')
                else:
                    try:
                        notify(record)
                    except Exception:
                        continue
                    record['notification_pending'] = False
                _save(store, record)
    if asks:
        from maestro.model_asks import flush
        _open_asks(store, bridge, result)
        result['asks'] = flush(store, transport, bridge)
    if notify is not None and not asks and switches(store):
        with store.locked():
            for record in switches(store):
                if record['status'] != 'applied' or not record.get('notification_pending'):
                    continue
                try:
                    notify(record)
                except Exception:
                    continue
                record['notification_pending'] = False
                atomic_write(store.home / 'switches' / (record['switch_id'] + '.json'), canonical(record))
    result['notification_pending'] = sum(bool(r.get('notification_pending')) for r in proposals(store) + switches(store))
    return result


_OPEN = ('pending_approval', 'registration_required')


def _await_profile(store, event, missing) -> None:
    """A set row Maestro cannot route without a profile: a new-class ask collects it on Telegram.

    The proposal is the durable state, so the event is acknowledged; the profile Dan enters
    makes `modelctl add` emit a new MODEL_SET. A replay finds the same identity."""
    ident = proposal_id(event['provider'], event['model_id'])
    destination = [event['provider'], event['model_id']]
    with store.locked():
        path = store.home / 'proposals' / (ident + '.json')
        if path.exists():
            record = json.loads(path.read_text())
            if record.get('destination') != destination:
                raise PolicyError('detection proposal identity mismatch')
            if record['status'] not in ('waiting_limits', 'rejected'):
                raise missing  # a settled proposal cannot carry a new ask; keep the actionable error
            return
        revision, _ = store.snapshot()
        extra = dict(unrouted_predecessor=True) if missing.unrouted else {}
        atomic_write(path, canonical(dict(schema=1, proposal_id=ident, detection=event, policy_revision=revision,
                                          status='waiting_limits', needs='profile', destination=destination,
                                          replacement_candidates=[], notification_pending=False, **extra)))


def _supersede_older_members(store, bridge) -> None:
    """Caller holds the policy lock. A waiting proposal is moot once a newer member of its
    class is set and routed: the class is already in Maestro (live rollout, 2026-10-08)."""
    _, policy = store.snapshot()
    routed = {(r['backend_id'], r['model_id']) for r in policy['routes']}
    for proposal in proposals(store):
        if proposal['status'] != 'waiting_limits':
            continue
        backend, model = proposal['destination']
        try:
            rows = bridge.configured_models(backend, identities=[model]).get('models') or []
        except PolicyError:
            continue  # the owner is unreachable; decide on a later consume
        mine = next((m for m in rows if model in (m['model_id'], *m.get('aliases', []))), None)
        if mine is None:
            continue
        newer = [m for m in rows if m.get('set') is True and m['family'] == mine['family']
                 and tuple(m['version']) > tuple(mine['version'])
                 and any((backend, ident) in routed for ident in (m['model_id'], *m.get('aliases', [])))]
        if newer:
            current = max(newer, key=lambda m: tuple(m['version']))['model_id']
            proposal.update(status='superseded', reason=f'a newer {mine["family"]} model ({current}) is already set')
            _save(store, proposal)


def _owner_versions(store, bridge, waiting) -> dict:
    """(backend, model or alias) -> owner version, for the models of waiting proposals and open asks."""
    from maestro import model_asks
    wanted = {}
    for backend, model in [p['destination'] for p in waiting] + [
            [r['backend'], r['model_id']] for r in model_asks.records(store) if r['status'] == 'open']:
        wanted.setdefault(backend, set()).add(model)
    versions = {}
    for backend, models in wanted.items():
        try:
            rows = bridge.configured_models(backend, identities=sorted(models)).get('models') or []
        except PolicyError:
            continue  # unknown versions fall back to detection order
        for m in rows:
            for ident in (m['model_id'], *m.get('aliases', [])):
                versions[(backend, ident)] = tuple(m['version'])
    return versions


def _access(bridge, waiting) -> dict:
    """(backend, model) -> modelctl's last access check (VERIFIED, INCONCLUSIVE or FAILED) for the
    detected models of waiting proposals. Unknown when the owner cannot say; the ask opens as before."""
    found = {}
    for backend in sorted({p['destination'][0] for p in waiting if not p.get('needs')}):
        try:
            checks = bridge.access(backend) if hasattr(bridge, 'access') else {}
        except PolicyError:
            continue
        found.update(((backend, model), value) for model, value in (checks or {}).items())
    return found


def _open_asks(store, bridge, result) -> None:
    from maestro import model_asks
    with store.locked():
        _supersede_older_members(store, bridge)
        waiting = sorted((p for p in proposals(store) if p['status'] == 'waiting_limits'),
                         key=lambda p: (p['detection'].get('first_seen', ''), p['proposal_id']))
        versions = _owner_versions(store, bridge, waiting) if waiting else {}
        access = _access(bridge, waiting)
        for proposal in waiting:
            checked = access.get(tuple(proposal['destination']))
            if checked == 'FAILED' and not proposal.get('needs'):
                continue  # not usable yet: modelctl re-checks each scan; ask once it works (Dan, 2026-10-08)
            model_asks.open_new_class(store, proposal, versions=versions,  # the newest member of a class wins
                                      access=checked if not proposal.get('needs') else None)
        for record in switches(store):
            if record['status'] != 'applied' or not record.get('notification_pending'):
                continue
            if model_asks.has_new_class_ask(store, record['destination']):
                note = 'covered by the new-class model ask'
            elif record['event'].get('predecessor_id') is not None:
                # Only an automatic inheritance asks; owner-entered values were Dan's own.
                try:
                    note = 'model ask ' + model_asks.open_limits_review(store, bridge, record)
                except PolicyError as exc:
                    result['errors'].append(f'limits review ask: {exc}')
                    continue
            else:
                note = 'settled: owner-entered values need no review'
            record.update(notification_pending=False, notification=note)
            atomic_write(store.home / 'switches' / (record['switch_id'] + '.json'), canonical(record))


def proposal_notice(record: dict) -> str:
    destination = ':'.join(record['destination'])
    if 'switch_id' in record:
        return (f'Maestro switched to {destination}. Shared revision: {record["applied_revision"]}. '
                'Projects adopt between tasks. Please review modelctl limits/window and the Maestro routing profile.')
    candidates = ', '.join(':'.join(pair) for pair in record['replacement_candidates']) or 'none in the shared policy'
    return (f'Maestro: modelctl detected {destination}; a replacement proposal awaits your approval.\n'
            f'Would replace: {candidates}\n'
            f'Review: maestro models review --proposal {record["proposal_id"]}')


def send_to_me(record: dict) -> None:
    """Proposal notice transport: the host's send-to-me (Dan, 2026-10-01).

    `maestro models consume` runs from modelctl's scan timer, where no project `.env` (so
    no Maestro Telegram credential) is loaded; send-to-me is what modelctl itself notifies
    through. Raises unless send-to-me reports delivery, so the record stays pending.
    """
    exe = shutil.which('send-to-me')
    if not exe:
        raise PolicyError('send-to-me not found on PATH')
    result = subprocess.run([exe, '--text', proposal_notice(record)], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise PolicyError(f'send-to-me failed (exit {result.returncode})')


def _path(store, ident: str):
    if not isinstance(ident, str) or len(ident) != 64 or not all(c in '0123456789abcdef' for c in ident):
        raise PolicyError('invalid proposal id')
    return store.home / 'proposals' / (ident + '.json')


def _load(store, ident: str) -> dict:
    try:
        return json.loads(_path(store, ident).read_text())
    except OSError as exc:
        raise PolicyError(f'no detection proposal {ident}') from exc
    except ValueError as exc:
        raise PolicyError('unreadable detection proposal; repair before retrying') from exc


def _save(store, record: dict) -> None:
    atomic_write(_path(store, record['proposal_id']), canonical(record))


def _published_history(store) -> set[str]:
    """Every revision that has been the shared `current`, newest first via `previous_policy`.

    History starts at a shipped policy, which is never stored: after an upgrade the first
    publication's `previous_policy` is an older release's shipped digest with no file.
    A stored revision that fails its integrity check still raises.
    """
    seen: set[str] = set()
    revision = store.current()
    while revision is not None and revision not in seen:
        seen.add(revision)
        if not (store.home / 'revisions' / (revision + '.json')).exists():
            break
        revision = store.load_any(revision)['source'].get('previous_policy')
    return seen


def reconcile_proposals(store) -> list[dict]:
    """After a crash between approval and publication, trust only what was published.

    Revisions are content addressed, so the approved policy's digest names exactly the
    revision the approval would have published. A revision file alone is not proof:
    `publish` writes it before moving `current`, so it counts only once it is on the
    shared policy's history (`current` and its `previous_policy` chain).
    """
    with store.locked():
        history = None
        for record in proposals(store):
            if record.get('status') != 'approved':
                continue
            if history is None:
                history = _published_history(store)
            published = record['approved_policy_revision'] in history
            if published:
                record.update(status='applied', applied_revision=record['approved_policy_revision'])
            else:
                record.update(status='pending_approval',
                              last_error='approved replacement was not published; review again')
            _save(store, record)
        return proposals(store)


def review_proposal(store, bridge, ident: str, *, old=None, strength=None, relative_cost=None) -> dict:
    """Preview a proposal through the ordinary replacement use case; never publishes."""
    from maestro.model_replacement import preview_replace

    reconcile_proposals(store)
    record = _load(store, ident)
    if record['status'] not in _OPEN:
        raise PolicyError(f'proposal is {record["status"]}; nothing to review')
    revision, policy = store.snapshot()
    destination = tuple(record['destination'])
    related = set(record['detection']['related_model_ids'])
    # Recompute against the current policy; the stored list reflects the detection-time policy.
    candidates = [list(pair) for pair in sorted({(r['backend_id'], r['model_id']) for r in policy['routes']
                  if r['backend_id'] == destination[0] and r['model_id'] in related
                  and r['model_id'] != destination[1]})]
    result = dict(proposal=record, candidates=candidates, preview=None,
                  stale_policy=record['policy_revision'] != revision)
    if old is None:
        if len(candidates) != 1:
            return result  # the operator chooses which eligible route to replace
        old = tuple(candidates[0])
    if list(old) not in candidates:
        raise PolicyError('proposal replaces only a related eligible route; use the picker for others')
    preview = preview_replace(store, bridge, tuple(old), destination,
                              strength=strength, relative_cost=relative_cost)
    result['preview'] = preview
    with store.locked():
        record = _load(store, ident)
        if preview['status'] == 'registration_required':
            record.update(status='registration_required', suggested_limits=preview['suggested_limits'],
                          notice=preview['notice'])
        else:
            record['status'] = 'pending_approval'
        record['last_reviewed_revision'] = revision
        _save(store, record)
    result['proposal'] = record
    return result


def decide_proposal(store, bridge, ident: str, preview, *, approve: bool) -> dict | None:
    """Reject, or approve and apply one reviewed preview. Status follows what actually happened."""
    from maestro.model_replacement import apply_preview

    with store.locked():
        record = _load(store, ident)
        if record['status'] not in _OPEN:
            raise PolicyError(f'proposal is {record["status"]}; nothing to decide')
        if not approve:
            record['status'] = 'rejected'
            _save(store, record)
            return None
        if preview is None or tuple(preview['new']) != tuple(record['destination']):
            raise PolicyError('preview does not belong to this proposal')
        if preview['status'] != 'ready':
            raise PolicyError('registration required: use the modelctl owner add command; routing unchanged')
        record.update(status='approved', approved_expected_revision=preview['expected_revision'],
                      approved_policy_revision=digest(preview['policy']), replaced=preview['old'])
        _save(store, record)
    try:
        outcome = apply_preview(preview, store, bridge, confirmed=True)
    except PolicyError as exc:
        with store.locked():
            record = _load(store, ident)
            record.update(status='pending_approval', last_error=str(exc))
            _save(store, record)
        raise
    with store.locked():
        record = _load(store, ident)
        record.update(status='applied', applied_revision=outcome['revision'])
        record.pop('last_error', None)
        _save(store, record)
    return outcome


class SupersededSet(PolicyError):
    """A newer configured member makes this valid historical event terminal."""


class ProfileRequired(PolicyError):
    """The owner set limits, but routing needs a profile Dan has not given yet."""

    def __init__(self, message, *, unrouted=False):
        super().__init__(message)
        self.unrouted = unrouted  # a successor whose predecessor Maestro never routed


def _admission_ceiling(target, max_input) -> int:
    try:
        fresh = target['lifecycle']['fresh'][0]
    except (TypeError, KeyError, IndexError) as exc:
        raise PolicyError('unsafe or missing provider lifecycle row/native window') from exc
    if type(fresh) is not int or fresh <= 0:
        raise PolicyError('unsafe or missing provider lifecycle row/native window')
    return min(fresh, max_input)


def _profile_current(policy, target) -> bool:
    """Whether the target's current profile already carries its provider facts (Dan's values win)
    and its own lifecycle's admission ceiling."""
    from maestro.model_policy import catalog_defaults
    reported = target.get('provider_limits') or {}
    grade = target.get('routing_profile') or {}
    output = grade.get('max_output_tokens') or reported.get('max_output_tokens')
    native = grade.get('native_window_tokens') or reported.get('native_window_tokens')
    base = next(b for b in catalog_defaults(policy, {}).values() if b['backend_id'] == target['backend_id'])
    profile = base['model_profiles'].get(target['model_id'])
    limits = (profile.context_limits if profile else None) or base['context_limits']
    return ((output is None or limits.max_output_tokens == output)
            and (native is None or limits.max_input_tokens == min(native, target['enforced_window']))
            and limits.effective_context_ceiling == _admission_ceiling(target, limits.max_input_tokens))


def policy_for_set(policy, event, bridge, *, identities=(), owner=None, approved_pairs=()):
    """Build one automatic same-class transition from owner limits-set facts."""
    from copy import deepcopy
    from maestro.model_policy import catalog_defaults, replace_route, validate
    from maestro.backends import catalog
    from maestro.model_classes import facts

    validate_set_event(event)
    backend, model = event['provider'], event['model_id']
    choices = set(identities)
    choices.update(r['model_id'] for r in policy['routes'] if r['backend_id'] == backend)
    owner = owner if owner is not None else bridge.configured_models(backend, identities=sorted(choices))
    if not isinstance(owner, dict) or owner.get('contract') != 1:
        raise PolicyError('incompatible modelctl class contract')
    from maestro.model_classes import validate_rows
    rows = owner.get('models')
    validate_rows(rows)
    if any(row['backend_id'] != backend for row in rows):
        raise PolicyError('owner class metadata crossed backends')
    target = next((m for m in rows if m['model_id'] == model and m['backend_id'] == backend
                   and m.get('set') is True), None)
    if target is None:
        raise PolicyError('set event differs from current owner limits')
    if any(target[k] != event[k] for k in ('family', 'version', 'enforced_window')):
        # The owner's later change emitted (or deduped onto) its own SET; this one is history.
        raise SupersededSet('set event differs from the current owner row, which supersedes it')
    newer = [m for m in rows if m.get('set') is True and m['family'] == target['family'] and tuple(m['version']) > tuple(target['version'])]
    if newer:
        raise SupersededSet('set event is superseded; consume current model instead')
    if policy['schema'] == 2:
        return _policy_for_set_pairs(policy, event, bridge, owner, target, approved_pairs)
    if any(r['backend_id'] == backend and r['model_id'] == model for r in policy['routes']):
        stored = {(m['backend_id'], m['model_id']): m for m in facts(policy)}
        # Consuming one SET stores every row's facts, so a later SET in the same batch can
        # look adopted; its provider facts and ceiling must also be in its own profile.
        if (all(stored.get((m['backend_id'], m['model_id'])) == m for m in rows)
                and _profile_current(policy, target)):
            validate(policy)
            return deepcopy(policy)
    old_id = event.get('predecessor_id')
    if old_id is None:
        candidates = [m for m in rows if m['family'] == target['family'] and m['model_id'] != model
                      and any(r['backend_id'] == backend and r['model_id'] in [m['model_id'], *m.get('aliases', [])] for r in policy['routes'])]
        if candidates:
            old_id = max(candidates, key=lambda m: tuple(m['version']))['model_id']
        elif any(r['backend_id'] == backend and r['model_id'] in [model, *target.get('aliases', [])]
                 for r in policy['routes']):
            # A configured scan or manual edit need not name an old predecessor.
            # The already-adopted current member supplies its own verified profile.
            old_id = model
    previous = next((m for m in rows if m['model_id'] == old_id and m['backend_id'] == backend), None)
    # The provider vouches for this model's native window itself; a max output it does not
    # report keeps the backend default (Codex, Dan 2026-10-08).
    provider_verified = 'native_window_tokens' in (target.get('provider_limits') or {})
    defaults = catalog_defaults(policy, {})
    base = next(b for b in defaults.values() if b['backend_id'] == backend)
    shipped_base = next((b for b in catalog.SHIPPED_CATALOG_DEFAULTS.values()
                         if b['backend_id'] == backend), {})
    native_id = next((ident for ident in [model, *target.get('aliases', [])]
                      if ident in shipped_base.get('model_profiles', {})), model)
    original = shipped_base.get('model_profiles', {}).get(native_id)
    original_pair = shipped_base.get('pair_profiles', {}).get((native_id, catalog.MIGRATION_DEFAULT_EFFORT))
    predecessor = None
    if previous is not None:
        if previous['family'] != target['family']:
            raise PolicyError('same-backend predecessor profile required for this class')
        # Owner IDs/aliases identify shipped routes too; Maestro never parses names.
        old_route = next((r['model_id'] for r in policy['routes'] if r['backend_id'] == backend
                          and r['model_id'] in [old_id, *previous.get('aliases', [])]), None)
        if old_route is None and any(r['backend_id'] == backend and r['model_id'] == model for r in policy['routes']):
            old_route = model  # limits/profile edit of the already selected class member
        predecessor = base['model_profiles'].get(old_route)
        if predecessor is None and target.get('routing_profile') is None and original is None:
            # The predecessor was never routed: this is a new class to Maestro, so Dan's profile
            # ask applies, not a dead end (live rollout follow-up, fixed 2026-10-08).
            raise ProfileRequired(f'{backend}:{model}: limits are set, but its predecessor {old_id} has no '
                                  'routing profile; routing waits for its own explicit profile', unrouted=True)
    if previous is not None and predecessor is None:
        previous = old_id = None  # route on the target's own profile below
    if previous is not None:
        profile = original if original is not None and original.native_context_verified else predecessor
        if (not profile.native_context_verified and target.get('routing_profile') is None
                and not provider_verified):
            raise ProfileRequired(f'{backend}:{model}: limits are set; routing needs its own verified native '
                              'context capacity or explicit owner profile inputs')
        pair = (original_pair if profile is original else
                base['pair_profiles'][old_route, catalog.MIGRATION_DEFAULT_EFFORT])
        result = replace_route(policy, (backend,old_route), (backend,model), strength=pair.strength,
                               relative_cost=pair.relative_cost)
        # Persist the effective profile even for shipped targets: owner edits and
        # enforced windows must survive reconstruction from the immutable revision.
        native = profile.context_limits or base['context_limits']
        window = min(native.max_input_tokens, target['enforced_window'])
        dynamic = dict(backend_id=backend, model_id=model, predecessor_id=old_id,
                       strength=pair.strength, relative_cost=pair.relative_cost,
                       usage_pool_id=profile.usage_pool_id,
                       context_limits=dict(max_input_tokens=window, max_output_tokens=native.max_output_tokens,
                                           effective_context_ceiling=min(native.effective_context_ceiling, window)))
    else:
        if old_id is not None: raise PolicyError('same-backend predecessor profile required for this class')
        result = deepcopy(policy)
        if original is not None:
            if (not original.native_context_verified and target.get('routing_profile') is None
                    and not provider_verified):
                raise ProfileRequired(f'{backend}:{model}: limits are set; routing needs its own verified native '
                                  'context capacity or explicit owner profile inputs')
            row = dict(backend_id=backend,model_id=model,strength=original_pair.strength,
                       relative_cost=original_pair.relative_cost)
            native = original.context_limits or base['context_limits']
            window = min(native.max_input_tokens, target['enforced_window'])
            dynamic = dict(row, predecessor_id=None, usage_pool_id=original.usage_pool_id,
                           context_limits=dict(max_input_tokens=window, max_output_tokens=native.max_output_tokens,
                                               effective_context_ceiling=min(native.effective_context_ceiling, window)))
        else:
            grade = target.get('routing_profile')
            if not isinstance(grade,dict):
                raise ProfileRequired(f'{backend}:{model}: limits are set; routing waits for its own explicit profile. '
                                  f'Run: modelctl add {backend} --model {model} with --strength, '
                                  '--relative-cost and verified --max-output')
            row = dict(backend_id=backend,model_id=model,strength=grade['strength'],relative_cost=grade['relative_cost'])
            dynamic = dict(row, predecessor_id=None,
                           context_limits=dict(max_input_tokens=target['enforced_window'],
                                               max_output_tokens=grade['max_output_tokens'],
                                               effective_context_ceiling=target['lifecycle']['fresh'][0]))
        if not any(r['backend_id']==backend and r['model_id']==model for r in result['routes']):
            result['routes'].append(row)
        result['source'] = dict(kind='model_set',previous_policy=digest(policy),bootstrap=policy['source'])
    grade = target.get('routing_profile')
    reported = target.get('provider_limits') or {}
    if grade is not None:
        for route in result['routes']:
            if (route['backend_id'], route['model_id']) == (backend, model):
                route.update(strength=grade['strength'], relative_cost=grade['relative_cost'])
        if dynamic is not None:
            dynamic.update(strength=grade['strength'], relative_cost=grade['relative_cost'])
            dynamic['context_limits']['max_output_tokens'] = grade['max_output_tokens']
    if dynamic is not None:
        # Provider-verified facts replace inherited or catalog guesses; Dan's owner profile
        # (his max output, his native-window override) wins over them (Dan, 2026-10-08).
        limits = dynamic['context_limits']
        if (grade is None or 'max_output_tokens' not in grade) and reported.get('max_output_tokens'):
            limits['max_output_tokens'] = reported['max_output_tokens']
        native_window = (grade or {}).get('native_window_tokens') or reported.get('native_window_tokens')
        if native_window:
            limits['max_input_tokens'] = min(native_window, target['enforced_window'])
        # The admission ceiling follows this model's own lifecycle (start fresh), never an
        # inherited predecessor's (Haiku 5.5 kept Haiku 4.5's 120K under its 190K fresh range).
        limits['effective_context_ceiling'] = _admission_ceiling(target, limits['max_input_tokens'])
    older_ids = {ident for m in rows if m['backend_id'] == backend and m['family'] == target['family']
                 and tuple(m['version']) < tuple(target['version'])
                 for ident in [m['model_id'], *m.get('aliases', [])]}
    result['routes'] = [r for r in result['routes']
                        if r['backend_id'] != backend or r['model_id'] not in older_ids]
    from maestro.backends.registry import driver_name_for_backend_id
    driver = driver_name_for_backend_id(backend)
    for entry in result['roles'].values():
        if entry['models'].get(driver) in older_ids:
            entry['models'][driver] = model
    merged = {(m['backend_id'],m['model_id']): m for m in facts(policy)}
    merged.update({(m['backend_id'],m['model_id']): m for m in rows})
    result['source']['class_metadata'] = list(merged.values())
    inherited = deepcopy(policy['source'].get('inherited_profiles', []))
    # Pruned rows remain identity facts, but their profiles are no longer usable in
    # this new revision. Historical task revisions retain their original profiles.
    set_models = {(m['backend_id'], m['model_id']) for m in merged.values() if m['set']}
    inherited = [p for p in inherited if (p['backend_id'], p['model_id']) in set_models]
    if dynamic is not None:
        inherited = [p for p in inherited if (p['backend_id'],p['model_id']) != (backend,model)]
        inherited.append(dynamic)
    result['source']['inherited_profiles'] = inherited
    from maestro.model_replacement import _lifecycle
    defaults = catalog_defaults(result, {})
    native_base = next(b for b in defaults.values() if b['backend_id'] == backend)
    native = native_base['model_profiles'][model].context_limits or native_base['context_limits']
    _lifecycle(target.get('lifecycle'), native)
    validate(result)
    return result


def _policy_for_set_pairs(policy, event, bridge, owner, target, approved):
    """Reuse verified owner lifecycle handling while retaining pair-specific judgments.

    Owner model-wide grades cannot invent or overwrite an effort row. Dan's completed Telegram
    approval decides a model with no routes; without one, a same-class successor inherits its
    predecessor's pairs and a new class waits for approval.
    """
    from copy import deepcopy
    from maestro.backends.catalog import class_of
    from maestro.model_policy import validate
    backend, model = event['provider'], event['model_id']
    target_ids = {model, *target.get('aliases', [])}
    pairs = [dict(deepcopy(r), model_id=model) for r in policy['routes']
             if r['backend_id'] == backend and r['model_id'] in target_ids]
    if not pairs:
        pairs = [deepcopy(r) for r in approved]
    if not pairs:
        eligible = {r['model_id'] for r in policy['routes'] if r['backend_id'] == backend}
        predecessors = [m for m in owner['models'] if m['backend_id'] == backend
                        and m['family'] == target['family'] and tuple(m['version']) < tuple(target['version'])
                        and eligible.intersection([m['model_id'], *m.get('aliases', [])])]
        if predecessors:
            previous = max(predecessors, key=lambda m: tuple(m['version']))
            identities = {previous['model_id'], *previous.get('aliases', [])}
            pairs = [dict(deepcopy(r), model_id=model) for r in policy['routes']
                     if r['backend_id'] == backend and r['model_id'] in identities]
        else:
            raise ProfileRequired(f'{backend}:{model}: routing waits for approval of its effort pairs', unrouted=True)
    if any((r['backend_id'], r['model_id']) != (backend, model) for r in pairs):
        raise PolicyError('pair approval crossed models or backends')
    # The existing schema 1 path verifies owner facts, native context and publication
    # provenance. Its model grade is a compatibility input, never the schema 2 route.
    legacy = deepcopy(policy)
    legacy.update(schema=1, roles={})
    representatives = {(r['backend_id'], r['model_id']): r for r in policy['routes']}
    legacy['routes'] = [dict(backend_id=r['backend_id'], model_id=r['model_id'],
                             strength=class_of(r['intelligence']), relative_cost=r['relative_cost'])
                        for r in representatives.values()]
    changed = policy_for_set(legacy, event, bridge, owner=owner)
    if changed == legacy:
        return deepcopy(policy)
    remaining = {(r['backend_id'], r['model_id']) for r in changed['routes']}
    changed.update(schema=2, roles=deepcopy(policy['roles']))
    changed['routes'] = [deepcopy(r) for r in policy['routes']
                         if (r['backend_id'], r['model_id']) in remaining
                         # The compatibility path may replace an older member while
                         # retaining a shipped alias of the current member. All current
                         # identities are represented by the canonical pairs below.
                         and not (r['backend_id'] == backend and r['model_id'] in target_ids)] + pairs
    changed['source'].update(previous_policy=digest(policy), bootstrap=deepcopy(policy['source']))
    validate(changed)
    return changed



def _read_switch(path):
    try:
        record = json.loads(path.read_text())
        if not isinstance(record, dict): raise ValueError()
        validate_set_event(record.get('event'))
        ident = hashlib.sha256(canonical(record['event']['event_id'])).hexdigest()
        if (record.get('switch_id') != ident or path.stem != ident
            or record.get('destination') != [record['event']['provider'], record['event']['model_id']]
            or record.get('status') not in ('prepared', 'applied', 'unchanged', 'superseded')
            or type(record.get('notification_pending')) is not bool):
            raise ValueError()
        if (record['status'] == 'prepared' and not record['notification_pending']
            or record['status'] in ('unchanged', 'superseded') and record['notification_pending']):
            raise ValueError()
        for key in (('planned_revision',) if record['status'] == 'prepared'
                    else ('applied_revision',) if record['status'] == 'applied' else ()):
            value = record.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError()
        return record
    except (OSError, ValueError, TypeError, KeyError, PolicyError) as exc:
        raise PolicyError('unreadable or invalid switch journal') from exc


def switches(store):
    return [_read_switch(path) for path in sorted((store.home / 'switches').glob('*.json'))]


def _settle_set_proposals(store, destination, revision):
    """Caller holds policy lock and has proved this SET is published already."""
    for proposal in proposals(store):
        if proposal['destination'] == destination and proposal['status'] in _OPEN + ('waiting_limits',):
            proposal.update(status='applied', applied_revision=revision, notification_pending=False)
            _save(store, proposal)


def consume_set(store, bridge, event, *, reconcile=False):
    """Journal before publication; replay trusts the published policy history.

    `reconcile` re-derives an already routed current model from its owner row (a synthetic
    SET, see `_reconcile`): it only ever republishes that model, and no-ops leave no record."""
    validate_set_event(event)
    from pathlib import Path
    from maestro.model_lifecycle import preview_base, publish_and_adopt
    from maestro.model_inventory import registry, _adopt_locked
    ident = hashlib.sha256(canonical(event['event_id'])).hexdigest()
    path = store.home / 'switches' / (ident + '.json')
    with store.locked():
        record = _read_switch(path) if path.exists() else None
        if record is not None:
            if record['event'] != event: raise PolicyError('set event identity changed')
            if record['status'] in ('applied', 'unchanged', 'superseded'):
                if record['status'] in ('applied', 'unchanged'):
                    _settle_set_proposals(store, record['destination'],
                                          record.get('applied_revision') or store.current())
                if record['status'] == 'applied':
                    for project in registry(store):
                        _adopt_locked(Path(project['repo']), store)
                return 0
            if record['planned_revision'] in _published_history(store):
                record.update(status='applied', applied_revision=record['planned_revision'])
                if reconcile:
                    record.update(notification_pending=False, notification=_RECONCILE_NOTE)
                atomic_write(path, canonical(record))
                _settle_set_proposals(store, record['destination'], record['planned_revision'])
                for project in registry(store):
                    _adopt_locked(Path(project['repo']), store)
                return 0
        revision, policy = store.snapshot()
        preview = preview_base(store, revision)
        identities = _owner_identities(store, policy, event['provider'])
        owner = bridge.configured_models(event['provider'], identities=identities)
        try:
            from maestro.model_asks import approved_pairs
            changed = policy_for_set(policy, event, bridge, owner=owner,
                                     approved_pairs=approved_pairs(store, event['provider'], event['model_id']))
        except SupersededSet as exc:
            if reconcile:
                return 0  # a newer member or row arrived; its own SET switches it
            atomic_write(path, canonical(dict(switch_id=ident, event=event, status='superseded', reason=str(exc),
                         destination=[event['provider'], event['model_id']], notification_pending=False)))
            return 0
        if digest(changed) == digest(policy) or reconcile and _reroutes(policy, changed):
            if reconcile:
                return 0  # nothing to re-derive, or not a re-derivation: no journal record
            atomic_write(path, canonical(dict(switch_id=ident, event=event, status='unchanged',
                         destination=[event['provider'], event['model_id']], notification_pending=False)))
            _settle_set_proposals(store, [event['provider'], event['model_id']], revision)
            return 0
        preview['policy'] = changed
        owner_hash = digest(owner)
        record = dict(switch_id=ident, event=event, status='prepared', destination=[event['provider'],event['model_id']],
                      planned_revision=digest(changed), notification_pending=True)
        atomic_write(path, canonical(record))
    def revalidate():
        if digest(bridge.configured_models(event['provider'], identities=identities)) != owner_hash:
            raise PolicyError('owner limits changed before automatic publication')
    outcome = publish_and_adopt(preview, store, registry(store), check=revalidate)
    with store.locked():
        record.update(status='applied', applied_revision=outcome['revision'])
        if reconcile:
            record.update(notification_pending=False, notification=_RECONCILE_NOTE)
        atomic_write(path, canonical(record))
        _settle_set_proposals(store, record['destination'], outcome['revision'])
    return 1


_RECONCILE_PREFIX = 'reconcile:'
_RECONCILE_NOTE = 'settled: Maestro re-derived its own profile; no owner change to review'


def _owner_identities(store, policy, backend) -> list[str]:
    """The model ids to ask the owner about: this backend's routes plus every project's role choices."""
    from pathlib import Path
    from maestro.model_inventory import project_document, registry
    from maestro.model_policy import effective_roles
    from maestro.backends.registry import driver_name_for_backend_id
    driver = driver_name_for_backend_id(backend)
    choices = {r['model_id'] for r in policy['routes'] if r['backend_id'] == backend}
    for project in registry(store):
        try:
            effective, _ = effective_roles(project_document(Path(project['repo'])), None)
        except PolicyError:
            continue  # malformed projects are reported independently by adoption
        choices.update(entry['models'][driver] for entry in effective['roles'].values()
                       if driver in entry.get('models', {}))
    return sorted(choices)


def _reroutes(policy, changed) -> bool:
    """Whether a re-derivation would move roles or routes, which only a real SET or Dan may do."""
    def routes(p):
        return sorted((r['backend_id'], r['model_id']) for r in p['routes'])
    return changed['roles'] != policy['roles'] or routes(changed) != routes(policy)


def _reconcile(store, bridge, providers, result) -> None:
    """Re-derive every routed, current set model whose profile came from an owner SET (Dan, 2026-10-08).

    A change to Maestro's own derivation rules (provider facts, the admission ceiling) takes
    effect here, with no new MODEL_SET and no command: the daily `modelctl scan` runs consume.
    Each model that would change gets a synthetic SET through `consume_set`, so it publishes
    through the same crash-safe journal as a real switch; an unchanged model costs one
    `policy_for_set` and leaves no record. Unrouted or superseded rows are never touched (no
    asks), nor a model whose Telegram ask is applying (its own `modelctl add` SET follows).
    """
    if not hasattr(bridge, 'configured_models'):
        return  # a legacy owner: no class rows to re-derive from
    from maestro import model_asks
    from maestro.model_classes import validate_rows
    busy = {(r['backend'], r['model_id']) for r in model_asks.records(store) if r['status'] == 'applying'}
    wanted = set()
    for backend in sorted(providers):
        revision, policy = store.snapshot()
        try:
            owner = bridge.configured_models(backend, identities=_owner_identities(store, policy, backend))
            if not isinstance(owner, dict) or owner.get('contract') != 1:
                continue  # a legacy owner keeps its approval proposals
            rows = owner.get('models')
            validate_rows(rows)
        except PolicyError as exc:
            # Like discovery: no readable class rows means nothing to re-derive; the next scan retries.
            result.setdefault('reconcile_skipped', []).append(f'{backend}: {exc}')
            continue
        # Only profiles Maestro derived from an owner SET: a shipped route never consumed has no
        # derivation to redo, and its own SET keeps the actionable error when it lacks a profile.
        derived = {p['model_id'] for p in policy['source'].get('inherited_profiles', []) if p['backend_id'] == backend}
        routed = derived & {r['model_id'] for r in policy['routes'] if r['backend_id'] == backend}
        for row in rows:
            if (row.get('set') is not True or row.get('backend_id') != backend or row['model_id'] not in routed
                    or (backend, row['model_id']) in busy
                    or any(m.get('set') is True and m['family'] == row['family']
                           and tuple(m['version']) > tuple(row['version']) for m in rows)):
                continue
            revision, policy = store.snapshot()
            event = dict(kind='MODEL_SET', provider=backend, model_id=row['model_id'], family=row['family'],
                         version=row['version'], enforced_window=row['enforced_window'], predecessor_id=None,
                         event_id=f'{_RECONCILE_PREFIX}{backend}:{row["model_id"]}:{revision}:{digest(row)}')
            try:
                changed = policy_for_set(policy, event, bridge, owner=owner)
                if digest(changed) == digest(policy) or _reroutes(policy, changed):
                    continue
                wanted.add(event['event_id'])
                result['reconciled'] += consume_set(store, bridge, event, reconcile=True)
            except PolicyError as exc:
                result['errors'].append(f'reconcile {backend}:{row["model_id"]}: {exc}')
    # A crash between journal and publication leaves a prepared record that this pass no longer
    # computes: published means applied, otherwise the pass above already re-derived from scratch.
    with store.locked():
        history = None
        for record in switches(store):
            if (record['status'] != 'prepared' or record['event']['event_id'] in wanted
                    or not record['event']['event_id'].startswith(_RECONCILE_PREFIX)):
                continue
            history = _published_history(store) if history is None else history
            if record['planned_revision'] in history:
                record.update(status='applied', applied_revision=record['planned_revision'],
                              notification_pending=False, notification=_RECONCILE_NOTE)
            else:
                record.update(status='superseded', notification_pending=False)
                record.pop('planned_revision')
            atomic_write(store.home / 'switches' / (record['switch_id'] + '.json'), canonical(record))
