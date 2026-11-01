"""A provisional native result never disables the legacy controller ceiling."""
import select
import subprocess
import sys
import time

import pytest

from maestro import orchestrator
from maestro.backends import git_staging
from tests.hang_bound import HANG_SECONDS
from tests.test_native_git_crash import namespaced
from tests.test_orchestrator_switch_hooks import hooks, loop_env

real_reconcile_in_flight = orchestrator.reconcile_in_flight


@pytest.fixture
def pending_worker(namespaced):
    """A real live transaction owner; no native agent or inference is launched."""
    repo, workspace, start, boundary = namespaced
    program = '''
import sys
from pathlib import Path
from maestro.backends import git_staging
transaction = git_staging.Transaction.prepare(Path(sys.argv[1]), Path(sys.argv[2]))
transaction.start()
print('running', flush=True)
if sys.stdin.readline().strip() == 'promote':
    transaction.finish(success=True)
    print('promoted', flush=True)
'''
    proc = subprocess.Popen([sys.executable, '-c', program, str(boundary.worktree), str(workspace)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # The bound only stops a hang: under the parallel full suite a fresh
        # interpreter plus git can take far longer than any short literal.
        assert select.select([proc.stdout], [], [], HANG_SECONDS)[0], 'transaction owner did not start'
        assert proc.stdout.readline() == b'running\n'
        yield proc, workspace, boundary
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=HANG_SECONDS)


@pytest.mark.parametrize('expired', [False, True])
def test_worker_failed_does_not_replace_controller_stop_or_publication_gate(
    pending_worker, monkeypatch, expired,
):
    proc, workspace, boundary = pending_worker
    (workspace / 'DONE').write_text('provisional')
    (workspace / 'FAILED').write_text('worker-authored failure before teardown')
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: proc.poll() is None)
    killed = []
    def stop(window):
        killed.append(window)
        proc.kill()
        proc.wait(timeout=HANG_SECONDS)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window', stop)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **k: None)
    launch = time.time() - (orchestrator.TASK_TIMEOUT + 1 if expired else 1)
    clocks = {}
    state = dict(in_flight=[entry], launch_times={workspace.name: launch})
    assert orchestrator.reconcile_in_flight(state, clocks) == [entry]
    assert clocks[workspace.name] == launch
    if expired:
        assert killed == ['owned-fixture'], 'worker FAILED suppressed the controller stop'
        assert proc.poll() is not None
        assert (workspace / 'FAILED').read_text().startswith('timeout after ')
        assert not (workspace / 'DONE').exists()
        assert orchestrator.reconcile_in_flight(state, clocks) == [entry]
        assert killed == ['owned-fixture'], 'a stopped worker must not be killed twice'
    else:
        assert not orchestrator._native_git_reports_ready(workspace, running=True)
        assert (workspace / 'DONE').read_text() == 'provisional'
        assert killed == []
        assert proc.poll() is None
        assert boundary.worktree.exists()


def test_timeout_attempt_does_not_authorize_settlement_while_owner_is_alive(
    pending_worker, monkeypatch,
):
    proc, workspace, boundary = pending_worker
    (workspace / 'DONE').write_text('provisional')
    (workspace / 'FAILED').write_text('worker failure')
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    # Even a disappeared window is not proof its detached worker has stopped.
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: False)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window', lambda _: None)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **k: None)
    clocks = {workspace.name: time.time() - orchestrator.TASK_TIMEOUT - 1}
    assert orchestrator.reconcile_in_flight({'in_flight': [entry]}, clocks) == [entry]
    assert not orchestrator._native_git_reports_ready(workspace, running=False)
    assert proc.poll() is None and boundary.worktree.exists()
    proc.kill()
    proc.wait(timeout=HANG_SECONDS)
    assert orchestrator._native_git_reports_ready(workspace, running=False)
    assert not (workspace / 'DONE').exists()


