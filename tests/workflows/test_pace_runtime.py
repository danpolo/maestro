"""W5 behavior through the real claim, stored binding and restart seams."""
import json
from datetime import datetime, timezone

import pytest

from maestro.backends import catalog as cat
from maestro.taskgraph import normalize_spec
from maestro.workflows.policy import default_policy
from tests.graph_engineering.conftest import fixture_routing
from tests.workflows.test_w2_runtime import runtime  # shared real ControlStore/runner fixture


@pytest.mark.parametrize('waiting', [False, True])
def test_real_pace_admission_and_budget_wait_have_durable_stage_results(runtime, monkeypatch,
                                                                      tmp_path, waiting):
    from maestro.control.store import ControlStore
    from maestro.workflows.runner import WorkflowRunner
    from tests.test_pace_budget import pace_routing

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'journal-host.json'))
    store, make = runtime
    if waiting:
        make(routing=pace_routing())  # a real competing admitted attempt owns the headroom
    from maestro.workflows import routing as wf_routing
    calls = dict(resolve=0, reserve_selected=0, claim=0, session_decision=0)
    def count(owner, name):
        original = getattr(owner, name)
        def counted(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, counted)
    count(wf_routing, 'resolve')
    count(wf_routing.DispatchRouting, 'reserve_selected')
    count(WorkflowRunner, 'claim')
    count(wf_routing, 'session_decision')
    run, attempt, node = make(routing=pace_routing())
    assert calls == dict(resolve=1, reserve_selected=1,
                         claim=0 if waiting else 1, session_decision=0 if waiting else 1)
    with ControlStore.open_client(store.path) as reopened:
        events = [e for e in reopened.events(aggregate_id=run.run_id)
                  if e.type == 'PaceDecisionRecorded']
        assert len(events) == 1, 'every actual pace decision must survive reopening without an attempt'
        payload = events[0].payload
        decision = payload['decision']
        stages = decision['stages']
        assert [s['name'] for s in stages] == [
            'eligibility', 'soft_independence', 'five_hour', 'evidence', 'weekly_pace', 'budget']
        assert payload['run_id'] == run.run_id and payload['node_id'] == node.node_id
        assert payload['context']['snapshot_id']
        assert payload['policy_hash'] == run.routing.policy.policy_hash
        assert stages[-1]['inputs']['project_id'] == str(run.repo)
        assert stages[-1]['inputs']['amounts']['account:300'] > 0
        assert stages[-1]['inputs']['capacity']['account:300'] == pytest.approx(1.5)
        assert decision['selected']['pool_id'] == 'fixture-pool'
        if waiting:
            assert attempt is None
            assert payload['outcome'] == 'wait'
            assert stages[-1]['status'] == 'refused'
            assert stages[-1]['result']['reason'] == 'budget_exhausted'
            assert stages[-1]['result']['shared_exposure']['account:300'] > 0
            assert reopened.conn.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?',
                                         (run.run_id,)).fetchone()[0] == 0
            assert reopened.conn.execute('SELECT COUNT(*) FROM operation_intents WHERE attempt_id=?',
                                         (stages[-1]['inputs']['run_id'],)).fetchone()[0] == 0
        else:
            durable = reopened.conn.execute('SELECT binding_json FROM attempts WHERE attempt_id=?',
                                            (attempt['attempt_id'],)).fetchone()
            assert json.loads(durable[0])['pace_decision'] == decision
            assert stages[-1]['status'] == 'passed'
            assert stages[-1]['result']['reservation_id']
            assert stages[-1]['result']['admitted'] is True
            assert stages[-1]['inputs']['run_id'] == attempt['attempt_id']
    # Reconstruct the runner and retry a wait. Its first frozen event remains
    # byte-for-byte equivalent even when a later observation differs.
    restarted = WorkflowRunner(store, run.revision, run_id=run.run_id, repo=run.repo,
        routing=pace_routing(used=91), dispatcher=run.dispatcher, artifacts=run.artifacts,
        is_alive=lambda pid: True)
    restarted.tick()
    assert [e.payload for e in store.events(aggregate_id=run.run_id)
            if e.type == 'PaceDecisionRecorded'][0] == payload


