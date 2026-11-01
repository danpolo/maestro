"""Baseline persistence precedes publication of a prepared native Git lease."""
import os
from pathlib import Path
import json

import pytest

from maestro.backends import git_staging
from tests.test_git_staging import linked, confined, git


def test_all_baseline_files_and_directories_are_synced_before_prepared_publication(linked, monkeypatch):
    _, tree, report, _ = linked
    synced = set()
    original_sync, original_dump = os.fsync, git_staging._dump
    def fsync(fd):
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))
        original_sync(fd)
    def publish(path, value):
        if path.name == 'transaction.json' and value.get('state') == 'prepared':
            directory = path.parent
            required = [directory, directory.parent, report, *directory.rglob('*')]
            missing = [str(p.relative_to(report)) for p in required
                       if (p.stat().st_dev, p.stat().st_ino) not in synced]
            assert missing == [], 'prepared before baseline persistence: ' + ', '.join(missing)
        return original_dump(path, value)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(git_staging, '_dump', publish)
    transaction = git_staging.Transaction.prepare(tree, report)
    transaction.cleanup()


def test_baseline_fsync_failure_refuses_before_any_prepared_lease(linked, monkeypatch):
    _, tree, report, _ = linked
    original = os.fsync
    def fail_baseline(fd):
        path = os.readlink(f'/proc/self/fd/{fd}')
        if '/baseline-' in path:
            raise OSError('synthetic baseline persistence failure')
        original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail_baseline)
        with pytest.raises(OSError, match='synthetic baseline persistence failure'):
            git_staging.Transaction.prepare(tree, report)
    assert not (report / '.native-git/current.json').exists()
    assert not list((tree.parent / 'repo/.git/maestro-native').glob('*/active.json'))
    retry = git_staging.Transaction.prepare(tree, report)
    retry.cleanup()


def stage_commit(transaction, tree, *, packed=False):
    result = confined(transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('durable native change\\n')
subprocess.run(['git', 'add', 'file.txt'], check=True)
subprocess.run(['git', '-c', 'gc.auto=0', 'commit', '-m', 'native'], check=True)
''' + ("subprocess.run(['git', '-c', 'pack.threads=1', 'repack', '-ad'], check=True)\n" if packed else ''))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('packed', [False, True])
def test_objects_precede_ready_and_ref_persistence_precedes_promoted(linked, monkeypatch, packed):
    repo, tree, report, start = linked
    transaction = git_staging.Transaction.prepare(tree, report)
    transaction.start()
    stage_commit(transaction, tree, packed=packed)
    objects = repo / '.git/objects'
    synced = set()
    original_sync, original_dump = os.fsync, git_staging._dump

    def fsync(fd):
        original_sync(fd)
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))

    def publish(path, value):
        if path == Path(transaction.job) and value.get('state') in ('ready', 'promoted'):
            required = [objects, *objects.rglob('*')]
            if value['state'] == 'promoted':
                for relative in (transaction.ref, 'logs/' + transaction.ref):
                    leaf = transaction.common / relative
                    required += [leaf, *[p for p in leaf.parents if p.is_relative_to(transaction.common)]]
            missing = [str(p.relative_to(repo / '.git')) for p in required
                       if (p.stat().st_dev, p.stat().st_ino) not in synced]
            assert missing == [], value['state'] + ' before Git persistence: ' + ', '.join(missing)
        return original_dump(path, value)

    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(git_staging, '_dump', publish)
    tip = transaction.finish(success=True)
    assert tip != start
    assert git(tree, 'fsck', '--no-dangling').returncode == 0
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('reflogs', [False, True])
def test_noop_completion_supports_existing_ref_storage(linked, packed, reflogs):
    repo, tree, report, start = linked
    if not reflogs:
        git(repo, 'config', 'core.logAllRefUpdates', 'false')
        (repo / '.git/logs/refs/heads/impl/T-001').unlink()
    if packed:
        git(repo, 'pack-refs', '--all', '--prune')
    transaction = git_staging.Transaction.prepare(tree, report)
    transaction.start()
    assert transaction.finish(success=True) == start
    assert git_staging.publication_state(report) == 'promoted'


@pytest.mark.parametrize('failure', ['object-file', 'object-directory', 'ref-file', 'ref-directory', 'reflog'])
def test_git_sync_failure_never_publishes_success(linked, monkeypatch, failure):
    repo, tree, report, start = linked
    transaction = git_staging.Transaction.prepare(tree, report)
    transaction.start()
    stage_commit(transaction, tree)
    original = os.fsync
    reached = []

    def fsync(fd):
        target = Path(os.readlink(f'/proc/self/fd/{fd}'))
        objects = repo / '.git/objects'
        ref = transaction.common / transaction.ref
        matches = {
            'object-file': target.is_relative_to(objects) and target.is_file(),
            'object-directory': target == objects,
            'ref-file': target == ref,
            'ref-directory': target == ref.parent,
            'reflog': target == transaction.common / ('logs/' + transaction.ref),
        }
        if matches[failure]:
            reached.append(str(target))
            raise OSError('synthetic Git sync failure ' + failure)
        original(fd)

    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(OSError, match='synthetic Git sync failure'):
        transaction.finish(success=True)
    assert reached
    assert json.loads(Path(transaction.job).read_text())['state'] == 'aborted'
    assert git_staging.publication_state(report) == 'failed'
    if failure.startswith('object'):
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start
