"""Immutable shared routing policy. Provider lifecycle numbers stay in modelctl.

Consumers use the project's adopted revision, never the moving host pointer.
Publication and project adoption are deliberately separate transactions.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import inspect
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping


class PolicyError(ValueError):
    pass


#: Bump whenever the adopted-revision record format changes. 1 read only the pointer file;
#: 2 reads the control store's adoption event first (`adopted_policy`). An installed source
#: 3 resolves class choices and inherited profiles; 4 also freezes project/task choices
#: and serializes adoption/freezing with pruning; 5 reads a schema 2 row's `intelligence`
#: and the role requirements. Other readers require upgrade.
MODEL_POLICY_READER_VERSION = 5
#: Policy document schemas `validate` reads: 1 grades a model, 2 grades a (model, effort) pair.
POLICY_SCHEMAS = (1, 2)


def policy_home() -> Path:
    return Path(os.environ.get('MAESTRO_MODELS_HOME') or Path.home() / '.maestro/models')


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _score(value) -> bool:
    """A pair intelligence or a role's floor: a whole number from 0 to 100."""
    return type(value) is int and 0 <= value <= 100


#: What a schema 2 policy states per role (DESIGN.md §17.2): the lowest pair intelligence
#: the role accepts, an effort, and whether that effort is a floor or only a preference.
ROLE_REQUIREMENT_KEYS = frozenset({'min_intelligence', 'effort', 'effort_rule'})
EFFORT_RULES = ('minimum', 'preference')


def role_requirement_issues(roles: Mapping) -> list[str]:
    """What a schema 2 policy's `roles` table states that a role requirement does not allow."""
    from maestro.backends.catalog import CANONICAL_EFFORTS

    issues = []
    for role, entry in roles.items():
        if not _text(role) or not isinstance(entry, Mapping) or set(entry) != ROLE_REQUIREMENT_KEYS:
            issues.append(f'roles.{role}: must state exactly {", ".join(sorted(ROLE_REQUIREMENT_KEYS))}')
            continue
        if not _score(entry['min_intelligence']):
            issues.append(f'roles.{role}.min_intelligence must be a whole number from 0 to 100')
        if entry['effort'] not in CANONICAL_EFFORTS:
            issues.append(f'roles.{role}.effort {entry["effort"]!r} is not one of '
                          f'{", ".join(CANONICAL_EFFORTS)}')
        if entry['effort_rule'] not in EFFORT_RULES:
            issues.append(f'roles.{role}.effort_rule {entry["effort_rule"]!r} is not one of '
                          f'{", ".join(EFFORT_RULES)}')
    return issues


def validate(policy: dict) -> None:
    from maestro.backends.catalog import MODEL_STRENGTHS
    from maestro.backends import registry

    if not isinstance(policy, dict) or policy.get('schema') not in POLICY_SCHEMAS:
        raise PolicyError('unsupported model policy schema')
    paired = policy['schema'] == 2  # a route row is a (model, effort) pair (DESIGN.md §17.3)
    if set(policy) != {'schema', 'source', 'routes', 'roles', 'retired'}:
        raise PolicyError('unexpected model policy fields; lifecycle limits belong to modelctl')
    if not isinstance(policy['source'], dict) or not policy['source']:
        raise PolicyError('policy needs source provenance')
    if not isinstance(policy['routes'], list) or not policy['routes']:
        raise PolicyError('policy needs eligible routes')
    # Schema 1 grades the model with a class. Schema 2 scores the pair: the class is derived
    # from `intelligence` (`catalog.class_of`) and a row never states one.
    row_keys = ({'backend_id', 'model_id', 'effort', 'intelligence', 'relative_cost'} if paired
                else {'backend_id', 'model_id', 'strength', 'relative_cost'})
    seen, seen_pairs = set(), set()
    for row in policy['routes']:
        if not isinstance(row, dict) or set(row) - ({'provisional'} if paired else set()) != row_keys:
            raise PolicyError('invalid route profile')
        if type(row.get('provisional', False)) is not bool:
            raise PolicyError('invalid route profile: provisional must be true or false')
        pair = (row['backend_id'], row['model_id'])
        effort = row.get('effort')
        if effort is not None and not _text(effort):
            raise PolicyError('invalid route effort')
        if not all(_text(v) for v in pair) or (*pair, effort) in seen_pairs:
            raise PolicyError('empty or duplicate qualified route')
        try:
            registry.driver_name_for_backend_id(pair[0])
        except registry.UnknownBackend as exc:
            raise PolicyError(f'unknown backend {pair[0]}') from exc
        cost = row['relative_cost']
        graded = (_score(row['intelligence']) if paired else row['strength'] in MODEL_STRENGTHS)
        if (not graded or isinstance(cost, bool)
                or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0):
            raise PolicyError(f'invalid {"intelligence" if paired else "strength"}/cost profile')
        seen.add(pair)
        seen_pairs.add((*pair, effort))
    from maestro.model_classes import validate_facts
    validate_facts(policy)
    retired = policy['retired']
    if not isinstance(retired, list) or any(
        not isinstance(p, list) or len(p) != 2 or not all(_text(v) for v in p) for p in retired
    ):
        raise PolicyError('invalid retired routes')
    if seen & {tuple(p) for p in retired}:
        raise PolicyError('retired route cannot be eligible')
    if not isinstance(policy['roles'], dict):
        raise PolicyError('invalid role preferences')
    if paired:
        if issues := role_requirement_issues(policy['roles']):
            raise PolicyError('invalid role: ' + '; '.join(issues))
        return
    for role, entry in policy['roles'].items():
        if not _text(role) or not isinstance(entry, dict) or set(entry) != {'backend', 'models'}:
            raise PolicyError('invalid role preference')
        if entry['backend'] not in registry.known_backends() or not isinstance(entry['models'], dict):
            raise PolicyError('invalid role backend/models')
        for driver, model in entry['models'].items():
            if driver not in registry.known_backends() or not _text(model):
                raise PolicyError('invalid role model')
            backend = registry.driver_ref(driver).backend_id
            if (backend, model) not in seen:
                raise PolicyError(f'{role}: preferred route {backend}:{model} is not eligible')


