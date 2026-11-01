"""Shared Git object integrity under the actual disposable native mount profile."""
from pathlib import Path

import pytest

from tests.test_git_staging import confined, git, linked


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('action', ['overwrite', 'unlink'])
def test_native_cannot_corrupt_preexisting_shared_git_objects(linked, packed, action):
    from maestro.backends.git_staging import Transaction
    repo, tree, reports, start = linked
    objects = repo / '.git/objects'
    if packed:
        git(repo, '-c', 'pack.threads=1', 'repack', '-ad')
        target = next((objects / 'pack').glob('*.pack'))
    else:
        target = objects / start[:2] / start[2:]
    original = target.read_bytes()
    transaction = Transaction.prepare(tree, reports)
    transaction.start()
    try:
        result = confined(transaction, tree, f'''
from pathlib import Path
target = Path({str(target)!r})
try:
    if {action!r} == 'overwrite':
        target.chmod(0o600)
        target.write_bytes(b'synthetic corruption')
    else:
        target.unlink()
except OSError:
    print('refused')
else:
    print('corrupted')
''')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'refused', 'native process can corrupt a shared Git object'
        assert target.read_bytes() == original
        assert git(repo, 'fsck', '--no-dangling').returncode == 0
    finally:
        transaction.cleanup()
