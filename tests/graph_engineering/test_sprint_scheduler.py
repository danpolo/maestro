"""Sprint work enters the existing graph loop and its ordinary gates."""
from datetime import datetime, timezone
import dataclasses
import json
import os
import time

import pytest

from maestro import backlog, orchestrator, sprint, state
from maestro.backends import catalog as cat
from maestro.docs import followups
from maestro.control.resources import HostReservationLedger
from maestro.workflows import compiler
from maestro.workflows.pace import PaceEstimate
from maestro.workflows.policy import default_policy

from .conftest import fixture_routing
from .test_dispatch_wiring import _create_control_db, _graph_scenario, _task, _task_runs


@pytest.mark.parametrize('active', [True, False])
def test_a_live_sprint_opens_only_eligible_polish_before_main_work_finishes(run_lane, active):
    def routing():
        now = orchestrator.time.time()
        result = fixture_routing()
        observation = cat.QuotaObservation('fixture-pool', {
            10080: cat.QuotaWindow(10080, 10, now + 3600)},
            observed_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
        result.resources.pool('fixture-pool').observation = observation
        decision = sprint.evaluate('fixture-pool', observation, config=sprint.SprintConfig(),
            now_epoch=now, week=now+3600).to_dict()
        decision['active'] = active
        document = state.read_state()
        document['sprint'] = {'decisions': {'fixture-pool': decision}}
        state.write_state(document)
        result.sprint_reader = lambda: state.read_state().get('sprint', {})
        return result

    def before(run):
        _create_control_db(run)
        for tid in ('DONE', 'HELD'):
            backlog.add(run.repo, {'id': tid, 'scope': ['src/polish.py']},
                [followups.FollowUp(f'{tid} follow-up', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')

    run = run_lane(_graph_scenario(f'sprint_early_{active}', routing=routing, before_run=before,
        tasks=[_task('MAIN', scope=['src/main.py']), _task('DONE', status='complete'),
               _task('HELD', status='complete', hold=True)]))

    assert run.returncode == 0
    assert {r['task_id'] for r in _task_runs()} == ({'MAIN', 'polish-1-a'} if active else {'MAIN'})
    assert [(r['source']['task'], r['state']) for r in backlog.items(run.repo)] == [
        ('DONE', 'assigned' if active else 'open'), ('HELD', 'open')]
    assert 'queue_exhausted' not in run.journal_events()


def test_a_previously_staged_polish_task_cannot_escape_a_new_source_hold(run_lane):
    from maestro.docs import roadmap

    def before(run):
        _create_control_db(run)
        backlog.add(run.repo, {'id': 'DONE', 'scope': ['src/polish.py']},
            [followups.FollowUp('Created before the operator hold', 1, 'review')],
            at='2026-10-27T00:00:00+00:00')
        staged = backlog.stage(run.repo, [], {'DONE'}, at='2026-10-27T00:00:00+00:00')
        roadmap.add_tasks(staged, 'earlier polish', 'polish')

    run = run_lane(_graph_scenario('sprint_staged_source_hold', before_run=before,
        tasks=[_task('MAIN', scope=['src/main.py']), _task('DONE', status='complete', hold=True)]))
    assert run.returncode == 0
    assert {r['task_id'] for r in _task_runs()} == {'MAIN'}
    assert [r['state'] for r in backlog.items(run.repo)] == ['assigned']


def _budget_lane(monkeypatch, *, mixed=False, active=True):
    from maestro import scheduling

    original_compile = compiler.compile_workflow
    def compile_task(spec, **kwargs):
        revision = original_compile(spec, **kwargs)
        first = revision.node('implementer')
        if spec.task_id == 'ORDINARY':
            resource = dataclasses.replace(first, node_id='resource', resource_request={'ram_mb': 1})
            nodes = (resource, dataclasses.replace(first, node_id='budget')) if mixed else (resource,)
        else:
            nodes = (dataclasses.replace(first, node_id='budget'),)
        return dataclasses.replace(revision, nodes=nodes, edges=(),
            terminal_requirements=tuple(n.node_id for n in nodes))
    monkeypatch.setattr(compiler, 'compile_workflow', compile_task)
    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 1, 'cpu': 8, 'vram_mb': 0}))
    monkeypatch.setattr(orchestrator, '_ELIGIBILITY', scheduling.EligibilityClock())

    def before(run):
        _create_control_db(run)
        # Align this lane's otherwise deterministic loop clock to the real
        # worker/ledger clocks, without replacing their captured clock functions.
        orchestrator.time._now = time.time()
        now = orchestrator.time.time()
        HostReservationLedger().reserve_counted({'ram_mb': 1}, capacity={'ram_mb': 1},
            project_id='other', run_id='ram', writer_pid=os.getpid())
        hold = HostReservationLedger().reserve_budget('fixture-pool', {'account:300': 82},
            capacity={'account:300': 82}, resets={'account:300': now+1800},
            project_id='other', run_id='budget', writer_pid=os.getpid())
        assert hold.admission.admitted

    def routing():
        orchestrator.time._now = time.time()
        now = orchestrator.time.time()
        result = fixture_routing(policy=default_policy().with_objective('pace'))
        observation = cat.QuotaObservation('fixture-pool', {
            300: cat.QuotaWindow(300, 10, now+1800),
            10080: cat.QuotaWindow(10080, 10, now+3600)},
            observed_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
        result.resources.pool('fixture-pool').observation = observation
        result.pace_estimates = {'*': PaceEstimate(burn_pct_per_cost=82)}
        decision = sprint.evaluate('fixture-pool', observation, config=sprint.SprintConfig(),
            now_epoch=now, week=now+3600).to_dict()
        decision['active'] = active
        document = state.read_state()
        document['sprint'] = {'decisions': {'fixture-pool': decision}}
        state.write_state(document)
        result.sprint_reader = lambda: state.read_state().get('sprint', {})
        return result
    return _graph_scenario(f'sprint_budget_{mixed}_{active}', before_run=before, routing=routing,
        project_yaml={'engineering': {'runner': 'graph'},
                      'routing': {'pace': {'initial_burn_pct_per_cost': 82}}},
        tasks=[_task('ORDINARY', scope=['src/main.py']), _task('BUDGET', scope=['src/budget.py'])])


def test_only_actual_budget_waits_are_classified_for_pull_forward(run_lane, monkeypatch):
    run = run_lane(_budget_lane(monkeypatch, mixed=True))
    assert run.returncode == 0
    waits = state.read_state()['admission_waiting']
    assert waits['BUDGET']['reason'] == 'budget_exhausted'
    assert waits['BUDGET'].get('budget_only') is True
    assert waits['BUDGET'].get('nodes', {}).get('budget', {}).get('pool_id') == 'fixture-pool'
    assert waits['ORDINARY'].get('budget_only') is False
    assert {n['reason'] for n in waits['ORDINARY'].get('nodes', {}).values()} == {
        'resource_exhausted', 'budget_exhausted'}
    assert state.control_store().conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


@pytest.mark.parametrize('active', [True, False])
def test_sprint_retries_budget_only_work_first_after_real_controller_restart(
        run_lane, monkeypatch, active):
    run = run_lane(_budget_lane(monkeypatch, active=active))
    assert state.control_store().conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
    _release_and_restart(run)
    rows = state.control_store().conn.execute(
        'SELECT r.task_id FROM attempts a JOIN task_runs r ON r.run_id=a.run_id').fetchall()
    assert [r['task_id'] for r in rows] == ['BUDGET' if active else 'ORDINARY']
    assert 'graph_run_resumed' in run.journal_events()


def _release_and_restart(run):
    for reservation in HostReservationLedger().reservations():
        if reservation.project_id == 'other':
            HostReservationLedger().release(reservation.reservation_id)
    # Recovery's normal order is started_at/run_id; avoid a random run-id
    # tie within one wall-clock second masking the scheduler requirement.
    with state.control_store().transaction() as conn:
        conn.execute("UPDATE task_runs SET started_at = CASE task_id"
                     " WHEN 'ORDINARY' THEN '2026-10-27T00:00:00Z'"
                     " ELSE '2026-10-27T00:00:01Z' END")
    orchestrator.HALT_FILE.unlink()
    assert orchestrator._graph_main(state.read_state()) == 0


@pytest.mark.parametrize('barrier', ['off', 'stop', 'other_pool', 'future_decision',
                                   'hold', 'parked', 'prerequisite', 'priority', 'active_attempt',
                                   'full_five_hour', 'unknown_five_hour', 'goal_replan',
                                   'cyclic_roadmap'])
def test_prior_budget_wait_never_bypasses_current_gates_on_restart(run_lane, monkeypatch, barrier):
    from maestro import taskgraph
    from maestro.workflows import runner as wf_runner
    from .conftest import _roadmap_document

    scenario = _budget_lane(monkeypatch)
    original_routing = scenario.routing
    restarted = False
    calls = []
    original_tick = wf_runner.WorkflowRunner.tick
    def tick(self, *args, **kwargs):
        calls.append(self.revision.task_id)
        return original_tick(self, *args, **kwargs)
    monkeypatch.setattr(wf_runner.WorkflowRunner, 'tick', tick)
    def routing():
        result = original_routing()
        if not restarted:
            return result
        document = state.read_state()
        if barrier in ('off', 'stop'):
            document['sprint'] = sprint.control(document['sprint'], barrier,
                now_epoch=orchestrator.time.time())
        elif barrier == 'other_pool':
            decision = document['sprint']['decisions']['fixture-pool']
            document['sprint']['decisions'] = {'unrelated-pool': dict(decision, pool_id='unrelated-pool')}
        elif barrier == 'future_decision':
            document['sprint']['decisions']['fixture-pool']['inputs']['now_epoch'] += 100
        elif barrier in ('full_five_hour', 'unknown_five_hour'):
            observation = result.resources.pool('fixture-pool').observation
            windows = dict(observation.windows)
            windows[300] = cat.QuotaWindow(300, 100 if barrier == 'full_five_hour' else None,
                                          orchestrator.time.time()+1800)
            result.resources.pool('fixture-pool').observation = dataclasses.replace(
                observation, windows=windows)
        state.write_state(document)
        return result
    scenario.routing = routing
    run = run_lane(scenario)
    assert state.read_state()['admission_waiting']['BUDGET']['budget_only'] is True
    if barrier in ('hold', 'priority', 'prerequisite', 'goal_replan', 'cyclic_roadmap'):
        tasks = taskgraph.scan_tasks(orchestrator.ROADMAP_FILE.read_text())
        for task in tasks:
            if task['id'] == 'BUDGET' and barrier == 'hold':
                task['hold'] = True
            elif task['id'] == 'BUDGET' and barrier == 'prerequisite':
                task['deps'] = ['UNFINISHED']
            elif task['id'] == 'ORDINARY' and barrier == 'priority':
                task['priority'] = 10
            elif task['id'] == 'BUDGET' and barrier == 'goal_replan':
                task.update(serves=['G3'], contribution='direct')
        if barrier == 'prerequisite':
            tasks.append(_task('UNFINISHED', hold=True))
        elif barrier == 'goal_replan':
            from maestro import goal_store
            goal_store.update(run.repo, 'replans', lambda rows: [*rows,
                {'id': 'RP-1', 'goal': 'G3', 'status': 'asked'}])
        elif barrier == 'cyclic_roadmap':
            tasks += [_task('CYCLE-A', deps=['CYCLE-B'], hold=True),
                      _task('CYCLE-B', deps=['CYCLE-A'], hold=True)]
        orchestrator.ROADMAP_FILE.write_text(_roadmap_document(tasks))
        taskgraph.record_applied(run.repo, taskgraph.load_document(orchestrator.ROADMAP_FILE))
    elif barrier == 'parked':
        document = state.read_state()
        document['parked_tasks'] = ['BUDGET']
        state.write_state(document)
    elif barrier == 'active_attempt':
        store = state.control_store()
        row = store.conn.execute("SELECT * FROM task_runs WHERE task_id='BUDGET'").fetchone()
        revision = compiler.load_revision(store, row['workflow_id'], row['workflow_revision'])
        instance = wf_runner.WorkflowRunner(store, revision, run_id=row['run_id'], repo=run.repo)
        instance.claim(revision.node('budget'))
    restarted = True
    calls.clear()
    _release_and_restart(run)
    if barrier in ('full_five_hour', 'unknown_five_hour'):
        assert state.control_store().conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        expected = 'route_quota_exhausted' if barrier == 'full_five_hour' else 'route_quota_unknown'
        assert state.read_state()['admission_waiting']['BUDGET']['reason'] == expected
    else:
        assert calls[0] == 'ORDINARY', 'an ineligible or controlled wait must lose sprint priority'


def test_polish_source_counted_resources_reach_real_graph_admission(run_lane, monkeypatch):
    from maestro import scheduling
    from maestro.docs import roadmap

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 1, 'cpu': 8, 'vram_mb': 0}))
    def before(run):
        _create_control_db(run)
        backlog.add(run.repo, {'id': 'DONE', 'resources': {'ram_mb': 2}},
            [followups.FollowUp('Resource-contained polish', 1, 'review')],
            at='2026-10-27T00:00:00+00:00')
        staged = backlog.stage(run.repo, [], {'DONE'}, at='2026-10-27T00:00:00+00:00')
        roadmap.add_tasks(staged, 'earlier polish', 'polish')
    run = run_lane(_graph_scenario('sprint_polish_counted_resources', before_run=before,
        tasks=[_task('DONE', status='complete')]))
    assert run.returncode == 0
    # Existing W9 records a failed audit attempt for a permanently oversized
    # request; it must never launch a worker or retain a host reservation.
    from .test_dispatch_wiring import _windows
    rows = state.control_store().conn.execute(
        'SELECT node_id,status,route_refusal FROM attempts').fetchall()
    assert [tuple(row) for row in rows] == [
        ('implementer', 'failed', 'exceeds_host_capacity: ram_mb requested 2, host capacity 1')]
    assert _windows(run) == []
    assert HostReservationLedger().reservations() == []


