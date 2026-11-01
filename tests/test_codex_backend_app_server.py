"""Real driver/runtime/protocol boundaries; native model execution alone is fake."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import subprocess

import pytest

from maestro.backends.base import CompletionSpec, LaunchSpec
from maestro.backends.codex import CodexBackend
from maestro.backends.codex_launch import CodexLaunchBackend
from tests.test_codex_app_server import server
from tests.hang_bound import HANG_SECONDS


@pytest.fixture
def configured_driver(server, tmp_path, monkeypatch):
    server.write_text(f'#!{sys.executable}\nimport sys\n'
        'print("synthetic native diagnostic", file=sys.stderr)\n' + server.read_text())
    server.chmod(0o755)
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'auth.json').write_text('synthetic')
    (native / 'config.toml').write_text('invalid normal TOML [')
    permissions = tmp_path / 'permissions.json'
    permissions.write_text(json.dumps({'version': 1, 'allow': ['Bash(echo *)']}))
    monkeypatch.setenv('MAESTRO_PERMISSIONS_FILE', str(permissions))
    from maestro.backends import codex_runtime
    original_prepare = codex_runtime.prepare
    def prepare(**kwargs):
        kwargs['host_home'] = tmp_path
        return original_prepare(**kwargs)
    monkeypatch.setattr(codex_runtime, 'prepare', prepare)
    return CodexBackend(binary=str(server), codex_home=native)


@pytest.mark.parametrize('driver_type', [CodexBackend, CodexLaunchBackend])
@pytest.mark.parametrize('effort_value', ['high', '"high"'])
def test_completion_uses_private_app_server_and_selected_effort(configured_driver,
                                                driver_type, effort_value, tmp_path):
    driver = driver_type(binary=configured_driver.binary_path(),
                         codex_home=configured_driver.codex_home())
    tree, report = tmp_path / 'tree', tmp_path / 'report'
    tree.mkdir()
    report.mkdir()
    result = driver.complete(CompletionSpec(prompt='answer', model='gpt-6-luna',
        cwd=tree, log_file=report / 'impl.log', effort_config=(('model_reasoning_effort', effort_value),)))
    assert result.text == 'accept'
    assert result.returncode == 0
    events = [json.loads(line) for line in (report / 'impl.log').read_text().splitlines()]
    assert any(event.get('type') == 'maestro.approval' for event in events)
    assert any(event.get('type') == 'maestro.native_stderr'
               and 'synthetic native diagnostic' in event['message'] for event in events)
    assert (report / 'codex_thread_id.txt').read_text() == 'thread'
    configs = list((report / '.codex-runtime').glob('call-*/config.toml'))
    assert len(configs) == 1
    assert 'model_reasoning_effort = "high"' in configs[0].read_text()
    assert not list(tree.iterdir())


def test_completion_refuses_unverified_launch_overrides(configured_driver, tmp_path):
    log = tmp_path / 'report' / 'impl.log'
    result = configured_driver.complete(CompletionSpec(prompt='q', model='gpt-6-luna',
        cwd=tmp_path, log_file=log, effort_config=(('approval_policy', '"never"'),)))
    assert result.text == '' and result.returncode != 0
    assert 'unsupported Codex launch controls' in log.read_text()


@pytest.mark.parametrize('operation', ['launch', 'resume'])
@pytest.mark.parametrize('failure', ['launcher', 'window'])
def test_failed_launch_retains_empty_mountpoints_and_preserves_existing_content(
    configured_driver, tmp_path, monkeypatch, operation, failure,
):
    from maestro.backends.base import Handle
    from maestro.backends.codex_runtime import PreparedRun
    tree, report = tmp_path / 'tree', tmp_path / 'report'
    tree.mkdir()
    report.mkdir()
    existing = tree / '.agent'
    existing.mkdir()
    (existing / 'keep').write_text('existing customization')
    def fail(*args, **kwargs):
        raise OSError('injected launch failure')
    monkeypatch.setattr(PreparedRun, 'launcher', fail) if failure == 'launcher' else (
        monkeypatch.setattr(configured_driver, '_new_window', fail))
    spec = LaunchSpec('T1', 's1', 'gpt-6-luna', 'go', report, tree)
    with pytest.raises(OSError, match='injected launch failure'):
        if operation == 'launch':
            configured_driver.launch(spec)
        else:
            configured_driver.resume(Handle('codex', 's1', 'thread', report, ''), 'again', spec=spec)
    for root in (tree, report):
        for alias in ('.codex', '.agents', '_agent', '_agents', '.claude'):
            assert (root / alias).is_dir() and not list((root / alias).iterdir())
    assert (existing / 'keep').read_text() == 'existing customization'


@pytest.mark.parametrize('driver_type', [CodexBackend, CodexLaunchBackend])
def test_launch_and_restart_resume_use_app_server_and_retained_effort(
    configured_driver, driver_type, tmp_path, monkeypatch,
):
    driver = driver_type(binary=configured_driver.binary_path(),
        codex_home=configured_driver.codex_home())
    monkeypatch.setattr(driver, '_new_window', lambda *args: None)
    tree, report = tmp_path / 'tree', tmp_path / 'report'
    tree.mkdir()
    report.mkdir()
    spec = LaunchSpec('T1', 's1', 'gpt-6-luna', 'go', report, tree,
                      effort_config=(('model_reasoning_effort', '"high"'),))
    handle = driver.launch(spec)
    first = subprocess.run([sys.executable, '-I', str(report / 'launch.py')],
                           cwd=tree, capture_output=True, text=True, timeout=HANG_SECONDS)
    assert first.returncode == 0, first.stderr
    assert driver.thread_id_for(report) == 'thread'
    restarted = driver_type(binary=driver.binary_path(), codex_home=driver.codex_home())
    monkeypatch.setattr(restarted, '_new_window', lambda *args: None)
    restarted.resume(handle, 'continue')
    second = subprocess.run([sys.executable, '-I', str(report / 'launch.py')],
                            cwd=tree, capture_output=True, text=True, timeout=HANG_SECONDS)
    assert second.returncode == 0, second.stderr
    events = [json.loads(line) for line in (report / 'impl.log').read_text().splitlines()]
    assert sum(event.get('type') == 'maestro.approval' for event in events) == 2
    assert driver.thread_id_for(report) == 'thread'
    configs = list((report / '.codex-runtime').glob('call-*/config.toml'))
    assert len(configs) == 2
    assert all('model_reasoning_effort = "high"' in p.read_text() for p in configs)


@pytest.mark.parametrize('operation', ['launch', 'resume', 'complete'])
@pytest.mark.parametrize('alias', ['symlink', 'hardlink', 'ancestor', 'directory'])
def test_parent_controls_validated_before_any_write(configured_driver, tmp_path,
                                                    monkeypatch, operation, alias):
    from maestro.backends.base import Handle
    driver = configured_driver
    windows = []
    monkeypatch.setattr(driver, '_new_window', lambda *args: windows.append(args))
    tree, report = tmp_path / 'tree', tmp_path / 'report'
    tree.mkdir()
    report.mkdir()
    victim = tmp_path / 'outside'
    victim.write_text('must survive')
    name = 'custom.log' if operation == 'complete' else 'brief.txt'
    target = report / name
    if alias == 'symlink':
        target.symlink_to(victim)
    elif alias == 'hardlink':
        target.hardlink_to(victim)
    elif alias == 'directory':
        target.mkdir()
    else:
        alias_root = tmp_path / 'alias-root'
        alias_root.symlink_to(report, target_is_directory=True)
        report = alias_root / 'new-report'
    spec = LaunchSpec('T1', 's1', 'gpt-6-luna', 'go', report, tree)
    if operation == 'complete':
        result = driver.complete(CompletionSpec(prompt='q', model=spec.model, cwd=tree,
            log_file=report / name))
        assert result.returncode != 0 and result.text == ''
    else:
        with pytest.raises((ValueError, OSError)):
            if operation == 'launch':
                driver.launch(spec)
            else:
                driver.resume(Handle('codex', 's1', 'thread', report, 'impl-T1'),
                              'continue', spec=spec)
    assert victim.read_text() == 'must survive'
    assert not (report / '.codex-runtime').exists()
    assert not (report / 'codex_launch.json').exists()
    assert windows == []
    if alias == 'ancestor':
        assert not report.exists()


def test_completion_directory_creation_failure_is_a_completion(configured_driver, tmp_path):
    parent = tmp_path / 'not-a-directory'
    parent.write_text('keep')
    result = configured_driver.complete(CompletionSpec(prompt='q', model='gpt-6-luna',
        cwd=tmp_path, log_file=parent / 'impl.log'))
    assert result.returncode != 0 and result.text == ''
    assert parent.read_text() == 'keep'


def test_fresh_launch_never_recovers_prior_thread_before_worker_start(
    configured_driver, tmp_path, monkeypatch,
):
    driver = configured_driver
    monkeypatch.setattr(driver, '_new_window', lambda *args: None)
    tree, report = tmp_path / 'tree', tmp_path / 'report'
    tree.mkdir()
    report.mkdir()
    log = report / 'impl.log'
    log.write_text('{"type":"thread.started","thread_id":"prior-conversation"}\n')
    (report / 'codex_thread_id.txt').write_text('prior-conversation')
    spec = LaunchSpec('T1', 'fresh-session', 'gpt-6-luna', 'fresh brief', report, tree)
    driver.launch(spec)
    restarted = CodexBackend(binary=driver.binary_path(), codex_home=driver.codex_home())
    assert driver.thread_id_for(report) is None
    assert restarted.thread_id_for(report) is None
    restarted._new_window = lambda *args: None
    from tests.backends.test_codex_driver import _settle_mocked_worker
    _settle_mocked_worker(report)
    from maestro.backends.base import Handle
    restarted.resume(Handle('codex', 'fresh-session', None, report, 'impl-T1'), 'continue')
    from tests.backends.test_codex_driver import _job
    assert _job(report)['resume_id'] == ''


def test_parent_control_write_cannot_follow_an_ancestor_swapped_after_validation(tmp_path, monkeypatch):
    from maestro.backends import control_files
    import os
    report, outside = tmp_path / 'report', tmp_path / 'outside'
    report.mkdir()
    outside.mkdir()
    victim = outside / 'control.txt'
    victim.write_text('must survive')
    original_open = os.open
    swapped = []

    def race(path, flags, *args, **kwargs):
        if Path(path).name == 'control.txt' and not swapped:
            report.rename(tmp_path / 'original-report')
            report.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', race)
    try:
        control_files.write(report / 'control.txt', 'trusted control')
    except (ValueError, OSError):
        pass
    assert swapped
    assert victim.read_text() == 'must survive'


def rollout(root, thread_id, tokens):
    path = root / 'sessions' / '2026' / '10' / '16' / f'rollout-test-{thread_id}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'type': 'event_msg', 'payload': {
        'type': 'token_count', 'info': {'model_context_window': 100000,
            'last_token_usage': {'total_tokens': tokens, 'input_tokens': tokens},
            'total_token_usage': {'input_tokens': tokens, 'cached_input_tokens': 20,
                                  'output_tokens': 40}},
        'rate_limits': {'primary': {'used_percent': 12, 'window_minutes': 300}}}}) + '\n')
    return path


def test_restart_telemetry_uses_only_the_workspaces_private_state(configured_driver, tmp_path):
    from maestro.backends.base import Handle
    report = tmp_path / 'report'
    report.mkdir()
    (report / 'codex_thread_id.txt').write_text('thread')
    private = rollout(report / '.codex-runtime' / 'state', 'thread', 22000)
    rollout(configured_driver.codex_home(), 'thread', 80000)
    rollout(tmp_path / 'other-report' / '.codex-runtime' / 'state', 'thread', 60000)
    restarted = CodexBackend(binary=configured_driver.binary_path(),
        codex_home=configured_driver.codex_home())
    sample = restarted.usage(Handle('codex', 's1', None, report, 'impl-T1'))
    assert sample is not None
    assert sample.session_id == 's1' and sample.context_total_input_tokens == 22000
    assert sample.context_used_pct == pytest.approx(10 / 88 * 100)
    assert restarted.attempt_token_usage(report)['fresh_input'] == 21980
    assert private.exists()


@pytest.mark.parametrize('alias', ['missing', 'symlink-file', 'symlink-parent', 'hardlink'])
def test_private_telemetry_never_substitutes_shared_or_aliased_rollouts(
    configured_driver, tmp_path, alias,
):
    from maestro.backends.base import Handle
    report = tmp_path / 'report'
    report.mkdir()
    (report / 'codex_thread_id.txt').write_text('thread')
    state = report / '.codex-runtime' / 'state'
    state.mkdir(parents=True)
    shared = rollout(configured_driver.codex_home(), 'thread', 80000)
    if alias == 'symlink-parent':
        (state / 'sessions').symlink_to(configured_driver.sessions_dir(), target_is_directory=True)
    elif alias != 'missing':
        path = state / 'sessions' / '2026' / '10' / '16' / shared.name
        path.parent.mkdir(parents=True)
        if alias == 'symlink-file':
            path.symlink_to(shared)
        else:
            path.hardlink_to(shared)
    assert configured_driver.usage(Handle('codex', 's1', None, report, 'impl-T1')) is None
    assert configured_driver.attempt_token_usage(report) is None
