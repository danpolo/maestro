"""Validated project registry, read-only preview, and conservative idle adoption."""
from __future__ import annotations

import ast
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from typing import Mapping

import yaml

from maestro import bootstrap
from maestro.model_policy import (MODEL_POLICY_READER_VERSION, PolicyError, PolicyStore, atomic_write, canonical, digest, effective_roles,
                                  invalid_routes)


def project_document(repo: Path) -> dict:
    try:
        document = yaml.safe_load((repo / 'project.yaml').read_text())
        identity = document['project']
        if (not isinstance(document, dict) or not isinstance(identity, dict)
                or not isinstance(identity.get('name'), str) or not identity['name'].strip()
                or Path(identity['repo_path']).resolve() != repo.resolve()):
            raise ValueError('project identity mismatch')
        # Historical schema 1 projects retain their readable override forms. Doctor
        # enforces the current config contract; this reader must also adopt old revisions.
        from maestro.model_policy import shipped_policy_v1
        effective_roles(document, shipped_policy_v1())
        return document
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise PolicyError(f'invalid project identity/configuration: {repo}') from exc


def registry(store: PolicyStore) -> list[dict]:
    path = store.home / 'projects.json'
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if data['schema'] != 1 or not isinstance(data['projects'], list):
            raise ValueError()
        if any(not isinstance(r, dict) or set(r) != {'repo', 'name'} for r in data['projects']):
            raise ValueError()
        return data['projects']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PolicyError('invalid shared project registry') from exc


def register_project(repo: Path, store=None) -> dict:
    store = store or PolicyStore()
    repo = Path(repo).resolve()
    document = project_document(repo)
    record = dict(repo=str(repo), name=document['project']['name'])
    with store.locked():
        rows = registry(store)
        rows = [r for r in rows if r['repo'] != str(repo)] + [record]
        atomic_write(store.home / 'projects.json', canonical(dict(schema=1, projects=rows)))
    return record


def candidate_projects(root: Path) -> list[dict]:
    """Immediate children are candidates only; registration is explicit."""
    found = []
    for repo in sorted(Path(root).iterdir()):
        if repo.is_dir() and (repo / 'project.yaml').exists():
            try:
                document = project_document(repo)
                found.append(dict(repo=str(repo.resolve()), name=document['project']['name']))
            except PolicyError as exc:
                found.append(dict(repo=str(repo.resolve()), status='invalid', reason=str(exc)))
    return found


