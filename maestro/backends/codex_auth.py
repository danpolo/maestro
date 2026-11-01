"""Metadata-only freshness checks for the original stock Codex file login.

Native renewal may change file contents in place. Replacing the file or one of
its ancestors invalidates a bound inode; no credential descriptor is retained.
"""
import fcntl
import math
import os
from pathlib import Path
import select
import stat
import sys
import time

from maestro.backends import control_files


# A fresh shim has parent-death protection. Its detached trusted subreaper owns
# bwrap's whole lineage, including death before bwrap reports namespace status.
# Only the empty flock and lifecycle pipes remain in the observer after spawn.
# Explicit script content authorizes native exec; EOF alone executes no native
# command. waitpid(ECHILD) precedes observer lock close and completion delivery.
_LIFETIME_EXEC = r'''
import ctypes,os,select,signal,sys
lock=int(sys.argv[1]);completion=int(sys.argv[2]);parent=int(sys.argv[3]);argv=sys.argv[4:]
libc=ctypes.CDLL(None,use_errno=True)
def prctl(option,value):
    if libc.prctl(option,value,0,0,0)!=0:raise OSError(ctypes.get_errno(),'lifetime prctl failed')
def close_except(keep):
    for name in os.listdir('/proc/self/fd'):
        fd=int(name)
        if fd not in keep:
            try:os.close(fd)
            except OSError:pass
def kill_children():
    # This single-threaded subreaper never reaps during this loop, so each
    # listed child identity remains ours (even if it becomes a zombie).
    with open('/proc/self/task/%d/children'%os.getpid()) as stream:
        children=stream.read().split()
    for value in children:
        try:os.kill(int(value),signal.SIGKILL)
        except ProcessLookupError:pass
prctl(1,signal.SIGKILL)
if os.getppid()!=parent:os._exit(125)
alive,heartbeat=os.pipe();result,report=os.pipe();gate,release=os.pipe()
os.set_inheritable(gate,True)
observer=os.fork()
if observer==0:
    if os.fork()!=0:os._exit(0)
    os.setsid()
    child=None;status=125;success=False
    try:
        prctl(36,1)
        wake,notify=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC)
        signal.signal(signal.SIGCHLD,lambda *_:None)
        signal.set_wakeup_fd(notify)
        # Only the shim owns the heartbeat writer. Close it before fork so a
        # child awaiting scheduling cannot hide controller death from the gate.
        os.close(heartbeat);os.close(result)
        child=os.fork()
        if child==0:
            for fd in (alive,report,release,completion,wake,notify):os.close(fd)
            index=argv.index('--');script='/dev/.maestro-lifetime-exec'
            command=[argv[0],*argv[1:index],'--ro-bind-data',str(gate),script,
                     '--',sys.executable,'-I',script,*argv[index+1:]]
            os.execv(command[0],command)
        close_except((lock,alive,report,completion,release,wake,notify))
        # All observer setup and descriptor closure precede native authorization.
        if select.select([alive],[],[],0)[0] and not os.read(alive,1):
            raise RuntimeError('Codex controller died before native authorization')
        os.write(release,b'import os,sys\nos.execvp(sys.argv[1],sys.argv[1:])\n')
        os.close(release);release=None
        watched=[alive,wake]
        cancelled=False
        while True:
            while True:
                try:pid,code=os.waitpid(-1,os.WNOHANG)
                except ChildProcessError:
                    success=True
                    break
                if not pid:break
                if pid==child:
                    child=None
                    value=os.waitstatus_to_exitcode(code)
                    status=value if value>=0 else 128-value
            if success:break
            if cancelled:kill_children()
            ready,_,_=select.select(watched,[],[])
            if wake in ready:
                try:os.read(wake,65536)
                except BlockingIOError:pass
            if alive in ready and not os.read(alive,1):
                watched.remove(alive)
                cancelled=True
                kill_children()
    except BaseException:
        if child==0:os._exit(125)
        # A setup/error EOF never authorizes native exec. Kill the direct child
        # while its identity is unreaped, then reap EVERY adopted descendant.
        if release is not None:
            os.close(release);release=None
        while True:
            kill_children()
            try:os.waitpid(-1,0)
            except ChildProcessError:break
    finally:
        if release is not None:os.close(release)
        os.close(lock)
        try:os.write(completion,b'1' if success else b'0')
        except BrokenPipeError:pass
        try:os.write(report,bytes([min(status,255)]))
        except BrokenPipeError:pass
    os._exit(0)
os.waitpid(observer,0)
close_except((heartbeat,result))
reply=os.read(result,1)
os._exit(reply[0] if reply else 125)
'''