def shipped_policy() -> dict:
    """The approved pair inventory, shared by bootstrap and publication."""
    path = Path(__file__).with_name('model_pairs.json')
    payload = path.read_bytes()
    table = json.loads(payload)
    policy = dict(schema=2, source={
        'kind': 'shipped', 'pairs_sha256': hashlib.sha256(payload).hexdigest(),
    }, routes=table['routes'], roles=table['roles'], retired=[])
    validate(policy)
    return policy


def shipped_policy_v1() -> dict:
    """Compatibility source for schema 1 migration and initial-source preservation."""
    from maestro import roles
    from maestro.backends import catalog, registry

    legacy_models = {
        'claude': {'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'},
        'codex': {'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna'},
        'antigravity_cli': catalog.PIN_ONLY_MODELS['antigravity_cli'],
    }
    routes = []
    for base in catalog.SHIPPED_CATALOG_DEFAULTS.values():
        for model in base['available_models']:
            if model not in legacy_models[base['backend_id']]:
                continue
            pair = base['pair_profiles'][model, catalog.MIGRATION_DEFAULT_EFFORT]
            routes.append(dict(backend_id=base['backend_id'], model_id=model,
                               strength=pair.strength, relative_cost=pair.relative_cost))
    defaults = {role: dict(backend=registry.DEFAULT_BACKEND, models=dict(models))
                for role, models in roles.DEFAULT_MODELS.items()}
    return dict(schema=1, source={
        'kind': 'shipped',
        'catalog_sha256': hashlib.sha256(Path(catalog.__file__).read_bytes()).hexdigest(),
        'roles_sha256': hashlib.sha256(Path(roles.__file__).read_bytes()).hexdigest(),
    }, routes=routes, roles=defaults, retired=[])


def _schema_1_tool(*policies: dict) -> None:
    """Role assignment applies only to a schema 1 preference table."""
    for policy in policies:
        validate(policy)
        if policy['schema'] != 1:
            raise PolicyError('this tool assigns schema 1 role preferences; schema 2 roles state requirements')


def replace_route(policy: dict, old: tuple[str, str], new: tuple[str, str], *,
                  strength: str | None = None, relative_cost: float | None = None) -> dict:
    from maestro.backends import registry

    validate(policy)
    rows = {(row['backend_id'], row['model_id']): row for row in policy['routes']}
    if old not in rows:
        raise PolicyError('selected old route is no longer eligible')
    if list(new) in policy['retired']:
        raise PolicyError('destination route is retired')
    if old == new:
        return deepcopy(policy)
    destination = rows.get(new)
    if policy['schema'] == 2:
        result = deepcopy(policy)
        result['routes'] = [r for r in result['routes'] if (r['backend_id'], r['model_id']) != old]
        if destination is None:
            from maestro.model_classes import facts
            known = {(m['backend_id'], m['model_id']): m for m in facts(policy)}
            predecessor, successor = known.get(old), known.get(new)
            if not (old[0] == new[0] and predecessor and successor
                    and predecessor['family'] == successor['family'] and successor['set']
                    and tuple(successor['version']) > tuple(predecessor['version'])):
                raise PolicyError('destination requires approved effort pairs or a verified same-class successor')
            result['routes'].extend(dict(deepcopy(r), backend_id=new[0], model_id=new[1])
                                    for r in policy['routes'] if (r['backend_id'], r['model_id']) == old)
        result['source'] = dict(deepcopy(policy['source']), kind='replacement',
                                previous_policy=digest(policy), bootstrap=deepcopy(policy['source']))
        validate(result)
        return result
    if destination is None:
        if strength is None or relative_cost is None:
            raise PolicyError('new destination requires its own strength/cost profile')
        destination = dict(backend_id=new[0], model_id=new[1], strength=strength, relative_cost=relative_cost)
    result = deepcopy(policy)
    result['routes'] = [r for r in result['routes'] if (r['backend_id'], r['model_id']) != old]
    if not any((r['backend_id'], r['model_id']) == new for r in result['routes']):
        result['routes'].append(deepcopy(destination))
    old_driver = registry.driver_name_for_backend_id(old[0])
    new_driver = registry.driver_name_for_backend_id(new[0])
    for entry in result['roles'].values():
        if entry['models'].get(old_driver) == old[1]:
            del entry['models'][old_driver]
            entry['models'][new_driver] = new[1]
            if entry['backend'] == old_driver:
                entry['backend'] = new_driver
    result['source'] = {'kind': 'replacement', 'previous_policy': digest(policy),
                        'bootstrap': policy['source']}
    for key in ('class_metadata', 'inherited_profiles'):
        if key in policy['source']:
            result['source'][key] = deepcopy(policy['source'][key])
    validate(result)
    return result


def assign_roles(policy: dict, pair: tuple[str, str], roles: list[str]) -> dict:
    """Prefer an eligible route for `roles` on its own backend; every other route stays eligible.

    Unlike `replace_route`, the change is per role: other roles keep their preference.
    """
    from maestro.backends import registry

    _schema_1_tool(policy)
    if not any((r['backend_id'], r['model_id']) == tuple(pair) for r in policy['routes']):
        raise PolicyError(f'{pair[0]}:{pair[1]} is not an eligible route')
    unknown = sorted(set(roles) - set(policy['roles']))
    if unknown:
        raise PolicyError(f'unknown shared roles: {", ".join(unknown)}')
    driver = registry.driver_name_for_backend_id(pair[0])
    result = deepcopy(policy)
    for role in roles:
        result['roles'][role]['models'][driver] = pair[1]
    result['source'] = {'kind': 'role_assignment', 'previous_policy': digest(policy),
                        'bootstrap': policy['source']}
    for key in ('class_metadata', 'inherited_profiles'):
        if key in policy['source']:
            result['source'][key] = deepcopy(policy['source'][key])
    validate(result)
    return result


def role_references(policy: dict, pair: tuple[str, str]) -> list[str]:
    """Shared roles whose preferences name `pair`."""
    from maestro.backends import registry
    if policy['schema'] == 2:
        return []
    return sorted(role for role, entry in policy['roles'].items()
                  if any(registry.driver_ref(d).backend_id == pair[0] and m == pair[1]
                         for d, m in entry['models'].items()))


