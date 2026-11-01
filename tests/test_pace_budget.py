"""B7 budget accounting through real host coordination and graph dispatch."""
from datetime import datetime, timezone
import json
import os
import pytest

from maestro.backends import catalog as cat
from maestro.control.resources import HostReservationLedger
from maestro.taskgraph import normalize_spec
from maestro.workflows.policy import default_policy
from tests.graph_engineering.conftest import fixture_routing
from tests.workflows.test_w2_runtime import runtime


def pace_routing(*, used=90.5):
    routing = fixture_routing(policy=default_policy().with_objective('pace'))
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-budget', 'kind': 'auto', 'cmd': 'true'}]})
    now = datetime.now(timezone.utc)
    routing.resources.observe(cat.QuotaObservation(pool_id='fixture-pool',
        observed_at=now.isoformat(), windows={300: cat.QuotaWindow(300, used,
            now.timestamp() + 1800)}))
    return routing


def test_selected_route_holds_cost_and_next_attempt_waits_before_claim(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    store, make = runtime
    first, attempt, _ = make(routing=pace_routing())
    assert attempt is not None
    holds = HostReservationLedger().reservations()
    assert len(holds) == 1, 'selected pace route must hold host-wide budget before launch'
    assert holds[0].run_id == attempt['attempt_id']
    second, attempt2, node2 = make(routing=pace_routing())
    assert attempt2 is None, 'another attempt cannot spend the same remaining headroom'
    assert 'budget_exhausted' in second.tick().waiting[node2.node_id]
    assert store.conn.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?',
                              (second.run_id,)).fetchone()[0] == 0


def test_restarted_runner_reasserts_frozen_charge_and_terminal_result_releases(runtime, monkeypatch, tmp_path):
    from maestro.workflows.runner import WorkflowRunner
    from tests.workflows.test_w2_runtime import finish
    path = tmp_path / 'host.json'
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(path))
    store, make = runtime
    run, attempt, _ = make(routing=pace_routing())
    original = HostReservationLedger().reservations()[0]
    frozen = json.loads(attempt['binding_json'])['pace_budget']
    assert frozen['amounts'] == original.amounts
    path.unlink()  # coordination file is reconstructible from the durable attempt
    restarted = WorkflowRunner(store, run.revision, run_id=run.run_id,
        repo=run.repo, routing=pace_routing(used=91.9), dispatcher=run.dispatcher,
        artifacts=run.artifacts, is_alive=lambda pid: True)
    restarted.tick()
    restored = HostReservationLedger().reservations()
    assert len(restored) == 1, 'restart must reassert running selected cost without readmission'
    assert restored[0].amounts == original.amounts
    finish(restarted, attempt, status='succeeded')
    assert HostReservationLedger().reservations() == []


@pytest.mark.parametrize('invalid_yaml', ['true', '0', '.nan'])
def test_invalid_current_priority_cannot_fall_back_for_an_existing_budget_waiter(
        runtime, monkeypatch, tmp_path, invalid_yaml):
    from maestro import config

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    monkeypatch.setenv('MAESTRO_GLOBAL_CONFIG', str(tmp_path / 'no-global.yaml'))
    project = tmp_path / 'project.yaml'
    monkeypatch.setattr(config, 'PROJECT_YAML', project)
    project.write_text('priority: 1\n')
    store, make = runtime
    dispatch = pace_routing()
    dispatch.project_priority_reader = config.project_priority
    admitted, first, _ = make(routing=dispatch)
    waiter, pending, node = make(routing=dispatch)
    assert pending is None
    original_binding = first['binding_json']
    original_decisions = [e.payload for e in store.events() if e.type == 'PaceDecisionRecorded']
    original_hold = HostReservationLedger().reservations()[0]
    launches = []
    monkeypatch.setattr(waiter.dispatcher, 'dispatch', lambda *args, **kwargs: launches.append(args))
    project.write_text(f'priority: {invalid_yaml}\n')
    waiter.tick()
    # ConfigError follows the established failed audit path, never admission at
    # the cached valid weight, and never a new launched operation.
    [refused] = store.conn.execute('SELECT * FROM attempts WHERE run_id=?',
                                  (waiter.run_id,)).fetchall()
    assert refused['status'] == 'failed'
    assert 'ConfigError: priority must be a positive finite number' in refused['route_refusal']
    [intent] = store.intents_for_attempt(refused['attempt_id'])
    assert intent['status'] == 'failed'
    assert refused['binding_json'] is None and launches == []
    assert store.conn.execute('SELECT binding_json FROM attempts WHERE attempt_id=?',
                              (first['attempt_id'],)).fetchone()[0] == original_binding
    assert [e.payload for e in store.events() if e.type == 'PaceDecisionRecorded'] == original_decisions
    [held] = HostReservationLedger().reservations()
    assert held.run_id == first['attempt_id']
    assert held.amounts == original_hold.amounts and held.budget == original_hold.budget
    assert admitted.run_id != waiter.run_id


