"""Original-login identity changes must never authorize task promotion."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from maestro.backends import codex_app_server, codex_runtime, codex_worker
from tests.test_git_staging import linked, git, confined
from tests.test_codex_app_server import server
from tests.test_codex_backend_app_server import configured_driver
from maestro.backends.base import CompletionSpec
from tests.codex_host_isolation import skip_real_home_inventory
# Completion budget in cases that assert an auth-identity refusal (the shared
# HANG_SECONDS). It only stops a hung case: a tight budget let preparation cost under
# load, not the auth seam, decide between the refusal (1) and a timeout (124).
from tests.hang_bound import HANG_SECONDS


@pytest.fixture(autouse=True)
def _no_real_home_inventory(monkeypatch):
    # host_home=tmp_path alone does not isolate preparation: the fixture trees'
    # ancestors reach the developer's real home discovery directories.
    skip_real_home_inventory(monkeypatch)


def replace_auth(auth):
    replacement = auth.with_name('replacement.json')
    replacement.write_text('{"OPENAI_API_KEY":"sk-synthetic-replaced"}')
    replacement.replace(auth)


@pytest.mark.parametrize('boundary', ['exec', 'item/started', 'turn/completed'])
def test_production_completion_rejects_live_auth_replacement_and_reaps_child(
        configured_driver, tmp_path, monkeypatch, boundary):
    tree, reports = tmp_path / 'tree', tmp_path / 'reports'
    tree.mkdir()
    reports.mkdir()
    # Keep the real preparation/namespace path, with this fixture's host profile.
    # Inventorying the operator's live home can exhaust the call before the
    # auth-replacement seam, making the result depend on unrelated home contents.
    original_prepare = codex_runtime.prepare
    def prepared(**kwargs):
        return original_prepare(**kwargs, host_home=tmp_path)
    monkeypatch.setattr(codex_runtime, 'prepare', prepared)
    processes, changed = [], []
    original_event = codex_app_server._Client.event
    def change():
        if not changed:
            replace_auth(configured_driver.codex_home() / 'auth.json')
            changed.append(True)
    def popen(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        processes.append(child)
        if boundary == 'exec':
            change()
        return child
    def event(client, message):
        if message.get('method') == boundary:
            change()
        return original_event(client, message)
    monkeypatch.setattr(configured_driver, '_popen', popen)
    monkeypatch.setattr(codex_app_server._Client, 'event', event)
    result = configured_driver.complete(CompletionSpec(prompt='fixture', model='gpt-6-luna',
        cwd=tree, log_file=reports / 'impl.log', timeout=HANG_SECONDS))
    assert changed and processes and all(p.poll() is not None for p in processes)
    assert result.returncode == 1 and result.text == ''
    log = (reports / 'impl.log').read_text()
    assert 'auth identity changed' in log
    if boundary != 'turn/completed':
        assert '"decision": "accept"' not in log


@pytest.mark.parametrize('boundary', ['result', 'ready-journal'])
def test_production_completion_rejects_replaced_auth_without_promoting(
        linked, configured_driver, tmp_path, monkeypatch, boundary):
    from maestro.backends import git_staging
    repo, tree, reports, start = linked
    runs = []
    original_prepare = codex_runtime.prepare
    def prepared(**kwargs):
        run = original_prepare(**kwargs, host_home=tmp_path)
        runs.append(run)
        return run
    def completed(**kwargs):
        staged = confined(runs[0].transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('completion staged change\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'staged'], check=True)
''')
        assert staged.returncode == 0, staged.stderr
        if boundary == 'result':
            replace_auth(configured_driver.codex_home() / 'auth.json')
        return codex_app_server.TurnResult('DONE', 'synthetic-thread')
    original_save = git_staging.Transaction._save
    def save(transaction, data, state):
        original_save(transaction, data, state)
        if state == 'ready' and boundary == 'ready-journal':
            replace_auth(configured_driver.codex_home() / 'auth.json')
    monkeypatch.setattr(codex_runtime, 'prepare', prepared)
    monkeypatch.setattr(codex_app_server, 'run_turn', completed)
    monkeypatch.setattr(git_staging.Transaction, '_save', save)
    result = configured_driver.complete(CompletionSpec(prompt='fixture', model='gpt-6-luna',
        cwd=tree, log_file=reports / 'impl.log', writable=True, timeout=HANG_SECONDS))
    assert runs
    assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == start
    assert result.returncode == 1 and result.text == ''
    assert 'auth identity changed' in (reports / 'impl.log').read_text()
    assert json.loads(Path(runs[0].transaction.job).read_text())['state'] == 'aborted'


def prepare(linked, tmp_path):
    _, tree, reports, _ = linked
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'auth.json').write_text('{"OPENAI_API_KEY":"sk-synthetic-original"}')
    return codex_runtime.prepare(cwd=tree, workspace=reports, native_home=native,
        host_home=tmp_path, model='gpt-6-luna', effort='low', write_roots=(tree, reports),
        allow=('Bash(echo *)',))


def test_production_worker_rejects_replaced_auth_without_promoting(linked, tmp_path, monkeypatch):
    repo, tree, reports, start = linked
    run = prepare(linked, tmp_path)
    try:
        run.launcher(binary='codex', prompt='fixture')
        def completed(job, *, lifetime):
            lifetime.verify()
            staged = confined(run.transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('staged change\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'staged'], check=True)
''')
            assert staged.returncode == 0, staged.stderr
            replace_auth(run.native_home / 'auth.json')
            return codex_app_server.TurnResult('DONE', 'synthetic-thread')
        monkeypatch.setattr(codex_worker, '_run', completed)
        with pytest.raises((ValueError, RuntimeError), match='auth.*identity'):
            codex_worker.main(str(run.control / 'job.json'))
        assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == start
        assert json.loads(Path(run.transaction.job).read_text())['state'] == 'aborted'
    finally:
        run.cleanup()


@pytest.mark.parametrize('boundary', ['wrap', 'launcher', 'mount-capture'])
def test_auth_replacement_after_preparation_is_refused(linked, tmp_path, boundary):
    from maestro.backends import control_files
    run = prepare(linked, tmp_path)
    try:
        wrapped = run.wrap(['codex', 'app-server'])
        replace_auth(run.native_home / 'auth.json')
        with pytest.raises(ValueError, match='identity'):
            if boundary == 'wrap':
                run.wrap(['codex', 'app-server'])
            elif boundary == 'launcher':
                run.launcher(binary='codex', prompt='fixture')
            else:
                with control_files.pinned_mounts(wrapped):
                    pytest.fail('replaced auth was accepted')
    finally:
        run.cleanup()


@pytest.mark.parametrize('boundary', ['exec', 'item/started', 'turn/completed'])
def test_worker_protocol_rejects_live_auth_replacement_and_reaps_child(
        linked, tmp_path, monkeypatch, server, boundary):
    _, tree, _, start = linked
    run = prepare(linked, tmp_path)
    processes = []
    try:
        run.launcher(binary='codex', prompt='fixture')
        job_path = run.control / 'job.json'
        job = json.loads(job_path.read_text())
        wrapped = run.wrap([sys.executable, str(server), 'ok'])
        job.update(argv=list(wrapped), mounts=wrapped.identities)
        job_path.write_text(json.dumps(job))
        original_turn, original_event = codex_worker.run_turn, codex_app_server._Client.event
        changed = []
        def change():
            if not changed:
                replace_auth(run.native_home / 'auth.json')
                changed.append(True)
        def popen(*args, **kwargs):
            child = subprocess.Popen(*args, **kwargs)
            processes.append(child)
            if boundary == 'exec':
                change()
            return child
        def turn(**kwargs):
            return original_turn(**kwargs, popen=popen)
        def event(client, message):
            if message.get('method') == boundary:
                change()
            return original_event(client, message)
        monkeypatch.setattr(codex_worker, 'run_turn', turn)
        monkeypatch.setattr(codex_app_server._Client, 'event', event)
        with pytest.raises(SystemExit) as failure:
            codex_worker.main(str(job_path))
        assert failure.value.code == 1
        assert changed and processes and all(p.poll() is not None for p in processes)
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert json.loads(Path(run.transaction.job).read_text())['state'] == 'aborted'
        log = (run.workspace / 'impl.log').read_text()
        assert 'auth identity changed' in log
        if boundary != 'turn/completed':
            assert '"decision": "accept"' not in log
    finally:
        run.cleanup()


def test_auth_replacement_during_preparation_aborts_acquired_transaction(linked, tmp_path, monkeypatch):
    from maestro.backends import git_staging
    original = git_staging.Transaction.prepare
    transactions = []
    def replaced(*args):
        transaction = original(*args)
        transactions.append(transaction)
        replace_auth(tmp_path / 'native/auth.json')
        return transaction
    monkeypatch.setattr(git_staging.Transaction, 'prepare', replaced)
    with pytest.raises(ValueError, match='auth.*identity'):
        prepare(linked, tmp_path)
    assert json.loads(Path(transactions[0].job).read_text())['state'] == 'aborted'


def test_auth_replacement_at_ready_journal_prevents_cas(linked, tmp_path, monkeypatch):
    from maestro.backends import git_staging
    run = prepare(linked, tmp_path)
    try:
        run.transaction.start()
        original = git_staging.Transaction._save
        def changed(transaction, data, state):
            original(transaction, data, state)
            if state == 'ready':
                replace_auth(run.native_home / 'auth.json')
        monkeypatch.setattr(git_staging.Transaction, '_save', changed)
        with pytest.raises(ValueError, match='auth.*identity'):
            run.transaction.finish(success=True, verify=run.verify_auth)
        assert git(run.cwd, 'rev-parse', 'HEAD').stdout.strip() == linked[3]
        assert json.loads(Path(run.transaction.job).read_text())['state'] == 'aborted'
    finally:
        run.cleanup()


def test_same_inode_native_refresh_is_allowed_without_reading_credentials(linked, tmp_path, monkeypatch):
    from maestro.backends import codex_auth, control_files
    run = prepare(linked, tmp_path)
    try:
        auth = run.native_home / 'auth.json'
        auth.write_text('{"OPENAI_API_KEY":"sk-synthetic-native-renewal"}')
        original = control_files.read
        def read(path, *args, **kwargs):
            assert Path(path) != auth, 'parent read credential contents'
            return original(path, *args, **kwargs)
        monkeypatch.setattr(control_files, 'read', read)
        codex_auth.verify(run.auth_identity)
        run.wrap(['codex', 'app-server'])
    finally:
        run.cleanup()


@pytest.mark.parametrize('defect', ['missing', 'replaced'])
def test_worker_rejects_stale_or_missing_auth_before_native_start_and_settles_lease(
        linked, tmp_path, monkeypatch, defect):
    run = prepare(linked, tmp_path)
    try:
        run.launcher(binary='codex', prompt='fixture')
        job_path = run.control / 'job.json'
        if defect == 'missing':
            job = json.loads(job_path.read_text())
            job.pop('auth_identity', None)
            job_path.write_text(json.dumps(job))
        else:
            replace_auth(run.native_home / 'auth.json')
        called = []
        def native(job):
            called.append(True)
            return codex_app_server.TurnResult('DONE', 'synthetic')
        monkeypatch.setattr(codex_worker, '_run', native)
        with pytest.raises(ValueError, match='auth.*identity'):
            codex_worker.main(str(job_path))
        assert not called, 'native process started with unavailable original auth identity'
        assert json.loads(Path(run.transaction.job).read_text())['state'] == 'aborted'
    finally:
        run.cleanup()
