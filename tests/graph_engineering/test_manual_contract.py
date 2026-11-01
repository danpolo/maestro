"""P13 W6: the operator runs one step; controller evidence decides success."""
from pathlib import Path
import json
import re
import shlex
import subprocess

import pytest


@pytest.mark.parametrize('waiting_kind', [
    'operator-verification', 'post-merge-checking', 'post-merge-repair',
    'post-merge-failed', 'verification-drift', 'operator-failed',
])
def test_legacy_integrated_wait_survives_reject_without_relaunching_original(run_lane, waiting_kind):
    from maestro import state
    from maestro.hitl import commands
    from .conftest import LaneScenario
    from .test_baseline_lanes import _task

    original, repair = 'TASK-001', 'TASK-001-fix'
    parked = {'task_id': original, 'session_id': 'integrated', 'kind': waiting_kind,
              'merged_completion': {'smoke': {'pass': True}}, 'verification_snapshot': 'merged'}

    def reject(run):
        commands._process_reject('1')
        assert state.read_state()['waiting_on_dan']['1'] == parked

    run = run_lane(LaneScenario('w6_legacy_integrated_hold',
        tasks=[_task(original), _task(repair)],
        state={'waiting_on_dan': {'1': parked}}, before_run=reject,
        on_poll=lambda run: run.halt()))

    assert run.returncode == 0
    assert {entry['task_id'] for entry in run.in_flight()} == {repair}
    assert len(run.launched) == 1, 'only the independent repair may launch'
    assert state.read_state()['waiting_on_dan']['1'] == parked


@pytest.mark.parametrize('brief_kind', ['graph', 'legacy'])
def test_duetflow08_prep_names_the_exact_gate_command_and_paths(monkeypatch, tmp_path, brief_kind):
    from maestro.workflows import runner

    task = {
        'id': '08-timers-notify', 'dispatch': 'manual',
        'verifications': [{
            'id': 'V3', 'kind': 'auto', 'check': 'the timer installer is executable',
            'cmd': 'test -x deploy/install_timers.sh',
            'paths': ['deploy/install_timers.sh', 'deploy/README.md'],
        }],
    }
    monkeypatch.setattr(runner, '_roadmap_task', lambda task_id: task)
    if brief_kind == 'graph':
        brief = runner.prep_contract(task['id'])
    else:
        from maestro import implementer
        brief = implementer._make_prep_brief(task, tmp_path/'workspace', tmp_path/'tree')

    assert 'test -x deploy/install_timers.sh' in brief
    assert 'deploy/README.md' in brief
    assert 'step_command' in brief and 'step_check_cmd' in brief and 'expected_result' in brief


def test_manual_done_is_only_a_run_receipt_and_the_message_explains_gate_evidence():
    from maestro.hitl.verify import render_action_request

    text, _ = render_action_request('08', 'timers', 'request-08', 'bash install.sh', '/tree', step_command='bash install.sh')

    assert 'I ran it' in text
    assert 'exit code' in text
    assert 'Expected result' in text


def test_legacy_shell_action_cannot_bypass_controller_capture(run_lane, monkeypatch):
    from .test_g7_parity import _PrepAgent, _ActionDan, _manual_lane
    from .test_end_to_end import _run_status
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state
    from maestro.hitl import verify

    class LegacyPrep(_PrepAgent):
        def complete(self, spec):
            result = super().complete(spec)
            if spec.prompt.startswith('You are the implementer '):
                workspace = Path(re.search(r'^WORKSPACE: (\S+)$', spec.prompt, re.MULTILINE)[1])
                path = workspace/'result.json'
                doc = json.loads(path.read_text())
                doc.update(dan_action='printf "legacy step captured\\n"', step_command=None,
                           step_check_cmd='true', expected_result='legacy step captured')
                path.write_text(json.dumps(doc))
            return result

    class Dan(_ActionDan):
        def answer(self, run):
            for entry in (state.read_state().get(verify.STATE_KEY) or {}).values():
                if entry.get('sent') and entry['request_id'] not in self.answered:
                    record = verify._load(entry['request_id'])
                    assert '.orchestrator/manual/' in record['action']
                    assert subprocess.run(shlex.split(record['action']), capture_output=True).returncode == 0
            return super().answer(run)

    _patch_fixture_cli_driver(monkeypatch, LegacyPrep())
    run = run_lane(_manual_lane('w6_legacy_shell_capture', monkeypatch, Dan(['done'])))
    assert run.returncode == 0 and _run_status() == 'succeeded'