def test_nonpace_admission_does_not_read_invalid_current_project_priority(
        runtime, monkeypatch, tmp_path):
    from maestro import config

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    monkeypatch.setenv('MAESTRO_GLOBAL_CONFIG', str(tmp_path / 'no-global.yaml'))
    project = tmp_path / 'project.yaml'
    project.write_text('priority: true\n')
    monkeypatch.setattr(config, 'PROJECT_YAML', project)
    dispatch = pace_routing()
    dispatch.policy = default_policy()
    calls = []
    def current_priority():
        calls.append(True)
        return config.project_priority()
    dispatch.project_priority_reader = current_priority
    _, make = runtime
    _, attempt, _ = make(routing=dispatch)
    assert attempt is not None and attempt['status'] == 'claimed'
    assert calls == []
    binding = json.loads(attempt['binding_json'])
    assert binding.get('pace_budget') is None and binding.get('pace_decision') is None
    assert HostReservationLedger().reservations() == []


def budget_hold(ledger, attempt, *, project='high', priority=3, charge=1, cap=4, ttl=10000):
    return ledger.reserve_budget('shared', {'account:300': charge},
        capacity={'account:300': cap}, resets={'account:300': ledger.clock() + 1800},
        project_id=project, run_id=attempt, writer_pid=os.getpid(),
        priority=priority, ttl_sec=ttl)


def test_contending_projects_get_priority_weighted_share_before_a_fifth_claim(tmp_path):
    ledger = HostReservationLedger(tmp_path / 'host.json')
    # Both projects register actual demand while no headroom is available.
    assert not budget_hold(ledger, 'high-0', cap=0).admission.admitted
    assert not budget_hold(ledger, 'low-0', project='low', priority=1, cap=0).admission.admitted
    for index in range(3):
        assert budget_hold(ledger, f'high-{index}').admission.admitted
    fourth = budget_hold(ledger, 'high-3')
    assert not fourth.admission.admitted, '3:1 priority must preserve a waiting peer share'
    assert fourth.admission.reason == 'budget_fair_share'
    assert budget_hold(ledger, 'low-0', project='low', priority=1).admission.admitted


def test_peers_whose_requests_exceed_their_share_do_not_block_each_other(tmp_path):
    clock = [1000.0]
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: clock[0])
    # Equal-priority A and B wait behind a third project's hold.
    assert budget_hold(ledger, 'c-0', project='c', priority=1, charge=10, cap=10).admission.admitted
    assert not budget_hold(ledger, 'a-0', project='a', priority=1, charge=6, cap=10).admission.admitted
    assert not budget_hold(ledger, 'b-0', project='b', priority=1, charge=6, cap=10).admission.admitted
    ledger.release_budget('c', 'c-0')
    clock[0] += 30
    # Each fair share is 5% but each needs 6%: neither peer could ever be
    # admitted inside its own share, so one must take the free headroom now
    # rather than both waiting for the 1800 s starvation guard.
    first = budget_hold(ledger, 'a-0', project='a', priority=1, charge=6, cap=10)
    assert first.admission.admitted, first.admission.reason


