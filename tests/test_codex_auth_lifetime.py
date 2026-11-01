"""Maestro clients queue on one login through native teardown, without auth reads."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from maestro.backends import codex, codex_app_server, codex_auth, codex_runtime, codex_worker, control_files
from maestro.backends.base import CompletionSpec
from maestro import confinement
from maestro.tool_policy import ToolPolicy
from tests.codex_host_isolation import skip_real_home_inventory
# Waits and budgets that only stop a hung case (the shared HANG_SECONDS). Each
# asserted ordering is held by an event or a lock, never by how fast a loaded suite
# prepares or spawns.
from tests.hang_bound import HANG_SECONDS


class _FakeClock:
    """Monotonic time that advances only when the code under test sleeps."""

    def __init__(self):
        self.now = time.monotonic()
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def test_complete_queues_same_login_until_first_native_turn_and_cleanup_finish(tmp_path, monkeypatch):
    native = tmp_path/'native'
    native.mkdir()
    (native/'auth.json').write_text('synthetic-login')
    entered, release, prepared_second = threading.Event(), threading.Event(), threading.Event()
    order = []
    def prepare(**kwargs):
        # Concurrency at the public API; namespace/FD behavior has separate real
        # process tests. Slow profile copying must not dominate this held turn.
        def wrap(argv):
            if kwargs['cwd'].name == 'second':
                prepared_second.set()
            return argv
        def cleanup():
            assert not lock_available(native/'.maestro-auth.lock')
        return SimpleNamespace(auth_identity=codex_auth.capture(native/'auth.json'),
            wrap=wrap, cleanup=cleanup, transaction=None,
            policy=ToolPolicy(allow=()), environment=lambda: {}, verify_config=lambda _: None)
    monkeypatch.setattr(codex_runtime, 'prepare', prepare)

    def turn(**kwargs):
        label = kwargs['prompt']
        order.append(label + '-start')
        if label == 'first':
            entered.set()
            assert release.wait(HANG_SECONDS)
        order.append(label + '-stop')
        return codex_app_server.TurnResult('accepted', label)

    monkeypatch.setattr(codex_app_server, 'run_turn', turn)
    def complete(label):
        tree = tmp_path/label
        tree.mkdir()
        return codex.CodexBackend(binary='unused', codex_home=native).complete(
            CompletionSpec(model='gpt-6-luna', prompt=label, cwd=tree, timeout=HANG_SECONDS))

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(complete, 'first')
        assert entered.wait(HANG_SECONDS)
        second = pool.submit(complete, 'second')
        try:
            assert prepared_second.wait(HANG_SECONDS)
            # Preparation is finished. This deliberately held first turn must
            # prevent the second from completing, regardless of preparation time.
            with pytest.raises(TimeoutError):
                second.result(timeout=2)
            assert order == ['first-start']
        finally:
            release.set()
        assert first.result(timeout=HANG_SECONDS).text == 'accepted'
        assert second.result(timeout=HANG_SECONDS).text == 'accepted'
    assert order == ['first-start', 'first-stop', 'second-start', 'second-stop']


def lock_available(path):
    with path.open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@pytest.mark.parametrize('mode', ['fresh', 'resume', 'missing'])
def test_generated_worker_holds_one_login_lifetime_across_fallback_and_cleanup(tmp_path, monkeypatch, mode):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic-login')
    lock = tmp_path/'.maestro-auth.lock'
    calls = []
    cleaned = []
    def turn(**kwargs):
        assert not lock_available(lock), 'generated native worker has no login lifetime lock'
        calls.append(kwargs['resume_id'])
        if mode == 'missing' and len(calls) == 1:
            raise codex_app_server.DeadThread('no rollout found for thread id')
        return codex_app_server.TurnResult('accepted', 'thread')
    monkeypatch.setattr(codex_worker, 'run_turn', turn)
    cleanup = control_files.cleanup_resources
    def resources(*args):
        assert not lock_available(lock), 'login lock released before resource teardown'
        cleaned.append(True)
        return cleanup(*args)
    monkeypatch.setattr(control_files, 'cleanup_resources', resources)
    job = dict(auth_identity=codex_auth.capture(auth), workspace=str(tmp_path), allow=[], deny=[],
               write_roots=[], timeout=HANG_SECONDS, mode='w', argv=[], cwd=str(tmp_path), env={},
               model='gpt-6-luna', effort='low', prompt='fixture', expected_config='{}',
               resume_id='' if mode == 'fresh' else 'thread',
               fallback_prompt='fresh brief' if mode == 'missing' else None)
    path = tmp_path/'job.json'
    path.write_text(json.dumps(job))
    codex_worker.main(str(path))
    assert calls == (['thread', ''] if mode == 'missing' else [job['resume_id']])
    assert cleaned == [True]
    assert lock_available(lock)


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return dict(pid=pid, parent=int(fields[1]), state=fields[0], start=fields[19])
    except FileNotFoundError:
        return None


def descendants(pid):
    rows = [process_identity(int(p.name)) for p in Path('/proc').glob('[0-9]*')]
    found = {pid}
    while True:
        new = {r['pid'] for r in rows if r and r['parent'] in found}
        if new <= found:
            break
        found |= new
    return [r for r in rows if r and r['pid'] in found - {pid}]


def live(row):
    current = process_identity(row['pid'])
    return bool(current and current['start'] == row['start'] and current['state'] != 'Z')


def wait_for(predicate, timeout=HANG_SECONDS):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'timed out waiting for lifecycle condition'
        time.sleep(.01)


def controller_entry():
    """Fresh subprocess, including the production frozen launcher path."""
    root, mode = Path(sys.argv[1]), sys.argv[2]
    backend = codex.CodexBackend(binary=root/'synthetic-server', codex_home=root/'native')
    # host_home=root alone does not isolate preparation: the tree's ancestors reach
    # the developer's real home, whose walk spent the readiness wait under load.
    skip_real_home_inventory()
    if mode in ('complete', 'telemetry'):
        # Match the generated case's isolated host profile. The lifecycle test
        # must exercise real preparation without inventorying the operator's
        # unrelated live home, whose size and changes are outside this fixture.
        original_prepare = codex_runtime.prepare
        def prepare(**kwargs):
            return original_prepare(host_home=root, **kwargs)
        codex_runtime.prepare = prepare
        if mode == 'telemetry':
            def observe(raw):
                value = json.loads(raw)
                if 'diagnostic_fds' in value:
                    marker = root/'reports/native-fds.json'
                    pending = marker.with_suffix('.tmp')
                    pending.write_text(json.dumps(value['diagnostic_fds']))
                    pending.replace(marker)
                return None
            backend._rate_limits_of_reply = observe
            backend.usage_from_app_server(timeout_sec=HANG_SECONDS)
            raise SystemExit(0)
        result = backend.complete(CompletionSpec(model='gpt-6-luna', prompt='fixture',
            cwd=root/'tree', writable=True, add_dirs=(root/'reports',),
            log_file=root/'reports/impl.log', timeout=HANG_SECONDS))
        raise SystemExit(result.returncode)
    run = codex_runtime.prepare(cwd=root/'tree', workspace=root/'reports',
        native_home=root/'native', host_home=root, model='gpt-6-luna', effort='low',
        write_roots=(root/'reports',))
    source = run.launcher(binary=str(root/'synthetic-server'), prompt='fixture',
                          timeout=HANG_SECONDS)
    exec(compile(source, '<frozen launcher>', 'exec'))


@pytest.mark.parametrize('mode', ['complete', 'generated', 'telemetry'])
def test_killed_controller_queues_successor_until_native_pid_namespace_is_gone(tmp_path, mode):
    for name in ('tree', 'reports', 'native'):
        (tmp_path/name).mkdir()
    auth = tmp_path/'native/auth.json'
    auth.write_text('synthetic-login')
    record = codex_auth.capture(auth)
    executable = tmp_path/'synthetic-server'
    executable.write_text(f'#!{sys.executable}\ntelemetry={mode == "telemetry"!r}\n' + '''
import json,os,subprocess,sys,time
from pathlib import Path
root=Path(__file__).parent
subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],start_new_session=True)
fds={}
for p in Path('/proc/self/fd').iterdir():
    try:fds[p.name]=os.readlink(p)
    except OSError:pass
if telemetry:
    print(json.dumps({'diagnostic_fds':fds}),flush=True)
else:
    marker=root/'reports/native-fds.json'
    pending=marker.with_suffix('.tmp')
    pending.write_text(json.dumps(fds))
    pending.replace(marker)
while True:time.sleep(1)
''')
    executable.chmod(0o755)
    child = subprocess.Popen([sys.executable, '-c',
        'from tests.test_codex_auth_lifetime import controller_entry; controller_entry()',
        str(tmp_path), mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    rows = []
    try:
        marker = tmp_path/'reports/native-fds.json'
        try:
            wait_for(lambda: marker.exists() or child.poll() is not None, HANG_SECONDS)
        except AssertionError:
            child.kill()
            stdout, stderr = child.communicate(timeout=HANG_SECONDS)
            pytest.fail(f'native readiness was not reached; stdout={stdout!r}; stderr={stderr!r}')
        assert child.poll() is None, child.communicate()
        fds = json.loads(marker.read_text())
        assert not any('.maestro-auth.lock' in name or name.endswith('/auth.json')
                       for name in fds.values()), fds
        rows = descendants(child.pid)
        from tests.test_codex_lifetime_observer import lock_observer
        observer = lock_observer(auth.parent/'.maestro-auth.lock')
        rows += descendants(observer['pid'])
        rows = list({row['pid']:row for row in rows}.values())
        assert len([r for r in rows if r['state'] != 'Z']) >= 4, rows
        assert not lock_available(auth.parent/'.maestro-auth.lock')
        child.kill()
        child.communicate(timeout=HANG_SECONDS)
        # The successor's admission is what is asserted: it may enter only once
        # every captured native process is gone. The deadline only bounds a hang.
        with codex_auth.Lifetime(record, deadline=time.monotonic()+HANG_SECONDS):
            assert not any(live(r) for r in rows), rows
        assert lock_available(auth.parent/'.maestro-auth.lock')
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=HANG_SECONDS)
        # Only this fixture's captured PID/start identities may be cleaned up.
        for row in rows:
            if live(row):
                os.kill(row['pid'], signal.SIGKILL)


def test_lock_wait_consumes_deadline_and_never_unlocks_another_lifetime(tmp_path, monkeypatch):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic-login')
    record = codex_auth.capture(auth)
    with codex_auth.Lifetime(record, deadline=time.monotonic()+HANG_SECONDS):
        # The queue's clock is the fixture's: the deadline, not suite load, ends the wait.
        clock = _FakeClock()
        monkeypatch.setattr(codex_auth, 'time', clock)
        started = clock.now
        with pytest.raises(TimeoutError, match='lifetime deadline'):
            with codex_auth.Lifetime(record, deadline=started+.15):
                pytest.fail('queued lifetime entered before release')
        # It polled the held lock and gave up exactly at its own deadline.
        assert len(clock.slept) >= 3
        assert clock.now - started == pytest.approx(.15)
        assert not lock_available(tmp_path/'.maestro-auth.lock')
    assert lock_available(tmp_path/'.maestro-auth.lock')


def test_telemetry_queue_debits_its_existing_budget_without_starting_a_native_client(tmp_path, monkeypatch):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic-login')
    spawned = []
    backend = codex.CodexBackend(binary='unused', codex_home=tmp_path,
        popen=lambda *args, **kwargs: spawned.append(args))
    with codex_auth.Lifetime(codex_auth.capture(auth), deadline=time.monotonic()+HANG_SECONDS):
        # One fixture clock for the sample budget and the queue: the queue must
        # spend the sample's own .15 s, neither a fresh budget nor real load.
        clock = _FakeClock()
        monkeypatch.setattr(codex, 'time', clock)
        monkeypatch.setattr(codex_auth, 'time', clock)
        started = clock.now
        assert backend.usage_from_app_server(timeout_sec=.15) is None
        assert not spawned, 'telemetry bypassed the login lifetime queue'
        assert clock.slept
        assert clock.now - started == pytest.approx(.15)
        assert 'lifetime deadline' in backend.last_telemetry_error
        assert not lock_available(tmp_path/'.maestro-auth.lock')


def test_supervisor_retains_lock_after_controller_closes_its_descriptor(tmp_path):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic-login')
    record = codex_auth.capture(auth)
    script = '''
from pathlib import Path
import sys,time
root=Path(sys.argv[1])
(root/'ready').touch()
while not (root/'release').exists():time.sleep(.01)
'''
    argv = confinement.bubblewrap_argv([sys.executable, '-I', '-c', script, str(tmp_path)],
        cwd=tmp_path, write_roots=(tmp_path,))
    child = None
    with codex_auth.Lifetime(record, deadline=time.monotonic()+HANG_SECONDS) as lifetime:
        with control_files.pinned_mounts(argv) as (pinned, fds):
            command, fds = lifetime.child_arguments(pinned, fds)
            child = subprocess.Popen(command, pass_fds=fds,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: (tmp_path/'ready').exists() or child.poll() is not None,
                     HANG_SECONDS)
            assert child.poll() is None, child.communicate()
            lifetime.__exit__(None, None, None)
            with pytest.raises(TimeoutError):
                with codex_auth.Lifetime(record, deadline=time.monotonic()+.15):
                    pytest.fail('supervisor dropped a live namespace lifetime')
            (tmp_path/'release').touch()
            teardown_deadline = time.monotonic() + HANG_SECONDS
            assert child.communicate(timeout=HANG_SECONDS) == (b'', b'')
            assert child.returncode == 0
            # Process reap can precede the kernel's final flock release. Test
            # admission through the real lifetime queue, within the SAME teardown
            # budget, instead of assuming the first nonblocking flock succeeds.
            with codex_auth.Lifetime(record, deadline=teardown_deadline):
                assert not lock_available(tmp_path/'.maestro-auth.lock')
            assert lock_available(tmp_path/'.maestro-auth.lock')
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=HANG_SECONDS)


@pytest.mark.parametrize('replacement', ['symlink', 'hardlink', 'regular'])
def test_login_lock_substitution_refuses_before_native_spawn(tmp_path, replacement):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic-login')
    record = codex_auth.capture(auth)
    lock = tmp_path/'.maestro-auth.lock'
    with codex_auth.Lifetime(record, deadline=time.monotonic()+HANG_SECONDS) as lifetime:
        lock.rename(tmp_path/'retained-lock')
        if replacement == 'symlink':
            lock.symlink_to(auth)
        elif replacement == 'hardlink':
            os.link(tmp_path/'retained-lock', lock)
        else:
            lock.touch(mode=0o600)
        with pytest.raises(ValueError, match='lock identity changed'):
            lifetime.verify()