def test_external_manual_action_is_proved_by_its_step_check_and_retains_output(run_lane, monkeypatch):
    from .test_g7_parity import _PrepAgent, _ActionDan, _manual_lane
    from .test_end_to_end import _run_status
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state

    class Prep(_PrepAgent):
        def complete(self, spec):
            result = super().complete(spec)
            if spec.prompt.startswith('You are the implementer '):
                workspace = Path(re.search(r'^WORKSPACE: (\S+)$', spec.prompt, re.MULTILINE)[1])
                path = workspace/'result.json'
                doc = json.loads(path.read_text())
                doc.update(dan_action='https://example.invalid/console — export the artifact',
                           step_command=None, step_kind='external',
                           step_check_cmd='test -s ../console-export.txt && cat ../console-export.txt',
                           expected_result='the console export is present')
                path.write_text(json.dumps(doc))
            return result

    class Dan(_ActionDan):
        def answer(self, run):
            from maestro.hitl import verify
            for entry in (state.read_state().get(verify.STATE_KEY) or {}).values():
                if entry.get('sent') and entry['request_id'] not in self.answered:
                    record = verify._load(entry['request_id'])
                    (Path(record['worktree']).parent/'console-export.txt').write_text('external artifact verified\n')
            return super().answer(run)

    _patch_fixture_cli_driver(monkeypatch, Prep())
    run = run_lane(_manual_lane('w6_external_action', monkeypatch, Dan(['done'])))
    assert run.returncode == 0 and _run_status() == 'succeeded'
    store = state.control_store()
    row = store.conn.execute("SELECT outcome_ref FROM attempts WHERE node_id='gate'").fetchone()
    from maestro.control.artifacts import ArtifactStore
    document = json.loads(ArtifactStore(run.repo/'.orchestrator/artifacts', store).read(row['outcome_ref']))
    evidence = document['outputs']['manual_evidence']
    assert evidence['step_check']['stdout'] == 'external artifact verified\n'
    assert 'receipt' not in evidence  # a GUI action has no captured shell exit


def test_duetflow08_captured_step_replays_without_redo(run_lane, monkeypatch):
    from .test_g7_parity import _PrepAgent, _ActionDan, _manual_lane
    from .test_end_to_end import _run_status
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state
    from maestro.hitl import verify

    class Prep(_PrepAgent):
        def complete(self, spec):
            result = super().complete(spec)
            if spec.prompt.startswith('You are the implementer '):
                workspace = Path(re.search(r'^WORKSPACE: (\S+)$', spec.prompt, re.MULTILINE)[1])
                path = workspace/'result.json'
                document = json.loads(path.read_text())
                document.update(step_command='printf "timers installed\\n"',
                                step_check_cmd='git cat-file -e HEAD^{commit}',
                                expected_result='timers installed')
                path.write_text(json.dumps(document))
            return result

    class Dan(_ActionDan):
        def answer(self, run):
            for entry in (state.read_state().get(verify.STATE_KEY) or {}).values():
                if entry.get('sent') and entry['request_id'] not in self.answered:
                    record = verify._load(entry['request_id'])
                    assert '.orchestrator/manual/' in record['action']
                    assert 'timers installed' in record['question']
                    assert 'printf' in record['question']
                    command = shlex.split(record['action'])
                    completed = subprocess.run(command, capture_output=True, text=True)
                    assert completed.returncode == 0, completed.stderr
                    assert 'timers installed' in completed.stdout
            return super().answer(run)

    _patch_fixture_cli_driver(monkeypatch, Prep())
    dan = Dan(['done'])
    run = run_lane(_manual_lane('w6_duetflow08_capture', monkeypatch, dan))
    assert run.returncode == 0
    assert _run_status() == 'succeeded'
    assert len(dan.requests()) == 1