@pytest.mark.parametrize('phase', ['before', 'expired', 'stopping'])
def test_main_waits_for_native_stop_before_failure_settlement(
    pending_worker, loop_env, monkeypatch, phase,
):
    proc, workspace, boundary = pending_worker
    (workspace / 'DONE').write_text('provisional')
    (workspace / 'FAILED').write_text('worker failure before cleanup')
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture',
                 worktree=str(boundary.worktree), backend='codex')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: proc.poll() is None)
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)
    loop_env.in_flight = [entry]
    def startup(state, clocks):
        clocks[workspace.name] = 999 if phase == 'before' else 999 - orchestrator.TASK_TIMEOUT
        return [entry]
    monkeypatch.setattr(orchestrator, '_reconcile_and_persist_startup_state', startup)
    monkeypatch.setattr(orchestrator, '_context_rotations', lambda *a: 0)
    monkeypatch.setattr(orchestrator, '_self_update_maybe', lambda: False)
    monkeypatch.setattr(orchestrator.metabranch, 'reconcile_meta', lambda *a: 'fixture')
    stopped, retried, parked = [], [], []
    def stop(window):
        stopped.append(window)
        if phase == 'expired':
            proc.kill()
            proc.wait(timeout=HANG_SECONDS)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window', stop)
    monkeypatch.setattr(orchestrator, '_do_retry', lambda *a: retried.append(a))
    monkeypatch.setattr(orchestrator, 'park_failed', lambda *a: parked.append(a))
    monkeypatch.setattr(orchestrator, '_remove_from_state', lambda *a: None)
    assert orchestrator.main() == 0
    assert retried == [], 'a provisional failure must never recreate the active task tree'
    assert boundary.worktree.exists()
    if phase == 'expired':
        assert stopped == ['owned-fixture']
        assert proc.poll() is not None
        assert len(parked) == 1 and parked[0][1].startswith('timeout after ')
    else:
        assert parked == []
        assert proc.poll() is None
        if phase == 'stopping':
            assert stopped, 'expiry must attempt a stop even when worker FAILED exists'


@pytest.mark.parametrize('provisional_done', [False, True])
def test_reconcile_expires_pending_native_publication_without_adopting_result(
    pending_worker, monkeypatch, provisional_done,
):
    proc, workspace, boundary = pending_worker
    if provisional_done:
        (workspace / 'DONE').write_text('provisional')
    (workspace / 'result.json').write_text('{"status":"done"}')
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: proc.poll() is None)
    killed, events = [], []
    def stop(window):
        killed.append(window)
        proc.kill()
        proc.wait(timeout=HANG_SECONDS)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window', stop)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **k: events.append(a))
    monkeypatch.setattr(orchestrator, '_reconcile_zombie_polls', {})
    # Preserve the original persisted launch epoch on controller restart.
    launch = time.time() - orchestrator.TASK_TIMEOUT - 1
    clocks = {}
    state = dict(in_flight=[entry], launch_times={workspace.name: launch})
    assert orchestrator.reconcile_in_flight(state, clocks) == [entry]
    assert clocks[workspace.name] == launch
    assert killed == ['owned-fixture']
    assert proc.poll() is not None
    assert not (workspace / 'DONE').exists()
    reason = f'timeout after {orchestrator.TASK_TIMEOUT // 3600}h'
    assert (workspace / 'FAILED').read_text() == reason
    assert orchestrator._native_git_reports_ready(workspace, running=False)
    assert (workspace / 'FAILED').read_text() == reason
    assert orchestrator.reconcile_in_flight(state, clocks) == [entry]
    assert killed == ['owned-fixture'], 'ceiling enforcement must stay idempotent'
    assert not any(event[0] == 'reconcile_synth_sentinel' for event in events)
    assert git_staging.publication_state(workspace) == 'pending'


def test_pending_native_publication_before_ceiling_keeps_provisional_result(namespaced, monkeypatch):
    repo, workspace, start, boundary = namespaced
    transaction = git_staging.Transaction.prepare(boundary.worktree, workspace)
    transaction.start()
    (workspace / 'DONE').write_text('provisional')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: True)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window', lambda _: pytest.fail('early kill'))
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture')
    try:
        clocks = {workspace.name: time.time()}
        assert orchestrator.reconcile_in_flight({'in_flight':[entry]}, clocks) == [entry]
        assert (workspace / 'DONE').read_text() == 'provisional'
        assert not (workspace / 'FAILED').exists()
        assert not orchestrator._native_git_reports_ready(workspace, running=True)
    finally:
        transaction.cleanup()


