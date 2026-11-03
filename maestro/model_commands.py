"""Local entry point for the shared model use case."""
import json
from pathlib import Path

from maestro.model_policy import PolicyStore, PolicyError
from maestro.modelctl_bridge import ModelctlBridge
from maestro.model_inventory import register_project, candidate_projects, registry, adopt, adoption_status, project_report
from maestro.model_replacement import preview_replace, apply_preview
from maestro.model_detection import (consume, open_proposals, reconcile_proposals, review_proposal,
                                     decide_proposal, send_to_me, switches)
from maestro.model_lifecycle import preview_rollback, preview_retire, apply_policy_preview
from maestro import model_access, model_asks, model_sheet


def run(args) -> int:
    try:
        store = PolicyStore()
        revision, policy = store.snapshot()
        view = dict(revision=revision, source='shared' if revision else 'shipped',
                    roles=policy['roles'], routes=policy['routes'], retired=policy['retired'],
                    projects=registry(store), adoption=adoption_status(store),
                    project_models=[project_report(Path(row['repo']), store) for row in registry(store)],
                    class_metadata=policy['source'].get('class_metadata', []),
                    switches=switches(store), open_proposals=open_proposals(store),
                    open_asks=model_asks.open_asks(store))
        if args.verb == 'status':
            print(json.dumps(view, indent=2) if args.as_json else _bindings(policy) + '\n' + _adoption(view))
            return 0
        if args.verb == 'register':
            print(json.dumps(register_project(Path(args.repo or '.'), store), indent=2))
            return 0
        if args.verb == 'candidates':
            print(json.dumps(candidate_projects(Path(args.root)), indent=2))
            return 0
        if args.verb == 'adopt':
            print(json.dumps([adopt(Path(p['repo']), store) for p in registry(store)], indent=2))
            return 0
        if args.verb == 'consume':
            transport = model_asks.TelegramTransport.from_config()
            outcome = consume(store, ModelctlBridge(), notify=send_to_me, asks=True, transport=transport)
            access = model_access.reconcile(store, ModelctlBridge(), transport)
            if access.get('taken_out'):
                # A removed model's routes fail its backend's events, which can go through now. Consume
                # still runs first: a same-class switch needs its predecessor's routes in place.
                outcome = dict(consume(store, ModelctlBridge(), notify=send_to_me, asks=True, transport=transport),
                               first_pass=outcome)
            outcome['access'] = access
            outcome['sheet'] = model_sheet.sync_if_enabled(store, ModelctlBridge())
            print(json.dumps(outcome, indent=2))
            return 1 if outcome['errors'] else 0
        if args.verb == 'sheet':
            print(json.dumps(model_sheet.sync(store, ModelctlBridge()), indent=2))
            return 0
        if args.verb == 'asks':
            if args.action == 'process':
                outcome = model_asks.process(store, ModelctlBridge(), model_asks.TelegramTransport.from_config())
                if outcome.get('applied'):
                    outcome['sheet'] = model_sheet.sync_if_enabled(store, ModelctlBridge())
                print(json.dumps(outcome, indent=2))
                return 1 if outcome['errors'] or outcome['send_errors'] else 0
            if args.action is not None:
                raise PolicyError('asks takes no action or `process`')
            print(json.dumps(view['open_asks'], indent=2))
            return 0
        if args.verb == 'proposals':
            print(json.dumps(reconcile_proposals(store), indent=2))
            return 0
        if args.verb == 'review':
            return _review(store, args.proposal)
        if args.verb in ('rollback', 'retire'):
            if args.verb == 'rollback':
                preview, bridge = preview_rollback(store, args.revision), None
            else:
                if not args.backend:
                    raise PolicyError('retire needs --backend')
                bridge = ModelctlBridge()
                preview = preview_retire(store, bridge, args.backend)
            print(json.dumps({k: v for k, v in preview.items() if k != 'policy'}, indent=2))
            if preview['status'] != 'ready':
                return 0
            if input(f'Publish this {args.verb}? Type yes: ').strip().lower() != 'yes':
                return 0
            print(json.dumps(apply_policy_preview(preview, store, confirmed=True, bridge=bridge), indent=2))
            return 0
        print(_bindings(policy))
        pairs = sorted({(r['backend_id'], r['model_id']) for r in policy['routes']})
        for i, pair in enumerate(pairs, 1):
            print(f'{i}. {pair[0]}:{pair[1]}')
        choice = input('Choose a binding number or backend:model to replace, or q to cancel: ').strip()
        if choice.lower() in ('q', ''):
            return 0
        old = _choice(choice, pairs)
        backend = input('Replacement harness (claude/codex; antigravity discovery unsupported): ').strip()
        bridge = ModelctlBridge()
        metadata = bridge.discover(backend)
        candidates = metadata['candidates']
        for i, row in enumerate(candidates, 1):
            print(f'{i}. {row["backend_id"]}:{row["model_id"]} (metadata only; registered={row["registered"]})')
        new = _choice(input('Choose replacement number, or q to cancel: ').strip(),
                      [(r['backend_id'], r['model_id']) for r in candidates])
        if new is None:
            return 0
        profile = next((r for r in policy['routes'] if (r['backend_id'], r['model_id']) == new), None)
        strength = cost = None
        if profile is None:
            strength = input('New profile strength (light/balanced/strong): ').strip()
            cost = float(input('New profile relative cost (positive): ').strip())
        preview = preview_replace(store, bridge, old, new, strength=strength, relative_cost=cost)
        print(json.dumps(preview, indent=2))
        if preview['status'] != 'ready':
            return 1
        if input('Publish this shared replacement? Type yes: ').strip().lower() != 'yes':
            return 0
        try:
            outcome = apply_preview(preview, store, bridge, confirmed=True)
        except (PolicyError, OSError):
            _selection_report(preview, store, bridge)
            raise
        print(json.dumps(outcome, indent=2))
        return 0
    except (PolicyError, OSError, EOFError, ValueError) as exc:
        print(f'Model policy: {exc}')
        return 1