def test_manual_prerequisites_use_the_existing_questions_contract(monkeypatch, tmp_path):
    from maestro import implementer
    from maestro.docs import consistency

    task = {'id': '08', 'dispatch': 'manual', 'questions': [
        {'id': 'chat', 'prompt': 'Which alert chat should the installer target?'}]}
    monkeypatch.setattr(consistency, '_roadmap_tasks', lambda: [task])
    problems = []
    consistency.check_questions_schema(problems)
    assert problems == []
    monkeypatch.setattr(implementer, '_answer_choice', lambda req: 'the configured test chat')
    brief = implementer._make_prep_brief(task, tmp_path/'workspace', tmp_path/'tree')
    assert 'Which alert chat' in brief and 'the configured test chat' in brief


def test_successful_finalizer_keeps_its_output_as_gate_evidence(tmp_path):
    from maestro.workflows.handlers import _run_post_action_cmd

    captured = {}
    assert _run_post_action_cmd('printf "imported 12 rows\\n"', tmp_path, output=captured) == ''
    assert captured['exit_status'] == 0
    assert captured['stdout'] == 'imported 12 rows\n'


@pytest.mark.parametrize('fault', ['nonzero', 'missing', 'stale', 'wrong_attempt', 'tampered_log', 'check_failed'])
def test_gate_rejects_bad_manual_capture(tmp_path, monkeypatch, fault):
    from maestro import manual_steps
    from maestro.workflows import handlers
    from .test_end_to_end import _git

    tree = tmp_path/'tree'
    tree.mkdir()
    _git(tree, 'init', '-q')
    _git(tree, 'config', 'user.name', 'fixture')
    _git(tree, 'config', 'user.email', 'fixture@example.invalid')
    _git(tree, 'commit', '--allow-empty', '-qm', 'seed')
    contract = manual_steps.prepare(tmp_path, run_id='r', attempt_id='a', tree=tree,
                                    command='printf "installed\\n"; exit 7' if fault == 'nonzero' else 'printf "installed\\n"',
                                    step_check_cmd='false' if fault == 'check_failed' else 'true',
                                    expected_result='installed')
    if fault != 'missing':
        subprocess.run(['bash', contract['script']], capture_output=True)
    if fault == 'stale':
        _git(tree, 'commit', '--allow-empty', '-qm', 'changed')
    if fault == 'wrong_attempt':
        receipt = Path(contract['receipt'])
        receipt.write_text(json.dumps({**json.loads(receipt.read_text()), 'attempt_id': 'other'}))
    if fault == 'tampered_log':
        Path(contract['log']).write_text('claimed success')
    monkeypatch.setattr(handlers, '_scope_refusal', lambda *args: pytest.fail('gate trusted bad capture'))
    from maestro.workflows.models import NodeSpec
    ctx = handlers.HandlerContext(run_id='r', task_id='T', node=NodeSpec(node_id='gate', kind='function', handler_ref='maestro.handlers.verification'),
                                  attempt_id='gate-a', inbox=handlers.AttemptInbox(tmp_path/'inbox', 'gate-a'),
                                  workspace=tmp_path/'worker', worktree=tree, params={'task_id': 'T'},
                                  inputs={'manual_contract': contract})
    outcome = handlers.verification_handler(ctx)
    assert outcome.status == 'failed'
    assert outcome.outputs['failure']['kind'] == 'verification_failed'
    if fault == 'nonzero':
        assert 'exited 7' in outcome.detail


def test_capture_preparation_is_restart_safe_and_never_reexecutes_the_step(tmp_path):
    from maestro import manual_steps

    # A fresh controller invocation resolves the same prepared script and capture.
    tree = tmp_path/'tree'
    tree.mkdir()
    from .test_end_to_end import _git
    _git(tree, 'init', '-q')
    _git(tree, '-c', 'user.name=fixture', '-c', 'user.email=f@example.invalid', 'commit', '--allow-empty', '-qm', 'seed')
    kwargs = dict(run_id='r', attempt_id='a', tree=tree, command='printf "one\\n" >> count',
                  step_check_cmd='test -s count', expected_result='one execution')
    first = manual_steps.prepare(tmp_path, **kwargs)
    assert subprocess.run(['bash', first['script']]).returncode == 0
    second = manual_steps.prepare(tmp_path, **kwargs)
    assert first == second
    assert subprocess.run(['bash', second['script']], capture_output=True).returncode != 0
    assert (tree/'count').read_text() == 'one\n'