@pytest.mark.parametrize('unknown', [False, True])
def test_real_quota_wait_is_durable_and_marks_unreached_stages(runtime, monkeypatch, tmp_path,
                                                             unknown):
    from maestro.control.store import ControlStore
    from maestro.workflows.runner import WorkflowRunner
    from tests.test_pace_budget import pace_routing

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'quota-host.json'))
    store, make = runtime
    dispatch = pace_routing(used=100)
    if unknown:
        dispatch.resources.pool('fixture-pool').observation = None
    run, attempt, node = make(routing=dispatch)
    assert attempt is None
    with ControlStore.open_client(store.path) as reopened:
        events = [e for e in reopened.events(aggregate_id=run.run_id)
                  if e.type == 'PaceDecisionRecorded']
        assert len(events) == 1
        payload = events[0].payload
        decision = payload['decision']
        assert payload['outcome'] == 'wait' and decision['selected'] is None
        assert [s['status'] for s in decision['stages']] == [
            'passed', 'passed', 'wait', 'skipped', 'skipped', 'skipped']
        gate = decision['stages'][2]
        reason = 'quota_unknown' if unknown else 'quota_exhausted'
        assert {r['five_hour'] for r in gate['result']['route_results']} == {reason}
        assert decision['stages'][-1]['result']['reason'] == 'no_selected_route'
        assert {r['reason'] for r in decision['rejected_routes']} == {reason}
        assert gate['inputs']['hard_limit_pct'] == 92
        observation = gate['inputs']['pools']['fixture-pool']['observation']
        assert observation is None if unknown else observation['windows']['300']['used_pct'] == 100
        assert all(c['estimate']['burn_observations'] == 0
                   and c['estimate']['burn_source'] == 'configured_initial_estimate'
                   for c in decision['candidates'])
    restarted = WorkflowRunner(store, run.revision, run_id=run.run_id, repo=run.repo,
        routing=dispatch, dispatcher=run.dispatcher, artifacts=run.artifacts,
        is_alive=lambda pid: True)
    assert node.node_id in restarted.tick().waiting
    assert store.conn.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?',
                              (run.run_id,)).fetchone()[0] == 0
    assert [e.payload for e in store.events(aggregate_id=run.run_id)
            if e.type == 'PaceDecisionRecorded'][0] == payload


def test_real_pace_journal_keeps_hard_and_evidence_losses_before_weekly_choice(runtime,
                                                                           monkeypatch, tmp_path):
    from maestro.control.store import ControlStore
    from tests.graph_engineering.conftest import fixture_cli_entry
    from tests.test_pace_budget import pace_routing

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'loss-host.json'))
    store, make = runtime
    _, original, _ = make(routing=pace_routing(used=10))
    shape = json.loads(original['shape_key_json'])
    entries = []
    for name in ('unknown', 'full', 'loser', 'winner', 'weak'):
        entry = fixture_cli_entry(backend_id=name, usage_pool_id=name,
            pair_profiles=cat.migrate_pairs(('fixture-1', 'light' if name == 'weak' else 'strong', 1)),
            pairs_migrated=True)
        entries.append(entry)
    dispatch = fixture_routing(entries=entries, policy=default_policy().with_objective('pace'))
    dispatch.task_spec = pace_routing().task_spec
    now = datetime.now(timezone.utc)
    for name in ('full', 'loser', 'winner', 'weak'):
        dispatch.resources.observe(cat.QuotaObservation(pool_id=name, observed_at=now.isoformat(),
            windows={300: cat.QuotaWindow(300, 100 if name == 'full' else 10, now.timestamp()+1800),
                     10080: cat.QuotaWindow(10080, 80 if name == 'winner' else 0,
                                           now.timestamp()+3600)}))
    for name in ('loser', 'winner'):
        for i in range(5):
            store.record_routing_outcome(dict(outcome_id=f'quality-{name}-{i}',
                attempt_id=f'quality-{name}-{i}', run_id=original['run_id'],
                shape_key_json=json.dumps({**shape, 'backend_id': name}),
                success=int(name == 'winner'), verified_by='run_outcome', amends=None,
                recorded_at='2026-01-02T00:00:00Z'))
    run, attempt, _ = make(routing=dispatch)
    assert attempt is not None and attempt['backend_id'] == 'winner'
    with ControlStore.open_client(store.path) as reopened:
        payload = next(e.payload for e in reopened.events(aggregate_id=run.run_id)
                       if e.type == 'PaceDecisionRecorded')
    stages = {s['name']: s for s in payload['decision']['stages']}
    assert {r['backend_id'] for r in stages['eligibility']['result']['rejected_routes']} == {'weak'}
    gate = {r['backend_id']: r['five_hour'] for r in stages['five_hour']['result']['route_results']}
    assert gate == {'unknown': 'quota_unknown', 'full': 'quota_exhausted',
                    'loser': 'admitted', 'winner': 'admitted'}
    evidence = stages['evidence']
    assert evidence['result']['best_success_rate'] == 1
    assert {r['backend_id'] for r in evidence['result']['rejected_routes']} == {'loser'}
    assert all(c['evidence']['success_source'] == 'verified_outcome_statistics'
               and c['evidence']['success_attempt_ids'] is None
               for c in evidence['inputs']['coverage'])
    assert stages['weekly_pace']['inputs']['pool_order'] == ['winner']
    assert stages['weekly_pace']['result']['selected']['backend_id'] == 'winner'
    assert {r['backend_id'] for r in payload['decision']['rejected_routes']} == {
        'weak', 'unknown', 'full', 'loser'}


