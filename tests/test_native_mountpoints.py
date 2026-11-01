"""Overlapping native invocations must keep each other's discovery mounts sealed."""
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.test_agy_runtime import installation
from tests.test_codex_app_server import server
from tests.hang_bound import HANG_SECONDS


@pytest.mark.parametrize('backend', ['agy', 'codex'])
@pytest.mark.parametrize('when', ['after-wrap', 'at-exec'])
@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_native_launch_never_uses_substituted_mount_source(
    installation, server, monkeypatch, backend, when, replacement,
):
    from maestro.backends import agy_runtime, codex_runtime, antigravity
    from maestro.backends.base import CompletionSpec
    from maestro.backends.codex import CodexBackend
    home, tree, workspace = installation
    runtime = agy_runtime if backend == 'agy' else codex_runtime
    native = home / '.gemini/antigravity-cli' if backend == 'agy' else home / '.codex'
    if backend == 'codex':
        native.mkdir()
        (native / 'auth.json').write_text('synthetic')
    program = ('from pathlib import Path\n'
        f'native=Path({str(native)!r})\n'
        '(native/"executed").write_text((native/"view").read_text())\n')
    if backend == 'codex':
        server.write_text(f'#!{sys.executable}\n' + program + server.read_text())
        server.chmod(0o755)
        driver = CodexBackend(binary=str(server), codex_home=native)
    else:
        server.write_text(f'#!{sys.executable}\n' + program +
            'print(\'{"status":"SUCCESS","response":"approved"}\')\n')
        server.chmod(0o755)
        monkeypatch.setattr(antigravity, 'BINARY', str(server))
        driver = antigravity.AntigravityBackend(gemini_home=home / '.gemini')
    hostile = tree.parent / 'hostile'
    hostile.mkdir()
    (hostile / 'view').write_text('substituted')
    original_prepare, original_wrap, original_popen = runtime.prepare, runtime.PreparedRun.wrap, subprocess.Popen
    runs, swapped = [], []
    def prepare(**kwargs):
        if backend == 'codex':
            kwargs['host_home'] = home
        run = original_prepare(**kwargs)
        (run.state / 'view').write_text('approved')
        runs.append(run)
        return run
    def swap():
        if not swapped:
            source = runs[0].state
            source.rename(source.with_name('saved-state'))
            if replacement == 'symlink':
                source.symlink_to(hostile, target_is_directory=True)
            else:
                hostile.rename(source)
            swapped.append(source if replacement == 'directory' else hostile)
    def wrap(run, argv):
        result = original_wrap(run, argv)
        if when == 'after-wrap':
            swap()
        return result
    def popen(*args, **kwargs):
        if when == 'at-exec':
            swap()
        return original_popen(*args, **kwargs)
    monkeypatch.setattr(runtime, 'prepare', prepare)
    monkeypatch.setattr(runtime.PreparedRun, 'wrap', wrap)
    if backend == 'codex':
        driver._popen = popen
    else:
        monkeypatch.setattr(subprocess, 'Popen', popen)
    result = driver.complete(CompletionSpec(prompt='test', model='gpt-6-luna', cwd=tree,
                                           log_file=workspace / 'impl.log',
                                           timeout=HANG_SECONDS))
    assert swapped
    evidence = swapped[0] / 'executed'
    assert not evidence.exists(), 'native executed using the substituted source mount'
    if when == 'at-exec' and replacement == 'directory':
        assert result.returncode == 0, 'the pinned original must still support actual native execution'


def test_mount_descriptors_are_consumed_without_leaking_to_native_process(installation):
    import json
    from maestro.backends import agy_runtime, agy_worker
    home, tree, workspace = installation
    run = agy_runtime.prepare(cwd=tree, workspace=workspace, home=home, write_roots=(tree,))
    credential = home / '.gemini/antigravity-cli/antigravity-oauth-token'
    info = credential.stat()
    program = ('import os,json\n'
        'leaked=[]\n'
        'for name in os.listdir("/proc/self/fd"):\n'
        ' try: info=os.fstat(int(name))\n'
        ' except OSError: continue\n'
        f' if (info.st_dev,info.st_ino)=={(info.st_dev, info.st_ino)!r}: leaked.append(name)\n'
        'print(json.dumps(leaked))\n')
    try:
        result = agy_worker.run(run.wrap([sys.executable, '-I', '-c', program]),
                                cwd=str(tree), env=run.environment(), capture_output=True,
                                timeout=HANG_SECONDS)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []
    finally:
        run.cleanup()


@pytest.mark.parametrize('backend', ['agy', 'codex'])
def test_finishing_one_invocation_cannot_detach_another_discovery_mount(installation, backend):
    home, tree, workspace = installation
    if backend == 'agy':
        from maestro.backends.agy_runtime import prepare
        def invocation(report):
            return prepare(cwd=tree, workspace=report, write_roots=(tree,), home=home)
    else:
        from maestro.backends.codex_runtime import prepare
        native = home / '.codex'
        native.mkdir()
        (native / 'auth.json').write_text('synthetic')
        def invocation(report):
            return prepare(cwd=tree, workspace=report, write_roots=(tree,),
                native_home=native, host_home=home, model='gpt-6-luna', effort='low')
    # Legitimate independent reports still share the nongit tree's discovery
    # paths; same-workspace overlap is refused by native_ownership separately.
    first, second = invocation(workspace), invocation(workspace.parent / 'second-report')
    ready, release = tree / 'ready', tree / 'release'
    program = ('from pathlib import Path\nimport time\n'
        f'root=Path({str(tree)!r})\n'
        '(root/"ready").touch()\n'
        'while not (root/"release").exists(): time.sleep(.01)\n'
        'try:\n'
        ' (root/".agents").mkdir(exist_ok=True)\n'
        ' (root/".agents/hooks.json").write_text("disabled fixture")\n'
        'except OSError: pass\n'
        'else: raise SystemExit("discovery mount detached by other invocation cleanup")\n')
    child = subprocess.Popen(second.wrap([sys.executable, '-I', '-c', program]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + HANG_SECONDS
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'native child did not start'
        first.cleanup()
    finally:
        release.touch()
        stdout, stderr = child.communicate(timeout=HANG_SECONDS)
        second.cleanup()
    assert child.returncode == 0, stdout + stderr
    assert not (tree / '.agents/hooks.json').exists()
