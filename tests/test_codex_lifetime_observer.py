"""Trusted observer startup refusal and normal teardown completion contract."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from maestro import confinement
from maestro.backends import codex_auth, control_files
from tests.hang_bound import HANG_SECONDS
from tests.test_codex_auth_lifetime import lock_available, process_identity, wait_for


def launch(tmp_path):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic-login')
    lifetime = codex_auth.Lifetime(codex_auth.capture(auth), deadline=time.monotonic()+HANG_SECONDS)
    lifetime.__enter__()
    argv = confinement.bubblewrap_argv([sys.executable,'-I','-c',
        "from pathlib import Path;import sys,time;root=Path(sys.argv[1]);(root/'ready').touch();"
        "\nwhile not (root/'release').exists():time.sleep(.01)",str(tmp_path)],
        cwd=tmp_path,write_roots=(tmp_path,),read_only_roots=(auth,))
    with control_files.pinned_mounts(argv) as (pinned,fds):
        command,fds = lifetime.child_arguments(pinned,fds)
        proc = subprocess.Popen(command,pass_fds=fds,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    return lifetime,proc


def lock_observer(path):
    expected=path.stat()
    found=[]
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            row=process_identity(int(proc.name))
            if not row or row['parent']!=1 or row['state']=='Z':continue
            for fd in (proc/'fd').iterdir():
                try:
                    info=fd.stat()
                    if (info.st_dev,info.st_ino)==(expected.st_dev,expected.st_ino):
                        found.append(row)
                        break
                except OSError:pass
        except OSError:pass
    assert len(found)==1,found
    return found[0]


def test_normal_teardown_joins_observer_before_lifetime_return(tmp_path):
    lifetime,proc=launch(tmp_path)
    observer=None
    try:
        wait_for(lambda:(tmp_path/'ready').exists() or proc.poll() is not None)
        assert proc.poll() is None,proc.communicate()
        observer=lock_observer(lifetime.path)
        os.kill(observer['pid'],signal.SIGSTOP)
        wait_for(lambda:process_identity(observer['pid'])['state']=='T')
        proc.terminate()
        assert proc.wait(timeout=HANG_SECONDS)==-signal.SIGTERM
        # The observer is deliberately unscheduled. A production join must
        # await its completion, rather than assume reaping bwrap released it.
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        with ThreadPoolExecutor(1) as pool:
            done=pool.submit(lifetime.finish_child)
            try:
                with pytest.raises(TimeoutError):done.result(timeout=.15)
                assert not lock_available(lifetime.path)
            finally:
                os.kill(observer['pid'],signal.SIGCONT)
            done.result(timeout=HANG_SECONDS)
        assert proc.communicate(timeout=HANG_SECONDS)==(b'',b'')
        lifetime.__exit__(None,None,None)
        assert lock_available(lifetime.path)
    finally:
        if observer:
            try:os.kill(observer['pid'],signal.SIGCONT)
            except ProcessLookupError:pass
        if proc.poll() is None:proc.kill()
        proc.communicate(timeout=HANG_SECONDS)
        lifetime.__exit__(None,None,None)


@pytest.mark.parametrize('failure',["raise ValueError('injected observer startup failure')",'os._exit(92)'])
def test_observer_startup_error_cannot_release_native_on_gate_eof(tmp_path,monkeypatch,failure):
    code=codex_auth._LIFETIME_EXEC
    needle="        # All observer setup and descriptor closure precede native authorization.\n"
    assert code.count(needle)==1
    monkeypatch.setattr(codex_auth,'_LIFETIME_EXEC',code.replace(needle,
        needle+'        '+failure+'\n'))
    lifetime,proc=launch(tmp_path)
    try:
        wait_for(lambda:(tmp_path/'ready').exists() or proc.poll() is not None)
        assert not (tmp_path/'ready').exists(),'observer error released native startup on gate EOF'
        assert proc.communicate(timeout=HANG_SECONDS)==(b'',b'')
        assert proc.returncode!=0
        with pytest.raises(ValueError,match='observer failed'):
            lifetime.finish_child()
    finally:
        if proc.poll() is None:proc.kill()
        proc.communicate(timeout=HANG_SECONDS)
        lifetime.__exit__(None,None,None)


def test_observer_closes_mount_and_credential_fds_and_waits_without_polling(tmp_path):
    lifetime,proc=launch(tmp_path)
    try:
        wait_for(lambda:(tmp_path/'ready').exists() or proc.poll() is not None)
        assert proc.poll() is None,proc.communicate()
        observer=lock_observer(lifetime.path)
        directory=Path(f'/proc/{observer["pid"]}')
        fds=[os.readlink(fd) for fd in (directory/'fd').iterdir()]
        assert len(fds)==6,fds
        assert fds.count(str(lifetime.path))==1,fds
        assert all(name==str(lifetime.path) or name.startswith('pipe:') for name in fds),fds
        def usage():
            fields=(directory/'stat').read_text().rsplit(')',1)[1].split()
            return int(fields[11])+int(fields[12]),int(fields[21])*os.sysconf('SC_PAGE_SIZE')
        before,rss=usage()
        # Deliberate idle interval measures event-driven supervision CPU cost.
        time.sleep(.15)
        after,_=usage()
        assert after-before<=1
        assert rss<64*1024*1024
        (tmp_path/'release').touch()
        assert proc.communicate(timeout=HANG_SECONDS)==(b'',b'')
        assert proc.returncode==0
        lifetime.finish_child()
    finally:
        if proc.poll() is None:proc.kill()
        proc.communicate(timeout=HANG_SECONDS)
        lifetime.__exit__(None,None,None)


@pytest.mark.parametrize('phase',['before-fork','before-fork-delayed-child','before-authorization'])
def test_controller_death_before_startup_cannot_run_native_or_leave_namespace(tmp_path,monkeypatch,phase):
    code=codex_auth._LIFETIME_EXEC
    needle=('        child=os.fork()\n' if phase.startswith('before-fork') else
            '        # All observer setup and descriptor closure precede native authorization.\n')
    assert code.count(needle)==1
    # This stop is in the detached observer; the shim's death must be noticed
    # before launch authorization when the observer is resumed.
    code=code.replace(needle,'        os.kill(os.getpid(),signal.SIGSTOP)\n'+needle)
    if phase=='before-fork-delayed-child':
        # Force the child to retain its inherited descriptors until after the
        # observer's EOF check. This is a scheduling boundary, not a sleep.
        code=code.replace('        if child==0:\n',
            '        if child!=0:os.waitpid(child,os.WUNTRACED)\n'
            '        if child==0:\n            os.kill(os.getpid(),signal.SIGSTOP)\n',1)
        code=code.replace("        os.write(release,b'import os,sys",
            "        os.kill(child,signal.SIGCONT)\n        os.write(release,b'import os,sys",1)
    monkeypatch.setattr(codex_auth,'_LIFETIME_EXEC',code)
    lifetime,proc=launch(tmp_path)
    observer=None
    try:
        def stopped():
            nonlocal observer
            try:observer=lock_observer(lifetime.path)
            except AssertionError:return False
            return observer['state']=='T'
        wait_for(stopped)
        proc.kill()
        proc.wait(timeout=HANG_SECONDS)
        os.kill(observer['pid'],signal.SIGCONT)
        with pytest.raises(ValueError,match='observer failed'):
            lifetime.finish_child()
        proc.communicate(timeout=HANG_SECONDS)
        assert not (tmp_path/'ready').exists()
        lifetime.__exit__(None,None,None)
        assert lock_available(lifetime.path)
    finally:
        if observer:
            try:os.kill(observer['pid'],signal.SIGCONT)
            except ProcessLookupError:pass
        if proc.poll() is None:proc.kill()
        proc.communicate(timeout=HANG_SECONDS)
        lifetime.__exit__(None,None,None)


def test_observer_death_during_native_run_refuses_completion(tmp_path):
    lifetime,proc=launch(tmp_path)
    try:
        wait_for(lambda:(tmp_path/'ready').exists() or proc.poll() is not None)
        assert proc.poll() is None,proc.communicate()
        observer=lock_observer(lifetime.path)
        os.kill(observer['pid'],signal.SIGKILL)
        proc.communicate(timeout=HANG_SECONDS)
        assert proc.returncode==125
        with pytest.raises(ValueError,match='observer failed'):
            lifetime.finish_child()
    finally:
        if proc.poll() is None:proc.kill()
        proc.communicate(timeout=HANG_SECONDS)
        lifetime.__exit__(None,None,None)