@pytest.mark.parametrize('field,value', [('priority', True), ('charge', True), ('cap', 101)])
def test_budget_rejects_non_numeric_weights_and_impossible_percentages(tmp_path, field, value):
    ledger = HostReservationLedger(tmp_path / 'host.json')
    with pytest.raises(ValueError):
        budget_hold(ledger, 'bad', **{field: value})
    assert ledger.reservations() == []


def test_waiting_age_increases_share_and_old_large_attempt_eventually_drains(tmp_path):
    clock = [1000.0]
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: clock[0])
    assert budget_hold(ledger, 'high-0').admission.admitted
    assert budget_hold(ledger, 'high-1').admission.admitted
    assert not budget_hold(ledger, 'low-large', project='low', priority=1, charge=4).admission.admitted
    clock[0] += 600
    assert not budget_hold(ledger, 'high-2').admission.admitted, 'waiting priority must age'
    clock[0] += 1200
    ledger.release_budget('high', 'high-0')
    ledger.release_budget('high', 'high-1')
    # An oldest continuously waiting attempt may need more than its fractional
    # share. Starvation protection drains room rather than backfilling forever.
    protected = budget_hold(ledger, 'high-3')
    assert not protected.admission.admitted
    assert protected.admission.reason == 'budget_starvation_guard'
    assert budget_hold(ledger, 'low-large', project='low', priority=1, charge=4).admission.admitted


def test_legacy_sync_heartbeat_live_retention_dead_purge_and_wait_expiry(tmp_path):
    clock = [1000.0]
    alive = {123, 456}
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: clock[0],
                                   is_alive=lambda pid: pid in alive)
    args = dict(capacity={'account:300': 1}, resets={'account:300': 2800},
                project_id='p', writer_pid=123, ttl_sec=10)
    held = ledger.reserve_budget('shared', {'account:300': 1}, run_id='a', **args).reservation
    assert not ledger.reserve_budget('shared', {'account:300': 1}, run_id='b', **args).admission.admitted
    clock[0] += 20
    ledger.sync_project('p', {}, writer_pid=123)
    rows = ledger.reservations()
    assert [r.run_id for r in rows] == ['a'], 'live cost survives expiry; abandoned demand does not'
    assert ledger.heartbeat(held.reservation_id)
    frozen = dict(pool_id='shared', amounts=held.amounts, **held.budget)
    ledger.sync_budget('p', {'a': frozen}, writer_pid=456)
    assert ledger.reservations()[0].writer_pid == 456
    alive.remove(456)
    assert ledger.reconcile() == [held.reservation_id]
    assert ledger.reservations() == []


def test_pools_account_and_model_scopes_keep_independent_percent_constraints(tmp_path):
    ledger = HostReservationLedger(tmp_path / 'host.json')
    assert budget_hold(ledger, 'a', charge=1, cap=10).admission.admitted
    base = dict(capacity={'account:300': 10, 'scope:opus:10080': 1},
        resets={'account:300': ledger.clock() + 1800, 'scope:opus:10080': ledger.clock() + 7200},
        writer_pid=os.getpid(), project_id='p')
    assert ledger.reserve_budget('scoped', {'account:300': 1, 'scope:opus:10080': 1},
        run_id='opus1', **base).admission.admitted
    refused = ledger.reserve_budget('scoped', {'account:300': 1, 'scope:opus:10080': 1},
        run_id='opus2', **base)
    assert not refused.admission.admitted and 'scope:opus:10080' in refused.admission.detail
    assert ledger.reserve_budget('scoped', {'account:300': 1},
        capacity={'account:300': 10}, resets={'account:300': ledger.clock()+1800},
        writer_pid=os.getpid(), project_id='p', run_id='sonnet').admission.admitted


def _budget_contender(path, project, barrier, release, result):
    ledger = HostReservationLedger(path)
    barrier.wait(timeout=10)
    answer = budget_hold(ledger, project, project=project, priority=1, cap=1)
    result.put((project, answer.admission.admitted))
    release.wait(timeout=10)
    ledger.release_budget(project, project)


