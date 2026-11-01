"""Bounded controller for an isolated native agy process."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from maestro.backends import control_files, git_staging, native_ownership

MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def run(argv, *, cwd=None, capture_output=False, text=True, timeout=3600,
        stdout=None, stderr=None, env=None, observe=None):
    """Drain both streams under one byte limit and deadline, then reap the child.

    Streaming launches and captured completions have the same output bound. Do
    not call communicate after cancellation: an escaped descendant can retain a
    pipe even after its original process group is killed. Production argv also
    has a private PID namespace, which reaps descendants outside that group.
    """
    if timeout <= 0:
        raise ValueError('native worker needs a positive timeout')
    deadline = time.monotonic() + timeout
    with control_files.pinned_mounts(argv) as (pinned, fds):
        process = subprocess.Popen(pinned, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if stderr == subprocess.STDOUT and not capture_output else subprocess.PIPE,
            start_new_session=True, **({'pass_fds': fds} if fds else {}))
    outputs = {'stdout': bytearray(), 'stderr': bytearray()}
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, ('stdout', stdout, 1))
            if process.stderr is not None:
                selector.register(process.stderr, selectors.EVENT_READ, ('stderr', stderr, 2))
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise RuntimeError('native worker output exceeds byte limit')
                    name, sink, inherited = key.data
                    if observe is not None:
                        observe(name, chunk)
                    if capture_output:
                        outputs[name].extend(chunk)
                    elif sink != subprocess.DEVNULL:
                        target = inherited if sink is None else (sink if isinstance(sink, int) else sink.fileno())
                        pending = memoryview(chunk)
                        while pending:
                            pending = pending[os.write(target, pending):]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        process.wait(timeout=remaining)
        output, errors = (bytes(outputs[name]) for name in ('stdout', 'stderr'))
        if text:
            output, errors = output.decode('utf-8'), errors.decode('utf-8')
        return subprocess.CompletedProcess(argv, process.returncode,
            output if capture_output else None, errors if capture_output else None)
    except subprocess.TimeoutExpired as exc:
        # Timeout output can end inside a multibyte character. Match subprocess'
        # byte-valued timeout evidence rather than decoding or discarding it.
        if capture_output:
            exc.output = bytes(outputs['stdout'])
            exc.stderr = bytes(outputs['stderr'])
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        process.wait(timeout=5)


class TerminalResult:
    """Require one real stdout terminal result for this invocation only."""
    def __init__(self):
        self.pending = bytearray()
        self.results = []

    def observe(self, name, chunk):
        if name != 'stdout':
            return
        self.pending.extend(chunk)
        while b'\n' in self.pending:
            line, _, rest = self.pending.partition(b'\n')
            self.pending = bytearray(rest)
            self.line(line)

    def line(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if isinstance(event, dict) and (event.get('event') or event.get('type')) == 'result':
            payload = event.get('result', event)
            self.results.append(payload.get('status') if isinstance(payload, dict) else None)

    def success(self):
        if self.pending:
            self.line(self.pending)
            self.pending.clear()
        return self.results == ['SUCCESS']


def main(job_path):
    job = json.loads(control_files.read(Path(job_path)))
    transaction = None
    reservation = native_ownership.Reservation.load(job['reservation']) if job.get('reservation') else None
    try:
        if reservation is not None:
            reservation.start()
        if job.get('git_transaction'):
            transaction = git_staging.Transaction.load(job['git_transaction'])
        if transaction is not None:
            transaction.start()
        terminal = TerminalResult()
        with control_files.open_file(Path(job['log_file']), job['mode']) as log:
            result = run(control_files.MountArgv(job['argv'], job.get('mounts', {})), cwd=job['cwd'], stdout=log,
                         stderr=log, env=job['env'], observe=terminal.observe)
            success = result.returncode == 0 and terminal.success()
            if transaction is not None:
                transaction.finish(success=success)
            raise SystemExit(result.returncode if result.returncode else (0 if success else 1))
    finally:
        control_files.cleanup_resources(
            ('Git', lambda: transaction.cleanup() if transaction is not None else None),
            ('ownership', lambda: native_ownership.cleanup(reservation)),
            ('mountpoint', lambda: control_files.release_mountpoints(job['created_mountpoints'])))