def retire_routes(policy: dict, pairs: list[tuple[str, str]]) -> dict:
    """Retire unreferenced routes. A referenced route needs a replacement first, never a guess."""
    validate(policy)
    for pair in pairs:
        if roles := role_references(policy, pair):
            raise PolicyError(f'{pair[0]}:{pair[1]} is preferred by {", ".join(roles)}; replace it first')
    result = deepcopy(policy)
    gone = {tuple(p) for p in pairs}
    result['routes'] = [r for r in result['routes'] if (r['backend_id'], r['model_id']) not in gone]
    result['retired'] = sorted({tuple(p) for p in result['retired']} | gone)
    result['retired'] = [list(p) for p in result['retired']]
    result['source'] = {'kind': 'retirement', 'previous_policy': digest(policy), 'bootstrap': policy['source']}
    for key in ('class_metadata', 'inherited_profiles'):
        if key in policy['source']:
            result['source'][key] = deepcopy(policy['source'][key])
    validate(result)
    return result


def rollback_policy(current: dict, target: dict, target_revision: str) -> dict:
    """A validated prior policy as a new revision. Retirement since then still wins."""
    validate(current)
    validate(target)
    retired ={tuple(p) for p in current['retired']} | {tuple(p) for p in target['retired']}
    result = deepcopy(target)
    result['routes'] = [r for r in result['routes'] if (r['backend_id'], r['model_id']) not in retired]
    result['retired'] = [list(p) for p in sorted(retired)]
    for pair in sorted(retired):
        if roles := role_references(result, pair):
            raise PolicyError(f'rollback target prefers retired {pair[0]}:{pair[1]} for {", ".join(roles)}')
    result['source'] = {'kind': 'rollback', 'restored': target_revision,
                        'previous_policy': digest(current), 'bootstrap': target['source']}
    for key in ('class_metadata', 'inherited_profiles'):
        if key in target['source']:
            result['source'][key] = deepcopy(target['source'][key])
    validate(result)
    return result


def effective_roles(project: Mapping, policy: dict | None) -> tuple[dict, dict]:
    """Merge by presence, retaining explicit provenance even when values match."""
    from maestro import roles

    if not isinstance(project, Mapping):
        raise PolicyError('project configuration must be a mapping')
    result = deepcopy(dict(project))
    configured = project.get('roles') or {}
    if not isinstance(configured, Mapping):
        raise PolicyError('project roles must be a mapping')
    shared = policy['roles'] if policy is not None else shipped_policy()['roles']
    paired = (policy if policy is not None else shipped_policy())['schema'] == 2
    entries = {}
    provenance = {}
    for role in set(shared) | set(configured):
        if not _text(role):
            raise PolicyError('invalid role name')
        if paired:
            raw = configured.get(role, {})
            if not isinstance(raw, Mapping):
                raise PolicyError(f'{role}: invalid role entry')
            entries[role] = {**deepcopy(shared.get(role) or {}),
                             **{key: deepcopy(raw[key]) for key in ROLE_KEYS & set(raw)}}
            provenance[role] = {}
            continue
        # Historical schema 1 role preferences and project overrides remain readable.
        base = {'backend': roles.registry.DEFAULT_BACKEND, 'models': {},
                **deepcopy(shared.get(role) or {})}
        origin = {k: 'shared' if policy is not None else 'shipped' for k in base['models']}
        raw = configured.get(role, {})
        if isinstance(raw, str):
            raw = {'backend': raw}
        if not isinstance(raw, Mapping):
            raise PolicyError(f'{role}: invalid role entry')
        if 'backend' in raw:
            if not _text(raw['backend']):
                raise PolicyError(f'{role}: invalid role backend')
            base['backend'] = raw['backend']
        models = raw.get('models', {})
        if not isinstance(models, Mapping):
            raise PolicyError(f'{role}: invalid model overrides')
        if any(not _text(driver) or not _text(model) for driver, model in models.items()):
            raise PolicyError(f'{role}: invalid model override values')
        base['models'].update(models)
        origin.update({k: 'project' for k in models})
        if 'model' in raw:
            if not _text(raw['model']):
                raise PolicyError(f'{role}: invalid model override')
            base['models'][base['backend']] = raw['model']
            origin[base['backend']] = 'project'
        for key in ROLE_KEYS & set(raw):  # the §17.2 shape passes through untouched
            base[key] = deepcopy(raw[key])
        entries[role] = base
        provenance[role] = origin
    if policy is not None:
        from maestro.model_classes import resolve_choice
        from maestro.backends import registry
        for entry in entries.values():
            for driver, model in entry.get('models', {}).items():
                try:
                    backend = registry.driver_ref(driver).backend_id
                except registry.UnknownBackend:
                    continue
                entry['models'][driver] = resolve_choice(policy, backend, model)
    result['roles'] = entries
    return result, provenance


ADOPTION_AGGREGATE = 'model_policy'


def adopted_revision(repo: Path, conn=None) -> str | None:
    """One authority per project, as for `state.json` (D08).

    A migrated project's latest adoption event wins; until its first one, the pointer file
    its own controller wrote before migration still counts. An un-migrated project has only
    the pointer file. Pass `conn` to read inside an open controller transaction.
    """
    import sqlite3
    from maestro.control.models import EventType

    repo = Path(repo)
    database = repo / '.orchestrator/control.sqlite3'
    try:
        if conn is None and database.exists():
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as ro:
                return adopted_revision(repo, ro)
        if conn is not None:
            row = conn.execute('SELECT payload FROM events WHERE aggregate_id=? AND type=?'
                               ' ORDER BY sequence DESC LIMIT 1',
                               (ADOPTION_AGGREGATE, EventType.MODEL_POLICY_ADOPTED)).fetchone()
            if row is not None:
                revision = json.loads(row[0])['revision']
                if not isinstance(revision, str):
                    raise ValueError('adoption event without a revision')
                return revision
        path = repo / '.orchestrator/model_policy.json'
        if not path.exists():
            return None
        return json.loads(path.read_text())['revision']
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise PolicyError('unreadable adopted model policy; refusing to use a moving default') from exc


def adopted_policy(repo: Path, store=None, conn=None) -> tuple[str | None, dict | None]:
    store = store or PolicyStore()
    revision = adopted_revision(repo, conn)
    if revision is None:
        return None, None
    try:
        return revision, store.load(revision)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PolicyError('unreadable adopted model policy; refusing to use a moving default') from exc