def test_current_source_resources_override_saved_backlog_metadata(run_lane, monkeypatch):
    from maestro import scheduling
    from maestro.docs import roadmap
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 1, 'cpu': 8, 'vram_mb': 0}))
    def before(run):
        _create_control_db(run)
        backlog.add(run.repo, {'id': 'DONE', 'resources': {'ram_mb': 1}},
            [followups.FollowUp('Source RAM changed after review', 1, 'review')],
            at='2026-10-27T00:00:00+00:00')
        staged = backlog.stage(run.repo, [], {'DONE'}, at='2026-10-27T00:00:00+00:00')
        roadmap.add_tasks(staged, 'earlier polish', 'polish')
    run = run_lane(_graph_scenario('sprint_current_source_resources', before_run=before,
        tasks=[_task('DONE', status='complete', resources={'ram_mb': 2})]))
    assert run.returncode == 0
    assert _windows(run) == []
    rows = state.control_store().conn.execute('SELECT status,route_refusal FROM attempts').fetchall()
    assert [tuple(row) for row in rows] == [
        ('failed', 'exceeds_host_capacity: ram_mb requested 2, host capacity 1')]
    assert HostReservationLedger().reservations() == []


@pytest.mark.parametrize('barrier', ['hold', 'resources'])
def test_merged_duplicate_source_keeps_its_real_admission_gates(run_lane, monkeypatch, barrier):
    from maestro import scheduling
    from maestro.docs import roadmap
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 1, 'cpu': 8, 'vram_mb': 0}))
    def before(run):
        _create_control_db(run)
        for tid in ('DONE-A', 'DONE-B'):
            backlog.add(run.repo, {'id': tid, 'resources': {'ram_mb': 2 if tid == 'DONE-B' else 1}},
                [followups.FollowUp(f'{tid} shared defect', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')
        [triage] = backlog.stage(run.repo, [], {'DONE-A', 'DONE-B'},
            at='2026-10-27T00:00:00+00:00')
        path = run.repo / backlog.triage_file(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'items': [
            {'id': 'BL-0001', 'decision': 'keep'},
            {'id': 'BL-0002', 'decision': 'duplicate', 'of': 'BL-0001', 'reason': 'same defect'}]}))
        [polish], _ = backlog.apply_triage(run.repo, triage, at='2026-10-27T00:00:00+00:00')
        if barrier == 'resources':
            assert polish.get('resources') == {'ram_mb': 2}
        roadmap.add_tasks([polish], 'after triage', 'polish')
    run = run_lane(_graph_scenario(f'sprint_duplicate_{barrier}', before_run=before,
        tasks=[_task('DONE-A', status='complete'),
               _task('DONE-B', status='complete', hold=barrier == 'hold')]))
    assert run.returncode == 0
    assert _windows(run) == []
    if barrier == 'hold':
        assert state.control_store().conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
    else:
        assert state.control_store().conn.execute(
            'SELECT route_refusal FROM attempts').fetchone()[0] == (
                'exceeds_host_capacity: ram_mb requested 2, host capacity 1')
    assert HostReservationLedger().reservations() == []


@pytest.mark.parametrize('other_owner', [False, True])
def test_polish_mutex_owner_can_retry_but_cannot_displace_another_live_owner(
        run_lane, monkeypatch, other_owner):
    from maestro import scheduling
    from maestro.docs import roadmap
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 1, 'cpu': 8, 'vram_mb': 0}))
    def before(run):
        _create_control_db(run)
        source = {'id': 'DONE', 'resource': 'eval-cpu', 'resources': {'ram_mb': 1}}
        backlog.add(run.repo, source,
            [followups.FollowUp('Retry after counted RAM becomes free', 1, 'review')],
            at='2026-10-27T00:00:00+00:00')
        staged = backlog.stage(run.repo, [], {'DONE'}, at='2026-10-27T00:00:00+00:00')
        roadmap.add_tasks(staged, 'earlier polish', 'polish')
        assert HostReservationLedger().reserve_counted({'ram_mb': 1}, capacity={'ram_mb': 1},
            project_id='other-project', run_id='ram', writer_pid=os.getpid()).admission.admitted
    polls = []
    def poll(run):
        polls.append(True)
        if len(polls) == 1:
            for hold in HostReservationLedger().reservations():
                if hold.project_id == 'other-project':
                    HostReservationLedger().release(hold.reservation_id)
        else:
            run.halt()
    tasks = ([_task('OTHER', resource='eval-cpu')] if other_owner else [])
    tasks.append(_task('DONE', status='complete', resource='eval-cpu'))
    run = run_lane(_graph_scenario(f'sprint_mutex_retry_{other_owner}', before_run=before,
        tasks=tasks, on_poll=poll))
    assert run.returncode == 0
    polish_workers = [cmd for cmd in _windows(run) if '-n maestro-polish-' in cmd]
    assert len(polish_workers) == (0 if other_owner else 1)
    if other_owner:
        assert {row['task_id'] for row in _task_runs()} == {'OTHER'}
    else:
        assert state.control_store().conn.execute(
            "SELECT status FROM attempts WHERE node_id='implementer'").fetchone()[0] == 'claimed'


