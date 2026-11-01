"""OS-level configuration/auth separation, with synthetic credentials only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import time

import pytest

from maestro.backends import codex_app_server
from tests.hang_bound import HANG_SECONDS


def prepare(tmp_path, writable=True):
    from maestro.backends import codex_runtime
    normal = tmp_path / 'native'
    tree, workspace = tmp_path / 'tree', tmp_path / 'reports'
    for path in (normal, tree, workspace):
        path.mkdir(exist_ok=True)
    (normal / 'auth.json').write_text('synthetic-original')
    (normal / 'config.toml').write_text('invalid normal TOML [')
    (normal / 'hooks.json').write_text('host hooks')
    return codex_runtime.prepare(cwd=tree, workspace=workspace, native_home=normal,
        host_home=tmp_path, model='gpt-6-luna', effort='low',
        write_roots=(tree, workspace) if writable else (workspace,),
        allow=('Bash(echo *)', 'Bash(cat *)', 'Bash(python3 *)'))


def test_runtime_separates_config_preserves_auth_and_seals_controls(tmp_path):
    from maestro.backends import codex_runtime
    assert callable(getattr(codex_runtime, 'prepare', None)), 'private Codex runtime missing'
    run = prepare(tmp_path)
    inode = (run.native_home / 'auth.json').stat().st_ino
    config = tomllib.loads(run.config.read_text())
    assert config['features']['unified_exec_tty'] is False
    assert not config.get('mcp_servers')
    program = '''
import json,pathlib,sys,tomllib
normal,tree,report,control,state=map(pathlib.Path,sys.argv[1:])
assert tomllib.loads((normal/'config.toml').read_text())['model']=='gpt-6-luna'
assert (normal/'auth.json').read_text()=='synthetic-original'
(normal/'auth.json').write_text('synthetic-native-refresh')
(normal/'native-state.txt').write_text('persisted')
results=[]
for path in (tree/'allowed',report/'DONE',control/'config.toml',tree.parent/'outside'):
    try:
        path.write_text('changed'); results.append(True)
    except OSError:
        results.append(False)
print(json.dumps(results))
'''
    result = subprocess.run(run.wrap([sys.executable, '-c', program,
        str(run.native_home), str(run.cwd), str(run.workspace), str(run.control), str(run.state)]),
        env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, True, False, False]
    assert (run.native_home / 'auth.json').stat().st_ino == inode
    assert (run.native_home / 'auth.json').read_text() == 'synthetic-native-refresh'
    assert (run.native_home / 'config.toml').read_text() == 'invalid normal TOML ['
    assert (run.state / 'native-state.txt').read_text() == 'persisted'
    assert (run.state / 'auth.json').read_bytes() == b''


def test_reader_cannot_modify_reviewed_tree_and_state_reuses_across_preparations(tmp_path):
    from maestro.backends import codex_runtime
    assert callable(getattr(codex_runtime, 'prepare', None)), 'private Codex runtime missing'
    run = prepare(tmp_path, writable=False)
    (run.state / 'resume-evidence').write_text('memory')
    run.cleanup()
    again = prepare(tmp_path, writable=False)
    assert again.control != run.control
    assert (again.state / 'resume-evidence').read_text() == 'memory'
    result = subprocess.run(again.wrap([sys.executable, '-c',
        "from pathlib import Path; Path('source.py').write_text('bad')"]),
        env=again.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode != 0
    assert not (run.cwd / 'source.py').exists()
    assert again.policy.file('Edit', run.control / 'config.toml').allowed is False


def test_credential_contents_never_copied_and_environment_is_allowlisted(tmp_path, monkeypatch):
    from maestro.backends import codex_runtime
    assert callable(getattr(codex_runtime, 'prepare', None)), 'private Codex runtime missing'
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-unwanted-key')
    monkeypatch.setenv('PYTHONPATH', 'hostile')
    monkeypatch.setenv('CODEX_THREAD_ID', 'parent-thread')
    run = prepare(tmp_path)
    assert not {'OPENAI_API_KEY', 'PYTHONPATH', 'CODEX_THREAD_ID'} & run.environment().keys()
    assert not any(b'synthetic-original' in p.read_bytes()
                   for p in run.directory.rglob('*') if p.is_file())


def test_private_runtime_preserves_only_selected_native_model_metadata(tmp_path):
    from maestro.backends import codex_runtime
    initial = prepare(tmp_path)
    catalog = {'models': [{'slug': initial.model, 'context_window': 340000,
                           'max_context_window': 872000},
                          {'slug': 'another-model', 'context_window': 999999}]}
    source = initial.native_home / 'model_catalog.json'
    source.write_text(json.dumps(catalog))
    (initial.native_home / 'config.toml').write_text(
        f'model_catalog_json = {json.dumps(str(source))}\n'
        'model_context_window = 320000\nmodel_auto_compact_token_limit = 290000\n'
        'notify = ["unwanted-hook"]\n')
    initial.cleanup()
    run = codex_runtime.prepare(cwd=initial.cwd, workspace=initial.workspace,
        native_home=initial.native_home, host_home=tmp_path, model=initial.model,
        effort='low', write_roots=initial.write_roots, allow=())
    private = tomllib.loads(run.config.read_text())
    assert private['model_context_window'] == 320000
    assert private['model_auto_compact_token_limit'] == 290000
    copied = Path(private['model_catalog_json'])
    assert copied.is_relative_to(run.control)
    assert json.loads(copied.read_text()) == {'models': [catalog['models'][0]]}
    assert 'notify' not in private
    source.write_text('source changed after preparation')
    assert json.loads(copied.read_text()) == {'models': [catalog['models'][0]]}


def test_worker_cannot_replace_parent_log_or_resume_controls(tmp_path):
    from maestro.backends import codex_runtime
    run = prepare(tmp_path)
    names = ('impl.log', 'codex_thread_id.txt', 'codex_launch.json', 'launch.py')
    for name in names:
        (run.workspace / name).write_text('trusted')
    run.cleanup()
    run = codex_runtime.prepare(cwd=run.cwd, workspace=run.workspace,
        model=run.model, effort=run.effort, write_roots=run.write_roots,
        native_home=run.native_home, host_home=tmp_path, allow=())
    program = '''
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
outcomes=[]
for name in sys.argv[2:]:
    for rename in (False,True):
        try:
            if rename:
                (root/name).rename(root/(name+'.moved'))
            else:
                (root/name).write_text('tampered')
            outcomes.append(True)
        except OSError:
            outcomes.append(False)
print(json.dumps(outcomes))
'''
    child = subprocess.run(run.wrap([sys.executable, '-c', program,
        str(run.workspace), *names]), env=run.environment(),
        capture_output=True, text=True, timeout=HANG_SECONDS)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == [False] * (len(names) * 2)
    assert all((run.workspace / name).read_text() == 'trusted' for name in names)


def test_symlink_runtime_and_auth_refuse_before_launch(tmp_path):
    from maestro.backends import codex_runtime
    assert callable(getattr(codex_runtime, 'prepare', None)), 'private Codex runtime missing'
    run = prepare(tmp_path)
    original = run.native_home / 'auth.json'
    original.unlink()
    original.symlink_to(run.native_home / 'config.toml')
    with pytest.raises(ValueError):
        prepare(tmp_path)


def test_ancestor_write_root_cannot_unmask_normal_native_configuration(tmp_path):
    from maestro.backends import codex_runtime
    initial = prepare(tmp_path)
    initial.cleanup()
    run = codex_runtime.prepare(cwd=tmp_path, workspace=initial.workspace,
        native_home=initial.native_home, host_home=tmp_path, model=initial.model,
        effort='low', write_roots=(tmp_path,), allow=())
    result = subprocess.run(run.wrap([sys.executable, '-c',
        'import os,tomllib; from pathlib import Path; '
        'print(tomllib.loads((Path(os.environ["CODEX_HOME"])/"config.toml").read_text())["model"])']),
        env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == initial.model
    assert (initial.native_home / 'config.toml').read_text() == 'invalid normal TOML ['


def test_worker_cannot_poison_controller_source_for_the_next_invocation(tmp_path, monkeypatch):
    from maestro.backends import codex_runtime
    import shutil
    initial = prepare(tmp_path)
    source = initial.cwd / 'installation' / 'maestro'
    shutil.copytree(Path(codex_runtime.__file__).resolve().parents[1], source)
    monkeypatch.setattr(codex_runtime, '__file__', str(source / 'backends/codex_runtime.py'))
    initial.cleanup()
    run = codex_runtime.prepare(cwd=initial.cwd, workspace=initial.workspace,
        native_home=initial.native_home, host_home=tmp_path, model=initial.model,
        effort='low', write_roots=initial.write_roots, allow=('Bash(python3 *)',))
    victim = source / 'backends/codex_worker.py'
    original = victim.read_bytes()
    program = '''
from pathlib import Path
import sys
victim = Path(sys.argv[1])
try:
    victim.write_text('raise SystemExit("poisoned")')
except OSError:
    print('refused')
else:
    raise SystemExit('worker changed future controller code')
'''
    result = subprocess.run(run.wrap([sys.executable, '-c', program, str(victim)]),
        env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'refused'
    assert victim.read_bytes() == original
    assert not run.policy.file('Edit', victim).allowed


@pytest.mark.parametrize('path,value', [
    (('model_reasoning_effort',), 'high'),
    (('sandbox_mode',), 'danger-full-access'),
    (('approval_policy',), 'never'),
    (('sandbox_workspace_write', 'network_access'), True),
    (('sandbox_workspace_write', 'exclude_slash_tmp'), False),
    (('permissions', 'maestro', 'filesystem', '/'), 'write'),
    (('permissions', 'maestro', 'network', 'enabled'), True),
    (('permissions', 'maestro', 'extends'), ':danger-full-access'),
    (('projects', 'CWD', 'trust_level'), 'trusted'),
    (('agents', 'enabled'), True),
    (('shell_environment_policy', 'set'), {'BASH_ENV': '/hostile'}),
    (('allow_login_shell',), True),
    (('project_doc_max_bytes',), 32768),
    (('tools',), {'web_search': True}),
    (('notify',), ['injected-hook']),
    (('model_providers',), {'openai': {'base_url': 'https://invalid.example'}}),
])
def test_effective_config_refuses_changed_worker_boundary(tmp_path, path, value):
    run = prepare(tmp_path)
    effective = tomllib.loads(run.config.read_text())
    effective.setdefault('shell_environment_policy', {})
    cursor = effective
    for key in path[:-1]:
        key = str(run.cwd) if key == 'CWD' else key
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value
    with pytest.raises(RuntimeError, match='sealed profile'):
        run.verify_config({'config': effective})


def test_effective_config_requires_exact_roots_and_secret_denials(tmp_path):
    run = prepare(tmp_path)
    effective = tomllib.loads(run.config.read_text())
    profile = effective['permissions']['maestro']['filesystem']
    del profile[str(run.native_home / 'auth.json')]
    with pytest.raises(RuntimeError, match='sealed profile'):
        run.verify_config({'config': effective})
    effective = tomllib.loads(run.config.read_text())
    effective['permissions']['maestro']['filesystem'][str(tmp_path / 'outside')] = 'write'
    with pytest.raises(RuntimeError, match='sealed profile'):
        run.verify_config({'config': effective})


@pytest.mark.skipif(os.environ.get('MAESTRO_TEST_STOCK_CODEX') != '1',
                    reason='explicit installed-runtime offline qualification lane')
def test_stock_codex_loads_private_profile_and_denies_nested_secret_reads(tmp_path):
    run = prepare(tmp_path)
    (run.native_home / 'auth.json').write_text('{"OPENAI_API_KEY":"sk-synthetic-not-real"}')
    nested = run.cwd / 'nested'
    nested.mkdir()
    (nested / '.env').write_text('synthetic-secret')
    (run.cwd / '.env').write_text('synthetic-secret')
    proc = subprocess.Popen(run.wrap(['codex', 'app-server']), cwd=run.cwd,
        env=run.environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True)
    client = codex_app_server._Client(proc, deadline=time.monotonic() + HANG_SECONDS,
                                     policy=run.policy, emit=lambda _: None)
    try:
        client.rpc('initialize', {'clientInfo': {'name': 'maestro-offline', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        client.send({'method': 'initialized'})
        run.verify_config(client.rpc('config/read', {'cwd': str(run.cwd)}))
        result = client.rpc('command/exec', {'command': ['/bin/bash', '-c', 'echo OK > allowed'],
            'cwd': str(run.cwd), 'permissionProfile': 'maestro', 'timeoutMs': 5000})
        assert result['exitCode'] == 0, result
        for path in ('.env', 'nested/.env'):
            denied = client.rpc('command/exec', {'command': ['/bin/cat', path],
                'cwd': str(run.cwd), 'permissionProfile': 'maestro', 'timeoutMs': 5000})
            assert denied['exitCode'] != 0, denied
            assert 'synthetic-secret' not in denied.get('stdout', '')
        for alias in ('.codex', '.agents', '.agent', '_agents', '_agent', '.claude'):
            injected = client.rpc('command/exec', {'command': [sys.executable, '-c',
                'from pathlib import Path; import sys; '
                'p=Path(sys.argv[1]); p.mkdir(exist_ok=True); '
                '(p/"config.toml").write_text("injected")', alias],
                'cwd': str(run.cwd), 'permissionProfile': 'maestro', 'timeoutMs': 5000})
            assert injected['exitCode'] != 0, (alias, injected)
            assert not (run.cwd / alias / 'config.toml').exists()
    finally:
        codex_app_server._stop(proc)
    assert (run.cwd / 'allowed').read_text().strip() == 'OK'