def capture(path):
    path = control_files.file(Path(path))
    directories = []
    for directory in reversed(path.parents):
        with control_files.directory_handle(directory) as fd:
            directories.append([str(directory), control_files.identity(os.fstat(fd))])
    with control_files.parent(path) as fd:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Codex auth identity needs a single-link regular file')
        leaf = control_files.identity(info)
    record = {'path': str(path), 'directories': directories, 'leaf': leaf}
    control_files.verify_directories(directories)
    return record


def verify(record):
    try:
        if not isinstance(record, dict) or set(record) != {'path', 'directories', 'leaf'}:
            raise ValueError('missing original login metadata')
        current = capture(record['path'])
        if current != record:
            raise ValueError('original login binding changed')
    except (OSError, ValueError, TypeError) as error:
        raise ValueError('Codex auth identity changed; refusing stale login binding') from error


class Lifetime:
    """Serialize Maestro native clients sharing the original file login.

    The empty lock is never deleted or explicitly unlocked. Bubblewrap consumes
    --sync-fd before native exec. Its trusted detached parent/subreaper retains
    the same open-file description until its entire native lineage is reaped,
    including the kernel teardown after bwrap closes its own descriptor copy.
    Unrelated stock clients do not participate in this Maestro mutex.
    """
    def __init__(self, record, *, deadline):
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError('Codex login lifetime needs a finite deadline')
        verify(record)
        self.record, self.deadline = record, deadline
        self.path = Path(record['path']).parent / '.maestro-auth.lock'
        self.stream = None
        self.children = []

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Codex login lifetime deadline exceeded')
        return remaining

    def verify(self):
        verify(self.record)
        if self.stream is None:
            raise ValueError('Codex login lifetime is not held')
        opened = os.fstat(self.stream.fileno())
        with control_files.parent(self.path) as parent:
            current = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
        if (control_files.identity(opened) != control_files.identity(current)
                or opened.st_nlink != 1 or current.st_nlink != 1
                or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size != 0):
            raise ValueError('Codex login lifetime lock identity changed')

    def __enter__(self):
        self.remaining()
        self.stream = control_files.open_file(self.path, 'a')
        try:
            while True:
                self.verify()
                remaining = self.remaining()
                try:
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(.05, remaining))
            self.verify()
            self.remaining()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def child_arguments(self, argv, fds):
        """Hand the lock only to the trusted bwrap supervisor, never native exec."""
        self.verify()
        self.remaining()
        options = argv[1:argv.index('--')] if '--' in argv else ()
        if (Path(argv[0]).name != 'bwrap'
                or not {'--unshare-pid', '--die-with-parent'} <= set(options)
                or any(option in options for option in ('--sync-fd', '--as-pid-1', '--info-fd', '--block-fd'))):
            raise ValueError('Codex login lifetime requires the confined PID supervisor')
        fd = self.stream.fileno()
        wrapped = [argv[0], '--sync-fd', str(fd), *argv[1:]]
        reader, writer = os.pipe()
        self.children.append((reader, writer))
        return [sys.executable, '-I', '-c', _LIFETIME_EXEC, str(fd), str(writer), str(os.getpid()), *wrapped], (*fds, fd, writer)

    def finish_child(self):
        """Join trusted observer completion after the caller has reaped bwrap.

        The completion byte follows final namespace exit and observer lock close.
        Closing the controller descriptor alone deliberately does not join: it
        can disappear while a live namespace still needs observer protection.
        """
        if not self.children:
            return
        reader, writer = self.children.pop(0)
        os.close(writer)
        try:
            if not select.select([reader], [], [], 5)[0]:
                raise TimeoutError('Codex login lifetime observer teardown deadline exceeded')
            if os.read(reader, 2) != b'1':
                raise ValueError('Codex login lifetime observer failed')
        finally:
            os.close(reader)

    def __exit__(self, *exc):
        for pair in self.children:
            for fd in pair:
                os.close(fd)
        self.children.clear()
        if self.stream is not None:
            self.stream.close()
            self.stream = None
