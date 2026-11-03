"""Rollback and owner-signalled retirement: preview, explicit confirmation, one
publication, then independent idle-boundary adoption (same path as replacement)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from maestro.backends import catalog
from maestro.model_inventory import registry, preview_project, adopt
from maestro.model_policy import PolicyError, digest, retire_routes, rollback_policy, role_references


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return 'unreadable'


def preview_base(store, revision) -> dict:
    """Staleness inputs checked again immediately before publication."""
    projects = registry(store)
    return dict(expected_revision=revision, registry_hash=digest(projects),
                config_hashes={p['repo']: _file_hash(Path(p['repo']) / 'project.yaml') for p in projects})


def verify_unchanged(preview: dict, store) -> list[dict]:
    if store.current() != preview['expected_revision']:
        raise PolicyError('shared policy changed since preview')
    projects = registry(store)
    if digest(projects) != preview['registry_hash']:
        raise PolicyError('project registry changed since preview')
    for project in projects:
        if _file_hash(Path(project['repo']) / 'project.yaml') != preview['config_hashes'][project['repo']]:
            raise PolicyError('project configuration changed since preview')
    return projects


def publish_and_adopt(preview: dict, store, projects, *, check=None) -> dict:
    # Registration also takes the policy lock, so the registry cannot move after this check.
    def verify_publication():
        verify_unchanged(preview, store)
        if check is not None:
            check()

    revision = store.publish(preview['policy'], expected=preview['expected_revision'],
                             check=verify_publication)
    # Each project adopts independently; a deferred project is reconciled by a later `adopt`.
    return dict(revision=revision, projects=[adopt(Path(p['repo']), store) for p in projects])


def _with_projects(preview: dict, store) -> dict:
    preview['projects'] = [preview_project(Path(p['repo']), preview['policy'], store) for p in registry(store)]
    return preview


def preview_rollback(store, target: str | None = None) -> dict:
    revision, current = store.snapshot()
    if revision is None:
        raise PolicyError('no shared policy published; nothing to roll back')
    if target is None:
        target = current['source'].get('previous_policy')
        if target is None:
            raise PolicyError('current policy records no previous revision; name one explicitly')
    policy = rollback_policy(current, store.load_any(target), target)
    preview = dict(preview_base(store, revision), kind='rollback', target=target,
                   policy=policy, status='ready')
    return _with_projects(preview, store)


def preview_retire(store, bridge, backend: str) -> dict:
    """Only the owner's explicit removal signal retires; a failed read raises (unknown, not retired)."""
    revision, current = store.snapshot()
    owner = bridge.retired(backend)
    removed = set(owner['removed'])
    # The owner reports class IDs; an older policy can still route a native alias.
    # Resolve only identities recorded in this immutable policy and on this backend.
    from maestro.model_classes import facts
    explicit_removed = frozenset(removed)
    for row in facts(current):
        identities = {row['model_id'], *row.get('aliases', [])}
        if row['backend_id'] == backend and explicit_removed.intersection(identities):
            removed.update(identities)
    # Shared routes, and verified models still reachable through explicit pins
    # (superseded or pin-only), both retire on the owner's signal.
    reachable = {r['model_id'] for r in current['routes'] if r['backend_id'] == backend}
    reachable |= {m for base in catalog.SHIPPED_CATALOG_DEFAULTS.values()
                  if base['backend_id'] == backend for m in base['available_models']}
    # A model out on an access hold can still return, so retiring it must reach it too.
    from maestro.model_access import _load as load_holds  # it imports this module
    for hold in load_holds(store)['held'].values():
        if hold['backend'] == backend:
            reachable.add(hold['model_id'])
            if explicit_removed.intersection(hold.get('removed', ())):
                removed.add(hold['model_id'])
    retired = {tuple(p) for p in current['retired']}
    eligible = sorted((backend, m) for m in removed & reachable if (backend, m) not in retired)
    blocked = [dict(route=list(p), roles=role_references(current, p)) for p in eligible if role_references(current, p)]
    retire = [p for p in eligible if not role_references(current, p)]
    preview = dict(preview_base(store, revision), kind='retire', backend=backend,
                   owner_state_hash=owner['state_hash'], retire=[list(p) for p in retire],
                   replacement_required=blocked, status='ready' if retire else 'nothing_to_retire')
    if retire:
        preview['policy'] = retire_routes(current, retire)
        _with_projects(preview, store)
    return preview


def apply_policy_preview(preview: dict, store, *, confirmed: bool, bridge=None) -> dict:
    if not confirmed:
        raise PolicyError('explicit confirmation required')
    if preview['status'] != 'ready':
        raise PolicyError(f'nothing to publish ({preview["status"]})')
    projects = verify_unchanged(preview, store)
    def verify_owner():
        if preview['kind'] == 'retire':
            if bridge is None or bridge.retired(preview['backend'])['state_hash'] != preview['owner_state_hash']:
                raise PolicyError('modelctl removal state changed since preview')

    verify_owner()
    return publish_and_adopt(preview, store, projects, check=verify_owner)
