"""Preview, explicit confirmation, one publication, independent project adoption."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from maestro import thirdparty
from maestro.backends import catalog
from maestro.model_inventory import registry, preview_project
from maestro.model_lifecycle import preview_base, verify_unchanged, publish_and_adopt
from maestro.model_policy import PolicyError, digest, replace_route, catalog_defaults, shipped_policy


def _candidate(metadata, pair):
    for row in metadata.get('candidates', []):
        if (row['backend_id'], row['model_id']) == pair:
            return row
    raise PolicyError(f'{pair[0]}:{pair[1]} unavailable; metadata failure never means retirement')


def _lifecycle(row, native):
    try:
        prepare, fresh, ceiling = row['prepare'], row['fresh'], row['ceiling']
        if not (0 < prepare[0] <= prepare[1] < fresh[0] <= fresh[1] < ceiling <= native.max_input_tokens):
            raise ValueError()
    except (TypeError, KeyError, IndexError, ValueError) as exc:
        raise PolicyError('unsafe or missing provider lifecycle row/native window') from exc


def preview_replace(store, bridge, old, new, *, strength=None, relative_cost=None) -> dict:
    revision, policy = store.snapshot()
    original = policy
    metadata = bridge.discover(new[0])
    candidate = _candidate(metadata, new)
    projects = registry(store)
    preview = dict(preview_base(store, revision), old=list(old), new=list(new),
                   metadata_hash=digest(metadata), status='ready',
                   registered=bool(candidate.get('registered')), selected=False)
    if not candidate.get('registered'):
        suggestion = metadata.get('unknown')
        predecessor = None
        if hasattr(bridge, 'configured_models'):
            try:
                owner = bridge.configured_models(new[0])
            except PolicyError:
                owner = None  # old owners still support a read-only fallback preview
            from maestro.model_classes import validate_rows
            if owner is not None and (not isinstance(owner, dict) or owner.get('contract') != 1):
                raise PolicyError('incompatible modelctl class contract')
            if owner is not None:
                validate_rows(owner.get('models'))
            matches = [r for r in (owner['models'] if owner is not None else []) if r['backend_id'] == new[0]
                       and r['family'] == candidate['family'] and r['set']]
            if matches:
                predecessor = max(matches, key=lambda r: tuple(r['version']))
                suggestion = predecessor.get('lifecycle')
        if not suggestion:
            raise PolicyError('no usable provider Model unknown fallback row; registration refused')
        preview.update(status='registration_required', suggested_limits=suggestion,
                       registration_command=f'modelctl add {new[0]} --model {new[1]}',
                       registration_kind='existing_class' if predecessor else 'new_class',
                       suggested_window=predecessor['enforced_window'] if predecessor else None,
                       notice='Provider Model unknown values are suggestions. Accept or customize before registration. '
                              'Register through modelctl add. Compatible owners publish set models automatically; '
                              'projects adopt between tasks. Explicit role changes remain in the Maestro picker.')
        if predecessor:
            preview['notice'] = ('Existing classes inherit their current limits and enforced window automatically '
                                 'through modelctl scan. Use modelctl add to review or adjust them. '
                                 'Compatible owners publish set models automatically; projects adopt between tasks.')
        return preview
    profiles = {(r['backend_id'], r['model_id']): r for r in shipped_policy()['routes']}
    if policy['schema'] == 2:
        if not any((r['backend_id'], r['model_id']) == new for r in policy['routes']):
            approved = [deepcopy(r) for r in shipped_policy()['routes']
                        if 'effort' in r and (r['backend_id'], r['model_id']) == new]
            if not approved:
                raise PolicyError('destination requires approved effort pairs; use its Telegram ask')
            policy = deepcopy(policy)
            policy['routes'].extend(approved)
    elif strength is None:
        from maestro.model_policy import shipped_policy_v1
        profiles = {(r['backend_id'], r['model_id']): r for r in shipped_policy_v1()['routes']}
        if new not in profiles:
            raise PolicyError('new destination requires its own strength/cost profile')
        strength = profiles[new]['strength']
        relative_cost = profiles[new]['relative_cost'] if relative_cost is None else relative_cost
    changed = replace_route(policy, old, new, strength=strength, relative_cost=relative_cost)
    if policy is not original:
        changed['source'].update(previous_policy=digest(original), bootstrap=deepcopy(original['source']))
    # Verify destination using its own register facts and native profile, never the old route.
    verified = catalog.catalog_from_register(thirdparty.load_register(), defaults=catalog_defaults(changed, {}))
    entry = verified.entries.get(new[0])
    if entry is None or new[1] not in entry.available_models:
        raise PolicyError('destination backend/model lacks verified support')
    _lifecycle(candidate.get('lifecycle'), entry.profile(new[1]).context_limits or entry.context_limits)
    preview['policy'] = changed
    preview['projects'] = [preview_project(Path(p['repo']), changed, store) for p in projects]
    return preview


def apply_preview(preview: dict, store, bridge, *, confirmed: bool) -> dict:
    if not confirmed:
        raise PolicyError('explicit switch confirmation required')
    if preview['status'] != 'ready':
        raise PolicyError('registration required: use the modelctl owner add command; routing unchanged')
    projects = verify_unchanged(preview, store)
    def verify_provider():
        metadata = bridge.discover(preview['new'][0])
        if digest(metadata) != preview['metadata_hash']:
            raise PolicyError('provider metadata or lifecycle table changed since preview')

    verify_provider()
    return publish_and_adopt(preview, store, projects, check=verify_provider)
