"""Native commits stay private until the trusted exact-ref audit/promotion."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from tests.hang_bound import HANG_SECONDS
from tests.test_codex_app_server import server

from maestro import confinement

pytestmark = pytest.mark.usefixtures('isolated_native_host_home')


def git(where, *args, check=True):
    return subprocess.run(['git', '-C', str(where), *args], check=check,
                          capture_output=True, text=True, timeout=HANG_SECONDS)


@pytest.mark.parametrize('kind', ['files', 'directories'])
@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_metadata_traversal_never_reads_a_replaced_source_root(tmp_path, monkeypatch, kind, replacement):
    from maestro.backends import git_staging
    root, hostile = tmp_path / 'root', tmp_path / 'hostile'
    for directory, name in ((root, 'approved'), (hostile, 'substituted')):
        (directory / name).mkdir(parents=True)
        (directory / name / 'tip').write_text(name)
    inode = root.stat().st_ino
    original_walk, original_listdir = os.walk, os.listdir
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(tmp_path / 'saved')
            if replacement == 'symlink':
                root.symlink_to(hostile, target_is_directory=True)
            else:
                hostile.rename(root)
    def walk(path, *args, **kwargs):
        if path == root:
            swap()
        return original_walk(path, *args, **kwargs)
    def listdir(path):
        if isinstance(path, int) and os.fstat(path).st_ino == inode:
            swap()
        return original_listdir(path)
    monkeypatch.setattr(os, 'walk', walk)
    monkeypatch.setattr(os, 'listdir', listdir)
    try:
        result = git_staging._inventory(root) if kind == 'files' else git_staging._directories(root)
    except (ValueError, OSError):
        pass
    else:
        assert result == ({'approved/tip': b'approved'} if kind == 'files' else {'approved'})
    assert swapped


@pytest.mark.parametrize('directory', ['tree', 'own', 'common'])
def test_trusted_git_uses_pinned_directory_inputs_during_exec(linked, tmp_path, monkeypatch, directory):
    from maestro.backends import git_staging
    repo, tree, _, start = linked
    own = repo / '.git/worktrees/tree'
    source = {'tree': tree, 'own': own, 'common': repo / '.git'}[directory]
    outside = tmp_path / 'substituted'
    shutil.copytree(source, outside, symlinks=True)
    if directory == 'tree':
        (outside / 'file.txt').write_text('substituted content\n')
        arguments = ('hash-object', 'file.txt')
        expected = git(tree, *arguments).stdout.strip()
    elif directory == 'own':
        (outside / 'HEAD').write_text('ref: refs/heads/evil\n')
        arguments, expected = ('symbolic-ref', '-q', 'HEAD'), 'refs/heads/impl/T-001'
    else:
        (outside / 'refs/heads/main').write_text('0' * 40 + '\n')
        arguments, expected = ('rev-parse', 'refs/heads/main'), start
    original = subprocess.run
    swapped = []
    def swap_then_run(argv, **kwargs):
        if argv[0] == 'git' and not swapped:
            saved = source.with_name(source.name + '-original')
            source.rename(saved)
            source.symlink_to(outside, target_is_directory=True)
            swapped.append(saved)
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', swap_then_run)
    try:
        try:
            result = git_staging._git(tree, *arguments)
        except (ValueError, OSError):
            pass  # refusing substituted path identity is also safe
        else:
            assert result.stdout.strip() == expected
        assert swapped
    finally:
        if swapped:
            source.unlink()
            swapped[0].rename(source)


@pytest.fixture
def linked(tmp_path):
    repo, tree, reports = (tmp_path / name for name in ('repo', 'tree', 'reports'))
    repo.mkdir()
    reports.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'core.hooksPath', '/dev/null')
    (repo / 'file.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    start = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    git(repo, 'branch', 'impl/T-002', start)
    git(repo, 'worktree', 'add', '-b', 'impl/T-001', str(tree), start)
    return repo, tree, reports, start


def confined(transaction, tree, source):
    argv = [sys.executable, '-c', source]
    wrapped = confinement.bubblewrap_argv(argv, cwd=tree,
        write_roots=(tree, *transaction.write_roots))
    split = len(wrapped) - len(argv) - 1
    return subprocess.run([*wrapped[:split], *transaction.mounts(), *wrapped[split:]],
                          text=True, capture_output=True, timeout=HANG_SECONDS)


def test_agy_git_add_all_excludes_generated_controls_but_keeps_user_files(linked, tmp_path):
    from maestro.backends import agy_runtime
    repo, tree, reports, start = linked
    home = tmp_path / 'home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    rule = tree / '.agents/rules/project.md'
    rule.parent.mkdir(parents=True)
    rule.write_text('a real project rule\n')
    run = agy_runtime.prepare(cwd=tree, workspace=reports,
        write_roots=(tree, reports), home=home, allow=('Bash(git *)',))
    try:
        run.transaction.start()
        program = '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('native change\\n')
subprocess.run(['git', 'add', '-A'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'all'], check=True)
'''
        result = subprocess.run(run.wrap([sys.executable, '-c', program]),
            env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
        assert result.returncode == 0, result.stderr
        tip = run.transaction.finish(success=True)
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == tip != start
        assert git(tree, 'diff-tree', '--no-commit-id', '--name-only', '-r', tip).stdout.splitlines() == [
            '.agents/rules/project.md', 'file.txt']
        assert git(tree, 'show', tip+':.agents/rules/project.md').stdout == 'a real project rule\n'
        assert git(tree, 'status', '--porcelain').stdout == ''
        assert git(repo, 'rev-parse', 'main').stdout.strip() == start
        assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start
    finally:
        run.cleanup()


@pytest.mark.parametrize('surface', ['tracked-hook', 'omitted-plugin', 'rewritten-rule'])
def test_exact_task_staging_keeps_real_frozen_customization_bytes(linked, tmp_path, surface):
    from maestro.backends import agy_runtime
    repo, tree, reports, _ = linked
    home = tmp_path / 'home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    if surface == 'tracked-hook':
        real, original = tree / '.agents/hooks.json', '{"real-user-hook":{"enabled":false}}\n'
    elif surface == 'omitted-plugin':
        real, original = tree / '.agents/plugins/real.txt', 'real user plugin\n'
    else:
        real, original = tree / '.agents/rules/real.md', '@[rule](../../instruction.txt)\n'
        (tree / 'instruction.txt').write_text('real included task content\n')
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(original)
    git(tree, 'add', '-A')
    git(tree, 'commit', '-m', 'real customization')
    main = git(repo, 'rev-parse', 'main').stdout
    sibling = git(repo, 'rev-parse', 'impl/T-002').stdout
    run = agy_runtime.prepare(cwd=tree, workspace=reports,
        write_roots=(tree, reports), home=home, allow=('Bash(git *)',))
    try:
        run.transaction.start()
        program = '''
from pathlib import Path
import subprocess
guard = Path('.agents/hooks.json')
assert 'maestro-permissions' in guard.read_text()
try:
    guard.write_text('{}')
except OSError:
    pass
else:
    raise AssertionError('execution guard was writable')
Path('file.txt').write_text('native task edit\\n')
subprocess.run(['git', 'add', '--', 'file.txt'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'task edit'], check=True)
'''
        result = subprocess.run(run.wrap([sys.executable, '-I', '-c', program]),
            env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
        assert result.returncode == 0, result.stderr
        tip = run.transaction.finish(success=True)
        assert git(tree, 'diff-tree', '--no-commit-id', '--name-only', '-r', tip).stdout == 'file.txt\n'
        assert git(tree, 'show', tip+':'+real.relative_to(tree).as_posix()).stdout == original
        assert real.read_text() == original
        assert git(tree, 'status', '--porcelain').stdout == ''
        assert git(repo, 'rev-parse', 'main').stdout == main
        assert git(repo, 'rev-parse', 'impl/T-002').stdout == sibling
    finally:
        run.cleanup()


def test_real_commit_is_private_until_exact_task_promotion(linked):
    from maestro.backends.git_staging import Transaction
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    result = confined(transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('native change\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'native'], check=True)
subprocess.run(['git', 'log', '-1', '--oneline'], check=True)
subprocess.run(['git', 'diff', '--exit-code', 'HEAD'], check=True)
''')
    assert result.returncode == 0, result.stderr
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    new = transaction.finish(success=True)
    assert new != start
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == new
    assert git(tree, 'status', '--porcelain').stdout == ''
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start


@pytest.mark.parametrize('override', ['forced-add', 'project-negation'])
def test_generated_control_override_still_refuses_publication(linked, tmp_path, override):
    from maestro.backends import agy_runtime, git_staging
    repo, tree, reports, start = linked
    home = tmp_path / 'home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    if override == 'project-negation':
        (tree / '.gitignore').write_text('!/.agents/hooks.json\n')
    original_exclude = (repo / '.git/info/exclude').read_bytes()
    run = agy_runtime.prepare(cwd=tree, workspace=reports,
        write_roots=(tree, reports), home=home, allow=('Bash(git *)',))
    try:
        run.transaction.start()
        program = '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('native change\\n')
subprocess.run(['git', 'add', '-A'], check=True)
'''
        if override == 'forced-add':
            program += "subprocess.run(['git', 'add', '-f', '.agents/hooks.json'], check=True)\n"
        program += "subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'override'], check=True)\n"
        result = subprocess.run(run.wrap([sys.executable, '-c', program]),
            env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
        assert result.returncode == 0, result.stderr
        with pytest.raises(git_staging.GitStagingError, match='not clean at staged tip'):
            run.transaction.finish(success=True)
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert git(repo, 'rev-parse', 'main').stdout.strip() == start
        assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start
        assert (repo / '.git/info/exclude').read_bytes() == original_exclude
        assert not (tree / '.agents/hooks.json').exists()
    finally:
        run.cleanup()


def test_generated_exclusion_never_hides_existing_user_control(linked):
    from maestro.backends.git_staging import Transaction, GitStagingError
    _, tree, reports, _ = linked
    control = tree / '.agents/hooks.json'
    control.parent.mkdir()
    control.write_text('real user configuration\n')
    transaction = Transaction.prepare(tree, reports)
    try:
        with pytest.raises(GitStagingError, match='real user file'):
            transaction.exclude_generated([control])
        assert control.read_text() == 'real user configuration\n'
        assert not (transaction.directory / 'discovery-info').exists()
    finally:
        transaction.cleanup()


def test_agy_failed_publication_keeps_native_output_and_failure(linked, tmp_path):
    from maestro.backends.antigravity import AntigravityBackend
    from maestro.backends.base import CompletionSpec
    repo, tree, reports, start = linked
    home = tmp_path / 'home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    raw = json.dumps({'status': 'SUCCESS', 'response': 'synthetic native response',
                      'usage': {'total_tokens': 123}, 'conversation_id': 'synthetic'})
    program = '''
from pathlib import Path
import subprocess, sys
Path('file.txt').write_text('native change\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True, stdout=sys.stderr)
subprocess.run(['git', 'add', '-f', '.agents/hooks.json'], check=True, stdout=sys.stderr)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'refused'], check=True, stdout=sys.stderr)
'''+f"print({raw!r})\nprint('synthetic native stderr', file=sys.stderr)\n"
    def native(argv, **kwargs):
        split = argv.index('--')
        return subprocess.run([*argv[:split+1], sys.executable, '-c', program],
            cwd=tree, env=kwargs['env'], capture_output=True, text=True, timeout=HANG_SECONDS)
    driver = AntigravityBackend(gemini_home=home / '.gemini', runner=native)
    result = driver.complete(CompletionSpec(prompt='synthetic inference', model='fixture',
        cwd=tree, writable=True, log_file=reports / 'impl.log'))
    assert result.returncode == 1 and result.text == ''
    log = (reports / 'impl.log').read_text()
    assert raw in log
    assert 'synthetic native stderr' in log
    assert 'native Git worktree is not clean at staged tip' in log
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    journal, = (reports / '.native-git').glob('call-*/transaction.json')
    assert json.loads(journal.read_text())['state'] == 'aborted'


@pytest.mark.parametrize('target', ['refs/heads/impl/T-002', 'config', 'HEAD',
                                  'worktrees/tree/commondir'])
def test_host_metadata_and_existing_siblings_are_read_only(linked, target):
    from maestro.backends.git_staging import Transaction
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    path = repo / '.git' / target
    before = path.read_bytes()
    result = confined(transaction, tree,
        f'from pathlib import Path; Path({str(path)!r}).write_text("tampered")')
    assert result.returncode != 0
    assert path.read_bytes() == before
    transaction.finish(success=False)


@pytest.mark.parametrize('change', ['foreign-ref', 'foreign-log', 'ref-lock',
                                  'admin-lock', 'empty-foreign-dir', 'dirty', 'detached'])
def test_postrun_audit_refuses_unpublishable_native_state(linked, change):
    from maestro.backends.git_staging import Transaction
    from maestro.backends.git_staging import GitStagingError as ConfinementError
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    paths = {'foreign-ref': repo / '.git/refs/heads/impl/foreign',
             'foreign-log': repo / '.git/logs/refs/heads/impl/foreign',
             'ref-lock': repo / '.git/refs/heads/impl/T-001.lock',
             'admin-lock': transaction.own / 'index.lock'}
    if change in paths:
        source = f'from pathlib import Path; Path({str(paths[change])!r}).write_text({start!r})'
        result = confined(transaction, tree, source)
        assert result.returncode == 0, result.stderr
    elif change == 'empty-foreign-dir':
        path = repo / '.git/refs/heads/impl/foreign'
        result = confined(transaction, tree,
            f'from pathlib import Path; Path({str(path)!r}).mkdir()')
        assert result.returncode == 0, result.stderr
    elif change == 'dirty':
        (tree / 'file.txt').write_text('uncommitted')
    else:
        # Host seam represents a corrupted persistent own admin at settle.
        (transaction.own / 'HEAD').write_text(start + '\n')
    with pytest.raises(ConfinementError):
        transaction.finish(success=True)
    assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == start
    assert 'aborted' in Path(transaction.job).read_text()


@pytest.mark.parametrize('change', ['add-directory', 'remove-directory', 'existing-lock'])
def test_foreign_own_admin_state_cannot_be_published(linked, change):
    from maestro.backends.git_staging import Transaction, GitStagingError
    _, tree, reports, start = linked
    own = Path(git(tree, 'rev-parse', '--absolute-git-dir').stdout.strip())
    foreign = own / 'foreign-empty'
    if change == 'remove-directory':
        foreign.mkdir()
    elif change == 'existing-lock':
        (own / 'index.lock').write_text('stale lock')
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    if change != 'existing-lock':
        action = 'mkdir' if change == 'add-directory' else 'rmdir'
        result = confined(transaction, tree,
            f'from pathlib import Path; Path({str(foreign)!r}).{action}()')
        assert result.returncode == 0, result.stderr
    with pytest.raises(GitStagingError, match='admin'):
        transaction.finish(success=True)
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start


@pytest.mark.parametrize('operation', ['start', 'finish', 'publication'])
def test_journal_lease_disagreement_cannot_run_or_publish(linked, operation):
    from maestro.backends import git_staging
    _, tree, reports, _ = linked
    transaction = git_staging.Transaction.prepare(tree, reports)
    if operation == 'publication':
        transaction.start()
        transaction.finish(success=True)
    elif operation == 'finish':
        transaction.start()
    marker = (transaction.lease / 'active.json' if operation == 'publication'
              else Path(transaction.job))
    data = json.loads(marker.read_text())
    data['state'] = 'aborted'
    git_staging._dump(marker, data)
    if operation == 'publication':
        assert git_staging.publication_state(reports) == 'failed'
    else:
        with pytest.raises(git_staging.GitStagingError, match='disagree'):
            if operation == 'finish':
                transaction.finish(success=True)
            else:
                transaction.start()


@pytest.mark.parametrize('operation', ['start', 'mounts', 'finish'])
def test_identical_content_replacement_of_git_pointer_is_not_original_identity(linked, operation):
    from maestro.backends.git_staging import Transaction, GitStagingError
    _, tree, reports, _ = linked
    transaction = Transaction.prepare(tree, reports)
    if operation == 'finish':
        transaction.start()
    marker = tree / '.git'
    replacement = tree / '.replacement'
    replacement.write_bytes(marker.read_bytes())
    replacement.replace(marker)
    try:
        with pytest.raises(GitStagingError, match='pointer'):
            if operation == 'finish':
                transaction.finish(success=True)
            else:
                getattr(transaction, operation)()
    finally:
        transaction.cleanup()


@pytest.mark.parametrize('candidate', [False, True])
def test_real_private_commit_on_packed_task_or_candidate_ref(linked, tmp_path, candidate):
    from maestro.backends.git_staging import Transaction
    repo, tree, reports, start = linked
    branch = 'impl/T-001'
    if candidate:
        tree = tmp_path / 'candidate'
        branch = 'impl/T-003-attempt/c1'
        git(repo, 'worktree', 'add', '-b', branch, str(tree), start)
    git(repo, 'pack-refs', '--all', '--prune')
    assert not (repo / '.git/refs/heads' / branch).exists()
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    result = confined(transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('packed candidate commit\\n')
subprocess.run(['git', 'add', 'file.txt'],check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'packed'],check=True)
''')
    assert result.returncode == 0, result.stderr
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    tip = transaction.finish(success=True)
    assert tip != start
    assert git(repo, 'rev-parse', branch).stdout.strip() == tip
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start


def test_expected_old_mismatch_never_overwrites_concurrent_tip(linked):
    from maestro.backends.git_staging import Transaction
    from maestro.backends.git_staging import GitStagingError as ConfinementError
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    alternate = git(repo, 'commit-tree', 'HEAD^{tree}', '-p', start, '-m', 'concurrent').stdout.strip()
    git(repo, 'update-ref', 'refs/heads/impl/T-001', alternate, start)
    with pytest.raises(ConfinementError, match='update-ref'):
        transaction.finish(success=True)
    assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == alternate


def test_fence_survives_preparation_and_worker_rejects_replay(linked):
    from maestro.backends.git_staging import Transaction
    from maestro.backends.git_staging import GitStagingError as ConfinementError
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    other = reports.parent / 'other-reports'
    other.mkdir()
    with pytest.raises(ConfinementError, match='reserved'):
        Transaction.prepare(tree, other)
    transaction.start()
    with pytest.raises(ConfinementError, match='fresh reservation'):
        Transaction.load(transaction.job).start()
    transaction.finish(success=False)
    following = Transaction.prepare(tree, other)
    following.start()
    following.finish(success=True)


def test_unstarted_reservation_cannot_report_promoted(linked):
    from maestro.backends.git_staging import Transaction
    from maestro.backends.git_staging import GitStagingError as ConfinementError
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    with pytest.raises(ConfinementError, match='running'):
        transaction.finish(success=True)
    transaction.finish(success=False)


@pytest.mark.parametrize('phase', ['prepared', 'running', 'ready'])
def test_dead_launcher_is_retained_and_clean_retry_can_reserve(linked, phase):
    from maestro.backends.git_staging import Transaction
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    data = json.loads(Path(transaction.job).read_text())
    # Represents a killed controller/worker; the impossible start identity avoids
    # depending on PID recycling or leaving an orphan test process.
    data.update(state=phase, owner={'pid': 1, 'start': 'impossible'})
    (transaction.lease / 'active.json').write_text(json.dumps(data))
    Path(transaction.job).write_text(json.dumps(data))
    following = Transaction.prepare(tree, reports)
    assert json.loads(Path(transaction.job).read_text())['state'] == 'interrupted'
    assert following.directory != transaction.directory
    following.finish(success=False)


def test_interrupted_dirty_attempt_requires_existing_task_restore(linked):
    from maestro.backends.git_staging import Transaction, GitStagingError
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    data = json.loads(Path(transaction.job).read_text())
    data.update(state='running', owner={'pid': 1, 'start': 'impossible'})
    (transaction.lease / 'active.json').write_text(json.dumps(data))
    (tree / 'file.txt').write_text('partial work')
    with pytest.raises(GitStagingError, match='task restore'):
        Transaction.prepare(tree, reports)
    assert (tree / 'file.txt').read_text() == 'partial work'
    git(tree, 'reset', '--hard', start)
    following = Transaction.prepare(tree, reports)
    assert json.loads(Path(transaction.job).read_text())['state'] == 'interrupted'
    following.finish(success=False)


@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_native_runtime_applies_private_git_after_all_write_rebinds(linked, tmp_path, backend):
    from maestro.backends import codex_runtime, agy_runtime
    repo, tree, reports, start = linked
    home = tmp_path / 'home'
    native = home / '.codex'
    native.mkdir(parents=True)
    (native / 'auth.json').write_text('synthetic')
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    if backend == 'codex':
        run = codex_runtime.prepare(cwd=tree, workspace=reports,
            model='gpt-6-luna', effort='low', write_roots=(tree, reports),
            native_home=native, host_home=home, allow=('Bash(git *)',))
    else:
        run = agy_runtime.prepare(cwd=tree, workspace=reports,
            write_roots=(tree, reports), home=home, allow=('Bash(git *)',))
    # A real process exercises the resulting mounts, not a mocked argv inspection.
    run.transaction.start()
    program = '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('runtime commit\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'runtime'], check=True)
'''
    result = subprocess.run(run.wrap([sys.executable, '-c', program]),
        env=run.environment(), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    tip = run.transaction.finish(success=True)
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == tip != start
    run.cleanup()


@pytest.mark.parametrize('backend', ['codex', 'agy'])
@pytest.mark.parametrize('failure', [False, True])
def test_bounded_driver_promotes_only_after_successful_native_call(linked, tmp_path,
                                                                monkeypatch, backend, failure):
    from maestro.backends import codex_app_server
    from maestro.backends.base import CompletionSpec
    from maestro.backends.codex import CodexBackend
    from maestro.backends.antigravity import AntigravityBackend
    repo, tree, reports, start = linked
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex/auth.json').write_text('synthetic')
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    program = '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('driver commit\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True, stdout=subprocess.DEVNULL)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'driver'], check=True,
               stdout=subprocess.DEVNULL)
print('{"status":"SUCCESS","response":"committed"}')
'''
    def execute(argv, **kwargs):
        split = argv.index('--')
        return subprocess.run([*argv[:split + 1], sys.executable, '-c', program],
            cwd=tree, env=kwargs.get('env'), capture_output=True, text=True, timeout=HANG_SECONDS)
    def native(**kwargs):
        result = execute(kwargs['argv'], env=kwargs['env'])
        assert result.returncode == 0, result.stderr
        if failure:
            raise RuntimeError('injected native failure after commit')
        return SimpleNamespace(text='committed')
    def agy_native(argv, **kwargs):
        result = execute(argv, **kwargs)
        assert result.returncode == 0, result.stderr
        if failure:
            result.returncode = 1
        return result
    monkeypatch.setattr(codex_app_server, 'run_turn', native)
    driver = (CodexBackend(codex_home=home / '.codex') if backend == 'codex' else
              AntigravityBackend(gemini_home=home / '.gemini', runner=agy_native))
    result = driver.complete(CompletionSpec(prompt='commit', model='fixture', cwd=tree,
                                           writable=True, log_file=reports / 'impl.log'))
    assert (result.returncode == 0) is not failure
    current = git(tree, 'rev-parse', 'HEAD').stdout.strip()
    assert (current != start) is not failure
    journals = list((reports / '.native-git').glob('call-*/transaction.json'))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text())['state'] == ('aborted' if failure else 'promoted')
    if backend == 'codex':
        from tests.native_test_home import assert_codex_discovery_isolated
        assert_codex_discovery_isolated(reports, tmp_path / 'host-home')


@pytest.mark.parametrize('backend', ['codex', 'agy'])
@pytest.mark.parametrize('phase', ['launch', 'resume'])
def test_generated_worker_promotes_task_commit_before_exit(linked, tmp_path, monkeypatch,
                                                         server, backend, phase):
    from maestro.backends.base import LaunchSpec, Handle
    from maestro.backends.codex import CodexBackend
    from maestro.backends.antigravity import AntigravityBackend
    repo, tree, reports, start = linked
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex/auth.json').write_text('synthetic')
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    command = '''
import subprocess
Path('file.txt').write_text('worker commit\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True, stdout=subprocess.DEVNULL)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'worker'], check=True,
               stdout=subprocess.DEVNULL)
'''
    executable = tmp_path / ('native-' + backend)
    if backend == 'codex':
        content = server.read_text().replace("    elif method == 'turn/start':\n",
            "    elif method == 'turn/start':\n" +
            ''.join('        ' + line + '\n' for line in command.splitlines()))
        executable.write_text(f'#!{sys.executable}\n' + content)
        driver = CodexBackend(binary=str(executable), codex_home=home / '.codex')
        monkeypatch.setattr(driver, '_new_window', lambda *args: None)
    else:
        executable.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n' + command +
            '\nprint(\'{"event":"result","result":{"status":"SUCCESS"}}\')\n')
        from maestro.backends import antigravity
        monkeypatch.setattr(antigravity, 'BINARY', str(executable))
        driver = AntigravityBackend(gemini_home=home / '.gemini')
        monkeypatch.setattr(driver, '_open_window', lambda *args: None)
    executable.chmod(0o755)
    spec = LaunchSpec('T-001', 'fixture-session', 'fixture', 'commit', reports, tree)
    if phase == 'launch':
        driver.launch(spec)
    else:
        driver.resume(Handle(backend, 'fixture-session', 'thread', reports, 'impl-T-001'),
                      'commit', spec=spec)
    launcher = reports / ('resume.py' if backend == 'agy' and phase == 'resume' else 'launch.py')
    result = subprocess.run([sys.executable, '-I', str(launcher)], cwd=tree,
                            capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() != start
    journal, = (reports / '.native-git').glob('call-*/transaction.json')
    assert json.loads(journal.read_text())['state'] == 'promoted'

    if backend == 'codex':
        from tests.native_test_home import assert_codex_discovery_isolated
        assert_codex_discovery_isolated(reports, tmp_path / 'host-home')


def test_graph_grants_no_host_ref_or_reflog_namespace(linked):
    from maestro.workflows.worker import _git_dirs
    repo, tree, reports, start = linked
    own = Path(git(tree, 'rev-parse', '--absolute-git-dir').stdout.strip())
    assert set(_git_dirs(tree)) == {own}


@pytest.mark.skipif(__import__('os').environ.get('MAESTRO_TEST_STOCK_CODEX') != '1',
                    reason='explicit installed-runtime offline qualification lane')
def test_stock_native_sandbox_can_commit_private_task_ref(linked, tmp_path):
    from maestro.backends import codex_runtime, codex_app_server
    import time
    repo, tree, reports, start = linked
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'auth.json').write_text('{"OPENAI_API_KEY":"sk-synthetic-not-real"}')
    run = codex_runtime.prepare(cwd=tree, workspace=reports,
        model='gpt-6-luna', effort='low', write_roots=(tree, reports),
        native_home=native, host_home=tmp_path)
    run.transaction.start()
    stderr = (reports / 'stock-offline-app-server.stderr').open('xb')
    process = subprocess.Popen(run.wrap(['codex', 'app-server']), cwd=tree,
        env=run.environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=stderr, start_new_session=True)
    client = codex_app_server._Client(process, deadline=time.monotonic() + 20,
                                     policy=run.policy, emit=lambda _: None)
    try:
        client.rpc('initialize', {'clientInfo': {'name': 'maestro-git-offline', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        client.send({'method': 'initialized'})
        run.verify_config(client.rpc('config/read', {'cwd': str(tree)}))
        result = client.rpc('command/exec', {'command': ['/bin/bash', '-c',
            'echo native > file.txt && git add file.txt && git -c gc.auto=0 commit -m native'],
            'cwd': str(tree), 'permissionProfile': 'maestro', 'timeoutMs': 5000})
        assert result['exitCode'] == 0, result
    finally:
        codex_app_server._stop(process)
        stderr.close()
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    tip = run.transaction.finish(success=True)
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == tip != start
    run.cleanup()


@pytest.mark.parametrize('outcome', ['promoted', 'aborted', 'interrupted'])
def test_legacy_done_waits_for_trusted_git_settlement(linked, outcome):
    from maestro.backends.git_staging import Transaction
    from maestro import orchestrator
    import select
    repo, tree, reports, start = linked
    child = None
    try:
        if outcome == 'interrupted':
            program = '''
import sys
from pathlib import Path
from maestro.backends.git_staging import Transaction
transaction = Transaction.prepare(Path(sys.argv[1]), Path(sys.argv[2]))
print('prepared', flush=True)
sys.stdin.read()
'''
            child = subprocess.Popen([sys.executable, '-c', program, str(tree), str(reports)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert select.select([child.stdout], [], [], HANG_SECONDS)[0]
            assert child.stdout.readline() == b'prepared\n'
        else:
            transaction = Transaction.prepare(tree, reports)
        (reports / 'DONE').write_text('native says done')
        (reports / 'result.json').write_text('{"status":"DONE"}')
        assert not orchestrator._native_git_reports_ready(reports, running=True)
        assert (reports / 'DONE').exists()
        if outcome == 'promoted':
            transaction.start()
            transaction.finish(success=True)
        elif outcome == 'aborted':
            transaction.finish(success=False)
        else:
            assert not orchestrator._native_git_reports_ready(reports, running=False)
            child.kill()
            child.wait(timeout=HANG_SECONDS)
        assert orchestrator._native_git_reports_ready(reports, running=False)
        assert (reports / 'DONE').exists() is (outcome == 'promoted')
        assert (reports / 'FAILED').exists() is (outcome != 'promoted')
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=HANG_SECONDS)


@pytest.mark.parametrize('status', ['SUCCESS', 'REFUSED', 'ERROR', 'missing', 'contradictory'])
def test_agy_worker_settles_actual_terminal_status_before_promotion(linked, status):
    from maestro.backends import agy_worker, control_files
    from maestro.backends.git_staging import Transaction
    repo, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    native = '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('soft denial commit\\n')
subprocess.run(['git','add','file.txt'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['git','-c','gc.auto=0','commit','-m','native'],check=True,stdout=subprocess.DEVNULL)
'''
    if status != 'missing':
        native += f'print({json.dumps({"event": "result", "result": {"status": status}})!r})\n'
    if status == 'contradictory':
        native += 'print(\'{"event":"result","result":{"status":"SUCCESS"}}\')\n'
    # The actual worker runs an actual confined native commit and reads its output.
    argv = [sys.executable, '-c', native]
    wrapped = confinement.bubblewrap_argv(argv, cwd=tree,
        write_roots=(tree, *transaction.write_roots))
    split = len(wrapped) - len(argv) - 1
    job = reports / 'worker.json'
    frozen = control_files.freeze_mounts([*wrapped[:split], *transaction.mounts(), *wrapped[split:]])
    job.write_text(json.dumps({'argv': frozen, 'mounts': frozen.identities,
        'cwd': str(tree), 'log_file': str(reports / 'impl.log'), 'mode': 'w',
        'env': dict(__import__('os').environ), 'git_transaction': transaction.job,
        'created_mountpoints': []}))
    with pytest.raises(SystemExit) as stopped:
        agy_worker.main(str(job))
    assert (stopped.value.code == 0) is (status == 'SUCCESS')
    assert (git(tree, 'rev-parse', 'HEAD').stdout.strip() != start) is (status == 'SUCCESS')
    assert json.loads(Path(transaction.job).read_text())['state'] == (
        'promoted' if status == 'SUCCESS' else 'aborted')


def test_git_metadata_read_never_follows_replaced_ancestor(tmp_path, monkeypatch):
    from maestro.backends.git_staging import _read
    parent, outside = tmp_path / 'metadata', tmp_path / 'outside'
    parent.mkdir()
    outside.mkdir()
    (parent / 'active.json').write_text('original')
    (outside / 'active.json').write_text('substituted')
    original_open = os.open
    swaps = []
    def race(path, flags, *args, **kwargs):
        if Path(path).name == 'active.json' and not swaps:
            parent.rename(tmp_path / 'pinned')
            parent.symlink_to(outside, target_is_directory=True)
            swaps.append(True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', race)
    try:
        content = _read(parent / 'active.json')
    except (ValueError, OSError):
        content = b'refused'
    assert swaps
    assert content in (b'original', b'refused')


def test_standalone_staging_seals_worktree_git_pointer(linked):
    from maestro.backends.git_staging import Transaction
    _, tree, reports, _ = linked
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    original = (tree / '.git').read_bytes()
    result = confined(transaction, tree,
        'from pathlib import Path; Path(".git").write_text("gitdir: /untrusted\\n")')
    assert result.returncode != 0
    assert (tree / '.git').read_bytes() == original
    transaction.finish(success=False)


def test_replaced_git_pointer_is_refused_before_native_launch(linked):
    from maestro.backends.git_staging import Transaction, GitStagingError
    _, tree, reports, _ = linked
    transaction = Transaction.prepare(tree, reports)
    original = (tree / '.git').read_bytes()
    try:
        (tree / '.git').write_bytes(original + b'\n')
        with pytest.raises(GitStagingError, match='pointer'):
            transaction.start()
    finally:
        (tree / '.git').write_bytes(original)
        transaction.cleanup()


def test_corrupted_publication_pointer_cannot_read_outside_journal_directory(linked, tmp_path, monkeypatch):
    from maestro.backends import git_staging
    _, tree, reports, _ = linked
    transaction = git_staging.Transaction.prepare(tree, reports)
    outside = tmp_path / 'unrelated.json'
    outside.write_text('{}')
    (reports / '.native-git/current.json').write_text(json.dumps({'journal': str(outside)}))
    reads = []
    original = git_staging._read
    def recording(path):
        reads.append(Path(path))
        return original(path)
    monkeypatch.setattr(git_staging, '_read', recording)
    assert git_staging.publication_state(reports) == 'failed'
    assert outside not in reads
    transaction.cleanup()


def test_failed_journal_write_preserves_previous_complete_record(tmp_path, monkeypatch):
    from maestro.backends import git_staging
    path = tmp_path / 'transaction.json'
    git_staging._dump(path, {'state': 'running'})
    def torn(value, stream, **kwargs):
        stream.write('{"state":')
        raise OSError('interrupted journal write')
    monkeypatch.setattr(json, 'dump', torn)
    with pytest.raises(OSError, match='interrupted'):
        git_staging._dump(path, {'state': 'ready'})
    assert json.loads(path.read_text()) == {'state': 'running'}


@pytest.mark.parametrize('field,value', [('version', 99), ('ref', 'refs/heads/main'),
    ('start_tip', 'bad'), ('directory', '/unrelated'), ('lease', '/unrelated'),
    ('own', '/unrelated'), ('common', '/unrelated')])
def test_loader_refuses_malformed_or_redirected_transaction(linked, field, value):
    from maestro.backends.git_staging import Transaction, GitStagingError
    _, tree, reports, _ = linked
    transaction = Transaction.prepare(tree, reports)
    data = json.loads(Path(transaction.job).read_text())
    data[field] = value
    Path(transaction.job).write_text(json.dumps(data))
    with pytest.raises((GitStagingError, ValueError), match='transaction|journal|metadata'):
        Transaction.load(transaction.job)


def test_real_process_crash_after_cas_never_adopts_done_and_clean_retry_can_continue(linked):
    from maestro.backends.git_staging import Transaction, publication_state
    from maestro import orchestrator
    _, tree, reports, start = linked
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    result = confined(transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('crash candidate\\n')
subprocess.run(['git','add','file.txt'],check=True)
subprocess.run(['git','-c','gc.auto=0','commit','-m','native'],check=True)
''')
    assert result.returncode == 0, result.stderr
    # Let an actual new trusted process finish the running transaction, then die
    # after successful update-ref and before the promoted journal replacement.
    data = json.loads(Path(transaction.job).read_text())
    data['state'] = 'prepared'
    Path(transaction.job).write_text(json.dumps(data))
    (transaction.lease / 'active.json').write_text(json.dumps(data))
    child = subprocess.run([sys.executable, '-c', '''
import os,sys
from maestro.backends import git_staging
transaction=git_staging.Transaction.load(sys.argv[1])
transaction.start()
original=git_staging._git
def crash(tree,*args,**kwargs):
    result=original(tree,*args,**kwargs)
    if args[0]=='update-ref': os._exit(73)
    return result
git_staging._git=crash
transaction.finish(success=True)
''', transaction.job], capture_output=True, text=True, timeout=HANG_SECONDS)
    assert child.returncode == 73, child.stderr
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() != start
    assert json.loads(Path(transaction.job).read_text())['state'] == 'ready'
    (reports / 'DONE').write_text('provisional')
    assert not orchestrator._native_git_reports_ready(reports, running=True)
    assert orchestrator._native_git_reports_ready(reports, running=False)
    assert not (reports / 'DONE').exists()
    following = Transaction.prepare(tree, reports)
    assert json.loads(Path(transaction.job).read_text())['state'] == 'interrupted'
    assert publication_state(reports) == 'pending'
    following.start()
    following.finish(success=True)
