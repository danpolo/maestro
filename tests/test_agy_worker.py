"""Real process output and lifetime bounds for the native worker controller."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import time

import pytest

from maestro.backends import agy_worker
from tests.hang_bound import HANG_SECONDS


class _GatedClock:
    """agy_worker's clock: frozen until the test opens the gate, then expired.

    The timeout path is the subject, not the host's speed. A short real
    timeout made these tests depend on the child starting within 1-2 s, which
    a loaded host misses. Here the deadline passes only after the child has
    done what the test observes. One frozen read after the gate lets the loop
    drain bytes the child wrote before the gate opened.
    """
    def __init__(self, gate):
        self.gate = gate
        self.drained = False

    def monotonic(self):
        if not self.gate():
            return 0.0
        if not self.drained:
            self.drained = True
            return 0.0
        return float('inf')


@pytest.mark.parametrize('stream', ['stdout', 'stderr', 'combined'])
@pytest.mark.parametrize('capture', [False, True])
def test_native_output_limit_covers_captured_and_logged_bytes(tmp_path, monkeypatch, stream, capture):
    limit = 3000 if stream == 'combined' else 1000
    monkeypatch.setattr(agy_worker, 'MAX_OUTPUT_BYTES', limit, raising=False)
    script = ('import os\n' +
        ('os.write(1,b"x"*2048)\n' if stream != 'stderr' else '') +
        ('os.write(2,b"y"*2048)\n' if stream != 'stdout' else ''))
    with (tmp_path / 'log').open('w') as log:
        with pytest.raises(RuntimeError, match='output.*limit'):
            agy_worker.run([sys.executable, '-I', '-c', script],
                capture_output=capture, stdout=None if capture else log,
                stderr=None if capture else subprocess.STDOUT,
                timeout=HANG_SECONDS)
    assert (tmp_path / 'log').stat().st_size <= limit


def test_bounded_worker_preserves_unicode_and_stdin_eof():
    result = agy_worker.run([sys.executable, '-I', '-c',
        'import os,sys; assert sys.stdin.read()==""; '
        'os.write(1,"αβ".encode()); os.write(2,"γ".encode())'],
        capture_output=True, timeout=HANG_SECONDS)
    assert result.returncode == 0
    assert (result.stdout, result.stderr) == ('αβ', 'γ')


def test_timeout_retains_partial_native_bytes_and_reaps_child(tmp_path, monkeypatch):
    import os

    identity = tmp_path / 'pid'
    # The partial bytes are in the pipes before the identity file appears, so
    # the gate opens only once there is partial output to retain.
    script = (
        'import os,time\nfrom pathlib import Path\n'
        'os.write(1,b"partial stdout\\xe2")\n'
        'os.write(2,b"partial stderr\\xff")\n'
        f'Path({str(identity)!r}).write_text(str(os.getpid()))\n'
        'time.sleep(60)\n'
    )
    monkeypatch.setattr(agy_worker, 'time', _GatedClock(
        lambda: identity.exists() and identity.read_text() != ''))
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        agy_worker.run([sys.executable, '-I', '-c', script],
                       capture_output=True, text=True, timeout=1)
    assert failure.value.output == b'partial stdout\xe2'
    assert failure.value.stderr == b'partial stderr\xff'
    with pytest.raises(ProcessLookupError):
        os.kill(int(identity.read_text()), 0)


@pytest.mark.parametrize('hang', [False, True])
def test_native_pid_namespace_reaps_descendant_that_escapes_process_group(tmp_path, monkeypatch, hang):
    import threading
    from maestro.confinement import bubblewrap_argv
    ready = tmp_path / 'ready'
    release = tmp_path / 'release'
    observed = threading.Event()
    if hang:
        # The timeout fires only after the live descendant has been observed.
        monkeypatch.setattr(agy_worker, 'time', _GatedClock(observed.is_set))
    child = ('import time\nfrom pathlib import Path\n'
        f'Path({str(ready)!r}).write_text("ready")\n'
        'time.sleep(60)\n')
    parent = ('import subprocess,sys,time\nfrom pathlib import Path\n'
        f'subprocess.Popen([sys.executable,"-I","-c",{child!r}],start_new_session=True, '
        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
        f'while not Path({str(release)!r}).exists(): time.sleep(.01)\n' +
        ('time.sleep(60)\n' if hang else 'print("done")\n'))
    argv = bubblewrap_argv([sys.executable, '-I', '-c', parent], cwd=tmp_path,
        write_roots=(tmp_path,))
    def execute():
        if hang:
            with pytest.raises(subprocess.TimeoutExpired):
                agy_worker.run(argv, capture_output=True, timeout=2)
        else:
            assert agy_worker.run(argv, capture_output=True,
                                  timeout=HANG_SECONDS).stdout == 'done\n'
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(execute)
        deadline = time.monotonic() + HANG_SECONDS
        while not ready.exists() and not pending.done() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'escaping descendant never started'
        children = []
        for path in Path('/proc').glob('[0-9]*'):
            try:
                if path.joinpath('cmdline').read_bytes().split(b'\0') == [
                        sys.executable.encode(), b'-I', b'-c', child.encode(), b'']:
                    children.append(path)
            except OSError:
                pass
        assert len(children) == 1, 'must observe the live descendant before teardown'
        release.touch()
        observed.set()
        pending.result(timeout=HANG_SECONDS)
    # Observe the same host process after namespace teardown, including setsid.
    deadline = time.monotonic() + HANG_SECONDS
    while time.monotonic() < deadline:
        surviving = []
        for path in children:
            try:
                if path.joinpath('stat').read_text().split(') ', 1)[1][0] != 'Z':
                    surviving.append(path)
            except (OSError, ProcessLookupError):
                pass
        if not surviving:
            break
        time.sleep(.01)
    assert not surviving, 'setsid descendant survived native worker completion'
