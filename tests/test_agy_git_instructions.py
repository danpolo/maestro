"""The real frozen Git profile must explain its staging boundary at every call."""
import json
import subprocess

import pytest

from maestro.backends import agy_runtime
from maestro.backends.antigravity import AntigravityBackend
from maestro.backends.base import CompletionSpec, Handle, LaunchSpec
from tests.test_git_staging import linked


@pytest.mark.parametrize('boundary', ['launch', 'resume', 'complete'])
def test_frozen_git_profile_dispatches_exact_task_staging_instructions(linked, tmp_path, monkeypatch, boundary):
    _, tree, reports, _ = linked
    home = tmp_path / 'home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    prompt = 'Change file.txt and commit it: git add -A && git commit -m task.'
    calls, prepared = [], []
    prepare = agy_runtime.prepare
    def capture_prepare(**kwargs):
        run = prepare(**kwargs)
        prepared.append(run)
        launcher = run.launcher
        def capture_launcher(argv, **options):
            calls.append(argv)
            return launcher(argv, **options)
        monkeypatch.setattr(type(run), 'launcher', lambda self, *a, **k: capture_launcher(*a, **k))
        return run
    monkeypatch.setattr(agy_runtime, 'prepare', capture_prepare)
    def synthetic_native(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            'status': 'SUCCESS', 'response': 'synthetic no-op; no inference'}), '')
    backend = AntigravityBackend(gemini_home=home / '.gemini', runner=synthetic_native)
    monkeypatch.setattr(backend, '_open_window', lambda *a: None)
    try:
        if boundary == 'complete':
            backend.complete(CompletionSpec(prompt=prompt, cwd=tree, writable=True,
                log_file=reports/'impl.log'))
        else:
            spec = LaunchSpec(task_id='T-001', session_id='impl-T-001-1',
                model='gemini-3.8-flash-medium', brief=prompt, worktree=tree, workspace=reports)
            if boundary == 'launch':
                backend.launch(spec)
            else:
                handle = Handle(backend='antigravity', session_id=spec.session_id,
                    native_id='synthetic-cid', workspace=reports, window='agents:impl-T-001')
                backend.resume(handle, prompt, spec=spec)
        assert len(calls) == len(prepared) == 1
        dispatched = calls[0][-1]
        assert calls[0][-2] == '-p' and prompt in dispatched
        assert 'git add -- <task paths>' in dispatched
        assert 'frozen customization' in dispatched
        assert 'Direct AGENTS.md and GEMINI.md are LIVE' in dispatched
        assert 'native directory scope and normal task edit permissions' in dispatched
        assert 'replaces any earlier git add -A' in dispatched
        assert 'unqualified' in dispatched
    finally:
        for run in prepared:
            run.cleanup()