#: What a role may state in config (DESIGN.md §17.2), and what it may no longer state.
ROLE_KEYS = frozenset({'strength', 'independent_of'})
RETIRED_ROLE_KEYS = ('effort', 'backend', 'model', 'models')
#: Config names are the graph's role ids. The legacy loop still asks for its `judge`.
LEGACY_ROLE_NAMES = {'judge': 'reviewer'}
DEFAULT_ROLE_STRENGTH = 'balanced'


def shipped_role_strengths() -> dict[str, str]:
    """Each shipped role template's `min_reasoning_strength`: the default of `strength`."""
    from maestro.workflows.policy import ROLES_TEMPLATE_DIR, load_agent_definitions

    return {role: str(definition.permitted_backend_capabilities.get('min_reasoning_strength')
                      or DEFAULT_ROLE_STRENGTH)
            for role, definition in load_agent_definitions(ROLES_TEMPLATE_DIR).items()}


def role_shape_issues(document: Mapping) -> list[str]:
    """What `document` (one config layer) says about roles that §17.2 does not allow."""
    from maestro.backends.catalog import MODEL_STRENGTHS

    issues = []
    if 'sprint' in document:
        from maestro.config import ConfigError, sprint_config
        try:
            sprint_config(document)
        except ConfigError as exc:
            issues.append(str(exc))
    if 'runaway' in document:
        from maestro.config import ConfigError, runaway_factor, runaway_mode
        try:
            runaway_mode(document)
            runaway_factor(document)
        except ConfigError as exc:
            issues.append(str(exc))
    if document.get('fallback_chain') is not None:
        issues.append('fallback_chain is retired: the router owns fallback')
    routing = document.get('routing')
    if routing is not None and not isinstance(routing, Mapping):
        issues.append('routing is not a mapping')
    if isinstance(routing, Mapping) and 'harness_order' in routing:
        issues.append('routing.harness_order is retired: selection is global (DESIGN §17.5)')
    if isinstance(routing, Mapping) and 'near_cost_margin' in routing:
        margin = routing['near_cost_margin']
        if (isinstance(margin, bool) or not isinstance(margin, (int, float))
                or not 0 <= margin <= 1):
            issues.append('routing.near_cost_margin must be a number in 0..1')
    if isinstance(routing, Mapping) and 'pace' in routing:
        from maestro.config import ConfigError, pace_estimates
        try:
            pace_estimates(document)
        except ConfigError as exc:
            issues.append(str(exc))
    configured = document.get('roles')
    if configured is None:
        return issues
    if not isinstance(configured, Mapping):
        return issues + ['roles is not a mapping']
    for role, entry in configured.items():
        if not isinstance(entry, Mapping):
            issues.append(f'roles.{role}: must be a mapping {{strength, independent_of}}')
            continue
        for key in RETIRED_ROLE_KEYS:
            if key in entry:
                issues.append(f'roles.{role}.{key} is retired: a role states only '
                              'strength and independent_of')
        for key in sorted(set(map(str, entry)) - ROLE_KEYS - set(RETIRED_ROLE_KEYS)):
            issues.append(f'roles.{role}.{key}: unknown role key')
        strength = entry.get('strength')
        if strength is not None and strength not in MODEL_STRENGTHS:
            issues.append(f'roles.{role}.strength {strength!r} is not one of '
                          f'{", ".join(MODEL_STRENGTHS)}')
        others = entry.get('independent_of')
        if others is not None and (not isinstance(others, list)
                                   or any(not _text(other) for other in others)):
            issues.append(f'roles.{role}.independent_of must be a list of role names')
    return issues


def role_strengths(project: Mapping) -> dict[str, str]:
    """`{role: strength}` for every shipped role and every configured one (§17.2).

    A value that is not a class reads as absent: `doctor` reports it, and the loop keeps
    running on the shipped default rather than stopping over a typo.
    """
    result = shipped_role_strengths()
    configured = project.get('roles') if isinstance(project, Mapping) else None
    for role in (configured if isinstance(configured, Mapping) else ()):
        result.setdefault(LEGACY_ROLE_NAMES.get(role, role), DEFAULT_ROLE_STRENGTH)
    result.update(configured_role_strengths(project))
    return result


def configured_role_strengths(project: Mapping) -> dict[str, str]:
    """`{role: strength}` for only the roles config states a class for.

    This is what the graph router is given: a role config does not name keeps the floor
    of its own template, which a project may have replaced.
    """
    from maestro.backends.catalog import MODEL_STRENGTHS

    result = {}
    configured = project.get('roles') if isinstance(project, Mapping) else None
    for role, entry in (configured.items() if isinstance(configured, Mapping) else ()):
        strength = entry.get('strength') if isinstance(entry, Mapping) else None
        if strength in MODEL_STRENGTHS:
            result[LEGACY_ROLE_NAMES.get(role, role)] = strength
    return result


def role_independence(project: Mapping) -> dict[str, tuple[str, ...]]:
    """Soft family independence, with the shipped reviewer's writer preference."""
    result = {'reviewer': ('implementer',)}
    configured = project.get('roles')
    for role, entry in (configured.items() if isinstance(configured, Mapping) else ()):
        others = entry.get('independent_of') if isinstance(entry, Mapping) else None
        if isinstance(others, list) and all(_text(other) for other in others):
            result[LEGACY_ROLE_NAMES.get(role, role)] = tuple(
                LEGACY_ROLE_NAMES.get(other, other) for other in others)
    return result


def role_requirements(policy: dict | None, project: Mapping | None = None) -> dict:
    """The task's approved numerical requirements; schema 1 retains its migration."""
    if project is not None and '_model_policy_frozen_requirements' in project:
        return deepcopy(project['_model_policy_frozen_requirements'])
    effective = policy if policy is not None else shipped_policy()
    return deepcopy(effective['roles']) if effective['schema'] == 2 else {}


def near_cost_margin(project: Mapping) -> float:
    routing = project.get('routing')
    value = routing.get('near_cost_margin', 0.20) if isinstance(routing, Mapping) else 0.20
    return (float(value) if not isinstance(value, bool) and isinstance(value, (int, float))
            and 0 <= value <= 1 else 0.20)


