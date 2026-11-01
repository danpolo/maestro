"""Untrusted object inputs fail before shared writes and exact-ref promotion."""
import json
from pathlib import Path

import pytest

from maestro.backends import git_staging, git_objects
from tests.test_git_staging import linked, confined, git
from tests.test_private_git_objects import hashes


def staged(linked, *, packed=False):
    _, tree, reports, _ = linked
    transaction = git_staging.Transaction.prepare(tree, reports)
    transaction.start()
    result = confined(transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('candidate private content\\n')
subprocess.run(['git','add','file.txt'],check=True)
subprocess.run(['git','-c','gc.auto=0','commit','-m','candidate'],check=True)
''' + ("subprocess.run(['git','-c','pack.threads=1','repack','-ad'],check=True)\n" if packed else ''))
    assert result.returncode == 0, result.stderr
    return transaction


def test_quarantine_mutation_after_strict_validation_refuses_before_shared_write(linked, monkeypatch):
    repo, tree, _, start = linked
    transaction = staged(linked)
    before = hashes(repo / '.git/objects')
    original = git_objects.ObjectValidation.validate
    changed = []
    def validate(view, tip):
        original(view, tip)
        path = next((view.quarantine / 'pack').glob('*.pack'))
        path.chmod(0o600)
        path.write_bytes(b'invalid replacement after strict validation')
        changed.append(True)
    monkeypatch.setattr(git_objects.ObjectValidation, 'validate', validate)
    try:
        with pytest.raises((ValueError, OSError)):
            transaction.finish(success=True)
        assert changed
        assert hashes(repo / '.git/objects') == before, 'unchecked quarantine bytes reached shared store'
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert json.loads(Path(transaction.job).read_text())['state'] == 'aborted'
    finally:
        transaction.cleanup()


@pytest.mark.parametrize('defect', ['unreachable', 'pack', 'symlink', 'hardlink', 'shared-alternate'])
def test_malformed_or_aliased_objects_never_touch_shared_store(linked, tmp_path, defect):
    repo, tree, _, start = linked
    transaction = staged(linked, packed=defect == 'pack')
    private = transaction.directory / 'objects'
    if defect == 'shared-alternate':
        (repo / '.git/objects/info/alternates').write_text(str(tmp_path / 'outside')+'\n')
    elif defect == 'pack':
        path = next((private / 'pack').glob('*.pack'))
        path.chmod(0o600)
        path.write_bytes(b'malformed native pack')
    else:
        path = private / 'ff' / ('0' * 38)
        path.parent.mkdir(exist_ok=True)
        victim = tmp_path / 'victim'
        victim.write_text('unrelated bytes')
        if defect == 'symlink':
            path.symlink_to(victim)
        elif defect == 'hardlink':
            path.hardlink_to(victim)
        else:
            path.write_bytes(b'not a compressed Git object')
    before = hashes(repo / '.git/objects')
    try:
        with pytest.raises((ValueError, OSError)):
            transaction.finish(success=True)
        assert hashes(repo / '.git/objects') == before
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert json.loads(Path(transaction.job).read_text())['state'] == 'aborted'
    finally:
        transaction.cleanup()


def test_native_alternate_paths_are_removed_before_trusted_git_execution(linked, tmp_path):
    repo, tree, _, _ = linked
    transaction = staged(linked)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'unrelated').write_text('foreign object authority')
    before = hashes(outside)
    (transaction.directory / 'objects/info/alternates').write_text(str(outside)+'\n')
    try:
        tip = transaction.finish(success=True)
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == tip
        assert hashes(outside) == before
        assert git(repo, 'fsck', '--strict', '--no-dangling').returncode == 0
    finally:
        transaction.cleanup()
