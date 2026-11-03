"""Preparation failures must settle resources acquired before final validation."""
import json
from pathlib import Path

import pytest

from maestro.backends import agy_runtime, codex_runtime, git_staging
from tests.test_git_staging import linked, git

pytestmark = pytest.mark.real_fsync


@pytest.mark.parametrize('backend', ['agy', 'codex'])
def test_post_prepare_refusal_aborts_already_acquired_git_transaction(linked, tmp_path, monkeypatch, backend):
    _, tree, report, _ = linked
    home = tmp_path / 'home'
    native = home / ('.gemini/antigravity-cli' if backend == 'agy' else '.codex')
    (native / 'bin').mkdir(parents=True)
    if backend == 'agy':
        (home / '.gemini/config').mkdir()
        (native / 'bin/agentapi').write_text('synthetic helper')
    else:
        (native / 'auth.json').write_text('synthetic credential')
    acquired = []
    original = git_staging.Transaction.prepare
    def acquire_then_replace(*args, **kwargs):
        transaction = original(*args, **kwargs)
        acquired.append(transaction)
        native.rename(native.with_name('saved-native'))
        native.mkdir()
        return transaction
    monkeypatch.setattr(git_staging.Transaction, 'prepare', acquire_then_replace)
    with pytest.raises(ValueError, match='preparation directory changed'):
        if backend == 'agy':
            agy_runtime.prepare(cwd=tree, workspace=report, home=home, write_roots=(tree,))
        else:
            codex_runtime.prepare(cwd=tree, workspace=report, native_home=native, host_home=home,
                                  write_roots=(tree,), model='fixture', effort='low')
    transaction, = acquired
    journal = json.loads(Path(transaction.job).read_text())
    lease = json.loads((transaction.lease / 'active.json').read_text())
    assert journal == lease
    assert journal['state'] == 'aborted'
    assert git(tree, 'status', '--porcelain').stdout == ''


@pytest.mark.parametrize('publication', ['transaction.json', 'active.json', 'current.json'])
@pytest.mark.parametrize('when', ['before', 'after'])
def test_failed_preparation_publication_does_not_strand_live_git_owner(linked, monkeypatch, publication, when):
    _, tree, report, _ = linked
    original = git_staging._dump
    failed = False
    def fail_once(path, value):
        nonlocal failed
        if path.name == publication and not failed:
            failed = True
            if when == 'after':
                original(path, value)
            raise OSError('synthetic publication failure')
        return original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(git_staging, '_dump', fail_once)
        with pytest.raises(OSError, match='synthetic publication failure'):
            git_staging.Transaction.prepare(tree, report)
    assert failed
    retry = git_staging.Transaction.prepare(tree, report)
    retry.cleanup()


def test_agy_git_discovery_refusal_settles_transaction_without_control_files(linked, tmp_path):
    repo, tree, report, _ = linked
    home = tmp_path / 'home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('synthetic outside metadata\n')
    exclude = repo / '.git/info/exclude'
    exclude.unlink()
    exclude.symlink_to(outside)
    with pytest.raises(ValueError):
        agy_runtime.prepare(cwd=tree, workspace=report, home=home, write_roots=(tree,))
    pointer = json.loads((report / '.native-git/current.json').read_text())
    journal = json.loads(Path(pointer['journal']).read_text())
    assert journal['state'] == 'aborted'
    # Empty mountpoints intentionally survive cleanup: unlinking one could
    # detach an overlapping invocation's protected discovery mount.
    assert all(not list((tree / alias).iterdir()) for alias in ('.agent', '.agents', '_agent', '_agents'))
    assert json.loads((report / '.native-owner/active.json').read_text())['state'] == 'released'
    assert outside.read_text() == 'synthetic outside metadata\n'


def test_marker_publication_failure_after_replace_releases_acquired_native_owner(tmp_path, monkeypatch):
    from maestro.backends import native_ownership
    original = git_staging._dump
    failed = False
    def publish_then_fail(path, value):
        nonlocal failed
        original(path, value)
        if not failed:
            failed = True
            raise OSError('synthetic fsync failure after replacement')
    owner = native_ownership.Reservation()
    report = tmp_path / 'report'
    with monkeypatch.context() as patch:
        patch.setattr(git_staging, '_dump', publish_then_fail)
        with pytest.raises(OSError, match='synthetic fsync failure'):
            owner.workspace(report)
        owner.cleanup()
    marker = report / '.native-owner/active.json'
    assert json.loads(marker.read_text())['state'] == 'released'
    retry = native_ownership.Reservation()
    try:
        retry.workspace(report)
    finally:
        retry.cleanup()


@pytest.mark.parametrize('previous', ['absent', 'released'])
def test_marker_failure_before_replace_preserves_previous_owner(tmp_path, monkeypatch, previous):
    from maestro.backends import native_ownership
    report = tmp_path / 'report'
    marker = report / '.native-owner/active.json'
    old = native_ownership.Reservation()
    if previous == 'released':
        old.workspace(report)
        old.cleanup()
    before = marker.read_bytes() if marker.exists() else None
    owner = native_ownership.Reservation()
    def fail(*args):
        raise OSError('before replacement')
    with monkeypatch.context() as patch:
        patch.setattr(git_staging, '_dump', fail)
        with pytest.raises(OSError, match='before replacement'):
            owner.workspace(report)
        owner.cleanup()
    assert (marker.read_bytes() if marker.exists() else None) == before
    retry = native_ownership.Reservation()
    try:
        retry.workspace(report)
    finally:
        retry.cleanup()