def legacy_writer_routes(entry: Mapping) -> list:
    """Actual launch routes retained by a legacy task, never role defaults.

    Empty or malformed old metadata stays unknown rather than becoming a fabricated
    author. Switches retain the tree, so their outgoing author remains relevant.
    """
    routes = []
    previous = entry.get('writer_routes') or ()
    if not isinstance(previous, (list, tuple)):
        previous = (None,)
    for route in previous:
        if isinstance(route, (list, tuple)) and len(route) == 2:
            pair = [str(route[0] or ''), str(route[1] or '')]
        else:
            pair = ['', '']
        if pair not in routes:
            routes.append(pair)
    pair = [str(entry.get('backend') or ''), str(entry.get('model') or '')]
    if pair not in routes:
        routes.append(pair)
    return routes


def legacy_binding(project: Mapping, policy: dict | None, role: str, *,
                   driver: str = '', model: str = '', native_catalog=None,
                   router_policy=None, strength=None, independent_families=frozenset()):
    """Use the real router for a legacy role, optionally restricted to a pinned column."""
    from dataclasses import replace
    from types import SimpleNamespace
    from maestro import thirdparty
    from maestro.backends import catalog, registry
    from maestro.backends.router import resolve_agent
    from maestro.workflows.policy import default_policy

    role = LEGACY_ROLE_NAMES.get(role, role)
    router_policy = router_policy or default_policy()
    if native_catalog is None:
        native_catalog = catalog.catalog_from_register(
            thirdparty.load_register(), defaults=graph_catalog_defaults(policy, project))
    entries = dict(native_catalog.entries)
    if driver:
        backend = registry.driver_ref(driver).backend_id
        entries = {key: value for key, value in entries.items() if key == backend}
    if model:
        entries = {key: replace(value, available_models=(model,),
                               model_profiles={m: p for m, p in value.model_profiles.items() if m == model},
                               pair_profiles={k: p for k, p in value.pair_profiles.items() if k[0] == model})
                   for key, value in entries.items() if model in value.available_models}
    return resolve_agent(
        SimpleNamespace(node_id=role, agent_definition_ref=role),
        {"risk": "low"}, catalog=catalog.CapabilityCatalog(entries), policy=router_policy,
        role_strength=strength or role_strengths(project).get(role),
        role_requirement=role_requirements(policy, project).get(role),
        near_cost_margin=near_cost_margin(project),
        independent_families=independent_families,
    )


def derived_roles(project: Mapping, policy: dict | None) -> dict:
    """`project` with `roles` as the legacy loop's resolver reads them (§17.2, §17.5).

    Each column is selected by the graph router, including eligibility, effort rules and
    in-pool cost. The head uses its neutral global pool order without pulling telemetry.
    """
    from maestro import thirdparty
    from maestro.backends import catalog, registry
    from maestro.backends.router import AgentBinding, dispatch_plan
    from maestro.workflows.policy import default_policy

    result = deepcopy(dict(project))
    defaults = graph_catalog_defaults(policy, project)
    native_catalog = catalog.catalog_from_register(thirdparty.load_register(), defaults=defaults)
    chain = harness_drivers(project)
    router_policy = default_policy()
    roles = {}
    for role, strength in role_strengths(project).items():
        models, efforts, controls = {}, {}, {}
        for driver in registry.known_backends():
            binding = legacy_binding(project, policy, role, driver=driver,
                                     native_catalog=native_catalog, router_policy=router_policy,
                                     strength=strength)
            if isinstance(binding, AgentBinding):
                plan = dispatch_plan(binding)
                models[driver], efforts[driver] = plan.model, binding.requested_effort
                controls[driver] = (plan.effort_args, plan.effort_config)
        head = legacy_binding(project, policy, role, native_catalog=catalog.CapabilityCatalog(
            {key: entry for key, entry in native_catalog.entries.items()
             if registry.driver_name_for_backend_id(key) in chain}),
             router_policy=router_policy, strength=strength)
        roles[role] = dict(strength=strength, models=models,
                           efforts=efforts, effort_controls=controls,
                           backend=registry.driver_name_for_backend_id(head.backend_id)
                           if isinstance(head, AgentBinding) else chain[0])
    for legacy, name in LEGACY_ROLE_NAMES.items():
        roles[legacy] = deepcopy(roles[name])
    result['roles'] = roles
    result['_model_policy_roles'] = True
    result['_harness_chain'] = list(chain)
    return result


def runtime_roles(project: Mapping, repo: Path) -> dict:
    """`derived_roles` under the policy this call runs on: the task's frozen one inside a
    task, else the project's adopted one."""
    _, policy, project = runtime_model_inputs(project, repo)
    return derived_roles(project, policy)


def invalid_routes(project: Mapping, policy: dict) -> dict:
    """Effective role models `policy` cannot serve for this project: {role: {driver: reason}}."""
    from maestro.backends import registry
    effective, _ = effective_roles(project, policy)
    invalid = {}
    retired = {tuple(p) for p in policy['retired']}
    native = catalog_defaults(policy, project)
    available = {(base['backend_id'], model) for base in native.values() for model in base['available_models']}
    for role, entry in effective['roles'].items():
        for driver, model in entry.get('models', {}).items():
            try:
                backend = registry.driver_ref(driver).backend_id
                if (backend, model) in retired or (backend, model) not in available:
                    raise PolicyError('retired or unsupported model')
                if registry.driver_ref(driver).modelctl_provider is not None:
                    provider_lifecycle(project, driver, model, policy)
            except (PolicyError, registry.UnknownBackend) as exc:
                invalid.setdefault(role, {})[driver] = str(exc)
    return invalid


def _task_file(repo: Path, task_id: str) -> Path:
    if not isinstance(task_id, str) or not task_id or task_id != task_id.strip():
        raise PolicyError('invalid frozen task identity')
    return Path(repo) / '.orchestrator/model_tasks' / (hashlib.sha256(task_id.encode()).hexdigest() + '.json')


def freeze_task(repo: Path, task_id: str, store=None, *, renew: bool = False, task=None) -> dict:
    """Bind a task to the project's adopted revision for its whole lifetime.

    `renew` starts a new task lifetime (a fresh launch); otherwise the existing binding
    must still equal the adopted revision. Adoption defers while a task is unfinished, so
    a difference means the pointer moved out of band: refuse rather than switch mid-task.
    """
    store = store or PolicyStore()
    # The owner holds this same lock from inventory through row deletion. Persist the
    # reservation before releasing it, so pruning sees either adoption or the task.
    with store.locked():
        return _freeze_task_locked(repo, task_id, store, renew=renew, task=task)