def _review(store, ident) -> int:
    if not ident:
        raise PolicyError('review needs --proposal <id> (see `maestro models proposals`)')
    bridge = ModelctlBridge()
    review = review_proposal(store, bridge, ident)
    if review['stale_policy']:
        print('Shared policy changed since detection; replacement candidates recomputed.')
    if review['preview'] is None:
        candidates = [tuple(c) for c in review['candidates']]
        if not candidates:
            print('No eligible route to replace; choose one with the argument-free picker.')
            return 1
        for i, pair in enumerate(candidates, 1):
            print(f'{i}. {pair[0]}:{pair[1]}')
        old = _choice(input('Choose the route this model replaces, or q to cancel: ').strip(), candidates)
        if old is None:
            return 0
        review = review_proposal(store, bridge, ident, old=old)
    preview = review['preview']
    print(json.dumps({k: v for k, v in preview.items() if k != 'policy'}, indent=2))
    if preview['status'] != 'ready':
        print(f'Registered: no. Selected: no. Run: {preview["registration_command"]}. '
              'Proposal kept open; compatible owners publish automatically once limits are set.')
        return 1
    answer = input('Apply this replacement? Type yes, reject, or anything else to keep it pending: ').strip().lower()
    if answer == 'reject':
        decide_proposal(store, bridge, ident, preview, approve=False)
        print('Proposal rejected; routing unchanged.')
        return 0
    if answer != 'yes':
        return 0
    try:
        outcome = decide_proposal(store, bridge, ident, preview, approve=True)
    except (PolicyError, OSError):
        _selection_report(preview, store, bridge)
        raise
    print(json.dumps(outcome, indent=2))
    return 0


def _selection_report(preview, store, bridge):
    """Report observed owner registration and publication after a failed selection."""
    from maestro.model_detection import _published_history
    from maestro.model_policy import digest
    registered = 'unknown'
    try:
        rows = bridge.discover(preview['new'][0])['candidates']
        row = next((r for r in rows if [r['backend_id'], r['model_id']] == preview['new']), None)
        if row is not None:
            registered = 'yes' if row.get('registered') else 'no'
    except (PolicyError, OSError, KeyError, TypeError):
        pass
    selected = 'unknown'
    try:
        selected = 'yes' if digest(preview['policy']) in _published_history(store) else 'no'
    except (PolicyError, OSError):
        pass
    print(f'Registered: {registered}. Selected: {selected}.')


def _adoption(view) -> str:
    lines = [f'{row["name"]}: {row["adoption"]}' for row in view['adoption']]
    lines += [f'proposal {p["proposal_id"][:12]} {":".join(p["destination"])} {p["status"]}'
              + (f' (modelctl add {p["destination"][0]} --model {p["destination"][1]})'
                 if p['status'] == 'waiting_limits' else f' (maestro models review --proposal {p["proposal_id"]})')
              for p in view['open_proposals']]
    lines += [f'ask {a["ask_id"]} {a["kind"]} {":".join(a["destination"])} {a["status"]}/{a["step"]}'
              + ('; message pending' if a['send_pending'] else '; waiting on Telegram') for a in view['open_asks']]
    lines += [f'switch {s["switch_id"][:12]} {":".join(s["destination"])} {s["status"]}; '
              + ('notice pending' if s['notification_pending'] else 'notice settled') for s in view['switches']]
    for project in view['project_models']:
        for role, choices in project.get('class_choices', {}).items():
            lines += [f'{project["repo"]} {role}.{driver}: class {choice["family"] or choice["choice"]}'
                      f' -> {choice["model"]}' for driver, choice in choices.items()]
    return '\n'.join(lines)


def _bindings(policy) -> str:
    if policy['schema'] == 2:
        return '\n'.join(f'{i}. {role}: intelligence >= {entry["min_intelligence"]}; '
                         f'effort {entry["effort"]} ({entry["effort_rule"]})'
                         for i, (role, entry) in enumerate(sorted(policy['roles'].items()), 1))
    return '\n'.join(f'{i}. {role}: {entry["backend"]}:{entry["models"].get(entry["backend"], "unspecified")}'
                     for i, (role, entry) in enumerate(sorted(policy['roles'].items()), 1))


def _choice(raw, pairs):
    if raw.lower() in ('q', ''):
        return None
    try:
        pair = pairs[int(raw) - 1] if raw.isdecimal() and int(raw) > 0 else tuple(raw.split(':', 1))
    except IndexError as exc:
        raise PolicyError('invalid model selection') from exc
    if pair not in pairs:
        raise PolicyError('invalid qualified model selection')
    return pair