@pytest.mark.parametrize('quota_wait', [False, True])
def test_journal_write_failure_cannot_claim_launch_or_leak_and_restart_can_retry(runtime,
                                                                              monkeypatch,
                                                                              tmp_path,
                                                                              quota_wait):
    from maestro.control.resources import HostReservationLedger
    from maestro.workflows.runner import WorkflowRunner
    from maestro.workflows.handlers import TmuxDispatcher
    from tests.test_pace_budget import pace_routing

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'failed-journal-host.json'))
    store, make = runtime
    append = store.append_event
    broken = [True]
    def write(type, *args, **kwargs):
        if type == 'PaceDecisionRecorded' and broken[0]:
            raise OSError('synthetic durable journal write failure')
        return append(type, *args, **kwargs)
    monkeypatch.setattr(store, 'append_event', write)
    launched = []
    dispatch = TmuxDispatcher.dispatch
    def launch(self, *args, **kwargs):
        launched.append(args)
        return dispatch(self, *args, **kwargs)
    monkeypatch.setattr(TmuxDispatcher, 'dispatch', launch)
    run, attempt, node = make(routing=pace_routing(used=100 if quota_wait else 10))
    assert attempt is None, 'journal failure before claim must leave no fabricated attempt'
    assert launched == []
    assert HostReservationLedger().reservations() == []
    assert run.phases()[node.node_id] == 'pending'  # the unchanged durable preclaim phase
    assert 'pace_journal_unavailable' in run.tick().waiting[node.node_id]
    broken[0] = False
    restarted = WorkflowRunner(store, run.revision, run_id=run.run_id, repo=run.repo,
        routing=pace_routing(used=10), dispatcher=run.dispatcher, artifacts=run.artifacts,
        is_alive=lambda pid: True)
    assert restarted.tick().dispatched == [node.node_id]
    assert len(launched) == 1
    assert len([e for e in store.events(aggregate_id=run.run_id)
                if e.type == 'PaceDecisionRecorded']) == 1


def test_nonfinite_unknown_quota_wait_has_lossless_standard_json_evidence(runtime, monkeypatch,
                                                                        tmp_path):
    from maestro.control.store import ControlStore
    from tests.test_pace_budget import pace_routing

    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'nonfinite-host.json'))
    store, make = runtime
    run, attempt, _ = make(routing=pace_routing(used=float('nan')))
    assert attempt is None
    with ControlStore.open_client(store.path) as reopened:
        payload = next(e.payload for e in reopened.events(aggregate_id=run.run_id)
                       if e.type == 'PaceDecisionRecorded')
    try:
        json.dumps(payload, allow_nan=False)
    except ValueError:
        pytest.fail('durable unknown-input evidence must serialize as standard JSON')
    observation = payload['decision']['pools']['fixture-pool']['observation']
    assert observation['windows']['300']['used_pct'] == {'unavailable_number': 'nan'}
    assert {r['five_hour'] for r in payload['decision']['stages'][2]['result']['route_results']} == {
        'quota_unknown'}