def _freeze_task_locked(repo: Path, task_id: str, store, *, renew: bool, task) -> dict:
    path = _task_file(repo, task_id)
    revision, policy = adopted_policy(repo, store)
    if path.exists() and not renew:
        record = task_record(repo, task_id)
        if 'choices' not in record:
            raise PolicyError('historical task lacks frozen model choices; refusing to reconstruct them')
        if record['revision'] != revision:
            raise PolicyError(f'task {task_id} is bound to model policy {record["revision"]} '
                              f'but the project adopted {revision}; refusing a mid-task change')
        return record
    record = dict(task_id=task_id, revision=revision, policy=policy or shipped_policy())
    record['policy_hash'] = digest(record['policy'])
    import yaml
    try:
        from maestro import config
        project_path = Path(repo) / 'project.yaml'
        # Parsed here so an unreadable project file refuses the freeze; the choices frozen
        # are the merged ones (global under project), which is what the task runs on.
        document = yaml.safe_load(project_path.read_text()) or {} if project_path.exists() else {}
        if revision is not None and not project_path.exists():
            raise PolicyError('published policy task needs readable project configuration')
        if isinstance(document, Mapping):
            document = config.effective_config(repo)[0]
        effective_roles(document, record['policy'])  # validate before persisting choices
        task_models = {}
        if task is not None:
            from maestro.implementer import resolve_implementer_model
            from maestro.backends import registry
            from maestro.model_classes import resolve_choice
            for driver in registry.known_backends():
                model, warning = resolve_implementer_model(task, driver)
                if model:
                    model = _fresh_task_choice(record['policy'], driver, model)
                task_models[driver] = dict(model=model, warning=warning)
        record['choices'] = dict(roles=deepcopy(document.get('roles') or {}), task_models=task_models)
        record['choices_hash'] = digest(record['choices'])
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise PolicyError('cannot freeze unreadable project/task model choices') from exc
    database = Path(repo) / '.orchestrator/control.sqlite3'
    if database.exists():
        import sqlite3
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            record['control_sequence'] = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
    atomic_write(path, canonical(record))
    return record


def _fresh_task_choice(policy, driver, choice):
    """Ask the owner for absent literal identities at freeze, then follow adopted routes.

    Moving owner SET versions never determine this task's version: only the adopted
    policy's eligible routes do. The concrete answer is persisted in task choices.
    """
    from maestro.backends import registry
    from maestro.model_classes import facts, resolve_choice, validate_rows
    ref = registry.driver_ref(driver)
    rows = facts(policy)
    known = any(r['backend_id'] == ref.backend_id and
                (choice in [r['model_id'], *r.get('aliases', [])] or choice.casefold() == r['family'].casefold())
                for r in rows)
    if rows and not known and ref.modelctl_provider is not None:
        from maestro.modelctl_bridge import ModelctlBridge
        identities = [choice, *(r['model_id'] for r in policy['routes'] if r['backend_id'] == ref.backend_id)]
        owner = ModelctlBridge().configured_models(ref.backend_id, identities=identities)
        if not isinstance(owner, dict) or owner.get('contract') != 1:
            raise PolicyError('incompatible modelctl task identity contract')
        validate_rows(owner.get('models'))
        if any(r['backend_id'] != ref.backend_id for r in owner['models']):
            raise PolicyError('task identity metadata crossed backends')
        expanded = deepcopy(policy)
        existing = {(r['backend_id'], r['model_id']): deepcopy(r) for r in rows}
        for row in owner['models']:
            key = row['backend_id'], row['model_id']
            if key in existing:
                saved = existing[key]
                if any(saved[k] != row[k] for k in ('family', 'version')):
                    raise PolicyError('owner changed adopted task identity')
                # New aliases enrich identity only; SET/window/profile facts stay
                # frozen to the adopted revision.
                saved['aliases'] = sorted(set(saved.get('aliases', []) + row.get('aliases', [])))
            else:
                existing[key] = row
        expanded['source']['class_metadata'] = list(existing.values())
        validate_rows(expanded['source']['class_metadata'])
        policy = expanded
    resolved = resolve_choice(policy, ref.backend_id, choice)
    # Class/family pins follow the adopted version, but an explicit effort variant
    # on that version is already a concrete native choice. Do not reselect its effort.
    if any(row['backend_id'] == ref.backend_id
           and resolved in [row['model_id'], *row.get('aliases', [])]
           and choice in row.get('effort_variants', {}).values() for row in facts(policy)):
        return choice
    return resolved


def task_binding_finished(record: dict, conn) -> bool:
    """A terminal projection releases only a lifetime that ended after this freeze.

    Freezing precedes task/run registration. A reused task id may still project its
    previous terminal lifetime during that interval; that is no release proof.
    """
    from maestro.control.models import EventType
    if conn is None:
        return False
    row = conn.execute('SELECT status FROM tasks WHERE task_id=?', (record['task_id'],)).fetchone()
    if row is None or row[0] not in ('accepted', 'canceled'):
        return False
    if 'control_sequence' not in record:
        return True  # historical records relied on authoritative terminal projection
    sequence = record['control_sequence']
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise PolicyError('invalid frozen task control sequence')
    event = conn.execute('SELECT sequence,payload FROM events WHERE aggregate_id=? AND type IN (?,?) '
                         'ORDER BY sequence DESC LIMIT 1', (record['task_id'], EventType.TASK_REGISTERED,
                                                           EventType.TASK_STATUS_CHANGED)).fetchone()
    return bool(event and event[0] > sequence and json.loads(event[1]).get('status') == row[0])