def test_capture_preparation_recovers_after_an_interrupted_descriptor_write(tmp_path, monkeypatch):
    from maestro import manual_steps
    from .test_end_to_end import _git

    tree = tmp_path/'tree'
    tree.mkdir()
    _git(tree, 'init', '-q')
    _git(tree, '-c', 'user.name=fixture', '-c', 'user.email=f@example.invalid',
         'commit', '--allow-empty', '-qm', 'seed')
    original = json.dump
    interrupted = []

    def crash_once(document, stream, **kw):
        if not interrupted:
            interrupted.append(True)
            stream.write('{"partial":')
            stream.flush()
            raise OSError('fixture death during descriptor write')
        return original(document, stream, **kw)

    monkeypatch.setattr(manual_steps.json, 'dump', crash_once)
    kwargs = dict(run_id='r', attempt_id='a', tree=tree, command='printf recovered',
                  step_check_cmd='true', expected_result='recovered')
    with pytest.raises(OSError, match='fixture death'):
        manual_steps.prepare(tmp_path, **kwargs)
    contract = manual_steps.prepare(tmp_path, **kwargs)
    assert subprocess.run(['bash', contract['script']], capture_output=True).returncode == 0
    refusal, evidence = manual_steps.verify(contract, tree)
    assert not refusal and evidence['output'] == 'recovered'


@pytest.mark.parametrize('fault', ['descriptor', 'subject'])
def test_wrapper_refuses_changed_contract_before_running_any_command(tmp_path, fault):
    from maestro import manual_steps
    from .test_end_to_end import _git

    tree = tmp_path/'tree'
    tree.mkdir()
    _git(tree, 'init', '-q')
    _git(tree, '-c', 'user.name=fixture', '-c', 'user.email=f@example.invalid', 'commit', '--allow-empty', '-qm', 'seed')
    contract = manual_steps.prepare(tmp_path, run_id='r', attempt_id='a', tree=tree,
                                    command='printf original > executed', step_check_cmd='true',
                                    expected_result='original')
    if fault == 'descriptor':
        path = Path(contract['script']).with_suffix('.json')
        path.write_text(json.dumps({**json.loads(path.read_text()), 'command': 'printf tampered > executed'}))
    else:
        _git(tree, '-c', 'user.name=fixture', '-c', 'user.email=f@example.invalid', 'commit', '--allow-empty', '-qm', 'changed')
    result = subprocess.run(['bash', contract['script']], capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tree/'executed').exists()


def test_operator_observation_follows_merge_but_merge_review_still_precedes_it():
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.compiler import compile_workflow

    revision = compile_workflow(normalize_spec({
        'id': '08', 'verifications': [
            {'id': 'V1', 'kind': 'auto', 'cmd': 'true'},
            {'id': 'V-PHONE', 'kind': 'dan', 'check': 'the phone receives the test message'},
        ],
    }), merge_review='hitl_mode')
    order = list(revision.topological_order())
    observations = next(n for n in revision.nodes if any(
        c['id'] == 'V-PHONE' for c in n.params.get('dan_checks', ())))
    merge_review = next(n for n in revision.nodes if any(
        c['id'] == 'MERGE-REVIEW' for c in n.params.get('dan_checks', ())))
    assert order.index(merge_review.node_id) < order.index('merge')
    assert order.index('merge') < order.index(observations.node_id) < order.index('accept')
    assert merge_review.node_id != observations.node_id


def test_phone_observation_is_requested_only_after_code_lands(run_lane, monkeypatch):
    from .test_end_to_end import _Dan, _RevisingAgent, _happy_scenario, _git, TARGET, FEATURE, _run_status
    from .test_dispatch_wiring import _task
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro.hitl import verify

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    dan = _Dan(['pass'])
    monkeypatch.setattr(verify, 'send_via_bot', dan.send)
    monkeypatch.setattr(verify, 'telegram_configured', lambda: True)
    run = run_lane(_happy_scenario('w6_operator_after_merge', tasks=[_task('TASK-001', verifications=[
        {'id': 'V1', 'kind': 'auto', 'cmd': 'test -f feature.txt'},
        {'id': 'V-PHONE', 'kind': 'dan', 'check': 'the phone receives the test message'},
    ])], on_poll=dan.on_poll))
    assert _run_status() == 'succeeded'
    assert dan.main_when_asked and dan.main_when_asked[0] != _git(run.repo, 'rev-list', '--max-parents=0', TARGET)
    assert _git(run.repo, 'show', f'{TARGET}:{FEATURE}').startswith('done')
    [(text, markup)] = [(text, markup) for text, markup in dan.sent if markup is not None]
    assert [b['text'] for row in markup['inline_keyboard'] for b in row] == ['Pass', 'Fail']
    assert 'Nothing merges until' not in text