def test_claim_uses_durable_history_after_routing_reconstruction(runtime):
    store, make = runtime
    def routing():
        dispatch = fixture_routing(policy=default_policy().with_objective('pace'))
        dispatch.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
            'verifications': [{'id': 'V-pace', 'kind': 'auto', 'cmd': 'true'}]})
        now = datetime.now(timezone.utc)
        dispatch.resources.observe(cat.QuotaObservation(pool_id='fixture-pool',
            observed_at=now.isoformat(), windows={300: cat.QuotaWindow(300, 10, now.timestamp()+1800)}))
        return dispatch

    _, original, _ = make(routing=routing())
    for i in range(5):
        store.conn.execute('''INSERT INTO attempts
            (attempt_id, run_id, node_id, status, started_at, binding_json, shape_key_json, wall_time_sec)
            VALUES (?, ?, ?, 'succeeded', ?, ?, ?, 3600)''',
            (f'history-{i}', original['run_id'], original['node_id'],
             '2026-01-01T00:00:00+00:00', original['binding_json'], original['shape_key_json']))
    _, claimed, _ = make(routing=routing())
    binding = json.loads(claimed['binding_json'])
    candidate = next(c for c in binding['pace_decision']['candidates']
                     if c['effort'] == binding['requested_effort'])
    assert candidate['estimate']['runtime_sec'] == 3600
    assert candidate['estimate']['runtime_observations'] == 5


def test_claim_evidence_uses_latest_verified_verdict_after_restart(runtime):
    from maestro.control.store import ControlStore
    from maestro.workflows.routing import load_pace_history

    store, make = runtime
    def routing():
        dispatch = fixture_routing(policy=default_policy().with_objective('pace'))
        dispatch.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
            'verifications': [{'id': 'V-pace', 'kind': 'auto', 'cmd': 'true'}]})
        now = datetime.now(timezone.utc)
        dispatch.resources.observe(cat.QuotaObservation(pool_id='fixture-pool',
            observed_at=now.isoformat(), windows={300: cat.QuotaWindow(300, 10, now.timestamp()+1800)}))
        return dispatch

    _, original, _ = make(routing=routing())
    tokens = json.dumps({'token_split': {'fresh_input': 2, 'cache_read': 0,
                                       'cache_write': 0, 'output': 0}})
    for i in range(6):
        aid = f'verified-history-{i}'
        store.conn.execute('''INSERT INTO attempts
            (attempt_id, run_id, node_id, status, started_at, binding_json, shape_key_json,
             usage_settle_json) VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)''',
            (aid, original['run_id'], original['node_id'], '2026-01-01T00:00:00Z',
             original['binding_json'], original['shape_key_json'], tokens))
        if i < 5:
            store.record_routing_outcome(dict(outcome_id=f'out-{i}', attempt_id=aid,
                run_id=original['run_id'], shape_key_json=original['shape_key_json'],
                success=1, verified_by='run_outcome', amends=None,
                recorded_at='2026-01-02T00:00:00Z'))

    def evidence():
        _, row, _ = make(routing=routing())
        binding = json.loads(row['binding_json'])
        return next(c['evidence'] for c in binding['pace_decision']['candidates']
                    if c['effort'] == binding['requested_effort'])

    first = evidence()
    assert first['observations'] == 5 and first['successes'] == 5
    assert first['cost_observations'] == 5 and first['success_per_cost'] > 0
    # Append authoritative amendments, keeping terminal attempts succeeded.
    for i in range(5):
        store.record_routing_outcome(dict(outcome_id=f'amended-{i}',
            attempt_id=f'verified-history-{i}', run_id=original['run_id'],
            shape_key_json=original['shape_key_json'], success=0,
            verified_by='run_outcome', amends=f'out-{i}',
            recorded_at='2026-01-03T00:00:00Z'))
    # Equal timestamps resolve by row order. An older timestamp inserted later
    # must still not overturn the latest authoritative conclusion.
    for oid, success, at in [('tie-true', 1, '2026-01-03T00:00:00Z'),
                             ('tie-false', 0, '2026-01-03T00:00:00Z'),
                             ('late-old', 1, '2026-01-01T00:00:00Z')]:
        store.record_routing_outcome(dict(outcome_id=oid,
            attempt_id='verified-history-0', run_id=original['run_id'],
            shape_key_json=original['shape_key_json'], success=success,
            verified_by='run_outcome', amends='amended-0', recorded_at=at))
    with ControlStore.open_client(store.path) as reopened:
        durable = load_pace_history(reopened)
    verdicts = {r['attempt_id']: r.get('verified_success') for r in durable}
    assert verdicts['verified-history-0'] is False
    assert verdicts['verified-history-5'] is None
    amended = evidence()
    assert amended['observations'] == 5 and amended['successes'] == 0
    assert amended['cost_observations'] == 5 and amended['success_per_cost'] == 0
    assert len(store.routing_outcomes()) == 13


