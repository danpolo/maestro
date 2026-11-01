"""Native callers fence parent controls before preparing either backend."""
from pathlib import Path
import json
import select
import subprocess
import sys
from types import SimpleNamespace

import pytest

from maestro.backends.base import CompletionSpec, Handle, LaunchSpec
from maestro.backends.codex import CodexBackend
from maestro.backends.antigravity import AntigravityBackend
from tests.hang_bound import HANG_SECONDS
from tests.test_git_staging import linked, git

pytestmark = pytest.mark.usefixtures('isolated_native_host_home')


def _await_line(child, expected):
    """Wait for a child's handshake line; the bound only stops a hang."""
    assert select.select([child.stdout], [], [], HANG_SECONDS)[0], f'child never printed {expected!r}'
    assert child.stdout.readline() == expected


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_complete_cleanup_failure_returns_failure_instead_of_raising(tmp_path, monkeypatch, backend):
    from maestro.backends import native_ownership, codex_runtime, agy_runtime
    tree, reports = tmp_path / 'tree', tmp_path / 'reports'
    tree.mkdir()
    reports.mkdir()
    runtime = codex_runtime if backend == 'codex' else agy_runtime
    original = native_ownership.Reservation.cleanup
    def refuse(**kwargs):
        raise ValueError('synthetic preparation refusal')
    def failed_cleanup(owner):
        original(owner)
        raise ValueError('synthetic cleanup failure')
    monkeypatch.setattr(runtime, 'prepare', refuse)
    monkeypatch.setattr(native_ownership.Reservation, 'cleanup', failed_cleanup)
    driver = CodexBackend() if backend == 'codex' else AntigravityBackend()
    result = driver.complete(CompletionSpec(prompt='test', model='fixture', cwd=tree,
                                            log_file=reports / 'impl.log'))
    assert result.returncode != 0


def test_codex_resume_with_different_report_workspace_refuses_before_writes(tmp_path, monkeypatch):
    from maestro.backends import control_files
    old, new, tree = (tmp_path / name for name in ('old', 'new', 'tree'))
    old.mkdir()
    new.mkdir()
    tree.mkdir()
    driver = CodexBackend()
    monkeypatch.setattr(driver, '_write_app_server_launcher', lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(driver, '_new_window', lambda *a: None)
    writes = []
    monkeypatch.setattr(control_files, 'write', lambda path, text: writes.append(path))
    with pytest.raises(ValueError, match='workspace'):
        driver.resume(Handle('codex', 'fixture', 'native', old, 'impl-T-001'), 'again',
                      spec=LaunchSpec('T-001', 'fixture', 'fixture', 'new', new, tree))
    assert writes == []


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_complete_implicit_cwd_fences_tree_before_error_log(linked, monkeypatch, backend):
    from maestro.backends import native_ownership
    _, tree, reports, _ = linked
    owner = native_ownership.Reservation()
    owner.tree(tree)
    monkeypatch.chdir(tree)
    log = reports / 'impl.log'
    log.write_text('original')
    driver = CodexBackend() if backend == 'codex' else AntigravityBackend()
    try:
        result = driver.complete(CompletionSpec(prompt='blocked', model='fixture', log_file=log))
        assert result.returncode != 0
        assert log.read_text() == 'original'
    finally:
        owner.cleanup()


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_git_cleanup_failure_still_releases_independent_native_ownership(tmp_path, backend):
    from maestro.backends import codex_runtime, agy_runtime, native_ownership, git_staging
    owner = native_ownership.Reservation()
    owner.workspace(tmp_path / 'reports')
    def broken_cleanup():
        raise git_staging.GitStagingError('superseded Git reservation')
    run = SimpleNamespace(transaction=SimpleNamespace(cleanup=broken_cleanup),
                          created_mountpoints=(), reservation=owner)
    runtime = codex_runtime if backend == 'codex' else agy_runtime
    with pytest.raises(git_staging.GitStagingError):
        runtime.PreparedRun.cleanup(run)
    marker, = owner.directories
    assert json.loads((marker / 'active.json').read_text())['state'] == 'released'


def test_cleanup_malformed_resource_preserves_evidence_but_releases_other_owned_resources(tmp_path):
    from maestro.backends import native_ownership
    owner = native_ownership.Reservation()
    owner.workspace(tmp_path / 'a')
    owner.workspace(tmp_path / 'b')
    first, second = (path / 'active.json' for path in owner.directories)
    first.write_text('malformed evidence')
    with pytest.raises(ValueError):
        owner.cleanup()
    assert first.read_text() == 'malformed evidence'
    assert json.loads(second.read_text())['state'] == 'released'


@pytest.mark.parametrize('change', ['unknown-state', 'missing-owner', 'bad-owner', 'bad-token',
                                  'nan-deadline', 'string-deadline', 'not-object'])
def test_malformed_marker_cannot_be_reclaimed(tmp_path, change):
    from maestro.backends import native_ownership
    owner = native_ownership.Reservation()
    owner.workspace(tmp_path / 'reports')
    owner.queue()
    marker = owner.directories[0] / 'active.json'
    data = json.loads(marker.read_text())
    if change == 'unknown-state':
        data.update(state='unknown', owner={'pid': 999999999, 'start': '0'})
    elif change == 'missing-owner':
        del data['owner']
    elif change == 'bad-owner':
        data['owner'] = {'pid': 'invalid', 'start': '0'}
    elif change == 'bad-token':
        data['token'] = []
    elif change == 'nan-deadline':
        data['deadline'] = float('nan')
    elif change == 'string-deadline':
        data['deadline'] = 'expired'
    else:
        data = []
    marker.write_text(json.dumps(data))
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='invalid native ownership'):
        native_ownership.Reservation().workspace(tmp_path / 'reports')
    assert marker.read_bytes() == before