def test_two_real_project_processes_cannot_spend_the_same_last_percent(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context('fork')
    barrier, release, result = context.Barrier(2), context.Event(), context.Queue()
    path = str(tmp_path / 'host.json')
    processes = [context.Process(target=_budget_contender,
        args=(path, project, barrier, release, result)) for project in ('project-a', 'project-b')]
    try:
        for process in processes:
            process.start()
        answers = [result.get(timeout=10) for _ in processes]
        assert sum(admitted for _, admitted in answers) == 1
        held = [r for r in HostReservationLedger(path).reservations() if r.budget['state'] == 'held']
        assert sum(r.amounts['account:300'] for r in held) == 1
    finally:
        release.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
    assert all(process.exitcode == 0 for process in processes)
    assert HostReservationLedger(path).reservations() == []


def test_atomic_percent_accounting_burn_once_and_reset_monotonicity(tmp_path):
    from maestro.workflows.pace import PaceEstimate
    now = 1000
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: now)
    estimate = PaceEstimate(burn_pct_per_cost=2, runtime_sec=1800)
    def fits(cost, used, horizon):
        charge = estimate.charge_pct(cost, now + horizon, now)
        answer = ledger.reserve_budget('shared', {'account:300': charge},
            capacity={'account:300': 92-used}, resets={'account:300': now+horizon},
            project_id='p', run_id='grid', writer_pid=os.getpid())
        ledger.release_budget('p', 'grid')
        return answer.admission.admitted
    # 4 * 2 * .5 = 4%, exactly the available headroom; do not burn a second time.
    assert fits(4, 88, 900)
    assert not fits(4.01, 88, 900)
    costs = [.1, 1, 2, 4, 8]
    used_grid = [0, 50, 85, 88, 90, 91.9, 92]
    horizon_grid = [1800, 900, 300, 60, 1]
    admitted = {(used, horizon): sum(fits(cost, used, horizon) for cost in costs)
                for used in used_grid for horizon in horizon_grid}
    for horizon in horizon_grid:
        assert [admitted[used, horizon] for used in used_grid] == sorted(
            (admitted[used, horizon] for used in used_grid), reverse=True)
    for used in used_grid:
        assert [admitted[used, horizon] for horizon in horizon_grid] == sorted(
            admitted[used, horizon] for horizon in horizon_grid)


@pytest.mark.parametrize('kind', ['success', 'failed', 'quota_exhausted', 'operator_cancel'])
def test_actual_terminal_paths_release_only_their_attempt_budget(runtime, monkeypatch, tmp_path, kind):
    from tests.workflows.test_w2_runtime import finish
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    _, make = runtime
    run, attempt, _ = make(routing=pace_routing(used=10))
    other, peer, _ = make(routing=pace_routing(used=10))
    if kind == 'operator_cancel':
        run.operator_cancel()
    else:
        finish(run, attempt, status='succeeded' if kind == 'success' else 'failed',
            outputs={'failure': {'kind': kind}} if kind == 'quota_exhausted' else {})
    held = [r for r in HostReservationLedger().reservations() if r.budget['state'] == 'held']
    assert [r.run_id for r in held] == [peer['attempt_id']]


def test_real_lease_claim_refusal_releases_the_preclaim_budget(runtime, monkeypatch, tmp_path):
    from maestro.workflows.routing import DispatchRouting
    from maestro.workflows.runner import WorkflowRunner
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    reserve = DispatchRouting.reserve_selected
    def contend(routing, runner, node, binding, now):
        answer = reserve(routing, runner, node, binding, now)
        runner.store.claim_lease('writer:race', 'foreign-attempt', writer_pid=os.getpid(),
                                ttl_seconds=3600, is_alive=lambda pid: True)
        return answer
    monkeypatch.setattr(DispatchRouting, 'reserve_selected', contend)
    monkeypatch.setattr(WorkflowRunner, 'resource_id_for', lambda runner, node: 'writer:race')
    _, make = runtime
    run, attempt, _ = make(routing=pace_routing())
    assert attempt is None
    assert HostReservationLedger().reservations() == []