def test_durable_aggregate_quality_fallback_rereads_amendments_before_routing(runtime):
    from maestro.control.store import ControlStore
    from maestro.workflows.routing import load_statistics, resolve
    from tests.test_pace_routing import canonical_evidence_dispatch, NOW
    from tests.backends.test_router import agent_node

    store, make = runtime
    _, original, _ = make()
    dispatch, history = canonical_evidence_dispatch()
    # The authoritative outcomes outlive detailed attempt telemetry. Aggregate
    # quality is still usable, but it must not invent cost or attempt identities.
    for row in history:
        aid = row['attempt_id']
        store.record_routing_outcome(dict(outcome_id='initial-' + aid, attempt_id=aid,
            run_id=original['run_id'], shape_key_json=row['shape_key_json'],
            success=int(aid == 'low-0' or aid.startswith('medium-')),
            verified_by='run_outcome', amends=None, recorded_at='2026-01-02T00:00:00Z'))
    dispatch.statistics = load_statistics(store)
    initial = resolve(dispatch, agent_node(), None, now_epoch=NOW, history_store=store)
    assert initial.backend_id == 'peer' and initial.requested_effort == 'medium'
    for row in history:
        aid = row['attempt_id']
        store.record_routing_outcome(dict(outcome_id='amended-' + aid, attempt_id=aid,
            run_id=original['run_id'], shape_key_json=row['shape_key_json'],
            success=int(aid.startswith('low-')), verified_by='run_outcome',
            amends='initial-' + aid, recorded_at='2026-01-03T00:00:00Z'))
    with ControlStore.open_client(store.path) as reopened:
        amended = resolve(dispatch, agent_node(), None, now_epoch=NOW, history_store=reopened)
    assert amended.backend_id == 'peer' and amended.requested_effort == 'low'
    for candidate in amended.pace_decision.candidates:
        evidence = candidate['evidence']
        assert evidence['success_source'] == 'verified_outcome_statistics'
        assert evidence['success_observations'] == 5
        assert evidence['success_successes'] == (5 if candidate['effort'] == 'low' else 0)
        assert evidence['success_rate'] == (1 if candidate['effort'] == 'low' else 0)
        assert evidence['success_attempt_ids'] is None
        assert evidence['cost_observations'] == 0 and evidence['success_per_cost'] is None
    assert len(store.routing_outcomes()) == 20


def test_durable_history_keeps_old_unknown_activity_for_burn_overlap(runtime):
    from maestro.workflows.routing import resolve
    from tests.test_pace_routing import history_dispatch, NOW
    from tests.backends.test_router import agent_node

    store, make = runtime
    fixture = fixture_routing()
    fixture.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-history', 'kind': 'auto', 'cmd': 'true'}]})
    _, original, _ = make(routing=fixture)
    dispatch, history = history_dispatch()
    def insert(row):
        store.conn.execute('''INSERT INTO attempts
            (attempt_id, run_id, node_id, status, started_at, binding_json,
             shape_key_json, wall_time_sec, usage_start_json, usage_settle_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (row['attempt_id'], original['run_id'], original['node_id'], row['status'],
             row.get('started_at'), row.get('binding_json'), row.get('shape_key_json'),
             row.get('wall_time_sec'), row.get('usage_start_json'), row.get('usage_settle_json')))
    # No reliable pool or interval means potentially overlapping activity. Its age,
    # missing outcome and subsequent unrelated rows cannot justify dropping it.
    insert(dict(attempt_id='old-unknown', status='failed', binding_json='{malformed'))
    for row in history:
        insert(row)
    for i in range(300):
        insert(dict(attempt_id=f'unrelated-{i}', status='succeeded',
                    binding_json=json.dumps({'usage_pool_id': 'unrelated-pool'})))
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW, history_store=store)
    assert result.is_bound
    assert all(c['estimate']['burn_observations'] == 0
               and set(c['estimate']['burn_exclusions'].values()) == {
                   'overlapping_or_unknown_pool_activity'}
               for c in result.pace_decision.candidates)
    # An explicit other-pool fact removes this uncertainty. Older valid samples
    # still supply coverage; no last-N selection may silently erase them either.
    store.conn.execute('UPDATE attempts SET binding_json=? WHERE attempt_id=?',
        (json.dumps({'usage_pool_id': 'unrelated-pool'}), 'old-unknown'))
    measured = resolve(dispatch, agent_node(), None, now_epoch=NOW, history_store=store)
    assert all(c['estimate']['burn_observations'] == 5
               and c['estimate']['burn_pct_per_cost'] == 2
               for c in measured.pace_decision.candidates)