def task_record(repo: Path, task_id: str) -> dict:
    try:
        record = json.loads(_task_file(repo, task_id).read_text())
        if record['task_id'] != task_id or digest(record['policy']) != record['policy_hash']:
            raise PolicyError('frozen task model policy integrity failure')
        validate(record['policy'])
        if record['revision'] is not None and digest(record['policy']) != record['revision']:
            raise PolicyError('frozen task revision integrity failure')
        if 'choices' in record:
            choices = record['choices']
            if (not isinstance(choices, dict) or set(choices) != {'roles', 'task_models'}
                    or digest(choices) != record['choices_hash'] or not isinstance(choices['task_models'], dict)
                    or not isinstance(choices['roles'], dict)):
                raise PolicyError('frozen task model choices integrity failure')
            effective_roles({'roles': choices['roles']}, record['policy'])
            from maestro.backends import registry
            if choices['task_models'] and set(choices['task_models']) != set(registry.known_backends()):
                raise PolicyError('incomplete frozen task model choices')
            for driver, choice in choices['task_models'].items():
                if (driver not in registry.known_backends() or not isinstance(choice, dict)
                        or set(choice) != {'model', 'warning'}
                        or not isinstance(choice['model'], str)
                        or choice['model'] != choice['model'].strip()
                        or not isinstance(choice['warning'], str)):
                    raise PolicyError('invalid frozen task model choices')
        return record
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PolicyError(f'unreadable frozen task model binding: {task_id}: {exc}') from exc


def task_policy(repo: Path, task_id: str, store=None) -> dict:
    return task_record(repo, task_id)['policy']


_task_models = ContextVar('maestro_task_models', default=None)


@contextmanager
def task_model_context(repo: Path, task_id: str):
    """Scope task calls to their durable choices; reset even on a refused launch."""
    repo = Path(repo).resolve()
    if not _task_file(repo, task_id).exists():
        yield
        return
    record = task_record(repo, task_id)
    if 'choices' not in record:
        raise PolicyError('historical task lacks frozen model choices; refusing to reconstruct them')
    token = _task_models.set((repo, record))
    try:
        yield
    finally:
        _task_models.reset(token)


def with_task_models(function):
    """Keep existing task entry-point signatures and propagate context to bounded calls."""
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        from maestro import config
        arguments = signature.bind(*args, **kwargs).arguments
        task_id = arguments.get('task_id')
        if task_id is None:
            task = arguments.get('task') or arguments.get('entry') or {}
            task_id = task.get('id') or task.get('task_id')
        if not task_id:
            return function(*args, **kwargs)
        with task_model_context(config.PROJECT_YAML.parent, task_id):
            return function(*args, **kwargs)
    return wrapped


def runtime_model_inputs(project: Mapping, repo: Path):
    scoped = _task_models.get()
    if scoped is None or scoped[0] != Path(repo).resolve():
        revision, policy = adopted_policy(repo)
        return revision, policy, project
    record = scoped[1]
    project = deepcopy(dict(project))
    project['roles'] = deepcopy(record['choices']['roles'])
    project['_model_policy_task_models'] = record['choices']['task_models']
    if record['revision'] is None:
        # Before shared-policy adoption retain the existing non-strict bootstrap behavior,
        # but fill inherited columns from the frozen shipped policy, never today's defaults.
        project['roles'] = effective_roles(project, record['policy'])[0]['roles']
        project['_model_policy_frozen_requirements'] = role_requirements(record['policy'])
        project['_model_policy_frozen_policy'] = record['policy']
        return None, None, project
    return record['revision'], record['policy'], project


def harness_drivers(project: Mapping) -> tuple[str, ...]:
    """Registered launch targets. Configuration cannot prioritize a harness."""
    from maestro import roles
    return roles.default_chain()


def catalog_defaults(policy: dict | None, project: Mapping) -> dict:
    """Keep verified destination facts; only routing judgments come from shared policy.

    Schema 1 rows migrate directly to historical pairs; schema 2 rows carry their
    approved effort scores. Unknown native profiles are refused rather than cloning
    another model's window or pool. Project pins cannot add policy eligibility.
    """
    from maestro.backends import catalog

    if policy is None:
        policy = project.get('_model_policy_frozen_policy') or shipped_policy()
    validate(policy)
    defaults = deepcopy(catalog.SHIPPED_CATALOG_DEFAULTS)
    paired = policy['schema'] == 2
    wanted = {(r['backend_id'], r['model_id']): r for r in policy['routes']}
    configured = project.get('roles') or {}
    if not isinstance(configured, Mapping):
        raise PolicyError('invalid project roles')
    retired = {tuple(p) for p in policy['retired']}
    for base in defaults.values():
        models, profiles = [], {}
        for (backend, model), row in wanted.items():
            if backend != base['backend_id'] or (backend, model) in retired:
                continue
            from maestro.model_classes import profile as class_profile
            original = class_profile(policy, backend, model, base)
            if original is None:
                raise PolicyError(f'{backend}:{model}: verified native profile unavailable')
            models.append(model)
            profiles[model] = original
        base['available_models'] = models
        base['model_profiles'] = profiles
        if paired:
            base['pair_profiles'] = {
                (r['model_id'], r['effort']): catalog.PairProfile.from_score(
                    r['model_id'], r['effort'], r['intelligence'], r['relative_cost'])
                for r in policy['routes']
                if r['backend_id'] == base['backend_id'] and r['model_id'] in profiles}
        else:
            base['pair_profiles'] = catalog.migrate_pairs(*[
                (r['model_id'], r['strength'], r['relative_cost']) for r in policy['routes']
                if r['backend_id'] == base['backend_id'] and r['model_id'] in profiles])
        base['pairs_migrated'] = not paired
    return defaults


def graph_catalog_defaults(policy: dict | None, project: Mapping) -> dict:
    """The native defaults the graph catalog is built from: shared routing judgments and,
    under an adopted policy, the provider's own lifecycle limits. One builder for the
    controller and for the reports that describe it."""
    defaults = catalog_defaults(policy, project)
    return apply_provider_limits(defaults, project, policy) if policy is not None else defaults