def test_controller_failure_before_claim_releases_cost_in_the_same_tick(runtime, monkeypatch, tmp_path):
    from maestro.workflows.routing import DispatchRouting
    from maestro.workflows.runner import WorkflowRunner
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    reserve = DispatchRouting.reserve_selected
    def contend(routing, runner, node, binding, now):
        answer = reserve(routing, runner, node, binding, now)
        assert answer.admission.admitted
        return answer
    monkeypatch.setattr(DispatchRouting, 'reserve_selected', contend)
    def broken_claim(*args, **kwargs):
        raise RuntimeError('synthetic preclaim controller fault')
    monkeypatch.setattr(WorkflowRunner, 'claim', broken_claim)
    _, make = runtime
    run, attempt, node = make(routing=pace_routing())
    assert attempt is None
    assert HostReservationLedger().reservations() == [], 'a failed claim has no cost consumer'


def test_actual_failed_worker_launch_releases_the_selected_budget(runtime, monkeypatch, tmp_path):
    from maestro.workflows.handlers import TmuxDispatcher, Dispatched
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'host.json'))
    monkeypatch.setattr(TmuxDispatcher, 'dispatch', lambda self, window, argv, **kwargs:
        Dispatched(window=window, argv=tuple(argv), launched=False, detail='synthetic tmux refusal'))
    _, make = runtime
    run, attempt, node = make(routing=pace_routing())
    assert attempt['status'] == 'failed' and run.phases()[node.node_id] == 'failed'
    assert HostReservationLedger().reservations() == []


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('reset_after,elapsed,admitted', [
    (180, 181, False), (1620, 1621, True), (180, 1801, False),
    (180, 1, False),  # A fresh new-window deadline proves an early forced reset.
])
def test_live_charge_uses_remaining_new_window_exposure_without_losing_its_identity(
        runtime, monkeypatch, tmp_path, restart, reset_after, elapsed, admitted):
    from maestro.control import resources
    from maestro.workflows import runner as runner_module
    from maestro.workflows.runner import WorkflowRunner

    clock = [datetime.now(timezone.utc).timestamp()]
    path = tmp_path / 'host.json'
    ledger = HostReservationLedger(path, clock=lambda: clock[0], is_alive=lambda _: True)
    monkeypatch.setattr(resources, 'HostReservationLedger', lambda: ledger)
    monkeypatch.setattr(runner_module.time, 'time', lambda: clock[0])

    def observed(used, reset):
        routing = pace_routing(used=used)
        routing.resources.observe(cat.QuotaObservation(pool_id='fixture-pool',
            observed_at=datetime.fromtimestamp(clock[0], timezone.utc).isoformat(),
            windows={300: cat.QuotaWindow(300, used, reset)}))
        return routing

    _, make = runtime
    original_reset = clock[0] + reset_after
    run, attempt, _ = make(routing=observed(10, original_reset))
    assert attempt is not None
    original = ledger.reservations()[0]
    assert original.amounts['account:300'] == pytest.approx(reset_after / 1800)
    frozen = json.loads(attempt['binding_json'])['pace_budget']
    clock[0] += elapsed  # An actual rollover, followed by a new current observation.
    current = observed(90.8, clock[0] + 18000)
    if restart:
        path.unlink()
        restarted = WorkflowRunner(run.store, run.revision, run_id=run.run_id,
            repo=run.repo, routing=current, dispatcher=run.dispatcher,
            artifacts=run.artifacts, is_alive=lambda _: True)
        restarted.tick()
    _, next_attempt, _ = make(routing=current)
    assert (next_attempt is not None) is admitted, (
        'new-window admission must count remaining live exposure, with a full '
        'conservative hold when the writer outlives its runtime estimate')
    retained = next(row for row in ledger.reservations() if row.run_id == attempt['attempt_id'])
    assert retained.amounts == original.amounts
    assert retained.budget == original.budget
    assert json.loads(run.store.conn.execute('SELECT binding_json FROM attempts WHERE attempt_id=?',
        (attempt['attempt_id'],)).fetchone()[0])['pace_budget'] == frozen
    ledger.release_budget(str(run.repo), attempt['attempt_id'])
    assert all(row.run_id != attempt['attempt_id'] for row in ledger.reservations())


