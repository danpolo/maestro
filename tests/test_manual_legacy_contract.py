"""Real legacy prep/park/finalize flow, with only external delivery/merge edges replaced."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    from maestro import parking, prep_actions

    repo = tmp_path/'repo'
    repo.mkdir()
    for args in (['init', '-q'], ['config', 'user.name', 'fixture'],
                 ['config', 'user.email', 'fixture@example.invalid'],
                 ['commit', '--allow-empty', '-qm', 'seed']):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    state = {'in_flight': [{'task_id': 'T', 'session_id': 'prep-T'}]}
    sent, graduated, gate_calls = [], [], []
    monkeypatch.setattr(parking, 'REPO', repo)
    monkeypatch.setattr(parking, 'WORKSPACES', repo/'.orchestrator/workspaces')
    monkeypatch.setattr(prep_actions, 'PREPARED_ACTIONS_FILE', repo/'.orchestrator/prepared_actions.json')
    monkeypatch.setattr(parking, 'read_state', lambda: json.loads(json.dumps(state)))
    monkeypatch.setattr(parking, 'write_state', lambda s: (state.clear(), state.update(s)))
    monkeypatch.setattr(parking, 'append_journal', lambda *a, **kw: None)
    monkeypatch.setattr(parking, 'notify_telegram', sent.append)
    monkeypatch.setattr(parking, '_merge_prep_branch', lambda e: (True, 'fixture prep integrated'))
    monkeypatch.setattr(parking, 'run_dep_map', lambda: None)
    monkeypatch.setattr(parking, 'run_status', lambda: None)
    real_graduate = parking._graduate_manual_action
    monkeypatch.setattr(parking, '_graduate_manual_action', lambda *a: graduated.append(a))
    monkeypatch.setattr(parking, 'get_task_by_id', lambda t: {})
    monkeypatch.setattr(parking, 'run_verification_gate',
                        lambda *a, **kw: (gate_calls.append(a), (True, 'passed'))[1])
    # The legacy module's automatic git push must never reach a network in this fixture.
    monkeypatch.setattr(parking, 'subprocess', SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=0)))
    return SimpleNamespace(repo=repo, state=state, sent=sent, graduated=graduated,
                           gate_calls=gate_calls, parking=parking, real_graduate=real_graduate)


@pytest.mark.parametrize('capture', ['success', 'nonzero', 'missing', 'stale'])
def test_legacy_done_checks_controller_capture_before_graduation(legacy, capture):
    p = legacy.parking
    p.handle_prep_done('T', {}, {'task_id': 'T', 'session_id': 'prep-T'}, {
        'dan_action': 'printf "legacy ran\\n"',
        'step_command': 'printf "legacy ran\\n"' + ('; exit 7' if capture == 'nonzero' else ''),
        'step_check_cmd': 'true', 'expected_result': 'legacy ran',
    })
    parked = legacy.state['waiting_on_dan']['1']
    contract = parked.get('manual_contract')
    assert contract, 'legacy runtime must create and durably retain the capture contract'
    assert 'I ran it' in legacy.sent[0] and 'Expected result' in legacy.sent[0]
    if capture != 'missing':
        subprocess.run(['bash', contract['script']], capture_output=True)
    if capture == 'stale':
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'advanced'],
                       cwd=legacy.repo, check=True, capture_output=True)
    # Simulate controller restart by reloading only persisted waiting state.
    p._finalize_manual_action('1', json.loads(json.dumps(parked)), [])
    assert bool(legacy.graduated) == (capture == 'success')
    assert bool(legacy.gate_calls) == (capture == 'success')


def test_legacy_unrecorded_done_cannot_graduate(legacy):
    legacy.parking._finalize_manual_action('1', {'task_id': 'T', 'session_id': 'old'}, [])
    assert not legacy.graduated and not legacy.gate_calls
    assert any('capture' in s.lower() for s in legacy.sent)


def _run_legacy_step(legacy):
    legacy.parking.handle_prep_done('T', {}, {'task_id': 'T', 'session_id': 'prep-T'}, {
        'dan_action': 'printf ran', 'step_check_cmd': 'true', 'expected_result': 'ran',
    })
    parked = legacy.state['waiting_on_dan']['1']
    subprocess.run(['bash', parked['manual_contract']['script']], check=True, capture_output=True)
    return parked


@pytest.mark.parametrize('verdict', ['pass', 'fail', 'drift'])
def test_legacy_observation_waits_after_machine_gate_and_binds_the_subject(legacy, monkeypatch, verdict):
    from maestro.hitl import verify
    from maestro.workflows import post_merge
    from maestro import state

    parked = _run_legacy_step(legacy)
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: {
        'id': 'T', 'verifications': [{'id': 'V-DAN', 'kind': 'dan', 'check': 'phone receives message'}]})
    replies, calls, repairs = [None], [], []
    def poll(**kw):
        calls.append(kw)
        return replies.pop(0)
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: SimpleNamespace(poll=poll))
    monkeypatch.setattr(state, 'control_store', lambda: object())
    monkeypatch.setattr(post_merge, 'publish_fix', lambda ctl, result: (repairs.append(result), 'T-fix')[1])
    legacy.parking._finalize_manual_action('1', parked, [])
    assert not legacy.graduated and legacy.gate_calls
    assert calls and calls[0]['operator']
    assert legacy.state['waiting_on_dan']['1']['kind'] == 'operator-verification'
    replies.append(verify.DanVerdict('approved' if verdict != 'fail' else 'rejected',
                                   '' if verdict != 'fail' else 'message absent', 'req'))
    if verdict == 'drift':
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'advanced'],
                       cwd=legacy.repo, check=True, capture_output=True)
    legacy.parking.poll_manual_observations([])
    assert bool(legacy.graduated) == (verdict == 'pass')
    assert bool(repairs) == (verdict == 'fail')


def test_legacy_machine_post_merge_failure_opens_repair_without_a_human_ask(legacy, monkeypatch):
    from maestro.hitl import verify
    from maestro.workflows import post_merge
    from maestro import state

    parked = _run_legacy_step(legacy)
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: {
        'id': 'T', 'verifications': [{'id': 'V', 'kind': 'auto', 'stage': 'post_merge', 'cmd': 'false'}]})
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: pytest.fail('machine check asked Dan'))
    repairs = []
    monkeypatch.setattr(state, 'control_store', lambda: object())
    monkeypatch.setattr(post_merge, 'publish_fix', lambda ctl, result: (repairs.append(result), 'T-fix')[1])
    legacy.parking._finalize_manual_action('1', parked, [])
    assert not legacy.graduated and len(repairs) == 1
    assert legacy.state['waiting_on_dan']['1']['fix_task'] == 'T-fix'


@pytest.mark.parametrize('capture', ['success', 'missing', 'stale'])
def test_actual_await_poll_revalidates_manual_capture_and_observation(legacy, monkeypatch, capture):
    from maestro import orchestrator
    from maestro.hitl import verify

    parked = _run_legacy_step(legacy)
    if capture == 'missing':
        Path(parked['manual_contract']['receipt']).unlink()
    if capture == 'stale':
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'advanced'],
                       cwd=legacy.repo, check=True, capture_output=True)
    legacy.state['waiting_on_dan']['1']['kind'] = 'awaiting-verification'
    task = {'id': 'T', 'verifications': [{'id': 'D', 'kind': 'dan', 'check': 'phone'}]}
    calls = []
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: task)
    monkeypatch.setattr(orchestrator, 'get_task_by_id', lambda t: task)
    monkeypatch.setattr(orchestrator, 'read_state', legacy.parking.read_state)
    monkeypatch.setattr(orchestrator, 'REPO', legacy.repo)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, 'notify_telegram', legacy.sent.append)
    monkeypatch.setattr(orchestrator, '_graduate_manual_action', legacy.parking._graduate_manual_action)
    monkeypatch.setattr(orchestrator, '_classify_verifications', lambda *a: ([], []))
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: SimpleNamespace(
        poll=lambda **kw: (calls.append(kw), None)[1]))
    orchestrator.poll_awaiting_verifications([])
    assert not legacy.graduated, 'await poll must not bypass capture or operator verification'
    assert bool(calls) == (capture == 'success')


def test_legacy_gate_defers_post_merge_and_operator_but_preserves_ordinary_checks(tmp_path, monkeypatch):
    from maestro import gates, verifications

    marker = tmp_path/'deployed-runs'
    specs = [
        {'id': 'PRE', 'kind': 'auto', 'cmd': 'printf ready', 'expect': 'ready'},
        {'id': 'POST', 'kind': 'auto', 'stage': 'post_merge',
         'cmd': f'echo ran >> {marker}; printf deployed', 'expect': 'deployed'},
        {'id': 'DAN', 'kind': 'dan', 'check': 'phone'},
        {'id': 'PROOF', 'kind': 'manual'},
    ]
    monkeypatch.setattr(verifications, 'get_task_verifications', lambda t: specs)
    (tmp_path/'result.json').write_text(json.dumps({'verifications': {
        'PROOF': {'done': True, 'proof': 'checked the artifact'}}}))
    passed, message = gates.run_verification_gate('T', tmp_path, tmp_path, pre_merge_only=True)
    assert passed, message
    assert not marker.exists(), 'deployed checks must not execute before merge'
    specs[0]['expect'] = 'wrong'
    passed, message = gates.run_verification_gate('T', tmp_path, tmp_path, pre_merge_only=True)
    assert not passed and 'PRE' in message


def test_manual_finalize_executes_deployed_command_once_with_retained_evidence(legacy, monkeypatch):
    from maestro import gates, verifications, validation

    parked = _run_legacy_step(legacy)
    # The observed artifact belongs to the ignored controller area, outside source binding.
    marker = legacy.repo/'.orchestrator/count'
    specs = [{'id': 'POST', 'kind': 'auto', 'stage': 'post_merge',
              'cmd': f'echo ran >> {marker}; printf deployed', 'expect': 'deployed'}]
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: {'verifications': specs})
    monkeypatch.setattr(verifications, 'get_task_verifications', lambda t: specs)
    monkeypatch.setattr(legacy.parking, 'run_verification_gate', gates.run_verification_gate)
    legacy.parking._finalize_manual_action('1', parked, [])
    assert legacy.graduated
    assert marker.read_text().splitlines() == ['ran']
    evidence = legacy.state['waiting_on_dan']['1']['deployed_evidence']
    assert evidence['evidence'][0]['exit_status'] == 0
    assert 'deployed' in str(evidence['output'])


@pytest.mark.parametrize('outcome', ['pass', 'fail', 'waiting'])
def test_ordinary_merge_defers_graduation_until_post_merge_checks(legacy, monkeypatch, outcome):
    from maestro.hitl import verify
    from maestro.workflows import post_merge
    from maestro import state

    task = {'id': 'T', 'verifications': [
        {'id': 'POST', 'kind': 'auto', 'stage': 'post_merge', 'cmd': 'printf deployed', 'expect': 'deployed'},
        {'id': 'DAN', 'kind': 'dan', 'check': 'phone'}]}
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: task)
    verdict = None if outcome == 'waiting' else verify.DanVerdict(
        'approved' if outcome == 'pass' else 'rejected', 'phone absent', 'req')
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: SimpleNamespace(poll=lambda **kw: verdict))
    repairs = []
    monkeypatch.setattr(state, 'control_store', lambda: object())
    monkeypatch.setattr(post_merge, 'publish_fix', lambda ctl, result: (repairs.append(result), 'T-fix')[1])
    entry = {'task_id': 'T', 'session_id': 'impl-T', 'branch': 'task/T', 'worktree': str(legacy.repo)}
    deferred = legacy.parking.defer_merged_verification(entry, {'pass': True}, [])
    assert deferred == (outcome != 'pass')
    assert not legacy.graduated  # caller retains its existing immediate completion tail
    if deferred:
        parked = next(iter(legacy.state['waiting_on_dan'].values()))
        assert parked['merged_completion']['smoke'] == {'pass': True}
        assert parked['kind'] == ('post-merge-failed' if outcome == 'fail' else 'operator-verification')
    else:
        assert not legacy.state.get('waiting_on_dan')
    assert bool(repairs) == (outcome == 'fail')


@pytest.mark.parametrize('canary_ok', [True, False])
def test_polled_ordinary_completion_preserves_canary_and_report(legacy, monkeypatch, canary_ok):
    from maestro import merge
    from maestro.hitl import verify

    parked = {'task_id': 'T', 'session_id': 'impl-T', 'branch': 'task/T',
              'worktree': str(legacy.repo), 'kind': 'operator-verification',
              'merged_completion': {'smoke': {'pass': True, 'metrics': {'n': 3}}}}
    legacy.state['waiting_on_dan'] = {'1': parked}
    monkeypatch.setattr(legacy.parking, '_graduate_manual_action', legacy.real_graduate)
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: {
        'id': 'T', 'verifications': [{'id': 'D', 'kind': 'dan', 'check': 'phone'}]})
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: SimpleNamespace(
        poll=lambda **kw: verify.DanVerdict('approved', '', 'req')))
    events = []
    monkeypatch.setattr(legacy.parking, 'mark_roadmap_complete', lambda t: events.append('complete'))
    monkeypatch.setattr(legacy.parking, 'remove_worktree', lambda t: events.append('remove'))
    monkeypatch.setattr(legacy.parking, 'phase_report', lambda *a: events.append(('report', a[2])))
    monkeypatch.setattr(merge, '_touches_bot_files', lambda b: True)
    monkeypatch.setattr(merge, 'run_canary_deploy', lambda t: (events.append('canary'), canary_ok)[1])
    legacy.parking.poll_manual_observations([])
    assert 'complete' in events and 'remove' in events and 'canary' in events
    assert ('report', {'pass': True, 'metrics': {'n': 3}}) in events if canary_ok else not any(
        isinstance(e, tuple) and e[0] == 'report' for e in events)
    assert not legacy.state['waiting_on_dan']


def test_rechecking_pending_manual_result_reuses_deployed_evidence(legacy, monkeypatch):
    from maestro.hitl import verify

    parked = _run_legacy_step(legacy)
    marker = legacy.repo/'.orchestrator/count'
    task = {'id': 'T', 'verifications': [
        {'id': 'POST', 'kind': 'auto', 'stage': 'post_merge',
         'cmd': f'echo ran >> {marker}; printf deployed', 'expect': 'deployed'},
        {'id': 'D', 'kind': 'dan', 'check': 'phone'}]}
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: task)
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: SimpleNamespace(poll=lambda **kw: None))
    legacy.parking._finalize_manual_action('1', parked, [])
    legacy.parking._finalize_manual_action('1', legacy.state['waiting_on_dan']['1'], [])
    assert marker.read_text().splitlines() == ['ran']


def test_legacy_operator_wait_cannot_reenter_merge_via_approve(legacy, monkeypatch):
    from maestro.hitl import commands

    legacy.state['waiting_on_dan'] = {'1': {
        'task_id': 'T', 'session_id': 's', 'branch': 'task/T', 'worktree': str(legacy.repo),
        'kind': 'operator-verification'}}
    monkeypatch.setattr(commands, 'read_state', legacy.parking.read_state)
    monkeypatch.setattr(commands, 'write_state', legacy.parking.write_state)
    monkeypatch.setattr(commands, 'append_journal', lambda *a, **kw: None)
    monkeypatch.setattr(commands, 'WORKSPACES', legacy.repo)
    monkeypatch.setattr(commands, 'notify_telegram', legacy.sent.append)
    monkeypatch.setattr(commands, 'merge_and_eval', lambda *a: pytest.fail('already merged'))
    commands._process_approve('1', [])
    assert 'Pass' in legacy.sent[-1] and 'Fail' in legacy.sent[-1]


def test_legacy_repair_adapter_recovers_after_real_roadmap_publication(legacy, monkeypatch):
    from maestro import state, taskgraph
    from maestro.control.store import ControlStore

    roadmap = legacy.repo/'docs/ROADMAP.md'
    roadmap.parent.mkdir()
    roadmap.write_text('```yaml\nid: T\ntitle: Fixture\nstatus: pending\n'
                       'verifications:\n  - id: POST\n    kind: auto\n'
                       '    stage: post_merge\n    cmd: "false"\n```\n')
    store = ControlStore(legacy.repo/'.orchestrator/control.db', role='controller')
    monkeypatch.setattr(state, 'control_store', lambda: store)
    taskgraph.sync_to_store(store, taskgraph.build_graph(taskgraph.load_document(roadmap)))
    parked = {'task_id': 'T', 'session_id': 'impl-T', 'verification_snapshot': 'merged-sha'}
    legacy.state['waiting_on_dan'] = {'1': parked}
    original = taskgraph.sync_to_store
    def crash(*a, **kw):
        raise RuntimeError('death after CAS')
    monkeypatch.setattr(taskgraph, 'sync_to_store', crash)
    with pytest.raises(RuntimeError, match='death after CAS'):
        legacy.parking._legacy_repair('1', parked, 'deployed failed')
    assert legacy.state['waiting_on_dan']['1']['kind'] == 'post-merge-repair'
    monkeypatch.setattr(taskgraph, 'sync_to_store', original)
    legacy.parking.poll_manual_observations([])
    legacy.parking.poll_manual_observations([])
    fixes = [s for s in taskgraph.build_graph(taskgraph.load_document(roadmap)).specs.values()
             if s.legacy_fields.get('repairs_task') == 'T']
    assert len(fixes) == 1 and store.task(fixes[0].task_id) is not None
    assert fixes[0].legacy_fields['repair_subject'] == 'merged-sha'
    assert not fixes[0].depends_on
    assert legacy.state['waiting_on_dan']['1']['kind'] == 'post-merge-failed'


def test_legacy_repair_without_control_database_publishes_once_and_is_dispatchable(legacy, monkeypatch):
    from maestro import state, taskgraph

    roadmap = legacy.repo/'docs/ROADMAP.md'
    roadmap.parent.mkdir()
    roadmap.write_text('```yaml\nid: T\ntitle: Fixture\nstatus: pending\n```\n')
    monkeypatch.setattr(state, 'control_store', lambda: None)
    parked = {'task_id': 'T', 'session_id': 'impl-T', 'verification_snapshot': 'merged-sha'}
    legacy.state['waiting_on_dan'] = {'1': parked}
    legacy.parking._legacy_repair('1', parked, 'deployed failed')
    legacy.parking._legacy_repair('1', parked, 'deployed failed')
    graph = taskgraph.build_graph(taskgraph.load_document(roadmap))
    fixes = [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task') == 'T']
    assert len(fixes) == 1 and not fixes[0].depends_on
    assert fixes[0].to_yaml_dict()['status'] == 'pending'
    assert not (legacy.repo/'.orchestrator/control.sqlite3').exists()


def test_numeric_reject_cannot_drop_an_integrated_task_or_offer_remerge(legacy, monkeypatch):
    from maestro.hitl import commands

    parked = {'task_id': 'T', 'session_id': 'impl-T', 'branch': 'task/T',
              'worktree': str(legacy.repo), 'kind': 'operator-verification'}
    legacy.state['waiting_on_dan'] = {'1': parked}
    monkeypatch.setattr(commands, 'read_state', legacy.parking.read_state)
    monkeypatch.setattr(commands, 'write_state', legacy.parking.write_state)
    monkeypatch.setattr(commands, 'append_journal', lambda *a, **kw: None)
    monkeypatch.setattr(commands, 'notify_telegram', legacy.sent.append)
    monkeypatch.setattr(commands, 'WORKSPACES', legacy.repo)
    monkeypatch.setattr(commands, 'subprocess', SimpleNamespace(run=lambda *a, **kw: None))
    monkeypatch.setattr(commands, 'REPO_ROOT', legacy.repo, raising=False)
    commands._process_reject('1')
    assert legacy.state['waiting_on_dan']['1'] == parked
    assert '/verify' in legacy.sent[-1]


@pytest.mark.parametrize('commits', [False, True])
def test_await_resume_does_not_repeat_successful_finalizer(legacy, monkeypatch, commits):
    from maestro import prep_actions

    parked = _run_legacy_step(legacy)
    marker = legacy.repo/'.orchestrator/finalizer-runs'
    cmd = f'echo ran >> {marker}'
    if commits:
        cmd += '; git -c core.hooksPath=/dev/null commit --allow-empty -qm finalized'
    prep_actions.set_action('T', 'already ran', post_action_cmd=cmd, dan_id=1)
    monkeypatch.setattr(legacy.parking, 'subprocess', subprocess)
    gates = [(False, 'await absent'), (True, 'passed')]
    monkeypatch.setattr(legacy.parking, 'run_verification_gate', lambda *a, **kw: gates.pop(0))
    monkeypatch.setattr(legacy.parking, '_classify_verifications', lambda *a: (['AWAIT'], []))
    legacy.parking._finalize_manual_action('1', parked, [])
    assert not legacy.graduated
    reloaded = json.loads(json.dumps(legacy.state['waiting_on_dan']['1']))
    assert reloaded['kind'] == 'awaiting-verification'
    legacy.parking.resume_awaited_verification('1', reloaded, [])
    assert legacy.graduated
    assert marker.read_text().splitlines() == ['ran']


def test_async_observation_fail_holds_without_publishing_deployed_repair(legacy, monkeypatch):
    from maestro import orchestrator
    from maestro.hitl import verify
    from maestro.workflows import post_merge

    task = {'id': 'T', 'dispatch': 'async-job', 'verifications': [
        {'id': 'D', 'kind': 'dan', 'check': 'phone'}]}
    parked = {'task_id': 'T', 'session_id': '', 'branch': '', 'worktree': '',
              'kind': 'awaiting-verification'}
    legacy.state['waiting_on_dan'] = {'1': parked}
    monkeypatch.setattr(legacy.parking, 'get_task_by_id', lambda t: task)
    monkeypatch.setattr(orchestrator, 'get_task_by_id', lambda t: task)
    monkeypatch.setattr(orchestrator, 'read_state', legacy.parking.read_state)
    monkeypatch.setattr(orchestrator, 'REPO', legacy.repo)
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, 'notify_telegram', legacy.sent.append)
    monkeypatch.setattr(verify, 'DanVerificationChannel', lambda: SimpleNamespace(
        poll=lambda **kw: verify.DanVerdict('rejected', 'phone absent', 'req')))
    monkeypatch.setattr(post_merge, 'publish_fix', lambda *a: pytest.fail('no merge to repair'))
    orchestrator.poll_awaiting_verifications([])
    assert not legacy.graduated
    assert legacy.state['waiting_on_dan']['1']['kind'] == 'operator-failed'