def supports_reader(repo: Path) -> bool:
    """The installed source reads the same adoption record format this code writes."""
    source = bootstrap.adopted_root(repo=repo) or bootstrap.running_root()
    path = source / 'maestro/model_policy.py'
    try:
        tree = ast.parse(path.read_text())
        return any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name)
                   and t.id == 'MODEL_POLICY_READER_VERSION' for t in n.targets)
                   and isinstance(n.value, ast.Constant)
                   and n.value.value == MODEL_POLICY_READER_VERSION for n in tree.body)
    except (OSError, SyntaxError):
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def idle_reason(repo: Path, conn=None, *, controller_pid=None) -> str:
    """Unknown activity is a deferral. Read authoritative events before projections."""
    own = False
    try:
        from maestro.control.models import EventType
        if conn is None and (repo / '.orchestrator/control.sqlite3').exists():
            conn = sqlite3.connect((repo / '.orchestrator/control.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
            own = True
        state = None
        if conn is not None:
            row = conn.execute("SELECT payload FROM events WHERE aggregate_id='legacy_state' AND type=? ORDER BY sequence DESC LIMIT 1", (EventType.LEGACY_STATE_WRITTEN,)).fetchone()
            if row:
                state = json.loads(row[0]).get('document')
            if conn.execute("SELECT 1 FROM task_runs WHERE status IN ('running','paused') LIMIT 1").fetchone():
                return 'active graph run'
            if conn.execute("SELECT 1 FROM tasks WHERE status IN ('active','waiting','blocked') LIMIT 1").fetchone():
                return 'unfinished task or approval'
            if conn.execute("SELECT 1 FROM attempts WHERE status IN ('pending','claimed','running') LIMIT 1").fetchone():
                return 'pending or active attempt'
            for row in conn.execute('SELECT writer_pid, resource_id FROM writer_leases'):
                if row[0] == controller_pid and row[1] == 'controller':
                    continue
                if _alive(row[0]):
                    return 'live writer lease'
            from maestro.model_policy import task_binding_finished
            for path in (repo / '.orchestrator/model_tasks').glob('*.json'):
                if not task_binding_finished(json.loads(path.read_text()), conn):
                    return 'unfinished frozen task binding'
        if state is None:
            state = json.loads((repo / '.orchestrator/state.json').read_text())
        if not isinstance(state, dict):
            return 'unreadable state'
        for key in ('in_flight', 'waiting_on_dan', 'awaiting_dan_verification', 'resume_state',
                    'pending_approvals', 'async_jobs', 'self_fix_in_flight', 'redo_in_flight'):
            if state.get(key):
                return f'active or pending {key}'
        if state.get('phase_status') in ('in_progress', 'running', 'active'):
            return 'legacy phase in progress without idle proof'
        phase = state.get('phase') or {}
        if isinstance(phase, Mapping) and phase.get('status') == 'in_progress' and controller_pid is None:
            return 'legacy phase in progress without controller idle proof'
        return ''
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return 'unreadable or incompatible activity state'
    finally:
        if own:
            conn.close()


def preview_project(repo: Path, policy: dict, store=None) -> dict:
    store = store or PolicyStore()
    result = dict(repo=str(repo.resolve()))
    try:
        document = project_document(repo)
        result['config_hash'] = digest(document)
        result['effective'], result['provenance'] = effective_roles(document, policy)
        result['effective'] = result['effective']['roles']
        from maestro.model_policy import shipped_policy_v1
        configured, _ = effective_roles(document, shipped_policy_v1() if policy['schema'] == 1 else policy)
        # Preserve the configured class choice; effective versions follow that class.
        result['class_choices_preserved'] = {
            role: {d: configured['roles'][role]['models'][d] for d, o in origins.items() if o == 'project'}
            for role, origins in result['provenance'].items() if 'project' in origins.values()}
        # Explicit overrides this policy cannot serve are reported, never silently rerouted.
        result['invalid_routes'] = invalid_routes(document, policy)
        result['status'] = 'eligible'
        if not supports_reader(repo):
            result.update(status='upgrade_required', reason='installed version lacks shared policy reader')
        elif reason := idle_reason(repo):
            result.update(status='deferred', reason=reason)
    except PolicyError as exc:
        result.update(status='invalid', reason=str(exc))
    return result


def adopt(repo: Path, store=None, *, controller=None, owner=False) -> dict:
    """Serialize adoption with publication, task freezing and owner pruning."""
    store = store or PolicyStore()
    with store.locked():
        return _adopt_locked(repo, store, controller=controller, owner=owner)


def _adopt_locked(repo: Path, store, *, controller=None, owner=False) -> dict:
    """Same controller lease as execution, including stopped projects. HALT is untouched.

    A migrated project records a `ModelPolicyAdopted` event in the transaction that proves
    it idle. An un-migrated project has no lock another process could share, so only its
    own running controller (`owner=True`) adopts it, in the pointer file; a CLI defers
    rather than migrating it by creating the control database.
    """
    from maestro.control.models import EventType
    from maestro.control.store import ControlStore, SingleWriterViolation
    from maestro.model_policy import ADOPTION_AGGREGATE, adopted_revision

    store = store or PolicyStore()
    repo = Path(repo).resolve()
    try:
        revision, policy = store.snapshot()
    except PolicyError as exc:
        return _unreadable_current(repo, store, exc)
    result = preview_project(repo, policy, store)
    in_process = controller is not None or owner
    if result['status'] != 'eligible' and not (in_process and result['status'] == 'deferred'):
        return result
    if revision is None:
        return dict(result, status='unchanged', reason='no shared policy published')
    if not any(r['repo'] == str(repo) for r in registry(store)):
        return dict(result, status='unregistered', reason='validate and register project first')
    database = repo / '.orchestrator/control.sqlite3'
    if controller is None and not database.exists():
        if not owner:
            return dict(result, status='deferred',
                        reason='no control store; the project adopts at its own controller boundary')
        try:
            if reason := idle_reason(repo, controller_pid=os.getpid()):
                return dict(result, status='deferred', reason=reason)
            if adopted_revision(repo) == revision:
                return dict(result, status='unchanged', revision=revision)
            atomic_write(repo / '.orchestrator/model_policy.json', canonical(dict(schema=1, revision=revision)))
            return dict(result, status='updated', revision=revision)
        except (OSError, PolicyError) as exc:
            return dict(result, status='deferred', reason=str(exc))
    own = controller is None
    if not own and (not controller._holds_controller_lock
                    or controller.path.resolve() != database.resolve()):
        return dict(result, status='deferred', reason='controller lock not held for project')
    try:
        controller = controller or ControlStore.open_controller(database)
        with controller.transaction() as conn:
            reason = idle_reason(repo, conn, controller_pid=controller.pid)
            if reason:
                return dict(result, status='deferred', reason=reason)
            before = adopted_revision(repo, conn)
            if before == revision:
                return dict(result, status='unchanged', revision=revision)
            controller.append_event(EventType.MODEL_POLICY_ADOPTED, ADOPTION_AGGREGATE,
                                    payload=dict(revision=revision, previous=before), conn=conn)
        return dict(result, status='updated', revision=revision)
    except (SingleWriterViolation, OSError, PolicyError, sqlite3.Error) as exc:
        return dict(result, status='deferred', reason=str(exc))
    finally:
        if own and controller is not None:
            controller.close()


def _unreadable_current(repo: Path, store, exc: PolicyError) -> dict:
    """The shared pointer names something this reader cannot use; nothing is adopted.

    A newer release may publish a policy shape before this project's code moves. Routing
    reads only the adopted revision, so the project keeps running on it until idle
    self-update brings the reader. `unreadable` names what was skipped, for the journal.
    """
    try:
        revision = store.current()
    except PolicyError:
        revision = None
    named = f'revision {revision}' if revision else 'pointer'
    return dict(repo=str(repo), status='deferred', unreadable=revision or 'pointer',
                reason=f'shared policy {named} is not readable by reader version '
                       f'{MODEL_POLICY_READER_VERSION} ({exc}); adopted after self-update')


def project_report(repo: Path, store=None) -> dict:
    """One project's shared-model evidence for `doctor` and `models status`; read-only.

    Adoption state, whether the installed source reads the record, effective role pairs
    with provenance (project / shared / shipped), routes the policy cannot serve, the
    graph catalog ids the controller would build, and open detection proposals.
    """
    from maestro.model_detection import open_proposals, switches
    from maestro.model_policy import (adopted_policy, effective_roles, graph_catalog_defaults,
                                      invalid_routes, shipped_policy)

    store = store or PolicyStore()
    repo = Path(repo).resolve()
    shared = store.current()
    report = dict(repo=str(repo), shared_revision=shared,
                  registered=any(r['repo'] == str(repo) for r in registry(store)),
                  reader='supported' if supports_reader(repo) else 'upgrade_required')
    report['switches'] = switches(store)
    try:
        adopted, policy = adopted_policy(repo, store)
    except PolicyError as exc:
        report.update(adoption='unreadable', reason=str(exc))
        report['open_proposals'] = open_proposals(store)
        return report
    report['adopted_revision'] = adopted
    report['adoption'] = 'not_adopted' if adopted is None else 'current' if adopted == shared else 'behind'
    try:
        document = project_document(repo)
        effective, provenance = effective_roles(document, policy)
        from maestro.model_policy import shipped_policy_v1
        configured, _ = effective_roles(document, shipped_policy_v1() if policy and policy['schema'] == 1 else policy)
        report['roles'] = {role: (dict(entry) if 'models' not in entry else dict(backend=entry['backend'], models={
            driver: dict(model=model, origin=provenance[role][driver])
            for driver, model in entry['models'].items()})) for role, entry in effective['roles'].items()}
        from maestro.model_classes import facts
        from maestro.backends.registry import driver_ref
        owner_facts = facts(policy)
        report['class_choices'] = {role: {driver: dict(
            choice=configured['roles'][role]['models'][driver], model=model,
            family=next((row['family'] for row in owner_facts
                         if row['backend_id'] == driver_ref(driver).backend_id and row['model_id'] == model), None))
            for driver, model in entry.get('models', {}).items() if provenance[role][driver] == 'project'}
            for role, entry in effective['roles'].items() if 'project' in provenance[role].values()}
        report['invalid_routes'] = invalid_routes(document, policy or shipped_policy())
        report['graph_routes'] = {base['backend_id']: sorted(base['available_models'])
                                  for base in graph_catalog_defaults(policy, document).values()}
        report['config'] = 'valid'
    except PolicyError as exc:
        report.update(config='invalid', reason=str(exc))
    report['open_proposals'] = open_proposals(store)
    return report


def adoption_status(store=None) -> list[dict]:
    """What each registered project actually adopted versus the shared pointer; read-only."""
    from maestro.model_policy import adopted_policy

    store = store or PolicyStore()
    current = store.current()
    rows = []
    for project in registry(store):
        row = dict(project, current_revision=current)
        try:
            adopted, _ = adopted_policy(Path(project['repo']), store)
            row['adopted_revision'] = adopted
            row['adoption'] = ('not_adopted' if adopted is None
                               else 'current' if adopted == current else 'behind')
        except PolicyError as exc:
            row.update(adopted_revision=None, adoption='unreadable', reason=str(exc))
        rows.append(row)
    return rows


def dependency_inventory(store=None) -> dict:
    """Conservative owner contract: unknown state never authorizes row removal.

    Includes eligible fallback routes, not only preferred roles. Terminal task bindings
    are ignored only when the authoritative task projection proves them terminal.
    Callers removing rows hold the shared policy lock across this read and deletion.
    """
    from maestro.model_policy import adopted_policy, task_record, shipped_policy, task_binding_finished, _task_file
    store = store or PolicyStore()
    revision, current = store.snapshot()
    result = dict(contract=1, complete=True, current_revision=revision, projects=[], dependencies=[], errors=[])
    def retain(policy, kind, repo=None, task_id=None):
        for route in policy['routes']:
            result['dependencies'].append(dict(backend_id=route['backend_id'], model_id=route['model_id'],
                                               kind=kind, repo=str(repo) if repo else None, task_id=task_id))
    retain(current, 'shared_policy')
    for registered in registry(store):
        repo = Path(registered['repo'])
        try:
            document = project_document(repo)
            if not supports_reader(repo): raise PolicyError('registered project requires reader upgrade')
            adopted, policy = adopted_policy(repo, store)
            result['projects'].append(dict(repo=str(repo), adopted_revision=adopted))
            policy = policy or shipped_policy()
            retain(policy, 'adopted_policy', repo)
            effective, provenance = effective_roles(document, policy)
            from maestro.backends import registry as backends
            for role, entry in effective['roles'].items():
                for driver, model in entry.get('models', {}).items():
                    if provenance[role][driver] != 'project': continue
                    result['dependencies'].append(dict(backend_id=backends.driver_ref(driver).backend_id,
                                                       model_id=model, kind='project_choice', repo=str(repo),task_id=None))
            root = repo / '.orchestrator'
            conn = None
            try:
                tasks = {}
                if (root/'control.sqlite3').exists():
                    conn = sqlite3.connect((root/'control.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
                    conn.execute('BEGIN')
                    tasks = dict(conn.execute('SELECT task_id,status FROM tasks'))
                    runs = list(conn.execute("SELECT task_id FROM task_runs WHERE status IN ('running','paused')"))
                    attempts = list(conn.execute("SELECT r.task_id,a.backend_id,a.model_id,a.status FROM attempts a "
                        "JOIN task_runs r ON r.run_id=a.run_id WHERE r.status IN ('running','paused') "
                        "OR a.status IN ('pending','claimed','running')"))
                    for task_id, backend, model, status in attempts:
                        if not (isinstance(backend, str) and backend and isinstance(model, str) and model):
                            if status in ('pending', 'claimed', 'running'):
                                raise PolicyError('active attempt lacks concrete model binding')
                            continue
                        result['dependencies'].append(dict(backend_id=backend, model_id=model,
                            kind='attempt_binding', repo=str(repo), task_id=task_id))
                else:
                    runs = []
                # idle_reason's state validation also accounts for unreadable legacy state.
                reason = idle_reason(repo,conn)
                if reason.startswith('unreadable'): raise PolicyError(reason)
                active_tasks = {task_id for (task_id,) in runs}
                legacy = json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
                inflight = legacy.get('in_flight', [])
                if not isinstance(inflight, list): raise PolicyError('unreadable legacy in-flight state')
                for entry in inflight:
                    # Main's deterministic script path launches no agent and writes no
                    # backend/model. Accept only its complete, recognizable entry shape.
                    if (isinstance(entry, dict) and entry.get('role') == 'script'
                        and entry.get('status') == 'running' and 'backend' not in entry and 'model' not in entry
                        and all(isinstance(entry.get(key), str) and entry[key] for key in
                                ('session_id', 'task_id', 'worktree', 'window', 'branch', 'started_at'))
                        and entry['window'] == 'script-' + entry['task_id']):
                        continue
                    if (not isinstance(entry, dict) or not isinstance(entry.get('task_id'), str)
                        or not isinstance(entry.get('model'), str) or not entry['model']
                        or not isinstance(entry.get('backend'), str) or not entry['backend']):
                        raise PolicyError('legacy in-flight job lacks concrete model binding')
                    backend = backends.driver_ref(backends.normalise_name(entry['backend'])).backend_id
                    active_tasks.add(entry['task_id'])
                    result['dependencies'].append(dict(backend_id=backend, model_id=entry['model'],
                        kind='legacy_binding', repo=str(repo), task_id=entry['task_id']))
                frozen = {}
                for path in (root/'model_tasks').glob('*.json'):
                    record = json.loads(path.read_text())
                    task_id = record['task_id']
                    if not isinstance(task_id, str) or path != _task_file(repo, task_id):
                        raise PolicyError('frozen task filename does not match task identity')
                    record = task_record(repo, task_id)
                    if task_binding_finished(record, conn) and task_id not in active_tasks:
                        continue
                    frozen[task_id] = record['policy']
                    retain(frozen[task_id],'frozen_task',repo,task_id)
                    # Earlier records froze the policy, but not the literal task choice.
                    # Keep every still-servable shipped pin outside that policy's routes;
                    # guessing that the task did not choose it would authorize unsafe deletion.
                    from maestro.backends.catalog import SHIPPED_CATALOG_DEFAULTS
                    from maestro.model_classes import resolve_choice
                    bound = frozen[task_id]
                    retired = {tuple(pair) for pair in bound['retired']}
                    routes = {(r['backend_id'], r['model_id']) for r in bound['routes']}
                    possible = set()
                    for base in SHIPPED_CATALOG_DEFAULTS.values():
                        backend = base['backend_id']
                        for choice in base['available_models']:
                            if (backend, choice) in retired:
                                continue
                            model = resolve_choice(bound, backend, choice)
                            if (backend, model) not in routes | retired:
                                possible.add((backend, model))
                    for backend, model in sorted(possible):
                        result['dependencies'].append(dict(backend_id=backend, model_id=model,
                            kind='frozen_possible_pin', repo=str(repo), task_id=task_id))
                for (task_id,) in runs:
                    if task_id not in frozen: raise PolicyError('active run lacks frozen model binding')
            finally:
                if conn is not None: conn.close()
        except (PolicyError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
            result['complete']=False
            result['errors'].append(dict(repo=str(repo),reason=str(exc)))
    result['dependencies'].sort(key=lambda r:(r['backend_id'],r['model_id'],r['kind'],r['repo'] or '',r['task_id'] or ''))
    return result