@pytest.mark.parametrize('restart', [False, True])
def test_waiting_polish_runs_acquire_one_current_mutex_owner_and_then_release_it(
        run_lane, monkeypatch, restart):
    from maestro import scheduling, taskgraph
    from maestro.docs import roadmap
    from maestro.workflows import runner as wf_runner
    from .conftest import _roadmap_document
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 2, 'cpu': 8, 'vram_mb': 0}))
    instances = {}
    tick = wf_runner.WorkflowRunner.tick
    def observed_tick(self, *args, **kwargs):
        instances[self.run_id] = self
        return tick(self, *args, **kwargs)
    monkeypatch.setattr(wf_runner.WorkflowRunner, 'tick', observed_tick)

    def before(run):
        _create_control_db(run)
        previous = []
        for tid in ('DONE-A', 'DONE-B'):
            backlog.add(run.repo, {'id': tid, 'scope': [f'src/{tid}.py'],
                                  'resources': {'ram_mb': 1}},
                [followups.FollowUp(f'{tid} independent polish', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')
            staged = backlog.stage(run.repo, previous, {'DONE-A', 'DONE-B'},
                                   at='2026-10-27T00:00:00+00:00')
            previous.extend(staged)
            roadmap.add_tasks(staged, 'earlier polish', 'polish')
        assert HostReservationLedger().reserve_counted({'ram_mb': 2}, capacity={'ram_mb': 2},
            project_id='other-project', run_id='ram', writer_pid=os.getpid()).admission.admitted

    def poll(run):
        if run.polls == 1:
            assert len(_task_runs()) == 2
            assert _windows(run) == []
            tasks = taskgraph.scan_tasks(orchestrator.ROADMAP_FILE.read_text())
            for task in tasks:
                if task['id'] in ('DONE-A', 'DONE-B'):
                    task['resource'] = 'eval-cpu'
                else:
                    assert not task.get('resource'), 'raw polish rows must remain unchanged'
            orchestrator.ROADMAP_FILE.write_text(_roadmap_document(tasks))
            taskgraph.record_applied(run.repo, taskgraph.load_document(orchestrator.ROADMAP_FILE))
            for hold in HostReservationLedger().reservations():
                HostReservationLedger().release(hold.reservation_id)
            if restart:
                run.halt()
        else:
            run.halt()

    run = run_lane(_graph_scenario(f'sprint_live_peer_mutex_{restart}', before_run=before,
        tasks=[_task('DONE-A', status='complete'), _task('DONE-B', status='complete')],
        on_poll=poll))
    if restart:
        orchestrator.HALT_FILE.unlink()
        assert orchestrator._graph_main(state.read_state()) == 0
    assert len(_windows(run)) == 1, 'one live polish owner must progress while its peer waits'
    [owner] = state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchall()
    owner_id = owner['run_id']
    # Reopening the controller cannot transfer ownership to the waiting peer.
    orchestrator.HALT_FILE.unlink()
    assert orchestrator._graph_main(state.read_state()) == 0
    assert len(_windows(run)) == 1
    assert state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchone()['run_id'] == owner_id
    # Cancel through the real mailbox while this next controller stays alive.
    # Its existing run-label map must release the owner on the following poll.
    cancel = []
    def cancel_poll(current):
        if not cancel:
            cancel.append(state.control_store().enqueue('graph_cancel', {
                'task_id': instances[owner_id].revision.task_id},
                idempotency_key='release-mutex-owner'))
        else:
            current.halt()
    run.scenario.on_poll = cancel_poll
    orchestrator.HALT_FILE.unlink()
    assert orchestrator._graph_main(state.read_state()) == 0
    assert state.control_store().command(cancel[0].command_id).status == 'committed'
    assert len(_windows(run)) == 2
    assert state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchone()['run_id'] != owner_id


@pytest.mark.parametrize('restart', [False, True])
def test_disjoint_pending_polish_mutexes_can_converge_and_release_to_a_peer(
        run_lane, monkeypatch, restart):
    from maestro import scheduling, taskgraph
    from maestro.docs import roadmap
    from maestro.workflows import runner as wf_runner
    from .conftest import _roadmap_document
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 2, 'cpu': 8, 'vram_mb': 0}))
    instances = {}
    tick = wf_runner.WorkflowRunner.tick
    def observed_tick(self, *args, **kwargs):
        instances[self.run_id] = self
        return tick(self, *args, **kwargs)
    monkeypatch.setattr(wf_runner.WorkflowRunner, 'tick', observed_tick)

    def before(run):
        _create_control_db(run)
        previous = []
        for tid in ('DONE-A', 'DONE-B'):
            backlog.add(run.repo, {'id': tid, 'scope': [f'src/{tid}.py'],
                                  'resources': {'ram_mb': 1}},
                [followups.FollowUp(f'{tid} independent polish', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')
            staged = backlog.stage(run.repo, previous, {'DONE-A', 'DONE-B'},
                                   at='2026-10-27T00:00:00+00:00')
            previous.extend(staged)
            roadmap.add_tasks(staged, 'earlier polish', 'polish')
        assert HostReservationLedger().reserve_counted({'ram_mb': 2}, capacity={'ram_mb': 2},
            project_id='foreign-project', run_id='ram', writer_pid=os.getpid()).admission.admitted

    def poll(run):
        if run.polls in (1, 2):
            assert len(_task_runs()) == 2
            assert _windows(run) == []
            if run.polls == 2:
                owners = {label: row['run_id'] for row in state.read_state()['graph_runs']
                          for label in row.get('mutex_labels', [])}
                assert set(owners) == {'x', 'y'} and owners['x'] != owners['y']
            tasks = taskgraph.scan_tasks(orchestrator.ROADMAP_FILE.read_text())
            for task in tasks:
                if task['id'] in ('DONE-A', 'DONE-B'):
                    task['resource'] = (['x', 'y'] if run.polls == 2
                                        else ('x' if task['id'] == 'DONE-A' else 'y'))
                else:
                    assert not task.get('resource'), 'raw polish mutex rows stay empty'
            orchestrator.ROADMAP_FILE.write_text(_roadmap_document(tasks))
            taskgraph.record_applied(run.repo, taskgraph.load_document(orchestrator.ROADMAP_FILE))
            if run.polls == 2:
                for hold in HostReservationLedger().reservations():
                    if hold.project_id == 'foreign-project':
                        HostReservationLedger().release(hold.reservation_id)
                if restart:
                    run.halt()
        else:
            run.halt()

    run = run_lane(_graph_scenario(f'sprint_converging_mutexes_{restart}', before_run=before,
        tasks=[_task('DONE-A', status='complete'), _task('DONE-B', status='complete')],
        on_poll=poll))
    if restart:
        orchestrator.HALT_FILE.unlink()
        assert orchestrator._graph_main(state.read_state()) == 0
    assert len(_windows(run)) == 1, 'converging idle owners must allow exactly one useful launch'
    [claimed] = state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchall()
    owner = claimed['run_id']
    assert {row['run_id'] for row in state.read_state()['graph_runs']
            if set(row.get('mutex_labels', [])) == {'x', 'y'}} == {owner}
    # Use the real mailbox to release the owner, then require its peer to progress
    # in the same controller rather than silently parking both contenders.
    cancel = []
    def cancel_poll(current):
        if not cancel:
            cancel.append(state.control_store().enqueue('graph_cancel', {
                'task_id': instances[owner].revision.task_id},
                idempotency_key='release-converged-mutex-owner'))
        else:
            current.halt()
    run.scenario.on_poll = cancel_poll
    orchestrator.HALT_FILE.unlink()
    assert orchestrator._graph_main(state.read_state()) == 0
    assert state.control_store().command(cancel[0].command_id).status == 'committed'
    assert len(_windows(run)) == 2
    assert state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchone()['run_id'] != owner


@pytest.mark.parametrize('restart', [False, True])
def test_idle_polish_mutex_owner_survives_retry_and_controller_restart(
        run_lane, monkeypatch, restart):
    from maestro import scheduling, taskgraph
    from maestro.docs import roadmap
    from maestro.workflows import runner as wf_runner
    from .conftest import _roadmap_document
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 2, 'cpu': 8, 'vram_mb': 0}))
    instances, selected = {}, {}
    tick = wf_runner.WorkflowRunner.tick
    def observed_tick(self, *args, **kwargs):
        instances[self.run_id] = self
        return tick(self, *args, **kwargs)
    monkeypatch.setattr(wf_runner.WorkflowRunner, 'tick', observed_tick)

    def before(run):
        _create_control_db(run)
        previous = []
        for tid, ram in (('DONE-A', 2), ('DONE-B', 1)):
            backlog.add(run.repo, {'id': tid, 'scope': [f'src/{tid}.py'],
                                  'resources': {'ram_mb': ram}},
                [followups.FollowUp(f'{tid} independent polish', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')
            staged = backlog.stage(run.repo, previous, {'DONE-A', 'DONE-B'},
                                   at='2026-10-27T00:00:00+00:00')
            previous.extend(staged)
            roadmap.add_tasks(staged, 'earlier polish', 'polish')
        hold = HostReservationLedger().reserve_counted({'ram_mb': 1}, capacity={'ram_mb': 2},
            project_id='foreign-project', run_id='ram', writer_pid=os.getpid())
        assert hold.admission.admitted
        selected['foreign'] = hold.reservation.reservation_id

    def poll(run):
        store = state.control_store()
        if run.polls == 1:
            rows = _task_runs()
            assert len(rows) == 2
            assert len(_windows(run)) == 1
            selected['owner'] = store.conn.execute(
                "SELECT run_id FROM attempts WHERE status='claimed'").fetchone()['run_id']
            assert instances[selected['owner']].revision.task_id == 'polish-2-a'
            # Make A unambiguously older than the admitted B, rather than depending
            # on UUID ordering when the synthetic clock opens both together.
            for row in rows:
                store.conn.execute('UPDATE task_runs SET started_at=? WHERE run_id=?',
                    ('2026-10-27T00:00:02+00:00' if row['run_id'] == selected['owner']
                     else '2026-10-27T00:00:01+00:00', row['run_id']))
            store.conn.commit()
            tasks = taskgraph.scan_tasks(orchestrator.ROADMAP_FILE.read_text())
            for task in tasks:
                if task['id'] in ('DONE-A', 'DONE-B'):
                    task['resource'] = 'eval-cpu'
                else:
                    assert not task.get('resource')
            orchestrator.ROADMAP_FILE.write_text(_roadmap_document(tasks))
            taskgraph.record_applied(run.repo, taskgraph.load_document(orchestrator.ROADMAP_FILE))
        elif run.polls == 2:
            assert len(_windows(run)) == 1
            instances[selected['owner']].operator_retry('implementer')
            assert store.conn.execute(
                "SELECT phase FROM node_states WHERE run_id=? AND node_id='implementer'",
                (selected['owner'],)).fetchone()['phase'] == 'pending'
            assert store.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE status IN ('claimed','running')"
            ).fetchone()[0] == 0
            HostReservationLedger().release(selected['foreign'])
            if restart:
                run.halt()
        else:
            run.halt()

    run = run_lane(_graph_scenario(f'sprint_idle_mutex_owner_{restart}', before_run=before,
        tasks=[_task('DONE-A', status='complete'), _task('DONE-B', status='complete')],
        on_poll=poll))
    if restart:
        orchestrator.HALT_FILE.unlink()
        assert orchestrator._graph_main(state.read_state()) == 0
    assert len(_windows(run)) == 2, 'the retained owner must make useful retry progress'
    [claimed] = state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchall()
    assert claimed['run_id'] == selected['owner'], 'restart must retain the idle mutex owner'
    assert state.control_store().conn.execute(
        'SELECT COUNT(*) FROM attempts WHERE run_id != ?', (selected['owner'],)
    ).fetchone()[0] == 0, 'the older waiting peer cannot take ownership after retry'


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('protected', ['active_polish', 'idle_ordinary'])
def test_converging_polish_mutexes_preserve_protected_owner_and_release_to_peer(
        run_lane, monkeypatch, restart, protected):
    from maestro import scheduling, taskgraph
    from maestro.docs import roadmap
    from maestro.workflows import runner as wf_runner
    from .conftest import _roadmap_document
    from .test_dispatch_wiring import _windows

    monkeypatch.setattr(orchestrator, '_host_capacity', lambda: scheduling.HostCapacity(
        limits={'ram_mb': 3, 'cpu': 8, 'vram_mb': 0}))
    instances, selected = {}, {}
    tick = wf_runner.WorkflowRunner.tick
    def observed_tick(self, *args, **kwargs):
        instances[self.run_id] = self
        return tick(self, *args, **kwargs)
    monkeypatch.setattr(wf_runner.WorkflowRunner, 'tick', observed_tick)

    def before(run):
        _create_control_db(run)
        previous = []
        sources = [('DONE-A', 2), ('DONE-B', 1)] if protected == 'active_polish' else [('DONE', 1)]
        for tid, ram in sources:
            backlog.add(run.repo, {'id': tid, 'scope': [f'src/{tid}.py'],
                                  'resources': {'ram_mb': ram}},
                [followups.FollowUp(f'{tid} independent polish', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')
            staged = backlog.stage(run.repo, previous, {name for name, _ in sources},
                                   at='2026-10-27T00:00:00+00:00')
            previous.extend(staged)
            roadmap.add_tasks(staged, 'earlier polish', 'polish')
        hold = HostReservationLedger().reserve_counted(
            {'ram_mb': 2 if protected == 'active_polish' else 3}, capacity={'ram_mb': 3},
            project_id='foreign-project', run_id='ram', writer_pid=os.getpid())
        assert hold.admission.admitted
        selected['foreign'] = hold.reservation.reservation_id

    def poll(run):
        store = state.control_store()
        if run.polls == 1:
            rows = _task_runs()
            assert len(rows) == 2
            if protected == 'active_polish':
                assert len(_windows(run)) == 1
                selected['owner'] = store.conn.execute(
                    "SELECT run_id FROM attempts WHERE status='claimed'").fetchone()['run_id']
                assert instances[selected['owner']].revision.task_id == 'polish-2-a'
                # The active owner is deliberately younger than the idle peer.
                for row in rows:
                    store.conn.execute('UPDATE task_runs SET started_at=? WHERE run_id=?',
                        ('2026-10-27T00:00:02+00:00' if row['run_id'] == selected['owner']
                         else '2026-10-27T00:00:01+00:00', row['run_id']))
                store.conn.commit()
            else:
                assert _windows(run) == []
                selected['owner'] = next(row['run_id'] for row in rows if row['task_id'] == 'ORDINARY')
        if run.polls == 1 or (run.polls == 2 and protected == 'active_polish'):
            tasks = taskgraph.scan_tasks(orchestrator.ROADMAP_FILE.read_text())
            for task in tasks:
                if task['id'].startswith('DONE'):
                    task['resource'] = (['x', 'y'] if protected == 'idle_ordinary' or run.polls == 2
                                        else ('y' if task['id'] == 'DONE-A' else 'x'))
                elif task.get('polish_items'):
                    assert not task.get('resource'), 'only current source requirements change'
            orchestrator.ROADMAP_FILE.write_text(_roadmap_document(tasks))
            taskgraph.record_applied(run.repo, taskgraph.load_document(orchestrator.ROADMAP_FILE))
            if protected == 'idle_ordinary' or run.polls == 2:
                HostReservationLedger().release(selected['foreign'])
                if restart:
                    run.halt()
        else:
            run.halt()

    tasks = ([_task('DONE-A', status='complete'), _task('DONE-B', status='complete')]
             if protected == 'active_polish' else [
                 _task('ORDINARY', resource=['x', 'y'], resources={'ram_mb': 2},
                       scope=['src/ordinary.py']),
                 _task('DONE', status='complete')])
    run = run_lane(_graph_scenario(f'sprint_protected_mutex_{protected}_{restart}',
        before_run=before, tasks=tasks, on_poll=poll))
    if restart:
        orchestrator.HALT_FILE.unlink()
        assert orchestrator._graph_main(state.read_state()) == 0
    assert len(_windows(run)) == 1, 'a protected owner must exclude a peer even when RAM fits both'
    [claimed] = state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchall()
    assert claimed['run_id'] == selected['owner']
    assert {row['run_id'] for row in state.read_state()['graph_runs']
            if set(row.get('mutex_labels', [])) == {'x', 'y'}} == {selected['owner']}
    cancel = []
    def cancel_poll(current):
        if not cancel:
            cancel.append(state.control_store().enqueue('graph_cancel', {
                'task_id': instances[selected['owner']].revision.task_id},
                idempotency_key='release-protected-mutex-owner'))
        else:
            current.halt()
    run.scenario.on_poll = cancel_poll
    orchestrator.HALT_FILE.unlink()
    assert orchestrator._graph_main(state.read_state()) == 0
    assert state.control_store().command(cancel[0].command_id).status == 'committed'
    assert len(_windows(run)) == 2, 'cancellation must release the bundle for the waiting peer'
    [peer] = state.control_store().conn.execute(
        "SELECT run_id FROM attempts WHERE status='claimed'").fetchall()
    assert peer['run_id'] != selected['owner']


def test_live_priority_command_reaches_the_same_waiting_runs_next_budget_transaction(
        run_lane, monkeypatch):
    import subprocess
    from maestro import config
    from maestro.hitl import commands
    from maestro.workflows import runner as wf_runner
    from .test_dispatch_wiring import _windows

    instances, first = [], {}
    tick = wf_runner.WorkflowRunner.tick
    def observed_tick(self, *args, **kwargs):
        instances.append(self)
        return tick(self, *args, **kwargs)
    monkeypatch.setattr(wf_runner.WorkflowRunner, 'tick', observed_tick)

    def git(repo, *args):
        return subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
            '-c', 'user.name=W5 synthetic', '-c', 'user.email=w5@example.invalid', *args],
            check=True, capture_output=True, text=True).stdout.strip()

    def before(run):
        _create_control_db(run)
        git(run.repo, 'init')
        git(run.repo, 'add', 'project.yaml')
        git(run.repo, 'commit', '-m', 'synthetic initial priority')
        # The actual priority command commits inside this owned synthetic repo.
        monkeypatch.setenv('GIT_AUTHOR_NAME', 'W5 synthetic')
        monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'w5@example.invalid')
        monkeypatch.setenv('GIT_COMMITTER_NAME', 'W5 synthetic')
        monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'w5@example.invalid')
        monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
        monkeypatch.setenv('GIT_CONFIG_VALUE_0', '/dev/null')
        orchestrator.time._now = time.time()
        now = orchestrator.time.time()
        ledger = HostReservationLedger()
        held = ledger.reserve_budget('fixture-pool', {'account:300': 1},
            capacity={'account:300': 4}, resets={'account:300': now+3600},
            project_id='foreign-project', run_id='foreign-live', writer_pid=os.getpid())
        assert held.admission.admitted
        first['foreign'] = held.reservation.to_dict()
        assert not ledger.reserve_budget('fixture-pool', {'account:300': 1},
            capacity={'account:300': 0}, resets={'account:300': now+3600},
            project_id='foreign-project', run_id='foreign-next', writer_pid=os.getpid()).admission.admitted

    def routing():
        now = orchestrator.time.time()
        result = fixture_routing(policy=default_policy().with_objective('pace'))
        result.resources.pool('fixture-pool').observation = cat.QuotaObservation('fixture-pool', {
            300: cat.QuotaWindow(300, 88, now+3600)},
            observed_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
        result.pace_estimates = {'*': PaceEstimate(burn_pct_per_cost=3)}
        return result

    def deliver(weight, update_id):
        commands._dispatch_delivery({'update_id': update_id, 'message': {
            'chat': {'id': 42}, 'text': f'/priority {weight}'}}, [], '42')
        assert config.project_priority() == weight
        assert state.control_store().conn.execute(
            'SELECT status FROM command_inbox WHERE idempotency_key=?',
            (f'telegram:update:{update_id}',)).fetchone()['status'] == 'committed'

    def poll(run):
        # Keep the synthetic loop and real ledger clocks aligned across polls.
        orchestrator.time._now = time.time()
        if run.polls == 1:
            assert state.read_state()['admission_waiting']['WAITING']['reason'] == 'budget_fair_share'
            assert _windows(run) == []
            first['run_id'] = _task_runs()[0]['run_id']
            first['event'] = [e.payload for e in state.control_store().events(
                aggregate_id=first['run_id']) if e.type == 'PaceDecisionRecorded'][0]
            old_head = git(run.repo, 'rev-parse', 'HEAD')
            deliver(3, 701)
            assert git(run.repo, 'rev-parse', 'HEAD') != old_head
        elif run.polls == 2:
            rows = state.control_store().conn.execute('SELECT binding_json FROM attempts').fetchall()
            if rows:
                first['binding'] = rows[0]['binding_json']
                deliver(1, 702)
            else:
                run.halt()
        else:
            run.halt()

    run = run_lane(_graph_scenario('live_priority_waiting_run', before_run=before, routing=routing,
        on_poll=poll, tasks=[_task('WAITING')], project_yaml={
            'engineering': {'runner': 'graph'}, 'priority': 1,
            'routing': {'pace': {'initial_burn_pct_per_cost': 3}}}))
    rows = state.control_store().conn.execute('SELECT * FROM attempts').fetchall()
    assert len(rows) == 1, 'the existing budget waiter must use the committed priority without restart'
    assert rows[0]['run_id'] == first['run_id']
    assert len({id(instance) for instance in instances}) == 1
    assert len({id(instance.routing) for instance in instances}) == 1
    assert len(_windows(run)) == 1
    events = [e.payload for e in state.control_store().events(aggregate_id=first['run_id'])
              if e.type == 'PaceDecisionRecorded']
    assert events[0] == first['event']
    assert events[0]['decision']['stages'][-1]['inputs']['priority'] == 1
    admitted = json.loads(rows[0]['binding_json'])
    assert admitted['pace_decision']['stages'][-1]['inputs']['priority'] == 3
    assert rows[0]['binding_json'] == first['binding'], 'later priority changes cannot reprice admission'
    assert config.project_priority() == 1
    [foreign] = [r for r in HostReservationLedger().reservations() if r.run_id == 'foreign-live']
    assert foreign.to_dict() == first['foreign']