def test_async_result_can_receive_an_operator_pass_without_a_merge(run_lane, monkeypatch):
    from .test_end_to_end import _Dan, _happy_scenario, _run_status
    from .test_dispatch_wiring import _task, _attempts
    from maestro.hitl import verify
    from maestro import taskgraph

    dan = _Dan(['pass'])
    monkeypatch.setattr(verify, 'send_via_bot', dan.send)
    monkeypatch.setattr(verify, 'telegram_configured', lambda: True)
    run = run_lane(_happy_scenario('w6_async_operator', tasks=[_task(
        'TASK-001', dispatch='async-job', launch_cmd='true', verifications=[
            {'id': 'V1', 'kind': 'auto', 'cmd': 'true'},
            {'id': 'V-DAN', 'kind': 'dan', 'check': 'the job result is visible'},
        ])], on_poll=dan.on_poll))
    assert _run_status() == 'succeeded'
    assert len(dan.answered) == 1
    assert not [a for a in _attempts() if a['node_id'] == 'merge']
    graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(run.repo)))
    assert not [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task')]


def test_post_merge_machine_stage_requires_a_merge_lane():
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.compiler import compile_workflow
    from maestro.workflows.models import WorkflowError

    with pytest.raises(WorkflowError, match='post_merge.*merge'):
        compile_workflow(normalize_spec({
            'id': 'async', 'dispatch': 'async-job', 'launch_cmd': 'true',
            'verifications': [{'id': 'V', 'kind': 'auto', 'stage': 'post_merge', 'cmd': 'true'}],
        }))


def test_late_operator_pass_after_checkout_drift_cannot_accept_or_queue_a_fix(run_lane, monkeypatch):
    from .test_end_to_end import _Dan, _RevisingAgent, _happy_scenario, _run_status, _git
    from .test_dispatch_wiring import _task
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state, taskgraph
    from maestro.hitl import verify

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    dan = _Dan(['pass'])
    monkeypatch.setattr(verify, 'send_via_bot', dan.send)
    monkeypatch.setattr(verify, 'telegram_configured', lambda: True)
    drifted = []

    def advance_then_answer(run):
        if state.read_state().get(verify.STATE_KEY) and not drifted:
            drifted.append(_git(run.repo, 'rev-parse', 'HEAD'))
            _git(run.repo, 'commit', '--allow-empty', '-qm', 'another integration')
        dan.on_poll(run)

    run = run_lane(_happy_scenario('w6_late_operator_pass', tasks=[_task(
        'TASK-001', verifications=[
            {'id': 'V1', 'kind': 'auto', 'cmd': 'test -f feature.txt'},
            {'id': 'V-DAN', 'kind': 'dan', 'check': 'the phone receives the message'},
        ])], on_poll=advance_then_answer))
    assert drifted and dan.answered
    assert _run_status() == 'failed'
    assert not state.control_store().conn.execute(
        "SELECT 1 FROM acceptance_decisions WHERE decision='accepted'").fetchall()
    graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(run.repo)))
    assert not [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task')]


def test_operator_rechecks_checkout_after_poll_before_attesting_pass(tmp_path):
    from types import SimpleNamespace
    from .test_end_to_end import _git
    from maestro.workflows import handlers
    from maestro.workflows.models import NodeSpec
    from maestro.hitl.verify import DanVerdict

    tree = tmp_path/'tree'
    tree.mkdir()
    _git(tree, 'init', '-q')
    _git(tree, 'config', 'user.name', 'fixture')
    _git(tree, 'config', 'user.email', 'fixture@example.invalid')
    _git(tree, 'commit', '--allow-empty', '-qm', 'seed')
    original = _git(tree, 'rev-parse', 'HEAD')

    def poll(**kwargs):
        _git(tree, 'commit', '--allow-empty', '-qm', 'advanced during poll')
        return DanVerdict('approved', '', 'request')

    controller = SimpleNamespace(operator_subject=lambda: (original, tree),
                                 merged_snapshot=lambda: original,
                                 dan_channel=SimpleNamespace(poll=poll))
    node = NodeSpec(node_id='operator_check', kind='human', handler_ref='maestro.handlers.operator_check')
    ctx = handlers.HandlerContext(run_id='r', task_id='T', node=node, attempt_id='a',
        inbox=handlers.AttemptInbox(tmp_path/'inbox', 'a'), workspace=tmp_path/'workspace',
        params={'task_id': 'T', 'dan_checks': [{'id': 'V-DAN'}]}, controller=controller)
    result = handlers.operator_check_handler(ctx)
    assert result.status == 'failed' and 'checkout' in result.detail
    assert not result.outputs.get('dispositions')
    assert not result.outputs.get('post_merge_failure')


def test_missing_integrated_subject_does_not_publish_a_deployed_repair(run_lane, monkeypatch):
    from .test_end_to_end import _RevisingAgent, _happy_scenario, _run_status
    from .test_dispatch_wiring import _task
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro.workflows.runner import WorkflowRunner
    from maestro import taskgraph

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    monkeypatch.setattr(WorkflowRunner, 'merged_snapshot', lambda self: '')
    run = run_lane(_happy_scenario('w6_missing_subject', tasks=[_task(
        'TASK-001', verifications=[
            {'id': 'V1', 'kind': 'auto', 'cmd': 'test -f feature.txt'},
            {'id': 'V-DEPLOYED', 'kind': 'auto', 'stage': 'post_merge', 'cmd': 'true'},
        ])]))
    assert _run_status() == 'failed'
    graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(run.repo)))
    assert not [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task')]


def test_machine_deployed_check_runs_only_after_merge_without_asking_dan(run_lane, monkeypatch):
    from .test_end_to_end import _RevisingAgent, _happy_scenario, _run_status
    from .test_dispatch_wiring import _task
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state
    from maestro.hitl import verify

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    monkeypatch.setattr(verify, 'send_via_bot', lambda *a, **k: pytest.fail('machine check asked Dan'))
    run = run_lane(_happy_scenario('w6_machine_post_merge', tasks=[_task('TASK-001', verifications=[
        {'id': 'V1', 'kind': 'auto', 'cmd': 'test -f feature.txt'},
        {'id': 'V-DEPLOYED', 'kind': 'auto', 'stage': 'post_merge',
         'cmd': 'test "$(git symbolic-ref --short HEAD)" = main && test -f feature.txt'},
    ])]))
    assert run.returncode == 0
    assert _run_status() == 'succeeded'
    rows = state.control_store().conn.execute(
        "SELECT a.node_id, e.* FROM verification_evidence e JOIN attempts a USING(attempt_id) "
        "WHERE e.check_id='V-DEPLOYED'").fetchall()
    assert rows and {r['node_id'] for r in rows} == {'post_merge_checks'}
    assert all(r['outcome'] == 'passed' for r in rows)


@pytest.mark.parametrize('observation', ['machine', 'operator'])
def test_failed_post_merge_check_queues_one_dispatchable_fix_without_remerge(run_lane, monkeypatch, observation):
    from .test_end_to_end import _Dan, _RevisingAgent, _happy_scenario, _run_status
    from .test_dispatch_wiring import _task, _attempts
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state, taskgraph
    from maestro.hitl import verify

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    dan = _Dan(['fail message never arrived'])
    monkeypatch.setattr(verify, 'send_via_bot', dan.send)
    monkeypatch.setattr(verify, 'telegram_configured', lambda: True)
    check = ({'id': 'V-DEPLOYED', 'kind': 'auto', 'stage': 'post_merge', 'cmd': 'false'}
             if observation == 'machine' else {'id': 'V-PHONE', 'kind': 'dan', 'check': 'message arrives'})
    run = run_lane(_happy_scenario('w6_failed_post_' + observation, tasks=[_task('TASK-001', verifications=[
        {'id': 'V1', 'kind': 'auto', 'cmd': 'test -f feature.txt'}, check,
    ])], on_poll=dan.on_poll))
    assert _run_status() == 'failed'
    graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(run.repo)))
    fixes = [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task') == 'TASK-001']
    assert len(fixes) == 1
    fix = fixes[0]
    assert 'TASK-001' not in fix.depends_on
    assert not fix.hold
    assert state.control_store().task(fix.task_id) is not None
    assert len([a for a in _attempts() if a['node_id'] == 'merge']) == 1
    asks = [markup for _, markup in dan.sent if markup is not None]
    assert len(asks) == (0 if observation == 'machine' else 1)


@pytest.mark.parametrize('observation', ['machine', 'operator'])
def test_task_with_only_post_merge_criteria_integrates_without_early_acceptance(run_lane, monkeypatch, observation):
    from .test_end_to_end import _Dan, _RevisingAgent, _happy_scenario, _run_status, _drive_every_worker
    from .test_dispatch_wiring import _task
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state
    from maestro.hitl import verify

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    dan = _Dan(['pass'])
    monkeypatch.setattr(verify, 'send_via_bot', dan.send)
    monkeypatch.setattr(verify, 'telegram_configured', lambda: True)
    saw_integrated = []

    def drive(run):
        store = state.control_store()
        phases = dict(store.conn.execute('SELECT node_id, phase FROM node_states'))
        if phases.get('merge') == 'succeeded' and phases.get('accept') != 'succeeded':
            saw_integrated.append(True)
            assert not store.conn.execute("SELECT 1 FROM acceptance_decisions WHERE decision='accepted'").fetchall()
        dan.on_poll(run) if observation == 'operator' else _drive_every_worker(run)

    check = ({'id': 'V-DEPLOYED', 'kind': 'auto', 'stage': 'post_merge',
              'cmd': 'test "$(git symbolic-ref --short HEAD)" = main && test -f feature.txt'}
             if observation == 'machine' else {'id': 'V-PHONE', 'kind': 'dan', 'check': 'message arrives'})
    run = run_lane(_happy_scenario('w6_only_post_' + observation,
                                  tasks=[_task('TASK-001', verifications=[check])], on_poll=drive))
    assert run.returncode == 0
    assert saw_integrated
    assert _run_status() == 'succeeded'


def test_repair_publication_survives_controller_restart_after_roadmap_cas(run_lane, monkeypatch):
    import dataclasses
    from .test_end_to_end import _RevisingAgent, _happy_scenario, _drive_every_worker, _run_status
    from .test_dispatch_wiring import _task, _attempts
    from .test_failure_ladder import _patch_fixture_cli_driver
    from maestro import state, taskgraph

    _patch_fixture_cli_driver(monkeypatch, _RevisingAgent())
    original = taskgraph.sync_to_store
    interrupted = []

    def crash_after_cas(store, graph, **kwargs):
        if any(s.legacy_fields.get('repairs_task') for s in graph.specs.values()) and not interrupted:
            interrupted.append(True)
            raise RuntimeError('fixture controller death after ROADMAP CAS, before store publication')
        return original(store, graph, **kwargs)

    monkeypatch.setattr(taskgraph, 'sync_to_store', crash_after_cas)

    def stop_after_crash(run):
        if interrupted:
            run.halt()
        else:
            _drive_every_worker(run)

    run = run_lane(_happy_scenario('w6_fix_publication_restart', tasks=[_task('TASK-001', verifications=[
        {'id': 'V1', 'kind': 'auto', 'cmd': 'test -f feature.txt'},
        {'id': 'V-DEPLOYED', 'kind': 'auto', 'stage': 'post_merge', 'cmd': 'false'},
    ])], on_poll=stop_after_crash))
    assert interrupted and _run_status() == 'running'
    run.orchestrator.HALT_FILE.unlink()
    state._reset_control_store()
    run.scenario = dataclasses.replace(run.scenario, on_poll=_drive_every_worker)
    run.run()
    assert _run_status() == 'failed'
    graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(run.repo)))
    fixes = [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task') == 'TASK-001']
    assert len(fixes) == 1
    assert state.control_store().task(fixes[0].task_id) is not None
    assert len([a for a in _attempts() if a['node_id'] == 'merge']) == 1
