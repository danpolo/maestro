"""Actual native Git process death and the existing confined restore/retry."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from maestro import confinement, orchestrator
from maestro.backends import git_staging
from tests.hang_bound import HANG_SECONDS
from tests.test_git_staging import git


@pytest.fixture
def namespaced(tmp_path):
    repo, report = tmp_path / 'repo', tmp_path / 'report'
    repo.mkdir()
    report.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'core.hooksPath', '/dev/null')
    (repo / '.gitignore').write_text('.orchestrator/\n')
    (repo / 'file.txt').write_text('baseline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'baseline')
    start = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    git(repo, 'branch', 'impl/sibling')
    boundary = confinement.CommitBoundary.for_task(repo, 'T-001')
    boundary.worktree.parent.mkdir(parents=True)
    git(repo, 'worktree', 'add', '-b', boundary.branch, str(boundary.worktree), start)
    return repo, report, start, boundary


@pytest.mark.parametrize('phase', ['prepared', 'running', 'audit', 'ready-journal', 'ready',
                                  'pre-cas', 'post-cas', 'promoted-journal'])
def test_real_death_rejects_provisional_done_then_actual_restore_and_retry(namespaced, phase):
    repo, report, start, boundary = namespaced
    program = '''
import json,os,subprocess,sys
from pathlib import Path
from maestro import confinement
from maestro.backends import git_staging
tree,report,phase=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
transaction=git_staging.Transaction.prepare(tree,report)
(report/'job').write_text(transaction.job)
(report/'DONE').write_text('provisional')
if phase=='prepared': os._exit(73)
transaction.start()
(tree/'file.txt').write_text('partial native work\\n')
if phase=='running': os._exit(73)
argv=[sys.executable,'-c',"import subprocess; subprocess.run(['git','add','file.txt'],check=True); subprocess.run(['git','-c','gc.auto=0','commit','-m','private'],check=True)"]
wrapped=confinement.bubblewrap_argv(argv,cwd=tree,write_roots=(tree,*transaction.write_roots))
split=len(wrapped)-len(argv)-1
subprocess.run([*wrapped[:split],*transaction.mounts(),*wrapped[split:]],cwd=tree,check=True,capture_output=True)
audit=transaction._audit
def audited():
    result=audit()
    if phase=='audit': os._exit(73)
    return result
transaction._audit=audited
dump=git_staging._dump
def published(path,value):
    dump(path,value)
    state=value.get('state')
    if state=='ready' and (phase=='ready-journal' and path.name=='transaction.json' or phase=='ready' and path.name=='active.json'): os._exit(73)
    if phase=='promoted-journal' and state=='promoted' and path.name=='transaction.json': os._exit(73)
git_staging._dump=published
native_git=git_staging._git
def promoted(tree,*args,**kwargs):
    if phase=='pre-cas' and args[0]=='update-ref': os._exit(73)
    result=native_git(tree,*args,**kwargs)
    if phase=='post-cas' and args[0]=='update-ref': os._exit(73)
    return result
git_staging._git=promoted
transaction.finish(success=True)
raise SystemExit('crash seam was not reached')
'''
    child = subprocess.run([sys.executable, '-c', program, str(boundary.worktree), str(report), phase],
                           text=True, capture_output=True, timeout=HANG_SECONDS)
    assert child.returncode == 73, child.stderr + child.stdout
    job = Path((report / 'job').read_text())
    interrupted = json.loads(job.read_text())
    assert interrupted['state'] in ('prepared', 'running', 'ready', 'promoted')
    state = git_staging.publication_state(report)
    assert state != 'promoted'
    if state == 'pending':
        assert not orchestrator._native_git_reports_ready(report, running=True)
    else:
        # A torn promoted/ready pair is already a definite failure. The helper
        # returns ready-to-settle and replaces DONE with FAILED immediately.
        assert orchestrator._native_git_reports_ready(report, running=True)
        assert not (report / 'DONE').exists()
        assert (report / 'FAILED').exists()
    assert orchestrator._native_git_reports_ready(report, running=False)
    assert not (report / 'DONE').exists()
    if phase not in ('prepared', 'post-cas', 'promoted-journal'):
        with pytest.raises(git_staging.GitStagingError, match='needs task restore'):
            git_staging.Transaction.prepare(boundary.worktree, report)
    boundary.restore(start)
    assert (boundary.worktree / 'file.txt').read_text() == 'baseline\n'
    assert git(boundary.worktree, 'status', '--porcelain').stdout == ''
    retry = git_staging.Transaction.prepare(boundary.worktree, report)
    assert json.loads(job.read_text())['state'] == 'interrupted'
    retry.start()
    retry.finish(success=True)
    assert git_staging.publication_state(report) == 'promoted'
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    assert git(repo, 'rev-parse', 'impl/sibling').stdout.strip() == start
    assert git(boundary.worktree, 'rev-parse', 'HEAD').stdout.strip() == start
