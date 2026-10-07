"""Read-only, bounded inventory for the proposed shared model integration.

No discovery, inference, limit resolver/cache refresh, doctor, or project writes.
Only selected configuration/state fields leave the inspected repositories.
"""
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import yaml

ROOT = Path('/home/dan/projects')
MAESTRO = ROOT / 'maestro'
MODELCTL = ROOT / 'modelctl'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def pointer(path):
    return path.read_text().strip() if path.is_file() else None


PROBE = '''
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
from maestro import roles
config = json.loads(sys.argv[2])
out = {'source': roles.__file__, 'roles': {}, 'graph_catalog': {}}
for name in roles.KNOWN_ROLES:
    role = roles.role_config(name, config=config)
    out['roles'][name] = {
        'backend': role.backend, 'chain': list(role.chain),
        'models': {b: role.model_for(b) for b in role.chain},
    }
catalog_defaults = {}
out['graph_catalog_supported'] = importlib.util.find_spec('maestro.backends.catalog') is not None
if out['graph_catalog_supported']:
    from maestro.backends import catalog
    catalog_defaults = catalog.SHIPPED_CATALOG_DEFAULTS
for provider, entry in catalog_defaults.items():
    out['graph_catalog'][provider] = {
        'models': entry.get('available_models', []),
        'profiles': {mid: {'strength': p.strength, 'relative_cost': p.relative_cost}
                     for mid, p in entry.get('model_profiles', {}).items()},
    }
print(json.dumps(out))
'''


def main():
    global_root = pointer(Path('/home/dan/.maestro/current'))
    rows = []
    for repo in sorted(ROOT.iterdir()):
        if not repo.is_dir():
            continue
        orch = repo / '.orchestrator'
        config_path = repo / 'project.yaml'
        if not (orch.is_dir() or config_path.is_file()):
            continue
        row = {'project': str(repo), 'hashes': {}, 'inspection_errors': []}
        document = {}
        if config_path.is_file():
            try:
                document = yaml.safe_load(config_path.read_text()) or {}
                if not isinstance(document, dict):
                    raise ValueError('project.yaml is not a mapping')
            except (OSError, ValueError, yaml.YAMLError) as exc:
                row['inspection_errors'].append(type(exc).__name__)
        row['operational_config'] = {k: document[k] for k in ('roles', 'fallback_chain') if k in document}
        row['runner'] = (document.get('engineering') or {}).get('runner', 'legacy')
        row['halt'] = (orch / 'HALT').exists()
        row['project_pin'] = pointer(orch / 'current')
        row['effective_source'] = row['project_pin'] or global_root
        row['state'] = {}
        state_path = orch / 'state.json'
        if state_path.is_file():
            state = json.loads(state_path.read_text())
            row['state'] = {k: state[k] for k in ('maestro_version', 'phase', 'operator_backend') if k in state}
            row['legacy_in_flight_count'] = len(state.get('in_flight') or {})
        row['managed_project_candidate'] = config_path.is_file() and state_path.is_file()
        row['control_status_counts'] = {}
        db = orch / 'control.sqlite3'
        if db.is_file():
            with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('task_runs', 'attempts'):
                    if table in tables:
                        row['control_status_counts'][table] = dict(conn.execute(
                            f'SELECT status, count(*) FROM {table} GROUP BY status'))
        row['graph_model_constraints'] = []
        for path in sorted((repo / 'workflows/roles').glob('*.yaml')):
            role = yaml.safe_load(path.read_text()) or {}
            capabilities = role.get('permitted_backend_capabilities') or {}
            if capabilities.get('models'):
                row['graph_model_constraints'].append({'file': str(path.relative_to(repo)), 'models': capabilities['models']})
        for path in (config_path, state_path, orch / 'current', orch / 'HALT', repo / 'workflows/project.yaml'):
            row['hashes'][str(path.relative_to(repo))] = digest(path)
        if row['managed_project_candidate'] and row['effective_source']:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', MAESTRO_REPO=str(repo), MAESTRO_BOOTSTRAPPED='1')
            proc = subprocess.run([sys.executable, '-B', '-c', PROBE, row['effective_source'], json.dumps(row['operational_config'])],
                                  cwd='/tmp', env=env, text=True, capture_output=True, timeout=15)
            row['effective_probe_exit'] = proc.returncode
            if proc.returncode == 0:
                row['effective_bindings'] = json.loads(proc.stdout)
            else:
                row['inspection_errors'].append('effective-source probe failed; inspect locally')
        rows.append(row)
    owner_files = ('maestro/backends/catalog.py', 'maestro/limits.py', 'maestro/templates/project.yaml.tmpl',
                   'tests/backends/test_catalog.py', 'tests/test_limits.py')
    result = {
        'inspected_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Immediate children of /home/dan/projects only; no claim of host-wide coverage',
        'global_pointer': global_root,
        'projects': rows,
        'maestro_owner_hashes': {f: digest(MAESTRO / f) for f in owner_files},
        'modelctl_source_hashes': {str(f.relative_to(MODELCTL)): digest(f) for f in sorted((MODELCTL / 'modelctl').rglob('*.py'))},
        'provider_table_hashes': {p: digest(Path(p)) for p in ('/home/dan/.codex/model_context_limits.md', '/home/dan/.claude/model_context_limits.md')},
    }
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
