"""An actual isolated process timeout must retain evidence and abort publication."""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from maestro.backends import agy_worker, antigravity, control_files
from maestro.backends.antigravity import AntigravityBackend
from maestro.backends.base import Completion, CompletionSpec
from tests.test_git_staging import git, linked


def test_agy_timeout_keeps_partial_bytes_and_settles_real_git_owner(linked, tmp_path, monkeypatch):
    repo, tree, reports, _ = linked
    home = tmp_path/'home'
    (home/'.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home/'.gemini/config').mkdir()
    start = git(repo, 'rev-parse', 'impl/T-001').stdout.strip()
    script = (
        'import os,time\nfrom pathlib import Path\n'
        f'Path({str(reports / "DONE")!r}).write_text("provisional")\n'
        'os.write(1,b"partial stdout\\xe2")\n'
        'os.write(2,b"partial stderr\\xff")\n'
        'time.sleep(60)\n'
    )
    # Test timeout capture after real output, independently of host startup speed.
    # Both production deadlines use the same controlled clock; the two-second
    # budget expires only after the real worker observes both expected streams.
    clock = [100.0]
    launched = [None]
    received = {'stdout': bytearray(), 'stderr': bytearray()}
    real_monotonic = time.monotonic
    def monotonic():
        if launched[0] is not None:
            assert real_monotonic() - launched[0] < 20, 'isolated child did not produce its partial output'
        return clock[0]
    controlled_time = SimpleNamespace(monotonic=monotonic)
    monkeypatch.setattr(antigravity, 'time', controlled_time)
    monkeypatch.setattr(agy_worker, 'time', controlled_time)
    def observe(name, chunk):
        received[name].extend(chunk)
        if received == {'stdout': b'partial stdout\xe2', 'stderr': b'partial stderr\xff'}:
            clock[0] = 103.0
    def synthetic_native(argv, **kwargs):
        # Only inference is replaced. Keep the real bwrap namespace, mount
        # identities, bounded runner, Git journal, and parent cleanup.
        index = argv.index('agy')
        command = control_files.MountArgv(
            [*argv[:index], sys.executable, '-I', '-c', script], argv.identities)
        launched[0] = real_monotonic()
        return agy_worker.run(command, observe=observe, **kwargs)
    result = AntigravityBackend(gemini_home=home/'.gemini', runner=synthetic_native).complete(
        CompletionSpec(prompt='fixture', cwd=tree, writable=True, add_dirs=(reports,),
                       timeout=2, log_file=reports/'impl.log'))
    assert result == Completion(text='', returncode=124, timed_out=True)
    assert received == {'stdout': b'partial stdout\xe2', 'stderr': b'partial stderr\xff'}
    assert (reports/'impl.log').read_bytes() == (
        b'partial stdout\xe2partial stderr\xff\n(timed out after 2s)')
    assert (reports/'DONE').read_text() == 'provisional'
    pointer = json.loads((reports/'.native-git/current.json').read_text())
    journal = json.loads(control_files.read(Path(pointer['journal'])))
    assert journal['state'] == 'aborted'
    assert json.loads((reports/'.native-owner/active.json').read_text())['state'] == 'released'
    assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == start
    assert git(tree, 'status', '--porcelain').stdout == ''


def test_agy_preparation_consumes_total_deadline_before_native_spawn(linked, tmp_path, monkeypatch):
    from maestro.backends import agy_runtime

    repo, tree, reports, _ = linked
    home = tmp_path/'home'
    (home/'.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home/'.gemini/config').mkdir()
    start = git(repo, 'rev-parse', 'impl/T-001').stdout.strip()
    clock = [100.0]
    monkeypatch.setattr(antigravity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    prepare = agy_runtime.prepare
    def slow_prepare(**kwargs):
        run = prepare(**kwargs)
        clock[0] += 3
        return run
    monkeypatch.setattr(agy_runtime, 'prepare', slow_prepare)
    spawned = []
    def unexpected_native(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError('preparation exhausted the deadline before native spawn')
    result = AntigravityBackend(gemini_home=home/'.gemini', runner=unexpected_native).complete(
        CompletionSpec(prompt='fixture', cwd=tree, writable=True, add_dirs=(reports,),
                       timeout=2, log_file=reports/'impl.log'))
    assert result == Completion(text='', returncode=124, timed_out=True)
    assert not spawned
    assert (reports/'impl.log').read_bytes() == b'(timed out after 2s)'
    assert not (reports/'DONE').exists()
    pointer = json.loads((reports/'.native-git/current.json').read_text())
    journal = json.loads(control_files.read(Path(pointer['journal'])))
    assert journal['state'] == 'aborted'
    assert json.loads((reports/'.native-owner/active.json').read_text())['state'] == 'released'
    assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == start
    assert git(tree, 'status', '--porcelain').stdout == ''


def test_agy_deadline_expiring_in_audit_aborts_before_task_ref_promotion(linked, tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from maestro.backends import antigravity, git_staging

    repo, tree, reports, _ = linked
    home = tmp_path/'home'
    (home/'.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home/'.gemini/config').mkdir()
    start = git(repo, 'rev-parse', 'impl/T-001').stdout.strip()
    clock = [100.0]
    monkeypatch.setattr(antigravity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    audit = git_staging.Transaction._audit
    def slow_audit(self):
        result = audit(self)
        clock[0] += 11
        return result
    monkeypatch.setattr(git_staging.Transaction, '_audit', slow_audit)
    document = json.dumps({'status': 'SUCCESS', 'response': 'synthetic no-op'})
    def synthetic_native(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, document, '')
    result = AntigravityBackend(gemini_home=home/'.gemini', runner=synthetic_native).complete(
        CompletionSpec(prompt='fixture', cwd=tree, writable=True, timeout=10,
                       log_file=reports/'impl.log'))
    assert result == Completion(text='', returncode=124, timed_out=True)
    assert (reports/'impl.log').read_bytes() == document.encode() + b'\n(timed out after 10s)'
    pointer = json.loads((reports/'.native-git/current.json').read_text())
    assert json.loads(control_files.read(Path(pointer['journal'])))['state'] == 'aborted'
    assert git(repo, 'rev-parse', 'impl/T-001').stdout.strip() == start
    assert json.loads((reports/'.native-owner/active.json').read_text())['state'] == 'released'