def test_legacy_live_hold_without_projection_stays_conservative_after_reset(tmp_path):
    clock = [1000.0]
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: clock[0])
    held = budget_hold(ledger, 'legacy', charge=1, cap=1).reservation
    clock[0] += 1801
    refused = budget_hold(ledger, 'next', charge=1, cap=1)
    assert not refused.admission.admitted
    assert next(row for row in ledger.reservations() if row.run_id == 'legacy') == held


@pytest.mark.parametrize('restart', [False, True])
def test_admitted_hold_recovery_and_release_follow_attempt_after_objective_changes(
        runtime, monkeypatch, tmp_path, restart):
    from maestro.workflows.runner import WorkflowRunner
    from tests.workflows.test_w2_runtime import finish

    path = tmp_path / 'host.json'
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(path))
    _, make = runtime
    routing = pace_routing(used=10)
    run, attempt, _ = make(routing=routing)
    frozen = HostReservationLedger().reservations()[0]
    routing.policy = default_policy().with_objective('paid_efficiency')
    if restart:
        path.unlink()
        run = WorkflowRunner(run.store, run.revision, run_id=run.run_id,
            repo=run.repo, routing=routing, dispatcher=run.dispatcher,
            artifacts=run.artifacts, is_alive=lambda _: True)
        run.tick()
        restored = HostReservationLedger().reservations()
        assert len(restored) == 1, 'a policy change cannot hide an already admitted live cost'
        assert restored[0].amounts == frozen.amounts and restored[0].budget == frozen.budget
    finish(run, attempt, status='succeeded')
    assert HostReservationLedger().reservations() == [], 'settlement follows the frozen attempt'


@pytest.mark.parametrize('drift', [1, 600])
def test_same_window_deadline_jitter_cannot_decay_a_live_frozen_hold(tmp_path, drift):
    clock = [1000.0]
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: clock[0])
    original = ledger.reserve_budget('shared', {'account:300': .9},
        capacity={'account:300': 2}, resets={'account:300': 2620},
        project_id='p', run_id='first', writer_pid=os.getpid(),
        projection=dict(started_at=1000, runtime_sec=1800, full_amounts={'account:300': 1})).reservation
    clock[0] = 1900
    deadline = 2620 + drift
    refused = ledger.reserve_budget('shared', {'account:300': (deadline-clock[0])/1800},
        capacity={'account:300': 1.25}, resets={'account:300': deadline},
        project_id='p', run_id='next', writer_pid=os.getpid())
    assert not refused.admission.admitted and refused.admission.reason == 'budget_exhausted'
    assert next(row for row in ledger.reservations() if row.run_id == 'first') == original


@pytest.mark.parametrize('full', [float('inf'), float('nan'), True, -1, 10**400])
def test_invalid_or_overflowing_full_projection_fails_before_any_hold(tmp_path, full):
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: 1000)
    with pytest.raises(ValueError, match='projection'):
        ledger.reserve_budget('shared', {'account:300': .1},
            capacity={'account:300': 1}, resets={'account:300': 1180},
            project_id='p', run_id='invalid', writer_pid=os.getpid(),
            projection=dict(started_at=1000, runtime_sec=1800, full_amounts={'account:300': full}))
    assert ledger.reservations() == []


def test_full_projection_must_agree_with_the_charge_for_its_recorded_window(tmp_path):
    ledger = HostReservationLedger(tmp_path / 'host.json', clock=lambda: 1000)
    with pytest.raises(ValueError, match='projection'):
        ledger.reserve_budget('shared', {'account:300': .1},
            capacity={'account:300': 1}, resets={'account:300': 1180},
            project_id='p', run_id='inconsistent', writer_pid=os.getpid(),
            projection=dict(started_at=1000, runtime_sec=1800,
                            full_amounts={'account:300': 100}))
    assert ledger.reservations() == []
