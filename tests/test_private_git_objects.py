"""Private native objects and trusted, quarantined exact-task publication."""
import hashlib
import json
from pathlib import Path

import pytest

from maestro.backends import git_staging
from tests.test_git_staging import linked, confined, git


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('packed', [False, True])
def test_native_tip_is_private_until_trusted_promotion(linked, packed):
    repo, tree, reports, start = linked
    objects = repo / '.git/objects'
    if packed:
        git(repo, '-c', 'pack.threads=1', 'repack', '-ad')
    before = hashes(objects)
    transaction = git_staging.Transaction.prepare(tree, reports)
    transaction.start()
    try:
        result = confined(transaction, tree, '''
from pathlib import Path
import subprocess
Path('file.txt').write_text('private native commit\\n')
subprocess.run(['git','add','file.txt'],check=True)
subprocess.run(['git','-c','gc.auto=0','commit','-m','private'],check=True)
''' + ("subprocess.run(['git','-c','pack.threads=1','repack','-ad'],check=True)\n" if packed else ''))
        assert result.returncode == 0, result.stderr
        tip = (transaction.directory / transaction.ref).read_text().strip()
        assert tip != start
        assert hashes(objects) == before, 'native process wrote the shared object store'
        assert git(repo, 'cat-file', '-e', tip, check=False).returncode != 0
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert transaction.finish(success=True) == tip
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == tip
        assert git(tree, 'status', '--porcelain').stdout == ''
        assert git(repo, 'fsck', '--strict', '--no-dangling').returncode == 0
        assert all(hashes(objects).get(n) == h for n, h in before.items())
        assert git(repo, 'rev-parse', 'main').stdout.strip() == start
        assert git(repo, 'rev-parse', 'impl/T-002').stdout.strip() == start
        assert json.loads(Path(transaction.job).read_text())['state'] == 'promoted'
    finally:
        transaction.cleanup()
