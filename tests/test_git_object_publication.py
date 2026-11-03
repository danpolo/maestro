"""Real private-object publication concurrency, crashes and immutable imports."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from maestro.backends import git_objects, git_staging
from tests.hang_bound import HANG_SECONDS
from tests.test_git_staging import linked, confined, git
from tests.test_git_object_validation import staged
from tests.test_private_git_objects import hashes

pytestmark = pytest.mark.real_fsync


def test_independent_owners_validate_concurrently_and_publish_both_exact_refs(linked, tmp_path, monkeypatch):
    repo, tree, _, start = linked
    second = tmp_path/'second-tree'
    report = tmp_path/'second-report'
    report.mkdir()
    git(repo, 'worktree', 'add', str(second), 'impl/T-002')
    first = staged(linked)
    other = staged((repo, second, report, start))
    barrier = threading.Barrier(2)
    validate = git_objects.ObjectValidation.validate
    def concurrent(view, tip):
        validate(view, tip)
        barrier.wait(timeout=HANG_SECONDS)
    monkeypatch.setattr(git_objects.ObjectValidation, 'validate', concurrent)
    before = hashes(repo/'.git/objects')
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(t.finish, success=True) for t in (first, other)]
            tips = [f.result(timeout=2 * HANG_SECONDS) for f in futures]
        for transaction, tip in zip((first, other), tips):
            assert git(transaction.tree, 'rev-parse', 'HEAD').stdout.strip() == tip != start
            assert json.loads(Path(transaction.job).read_text())['state'] == 'promoted'
        assert git(repo, 'rev-parse', 'main').stdout.strip() == start
        assert all(hashes(repo/'.git/objects').get(n) == h for n,h in before.items())
        assert not list((repo/'.git/objects/pack').glob('*.keep'))
        assert git(repo, 'fsck', '--strict', '--no-dangling').returncode == 0
    finally:
        first.cleanup()
        other.cleanup()


@pytest.mark.parametrize('seam', ['keep', 'pack', 'idx', 'cas'])
def test_dead_import_preserves_ref_and_recovery_releases_only_its_keep(linked, seam):
    repo, tree, reports, start = linked
    transaction = git_staging.Transaction.prepare(tree, reports)
    program = r'''
import os,sys
from maestro.backends import git_staging,git_objects
from tests.test_git_staging import confined
t=git_staging.Transaction.load(sys.argv[1]); t.start()
r=confined(t,t.tree,"from pathlib import Path;import subprocess;Path('file.txt').write_text('new private tip\\n');subprocess.run(['git','add','file.txt'],check=True);subprocess.run(['git','-c','gc.auto=0','commit','-m','private'],check=True)")
assert r.returncode==0,r.stderr
seam=sys.argv[2]
link=os.link
def die_after_link(source,target,**kwargs):
    link(source,target,**kwargs)
    if str(target).endswith('.'+seam):os._exit(91)
os.link=die_after_link
git=git_staging._git
def die_before_cas(tree,*args,**kwargs):
    if seam=='cas' and args[0]=='update-ref':os._exit(91)
    return git(tree,*args,**kwargs)
git_staging._git=die_before_cas
t.finish(success=True)
raise AssertionError('fault seam was not reached')
'''
    child = subprocess.run([sys.executable, '-c', program, transaction.job, seam],
                           capture_output=True, text=True, timeout=HANG_SECONDS)
    assert child.returncode == 91, child.stdout+child.stderr
    assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    pack = repo/'.git/objects/pack'
    keep, = pack.glob('*.keep')
    assert keep.read_text() == transaction.job+'\n'
    foreign = pack/('pack-'+'f'*40+'.keep')
    foreign.write_text('another owner\n')
    retained = {p.name:p.read_bytes() for p in pack.glob('*.pack')}
    git(tree, 'reset', '--hard', start)
    retry = git_staging.Transaction.prepare(tree, reports)
    try:
        assert json.loads(Path(transaction.job).read_text())['state'] == 'interrupted'
        assert not keep.exists()
        assert foreign.read_text() == 'another owner\n'
        assert {p.name:p.read_bytes() for p in pack.glob('*.pack')} == retained
        retry.start()
        assert retry.finish(success=True) == start
        assert git(tree, 'status', '--porcelain').stdout == ''
        assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    finally:
        retry.cleanup()


@pytest.mark.parametrize('extension', ['.pack', '.idx'])
@pytest.mark.parametrize('same', [False, True])
def test_existing_publication_is_never_overwritten_and_foreign_keep_survives(linked, monkeypatch, extension, same):
    repo, tree, _, start = linked
    transaction = staged(linked)
    original = git_objects.ObjectValidation.publish
    evidence = []
    def collision(view):
        destination = repo/'.git/objects/pack'
        destination.mkdir(exist_ok=True)
        target = destination/(view.pack_name+extension)
        content = (view.quarantine/'pack'/target.name).read_bytes() if same else b'foreign collision bytes'
        target.write_bytes(content)
        target.chmod(0o444)
        keep = destination/(view.pack_name+'.keep')
        keep.write_text('another owner\n')
        evidence.append((target, content, keep))
        original(view)
    monkeypatch.setattr(git_objects.ObjectValidation, 'publish', collision)
    try:
        if same:
            tip = transaction.finish(success=True)
            assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == tip != start
        else:
            with pytest.raises(ValueError, match='collision'):
                transaction.finish(success=True)
            assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        target, content, keep = evidence[0]
        assert target.read_bytes() == content
        assert keep.read_text() == 'another owner\n'
        assert not list(target.parent.glob('.maestro-import-*'))
    finally:
        transaction.cleanup()


@pytest.mark.parametrize('seam', ['temporary', 'pack-directory'])
def test_import_fsync_failure_refuses_cas_and_releases_own_keep(linked, monkeypatch, seam):
    repo, tree, _, start = linked
    transaction = staged(linked)
    original = os.fsync
    reached = []
    def fsync(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        match = ('.maestro-import-' in name if seam == 'temporary' else
                 name == str(repo/'.git/objects/pack'))
        if match and not reached:
            reached.append(name)
            raise OSError('injected object import fsync failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    try:
        with pytest.raises(OSError, match='injected'):
            transaction.finish(success=True)
        assert reached
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert json.loads(Path(transaction.job).read_text())['state'] == 'aborted'
        assert not list((repo/'.git/objects/pack').glob('*.keep'))
    finally:
        transaction.cleanup()
