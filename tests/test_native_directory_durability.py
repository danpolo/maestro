"""A directory left by a failed fsync still needs its parent link persisted."""
import os

import pytest

from maestro.backends import git_staging
from tests.test_git_staging import linked

pytestmark = pytest.mark.real_fsync


@pytest.mark.parametrize('which', ['lease-parent', 'journal-parent'])
def test_retry_persists_directory_links_left_by_failed_creation(linked, monkeypatch, which):
    repo, tree, report, _ = linked
    target = repo / '.git/maestro-native' if which == 'lease-parent' else report / '.native-git'
    original_sync, original_dump = os.fsync, git_staging._dump
    failed = False
    synced = set()
    def fsync(fd):
        nonlocal failed
        if not failed and os.readlink(f'/proc/self/fd/{fd}') == str(target):
            failed = True
            raise OSError('directory creation fsync failed')
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))
        return original_sync(fd)
    def publish(path, value):
        if path.name == 'transaction.json' and value.get('state') == 'prepared':
            info = target.parent.stat()
            assert (info.st_dev, info.st_ino) in synced, 'retry did not persist prior directory link'
        return original_dump(path, value)
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(OSError, match='directory creation fsync failed'):
        git_staging.Transaction.prepare(tree, report)
    assert failed
    synced.clear()
    monkeypatch.setattr(git_staging, '_dump', publish)
    retry = git_staging.Transaction.prepare(tree, report)
    retry.cleanup()


def test_preparation_does_not_resync_unchanged_filesystem_ancestors(linked, monkeypatch):
    _, tree, report, _ = linked
    original = os.fsync
    root = os.stat('/')
    calls = 0
    def fsync(fd):
        nonlocal calls
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (root.st_dev, root.st_ino):
            calls += 1
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    transaction = git_staging.Transaction.prepare(tree, report)
    try:
        assert calls <= 2, 'unchanged root repeatedly persisted during one preparation'
    finally:
        transaction.cleanup()


def test_sync_scope_repersists_external_changes_and_failed_syncs(tmp_path, monkeypatch):
    from maestro.backends import control_files
    target = tmp_path / 'target'
    target.mkdir()
    original = os.fsync
    calls = []
    fail = False
    def fsync(fd):
        nonlocal fail
        if os.readlink(f'/proc/self/fd/{fd}') == str(target):
            calls.append('failed' if fail else 'synced')
            if fail:
                fail = False
                raise OSError('synthetic directory fsync failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    def persist():
        control_files.mkdir(target, parents=True, exist_ok=True, durable=True)
    with control_files.directory_syncs():
        persist()
        assert calls == ['synced']
        persist()
        assert calls == ['synced']
        (target / 'external-child').write_text('changed outside mkdir helper')
        persist()
        assert calls == ['synced', 'synced']
        (target / 'second-child').mkdir()
        fail = True
        with pytest.raises(OSError, match='synthetic directory fsync failure'):
            persist()
        persist()
        assert calls == ['synced', 'synced', 'failed', 'synced']
    with control_files.directory_syncs():
        persist()
    assert calls == ['synced', 'synced', 'failed', 'synced', 'synced']
