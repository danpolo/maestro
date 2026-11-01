"""Public intake commands feed the real controller and normal engineering workflow."""
import json

import maestro.state
from maestro import intake
from maestro.hitl import commands
from .test_end_to_end import (_happy_scenario, _git_sandbox, _CommittingAgent,
                              _drive_every_worker_of_any_run, _git)
from .test_failure_ladder import _patch_fixture_cli_driver
from .test_dispatch_wiring import _task
from .conftest import LaneScenario


def _proposal():
    return json.dumps({'task': {'title': 'Add the feature', 'short_desc': 'Add feature.txt',
        'scope': ['feature.txt'], 'constraints': ['Keep existing work'], 'deps': [], 'risk': 'low',
        'acceptance_criteria': ['feature.txt exists'], 'verifications': [
            {'id': 'V1', 'kind': 'auto', 'check': 'Feature exists', 'cmd': 'test -f feature.txt'}]},
        'questions': []})


def _capture(run):
    _git_sandbox(run)
    intake.handle(run.repo, '/report feature Add feature.txt', 123, lambda text: None)


def test_report_approval_flows_through_normal_implementation_review_acceptance_and_graduation(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    monkeypatch.setattr(intake, '_generate', lambda repo, prompt: _proposal())
    def on_poll(run):
        data = intake.load(run.repo, 'I-123')
        if data['status'] == 'awaiting_approval':
            intake.handle(run.repo, '/intake approve I-123 1', 124, lambda text: None)
        _drive_every_worker_of_any_run(run)
        if 'I-123' in run.orchestrator.get_completed_task_ids():
            run.halt()
    # Existing held work must stay intact, while intake creates its own runnable task.
    held = _task('EXISTING', hold=True)
    run = run_lane(_happy_scenario('intake_end_to_end', tasks=[held], before_run=_capture, on_poll=on_poll))
    assert run.returncode == 0
    store = maestro.state.control_store()
    assert store.task('I-123')['status'] == 'accepted'
    assert store.conn.execute("SELECT status FROM task_runs WHERE task_id='I-123'").fetchone()[0] == 'succeeded'
    assert _git(run.repo, 'show', 'main:feature.txt') == 'done'
    assert intake.load(run.repo, 'I-123')['status'] == 'graduated'
    assert 'EXISTING' in (run.repo / 'docs/ROADMAP.md').read_text()
    assert store.conn.execute('SELECT count(*) FROM acceptance_decisions WHERE task_id=? AND decision=?',
                              ('I-123', 'accepted')).fetchone()[0] >= 1


def test_paused_controller_prepares_intake_without_resuming_work(run_lane, monkeypatch):
    monkeypatch.setattr(intake, '_generate', lambda repo, prompt: _proposal())
    run = run_lane(_happy_scenario('intake_paused', state={'paused_by_user': True},
                                  before_run=_capture, on_poll=lambda r: r.halt()))
    assert intake.load(run.repo, 'I-123')['status'] == 'awaiting_approval'
    assert maestro.state.read_state()['paused_by_user'] is True
    assert maestro.state.control_store().conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


def test_legacy_controller_prepares_intake_via_its_normal_poll(run_lane, monkeypatch):
    monkeypatch.setattr(intake, '_generate', lambda repo, prompt: _proposal())
    def capture(run):
        intake.handle(run.repo, '/report feature Add feature.txt', 123, lambda text: None)
    run = run_lane(LaneScenario('intake_legacy', tasks=[_task('EXISTING', hold=True)],
                               before_run=capture, on_poll=lambda r: r.halt()))
    assert intake.load(run.repo, 'I-123')['status'] == 'awaiting_approval'
    assert not maestro.state.read_state().get('in_flight')


def test_legacy_controller_queues_approved_report_at_idle_without_launching_unmet_deps(run_lane, monkeypatch):
    raw = json.loads(_proposal())
    raw['task']['deps'] = ['EXISTING']
    monkeypatch.setattr(intake, '_generate', lambda repo, prompt: json.dumps(raw))
    def capture_approve(run):
        intake.handle(run.repo, '/report feature Add feature.txt', 123, lambda text: None)
        intake.process(run.repo, None, lambda text: None)
        intake.handle(run.repo, '/intake approve I-123 1', 124, lambda text: None)
    run = run_lane(LaneScenario('intake_legacy_queue', tasks=[_task('EXISTING', hold=True)],
                               before_run=capture_approve, on_poll=lambda r: r.halt()))
    assert intake.load(run.repo, 'I-123')['status'] == 'queued'
    assert maestro.state.control_store().task('I-123')['status'] == 'planned'
    assert not maestro.state.read_state().get('in_flight')
    assert maestro.state.control_store().conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