def provider_lifecycle(project: Mapping, driver: str, model: str, policy=None):
    """Existing Maestro parser, one provider file; no cache write or fallback row."""
    from maestro import limits
    from maestro.backends import catalog, registry
    paths = project.get('model_limits') or {}
    if not isinstance(paths, Mapping):
        raise PolicyError('invalid provider limits configuration')
    path = Path(paths.get(driver) or Path.home() / limits.AGENT_TABLE_DIRS[driver] / limits.TABLE_FILENAME).expanduser()
    try:
        parsed = limits.parse_limits_table(path.read_text())
        row = limits._lookup(parsed, limits.scoped_model_name(model, driver))
        if row is None:
            raise PolicyError(f'{driver}:{model}: provider lifecycle row missing')
        if not (0 < row.prepare_handoff_low <= row.prepare_handoff_high < row.normally_fresh_low
                <= row.normally_fresh_high < row.exception_ceiling):
            raise PolicyError(f'{driver}:{model}: unsafe provider lifecycle row')
        backend = registry.driver_ref(driver).backend_id
        base = next((b for b in catalog.SHIPPED_CATALOG_DEFAULTS.values()
                     if b['backend_id'] == backend and model in b['available_models']), None)
        profile = base.get('model_profiles', {}).get(model) if base else None
        if policy is not None:
            defaults = catalog_defaults(policy, project)
            adopted_base = next((b for b in defaults.values() if b['backend_id'] == backend), None)
            adopted_profile = adopted_base.get('model_profiles', {}).get(model) if adopted_base else None
            if adopted_profile is not None:
                base, profile = adopted_base, adopted_profile
        native = (profile.context_limits or base['context_limits']) if profile else None
        if native is None or row.exception_ceiling > native.max_input_tokens:
            raise PolicyError(f'{driver}:{model}: provider lifecycle ceiling exceeds verified native window')
        return row
    except (OSError, TypeError) as exc:
        raise PolicyError(f'{driver}:{model}: provider lifecycle metadata unavailable') from exc


def check_task_pin(project: Mapping, policy: dict, driver: str, model: str) -> str:
    """A task's model pin is served only as written: verified on `driver`, not retired,
    and with a usable provider lifecycle row. Never reinterpreted on another vendor."""
    from maestro.backends import catalog, registry

    ref = registry.driver_ref(driver)
    from maestro.model_classes import resolve_choice
    original_model = model
    model = resolve_choice(policy, ref.backend_id, model)
    supported = {m for base in catalog_defaults(policy, project).values()
                 if base['backend_id'] == ref.backend_id for m in base['available_models']}
    supported |= {m for base in catalog.SHIPPED_CATALOG_DEFAULTS.values()
                  if base['backend_id'] == ref.backend_id for m in base['available_models']}
    if model not in supported or (ref.backend_id, model) in {tuple(p) for p in policy['retired']}:
        raise PolicyError(f'task model pin {model} cannot be served by {driver}')
    if ref.modelctl_provider is not None:
        provider_lifecycle(project, driver, model, policy)
    for base in catalog_defaults(policy, project).values():
        if base['backend_id'] != ref.backend_id:
            continue
        profile = base.get('model_profiles', {}).get(model)
        if profile is not None and profile.effort_variants is not None:
            if original_model in profile.effort_variants.values():
                return original_model
    return model


def apply_provider_limits(defaults: dict, project: Mapping, policy=None) -> dict:
    from dataclasses import replace
    from maestro.backends.catalog import ContextLimits
    from maestro.backends import registry
    result = deepcopy(defaults)
    for driver in registry.known_backends():
        ref = registry.driver_ref(driver)
        if ref.modelctl_provider is None:
            continue
        base = result.get(ref.modelctl_provider)
        if not base:
            continue
        accepted, profiles = [], {}
        for model in base['available_models']:
            try:
                row = provider_lifecycle(project, driver, model, policy)
                profile = base['model_profiles'][model]
                native = profile.context_limits or base['context_limits']
                if row.exception_ceiling > native.max_input_tokens:
                    continue
                profiles[model] = replace(profile, context_limits=ContextLimits(
                    native.max_input_tokens, native.max_output_tokens, row.normally_fresh_low))
                accepted.append(model)
            except PolicyError:
                continue  # unknown limits are never eligibility
        base['available_models'] = accepted
        base['model_profiles'] = profiles
        if base.get('pair_profiles') is not None:  # a model with no limits row keeps no pair
            base['pair_profiles'] = {key: pair for key, pair in base['pair_profiles'].items()
                                     if key[0] in profiles}
    return result


class PolicyStore:
    def __init__(self, home: Path | None = None):
        self.home = home if home is not None else policy_home()

    @contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with (self.home / 'policy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def current(self) -> str | None:
        path = self.home / 'current'
        if not path.exists():
            return None
        try:
            value = path.read_text().strip()
        except (OSError, ValueError) as exc:
            raise PolicyError('invalid current policy pointer') from exc
        if not re.fullmatch('[0-9a-f]{64}', value):
            raise PolicyError('invalid current policy pointer')
        return value

    def load(self, revision: str) -> dict:
        if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision):
            raise PolicyError('invalid policy revision')
        try:
            policy = json.loads((self.home / 'revisions' / (revision + '.json')).read_text())
            if digest(policy) != revision:
                raise PolicyError('policy revision integrity failure')
            validate(policy)
            return policy
        except (OSError, ValueError) as exc:
            raise PolicyError(f'policy revision integrity failure: {revision}') from exc

    def load_any(self, revision: str) -> dict:
        """A stored revision, or this release's shipped policy by its digest."""
        for shipped in (shipped_policy(), shipped_policy_v1()):
            if revision == digest(shipped):
                return shipped
        return self.load(revision)

    def snapshot(self) -> tuple[str | None, dict]:
        revision = self.current()
        return revision, self.load(revision) if revision is not None else shipped_policy()

    def publish(self, policy: dict, *, expected: str | None, check=None) -> str:
        """CAS publication; `check` re-validates the caller's preview under the same lock."""
        validate(policy)
        revision = digest(policy)
        with self.locked():
            if self.current() != expected:
                raise PolicyError('policy changed since preview; preview again')
            if check is not None:
                check()
            # Preserve the initial source policy before the first publication references
            # it. A later code upgrade cannot reconstruct this exact shipped digest.
            if expected is None:
                for initial in (shipped_policy(), shipped_policy_v1()):
                    initial_revision = digest(initial)
                    if policy['source'].get('previous_policy') == initial_revision:
                        initial_path = self.home / 'revisions' / (initial_revision + '.json')
                        if initial_path.exists():
                            self.load(initial_revision)
                        else:
                            atomic_write(initial_path, canonical(initial))
            path = self.home / 'revisions' / (revision + '.json')
            if path.exists():
                self.load(revision)
            else:
                atomic_write(path, canonical(policy))
            atomic_write(self.home / 'current', (revision + '\n').encode())
        return revision