@pytest.mark.parametrize('alias', ['symlink', 'hardlink'])
def test_timeout_never_overwrites_worker_failed_alias(pending_worker, loop_env, monkeypatch, tmp_path, alias):
    import os
    proc, workspace, boundary = pending_worker
    outside = tmp_path / 'outside-grant.txt'
    outside.write_text('preserve host bytes')
    if alias == 'symlink':
        (workspace / 'FAILED').symlink_to(outside)
    else:
        os.link(outside, workspace / 'FAILED')
    (workspace / 'DONE').write_text('provisional')
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    monkeypatch.setattr(orchestrator, 'reconcile_in_flight', real_reconcile_in_flight)
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: proc.poll() is None)
    stopped = []
    def stop(window):
        stopped.append(window)
        proc.kill()
        proc.wait(timeout=HANG_SECONDS)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window', stop)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **k: None)
    clocks = {workspace.name: orchestrator.time.time() - orchestrator.TASK_TIMEOUT - 1}
    assert orchestrator.reconcile_in_flight({'in_flight': [entry]}, clocks) == [entry]
    assert outside.read_text() == 'preserve host bytes'
    assert stopped == ['owned-fixture']
    assert proc.poll() is not None
    assert not (workspace / 'DONE').exists()
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)
    loop_env.in_flight = [entry]
    entry.update(worktree=str(boundary.worktree), backend='codex')
    def startup(state, launches):
        launches.update(clocks)
        return [entry]
    monkeypatch.setattr(orchestrator, '_reconcile_and_persist_startup_state', startup)
    monkeypatch.setattr(orchestrator, '_context_rotations', lambda *a: 0)
    monkeypatch.setattr(orchestrator, '_self_update_maybe', lambda: False)
    monkeypatch.setattr(orchestrator.metabranch, 'reconcile_meta', lambda *a: 'fixture')
    parked = []
    monkeypatch.setattr(orchestrator, '_do_retry', lambda *a: pytest.fail('retried alias report'))
    monkeypatch.setattr(orchestrator, 'park_failed', lambda *a: parked.append(a))
    monkeypatch.setattr(orchestrator, '_remove_from_state', lambda *a: None)
    assert orchestrator.main() == 0
    assert len(parked) == 1 and parked[0][1].startswith('timeout after ')
    assert outside.read_text() == 'preserve host bytes'


@pytest.mark.parametrize('window_present,timing', [(True, 'later'), (False, 'later'), (True, 'during-stop')])
def test_late_promotion_after_timeout_still_parks_on_controller_restart(
    pending_worker, loop_env, monkeypatch, window_present, timing,
):
    import json
    proc, workspace, boundary = pending_worker
    (workspace / 'DONE').write_text('provisional')
    (workspace / 'result.json').write_text('{"status":"done"}')
    entry = dict(task_id='T-001', session_id=workspace.name, window='owned-fixture',
                 worktree=str(boundary.worktree), backend='codex')
    monkeypatch.setattr(orchestrator, 'WORKSPACES', workspace.parent)
    monkeypatch.setattr(orchestrator, 'reconcile_in_flight', real_reconcile_in_flight)
    monkeypatch.setattr(orchestrator, 'tmux_window_exists', lambda _: window_present and proc.poll() is None)
    def promote():
        proc.stdin.write(b'promote\n')
        proc.stdin.flush()
        assert select.select([proc.stdout], [], [], HANG_SECONDS)[0]
        assert proc.stdout.readline() == b'promoted\n'
        proc.wait(timeout=HANG_SECONDS)
    monkeypatch.setattr(orchestrator, '_kill_tmux_window',
        lambda _: promote() if timing == 'during-stop' else None)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **k: None)
    clocks = {workspace.name: orchestrator.time.time() - orchestrator.TASK_TIMEOUT - 1}
    state = dict(in_flight=[entry], launch_times=clocks)
    assert orchestrator.reconcile_in_flight(state, {}) == [entry]
    if timing == 'later':
        assert proc.poll() is None
        promote()
    assert git_staging.publication_state(workspace) == 'promoted'
    # Workers can rewrite report bytes. A fresh controller/entry/clock must
    # retain the original timeout outcome independently of these sentinels.
    (workspace / 'FAILED').unlink()
    (workspace / 'DONE').write_text('late success')
    restarted = json.loads(json.dumps(state))
    for _ in range(2):
        fresh_clocks = {}
        assert orchestrator.reconcile_in_flight(restarted, fresh_clocks) == [restarted['in_flight'][0]]
        assert orchestrator._timeout_expired(restarted['in_flight'][0], fresh_clocks)
        assert not (workspace / 'DONE').exists()
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)
    loop_env.in_flight = restarted['in_flight']
    monkeypatch.setattr(orchestrator, '_reconcile_and_persist_startup_state',
        lambda state, clocks: orchestrator.reconcile_in_flight(restarted, clocks))
    monkeypatch.setattr(orchestrator, '_context_rotations', lambda *a: 0)
    monkeypatch.setattr(orchestrator, '_self_update_maybe', lambda: False)
    monkeypatch.setattr(orchestrator.metabranch, 'reconcile_meta', lambda *a: 'fixture')
    retried, parked = [], []
    monkeypatch.setattr(orchestrator, '_do_retry', lambda *a: retried.append(a))
    monkeypatch.setattr(orchestrator, 'park_failed', lambda *a: parked.append(a))
    monkeypatch.setattr(orchestrator, '_remove_from_state', lambda *a: None)
    monkeypatch.setattr(orchestrator, 'merge_and_eval', lambda *a, **k: pytest.fail('adopted late success'))
    assert orchestrator.main() == 0
    assert retried == []
    assert len(parked) == 1 and parked[0][1].startswith('timeout after ')