def test_duplicate_reservation_directories_rejected_before_worker_locks(tmp_path):
    from maestro.backends import native_ownership
    owner = native_ownership.Reservation()
    owner.workspace(tmp_path / 'reports')
    job = owner.job
    job['directories'] *= 2
    try:
        with pytest.raises(ValueError, match='invalid native ownership'):
            native_ownership.Reservation.load(job)
    finally:
        owner.cleanup()


@pytest.mark.parametrize('backend', ['codex', 'agy'])
@pytest.mark.parametrize('failure', ['load', 'cleanup', 'start'])
def test_rejected_worker_releases_native_resources_without_hiding_git_failure(linked, backend, failure):
    from maestro.backends import codex_auth, git_staging, native_ownership
    _, tree, reports, _ = linked
    owner = native_ownership.Reservation()
    owner.workspace(reports)
    owner.tree(tree)
    transaction = git_staging.Transaction.prepare(tree, reports)
    journal = Path(transaction.job)
    data = json.loads(journal.read_text())
    if failure == 'load':
        data['version'] = 0
        git_staging._dump(journal, data)
    elif failure == 'cleanup':
        data['journal'] += '-superseded'
        git_staging._dump(transaction.lease / 'active.json', data)
    else:
        data['state'] = 'aborted'
        git_staging._dump(transaction.lease / 'active.json', data)
        git_staging._dump(journal, data)
    job = reports / 'job.json'
    auth = reports / 'auth.json'
    auth.write_text('synthetic login')
    # The job timeout is only a ceiling here: every case fails before any turn.
    job.write_text(json.dumps({'git_transaction': transaction.job, 'reservation': owner.job,
                               'created_mountpoints': [], 'timeout': HANG_SECONDS,
                               'auth_identity': codex_auth.capture(auth)}))
    # Queue only once the worker has imported, so its 60 s queued-reservation
    # deadline measures the hand-off, not interpreter startup on a loaded host.
    process = subprocess.Popen([sys.executable, '-c',
        f'import sys; from maestro.backends.{backend}_worker import main; '
        f'print("ready", flush=True); sys.stdin.readline(); main({str(job)!r})'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _await_line(process, b'ready\n')
        owner.queue()
        stdout, stderr = process.communicate(b'go\n', timeout=HANG_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    child = SimpleNamespace(returncode=process.returncode, stdout=stdout.decode(),
                            stderr=stderr.decode())
    assert child.returncode != 0, child.stdout
    assert ('Git' in child.stderr or 'git' in child.stderr), child.stderr
    assert all(json.loads((path / 'active.json').read_text())['state'] == 'released'
               for path in owner.directories)
    # Every marker claimed by the rejected child is now recoverable even while
    # its orchestrator parent remains alive; failure evidence stays in the journal.
    other = native_ownership.Reservation()
    try:
        other.workspace(reports)
        other.tree(tree)
        if failure == 'start':
            recovered = git_staging.Transaction.prepare(tree, reports)
            recovered.cleanup()
    finally:
        other.cleanup()


@pytest.mark.parametrize('resource', ['workspace', 'worktree', 'same-ref'])
def test_simultaneous_native_acquisition_has_exactly_one_owner(linked, tmp_path, resource):
    _, tree, reports, _ = linked
    second_tree = tree
    if resource == 'same-ref':
        second_tree = tmp_path / 'duplicate'
        git(linked[0], 'worktree', 'add', '--force', str(second_tree), 'impl/T-001')
    program = '''
import sys
from pathlib import Path
from maestro.backends.native_ownership import Reservation
owner=Reservation()
print('ready',flush=True)
sys.stdin.readline()
try:
    owner.workspace(Path(sys.argv[1]))
    if sys.argv[3] != 'workspace': owner.tree(Path(sys.argv[2]))
except ValueError:
    print('refused',flush=True)
else:
    print('acquired',flush=True)
sys.stdin.readline()
owner.cleanup()
'''
    children = [subprocess.Popen([sys.executable, '-c', program,
        str(reports if resource == 'workspace' or i == 0 else tmp_path / 'other-reports'),
        str(tree if i == 0 else second_tree), resource],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for i in range(2)]
    try:
        for child in children:
            _await_line(child, b'ready\n')
        for child in children:
            child.stdin.write(b'acquire\n')
            child.stdin.flush()
        results = []
        for child in children:
            assert select.select([child.stdout], [], [], HANG_SECONDS)[0]
            results.append(child.stdout.readline())
        assert sorted(results) == [b'acquired\n', b'refused\n']
    finally:
        for child in children:
            _, error = child.communicate(b'release\n', timeout=HANG_SECONDS)
            assert child.returncode == 0, error


def test_two_queued_workers_cannot_claim_the_same_token(linked):
    from maestro.backends import native_ownership
    _, tree, reports, _ = linked
    owner = native_ownership.Reservation()
    owner.workspace(reports)
    owner.tree(tree)
    job = json.dumps(owner.job)
    program = '''
import json,sys
from maestro.backends.native_ownership import Reservation
owner=Reservation.load(json.loads(sys.argv[1]))
print('ready',flush=True)
sys.stdin.readline()
try:
    owner.start()
except ValueError:
    owner.cleanup()
    print('refused',flush=True)
else:
    print('acquired',flush=True)
sys.stdin.readline()
owner.cleanup()
'''
    children = [subprocess.Popen([sys.executable, '-c', program, job],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        for child in children:
            _await_line(child, b'ready\n')
        # Queue only after both workers have started: the 60 s queued deadline
        # then covers the claim race itself, not interpreter startup under load.
        owner.queue()
        for child in children:
            child.stdin.write(b'claim\n')
            child.stdin.flush()
        results = []
        for child in children:
            assert select.select([child.stdout], [], [], HANG_SECONDS)[0]
            results.append(child.stdout.readline())
        assert sorted(results) == [b'acquired\n', b'refused\n']
        winner = children[results.index(b'acquired\n')]
        owner.cleanup()  # the orchestrator cannot release the winning child
        markers = [json.loads((path / 'active.json').read_text()) for path in owner.directories]
        assert all(data['state'] == 'running' and data['owner']['pid'] == winner.pid for data in markers)
    finally:
        for child in children:
            _, error = child.communicate(b'release\n', timeout=HANG_SECONDS)
            assert child.returncode == 0, error


@pytest.mark.parametrize('stale', ['dead', 'wrong-start', 'expired'])
def test_stale_reservation_recovery_refuses_old_worker_replay(tmp_path, stale):
    from maestro.backends import native_ownership, git_staging
    owner = native_ownership.Reservation()
    reports = tmp_path / 'reports'
    owner.workspace(reports)
    owner.queue()
    marker = owner.directories[0] / 'active.json'
    data = json.loads(marker.read_text())
    if stale == 'dead':
        data['owner']['pid'] = 999999999
    elif stale == 'wrong-start':
        data['owner']['start'] = str(int(data['owner']['start']) + 1)
    else:
        data['deadline'] = -1
    git_staging._dump(marker, data)
    replacement = native_ownership.Reservation()
    replacement.workspace(reports)
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='superseded'):
        native_ownership.Reservation.load(owner.job).start()
    owner.cleanup()
    assert marker.read_bytes() == before
    replacement.cleanup()


@pytest.mark.parametrize('state', ['prepared', 'queued'])
@pytest.mark.parametrize('stale', ['dead', 'wrong-start'])
def test_stale_parent_cannot_hand_resources_to_late_worker(tmp_path, state, stale):
    from maestro.backends import native_ownership, git_staging
    owner = native_ownership.Reservation()
    owner.workspace(tmp_path / 'reports')
    if state == 'queued':
        owner.queue()
    marker = owner.directories[0] / 'active.json'
    data = json.loads(marker.read_text())
    if stale == 'dead':
        data['owner']['pid'] = 999999999
    else:
        data['owner']['start'] = str(int(data['owner']['start']) + 1)
    git_staging._dump(marker, data)
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='fresh reservation'):
        native_ownership.Reservation.load(owner.job).start()
    assert marker.read_bytes() == before


@pytest.mark.parametrize('first', ['codex', 'agy'])
@pytest.mark.parametrize('second', ['codex', 'agy'])
@pytest.mark.parametrize('operation', ['launch', 'resume', 'complete'])
def test_overlapping_call_cannot_mutate_first_launch(tmp_path, monkeypatch, first, second, operation):
    home, tree, reports = (tmp_path / p for p in ('home', 'tree', 'reports'))
    tree.mkdir()
    reports.mkdir()
    (home / '.codex').mkdir(parents=True)
    (home / '.codex/auth.json').write_text('synthetic')
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()

    def driver(name):
        if name == 'codex':
            result = CodexBackend(codex_home=home / '.codex')
            monkeypatch.setattr(result, '_new_window', lambda *args: None)
        else:
            result = AntigravityBackend(gemini_home=home / '.gemini')
            monkeypatch.setattr(result, '_open_window', lambda *args: None)
        return result

    a, b = driver(first), driver(second)
    from maestro.backends import codex_app_server
    def unavailable(*args, **kwargs):
        raise RuntimeError('native boundary must not run on overlap')
    monkeypatch.setattr(codex_app_server, 'run_turn', unavailable)
    if second == 'agy':
        b._run = unavailable
    spec = LaunchSpec('T-001', 'fixture', 'fixture', 'first brief', reports, tree)
    a.launch(spec)
    before = {p.relative_to(reports): p.read_bytes() for p in reports.rglob('*') if p.is_file()}
    if operation == 'complete':
        result = b.complete(CompletionSpec(prompt='overlap', model='fixture', cwd=tree,
            log_file=reports / 'impl.log', timeout=1))
        assert result.returncode != 0
    else:
        with pytest.raises(ValueError, match='reserved'):
            if operation == 'launch':
                b.launch(spec)
            else:
                b.resume(Handle(second, 'fixture', 'native', reports, 'impl-T-001'),
                         'overlap', spec=spec)
    after = {p.relative_to(reports): p.read_bytes() for p in reports.rglob('*') if p.is_file()}
    assert after == before


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_window_failure_after_worker_claim_does_not_release_live_worker(linked, tmp_path,
                                                                       monkeypatch, backend):
    from maestro.backends import native_ownership
    repo, tree, reports, _ = linked
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex/auth.json').write_text('synthetic')
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    driver = (CodexBackend(codex_home=home / '.codex') if backend == 'codex' else
              AntigravityBackend(gemini_home=home / '.gemini'))
    # The stand-in worker starts before the launch and waits for its job path,
    # so the launch's 60 s queued deadline covers the hand-off, not interpreter
    # startup on a loaded host.
    program = '''
import json,sys
from maestro.backends.native_ownership import Reservation
from maestro.backends.git_staging import Transaction
print('ready', flush=True)
job=json.load(open(sys.stdin.readline().strip()))
reservation=Reservation.load(job['reservation'])
transaction=Transaction.load(job['git_transaction'])
reservation.start()
transaction.start()
print('claimed', flush=True)
sys.stdin.readline()
transaction.finish(success=False)
reservation.cleanup()
'''
    child = subprocess.Popen([sys.executable, '-c', program], cwd=Path.cwd(),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    children = [child]
    def starts_then_throws(*args):
        job, = reports.glob(('.codex-runtime/call-*/job.json' if backend == 'codex' else
                             '.agy-runtime/call-*/control/job.json'))
        child.stdin.write(f'{job}\n'.encode())
        child.stdin.flush()
        _await_line(child, b'claimed\n')
        raise RuntimeError('window opened but parent reporting failed')
    monkeypatch.setattr(driver, '_new_window' if backend == 'codex' else '_open_window',
                        starts_then_throws)
    try:
        _await_line(child, b'ready\n')
        with pytest.raises(RuntimeError, match='reporting failed'):
            driver.launch(LaunchSpec('T-001', 'fixture', 'fixture', 'first', reports, tree))
        journal, = reports.glob('.native-git/call-*/transaction.json')
        assert json.loads(journal.read_text())['state'] == 'running'
        other = native_ownership.Reservation()
        with pytest.raises(ValueError, match='reserved'):
            other.workspace(reports)
    finally:
        for child in children:
            output, error = child.communicate(b'release\n', timeout=HANG_SECONDS)
            assert child.returncode == 0, error


def test_same_branch_in_duplicate_worktree_is_reserved_before_runtime_writes(linked, tmp_path):
    from maestro.backends import native_ownership
    repo, tree, reports, _ = linked
    duplicate = tmp_path / 'duplicate'
    git(repo, 'worktree', 'add', '--force', str(duplicate), 'impl/T-001')
    a, b = native_ownership.Reservation(), native_ownership.Reservation()
    try:
        a.workspace(reports)
        a.tree(tree)
        with pytest.raises(ValueError, match='reserved'):
            b.tree(duplicate)
    finally:
        b.cleanup()
        a.cleanup()


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_resume_without_spec_fences_saved_tree_before_control_writes(linked, tmp_path,
                                                                    monkeypatch, backend):
    from maestro.backends import control_files, launch_state, native_ownership
    _, tree, reports, _ = linked
    spec = LaunchSpec('T-001', 'fixture', 'fixture', 'saved', reports, tree)
    driver = CodexBackend() if backend == 'codex' else AntigravityBackend()
    if backend == 'codex':
        driver._write_launch_meta(spec)
    else:
        launch_state.write(spec)
    owner = native_ownership.Reservation()
    owner.tree(tree)
    writes = []
    original = control_files.write
    def observed(path, text):
        writes.append(path)
        return original(path, text)
    monkeypatch.setattr(control_files, 'write', observed)
    try:
        with pytest.raises(ValueError, match='reserved'):
            driver.resume(Handle(backend, 'fixture', 'native', reports, 'impl-T-001'), 'again')
        assert writes == [], 'resume mutated controls before fencing the saved tree'
    finally:
        owner.cleanup()


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_expired_queued_launch_can_recover_git_lease_while_parent_lives(linked, tmp_path,
                                                                      monkeypatch, backend):
    from maestro.backends import git_staging
    _, tree, reports, _ = linked
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex/auth.json').write_text('synthetic')
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    driver = (CodexBackend(codex_home=home / '.codex') if backend == 'codex' else
              AntigravityBackend(gemini_home=home / '.gemini'))
    monkeypatch.setattr(driver, '_new_window' if backend == 'codex' else '_open_window',
                        lambda *args: None)
    spec = LaunchSpec('T-001', 'fixture', 'fixture', 'abandoned', reports, tree)
    driver.launch(spec)
    from tests.backends.test_codex_driver import _job
    job = _job(reports)
    old_journal = Path(job['git_transaction'])
    for directory in job['reservation']['directories']:
        marker = Path(directory) / 'active.json'
        data = json.loads(marker.read_text())
        assert data['state'] == 'queued'
        data['deadline'] = -1  # elapsed handoff deadline, without a real-time sleep
        git_staging._dump(marker, data)
    driver.launch(spec)
    assert json.loads(old_journal.read_text())['state'] == 'interrupted'
    assert len(list(reports.glob('.native-git/call-*/transaction.json'))) == 2
    if backend == 'codex':
        from tests.native_test_home import assert_codex_discovery_isolated
        assert_codex_discovery_isolated(reports, tmp_path / 'host-home')
