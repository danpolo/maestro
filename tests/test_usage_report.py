"""Behavior contract for W2's read-only usage reporting API."""
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def reporting():
    assert importlib.util.find_spec('maestro.usage_report'), 'W2 reporting API is missing'
    return importlib.import_module('maestro.usage_report')


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'usage ?#.sqlite3'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE task_runs(run_id TEXT PRIMARY KEY, task_id TEXT, workflow_id TEXT,
            workflow_revision INTEGER, status TEXT);
        CREATE TABLE workflow_revisions(workflow_id TEXT, revision INTEGER, dag_json TEXT);
        CREATE TABLE node_states(run_id TEXT, node_id TEXT, phase TEXT, last_attempt_id TEXT);
        CREATE TABLE events(sequence INTEGER PRIMARY KEY, run_id TEXT, attempt_id TEXT,
            type TEXT, payload TEXT, recorded_at TEXT);
        CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT,
            status TEXT, backend_id TEXT, model_id TEXT, effort TEXT, binding_json TEXT,
            usage_start_json TEXT, usage_settle_json TEXT, wall_time_sec REAL,
            reset_crossing INTEGER, failure_kind TEXT, failure_class TEXT);
    ''')
    conn.close()
    return path


def add_run(db, run='r1', task='t1', status='succeeded', nodes=None):
    nodes = nodes if nodes is not None else [
        {'node_id': 'looks_like_gate', 'kind': 'agent', 'agent_definition_ref': 'implementer'},
        {'node_id': 'writer_name', 'kind': 'agent', 'agent_definition_ref': 'reviewer'},
        {'node_id': 'checks', 'kind': 'function', 'handler_ref': 'maestro.handlers.verification'},
    ]
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO task_runs VALUES (?, ?, ?, 1, ?)', (run, task, run, status))
        conn.execute('INSERT INTO workflow_revisions VALUES (?, 1, ?)', (run, json.dumps({'nodes': nodes})))


def add_attempt(db, aid='a1', run='r1', node='looks_like_gate', status='succeeded',
                tokens=None, wall=2, cost=2, pool='codex-subscription', **fields):
    values = dict(attempt_id=aid, run_id=run, node_id=node, status=status,
                  backend_id='codex', model_id='sol', effort='high',
                  binding_json=json.dumps({'relative_cost': cost, 'usage_pool_id': pool}),
                  usage_settle_json=json.dumps({'token_split': tokens}) if tokens is not None else None,
                  wall_time_sec=wall)
    values.update(fields)
    with sqlite3.connect(db) as conn:
        keys = ','.join(values)
        conn.execute(f'INSERT INTO attempts ({keys}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def split(fresh=10, cache=20, write=0, output=5):
    return dict(fresh_input=fresh, cache_read=cache, cache_write=write, output=output)


def w2_event(db, event_type, payload, *, aid='a1', run='r1'):
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO events(run_id, attempt_id, type, payload) VALUES (?, ?, ?, ?)',
                     (run, aid, event_type, json.dumps(payload)))


def test_fault_rollup_uses_latest_append_only_observation_once_per_attempt(db):
    add_run(db)
    add_attempt(db, tokens=split(), wall=4)
    w2_event(db, 'ExtraStepObserved', {'ledger_id': 'attempt:a1', 'observed_cause': None})
    w2_event(db, 'ExtraStepObserved', {'ledger_id': 'attempt:a1', 'observed_cause': 'missed_earlier',
                                     'fault': 'planner', 'mapping_version': 1})
    task = reporting().build_usage_report(db)['tasks'][0]
    assert len(task.get('blame_ledger', [])) == 1, 'W2 ledger is not exposed'
    assert task['blame_ledger'][0]['fault'] == 'reviewer'
    assert task['blame_ledger'][0]['captured_fault'] == 'planner'
    fault = task['faults'][0]
    assert fault['fault'] == 'reviewer'
    assert fault['ledger_rows'] == 1
    assert fault['measurements']['tokens']['known_sum'] == 35
    assert fault['measurements']['wall_time_sec']['known_sum'] == 4
    assert fault['measurements']['cost_units']['known_sum'] == 70
    assert fault['routes'][0]['role'] == 'implementer'
    assert fault['quota_consumption'] is None


def test_goal_impacts_show_the_latest_attribution_and_the_captured_one(db):
    """GDT-12 (R58, R68): an attribution is an amendment under the observation's ledger id;
    the view shows the latest values, and the first row's attribution and provenance."""
    add_run(db)
    add_attempt(db, node='writer_name')
    provenance = {'run_id': 'r1', 'node_id': 'writer_name', 'attempt_id': 'a1',
                  'evidence': [{'kind': 'test', 'id': 't', 'result': 'failed'}]}
    first = {'ledger_id': 'goal:G1:review:a1', 'goal': 'G1', 'impact': 'no_progress_unexpected',
             'attribution': 'unattributed', 'provenance': provenance}
    w2_event(db, 'GoalImpactObserved', first)
    task = reporting().build_usage_report(db)['tasks'][0]
    [view] = task['goal_impacts']
    assert (view['attribution'], view['captured_attribution'], view['amendments']) == (
        'unattributed', 'unattributed', 0)
    assert view['rule'] is None and view['evidence'] == []
    for attribution, rule in (('unattributed', 'pending_evidence'), ('reviewer', 'gate_saw_green')):
        w2_event(db, 'GoalImpactObserved', {
            **first, 'attribution': attribution, 'attribution_cause': 'review_misclassified',
            'attribution_rule': rule, 'attribution_evidence': ['AC-1 GREEN'],
            'attributed_at': '2026-10-02T00:00:00Z', 'mapping_version': 2})
    [view] = reporting().build_usage_report(db)['tasks'][0]['goal_impacts']
    assert view == {'ledger_id': 'goal:G1:review:a1', 'run_id': 'r1', 'goal': 'G1',
                    'impact': 'no_progress_unexpected', 'attribution': 'reviewer',
                    'cause': 'review_misclassified', 'rule': 'gate_saw_green',
                    'evidence': ['AC-1 GREEN'], 'captured_attribution': 'unattributed',
                    'provenance': provenance, 'amendments': 2}
    assert reporting().build_usage_report(db)['tasks'][0]['blame_ledger'] == []


def test_fault_costs_stay_within_pools_and_waits_have_no_invented_attempt_spend(db):
    add_run(db)
    add_attempt(db, tokens=split())
    add_attempt(db, aid='other', tokens=split(), pool='other-pool')
    for aid in ('a1', 'other'):
        w2_event(db, 'ExtraStepObserved', {'ledger_id': f'attempt:{aid}', 'observed_cause': 'check_failed'}, aid=aid)
    w2_event(db, 'ExtraStepObserved', {'ledger_id': 'wait:r1:n', 'observed_cause': 'quota',
                                     'context': {'gate_admitted': False}}, aid=None)
    task = reporting().build_usage_report(db)['tasks'][0]
    faults = {f['fault']: f for f in task['faults']}
    assert faults['implementer']['measurements']['tokens']['known_sum'] == 70
    assert faults['implementer']['measurements']['cost_units']['known_sum'] is None
    assert len(faults['implementer']['routes']) == 2
    assert all(r['measurements']['cost_units']['known_sum'] == 70 for r in faults['implementer']['routes'])
    assert faults['none']['attempts'] == 0 and faults['none']['ledger_rows'] == 1
    assert faults['none']['measurements']['tokens']['known_sum'] is None


def test_complexity_and_diff_facts_are_nullable_historical_crosschecks(db):
    add_run(db)
    add_attempt(db)
    w2_event(db, 'ReviewComplexityObserved', {'ledger_id': 'review:a1', 'complexity': 4,
             'rubric_version': 1, 'diff_stats': {'files_changed': 2, 'insertions': 5, 'deletions': 1,
             'binary_files': 0, 'base_sha': 'base', 'head_sha': 'head'}})
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['complexity_reviews'][0]['complexity'] == 4
    assert task['complexity_reviews'][0]['diff_stats']['files_changed'] == 2
    w2_event(db, 'ReviewComplexityObserved', {'ledger_id': 'review:a1', 'complexity': 'wrong'})
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['complexity_reviews'][0]['complexity'] is None
    assert task['complexity_reviews'][0]['flags']


def test_unknown_and_malformed_blame_evidence_is_flagged_and_json_renderable(db):
    add_run(db)
    add_attempt(db)
    w2_event(db, 'ExtraStepObserved', {'ledger_id': 'attempt:a1', 'observed_cause': 'invented',
                                     'fault': float('nan'), 'context': [1]})
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['blame_ledger'][0]['fault'] == 'unclassified'
    assert task['blame_ledger'][0]['flags']
    assert task['faults'][0]['fault'] == 'unclassified'
    reporting().render_usage_report({'tasks': [task]})


def test_older_history_does_not_invent_blame_or_reviewer_scores(db):
    add_run(db)
    add_attempt(db, status='failed', failure_kind='verification_failed')
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['blame_ledger'] == [] and task['faults'] == []
    assert task['complexity_reviews'] == []
    assert 'blame_history_unavailable' in task['issues']


def test_rolls_up_retries_and_runs_by_recorded_roles_and_routes(db):
    add_run(db)
    add_run(db, run='r2', status='failed')
    add_attempt(db, tokens=split(), status='failed')
    add_attempt(db, aid='a2', tokens=split(), wall=3)
    add_attempt(db, aid='a3', run='r2', tokens=split(1, 0, 0, 1), effort='medium')
    add_attempt(db, aid='a4', node='writer_name', tokens=split(2, 0, 0, 2))
    add_attempt(db, aid='a5', node='checks', status='failed', failure_kind='verification_failed', failure_class='bad_output', backend_id=None, model_id=None, effort=None, wall=None, cost=None)
    report = reporting().build_usage_report(db)
    task = report['tasks'][0]
    assert task['run_statuses'] == {'succeeded': 1, 'failed': 1}
    assert task['counts']['implementer_cycles'] == 3
    assert task['counts']['review_rounds'] == 1
    assert task['counts']['gate_failures'] == 1
    assert task['attempts'] == 5
    assert task['measurements']['tokens']['known_sum'] == 76
    assert task['measurements']['tokens']['measured_attempts'] == 4
    assert task['measurements']['tokens']['complete'] is False
    assert task['measurements']['wall_time_sec']['known_sum'] == 9
    impl_high = next(g for g in task['routes'] if g['role'] == 'implementer' and g['effort'] == 'high')
    assert impl_high['attempts'] == 2
    assert impl_high['measurements']['cost_units']['known_sum'] == 140
    assert impl_high['measurements']['cost_units']['complete'] is True
    assert reporting().build_usage_report(db, task_id='absent')['tasks'] == []

def test_missing_and_malformed_measurements_keep_partial_coverage(db):
    add_run(db)
    add_attempt(db, tokens=split(), wall=0)
    add_attempt(db, aid='partial', tokens=split(fresh=None), wall=-1)
    add_attempt(db, aid='invalid', tokens=split(fresh=True, cache=1.5, output=float('nan')), wall=float('inf'))
    add_attempt(db, aid='corrupt', usage_settle_json='[1,2]', binding_json='bad json', wall=None)
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['measurements']['tokens'] == {'known_sum': 35, 'measured_attempts': 1, 'total_attempts': 4, 'complete': False}
    assert task['measurements']['fresh_input']['known_sum'] == 10
    assert task['measurements']['wall_time_sec']['known_sum'] == 0
    assert task['measurements']['cost_units']['known_sum'] == 70
    assert task['measurements']['cost_units']['measured_attempts'] == 1
    assert task['issues']
    json.dumps(task, allow_nan=False)


def test_old_nullable_schema_and_unknown_roles_are_explicit(db):
    add_run(db, nodes=[])
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE attempts')
        conn.execute('CREATE TABLE attempts(attempt_id TEXT, run_id TEXT, node_id TEXT, status TEXT)')
        conn.execute("INSERT INTO attempts VALUES ('old', 'r1', 'implementer_99', 'uncertain')")
    report = reporting().build_usage_report(db)
    task = report['tasks'][0]
    assert task['counts']['unclassified_attempts'] == 1
    assert task['counts']['implementer_cycles'] == 0
    assert task['measurements']['tokens']['known_sum'] is None
    assert task['measurements']['cost_units']['known_sum'] is None
    assert 'usage_settle_json' in report['missing_attempt_columns']
    assert task['routes'][0]['harness'] is None


def test_gate_holds_and_unclassified_failures_are_separate_from_failed_checks(db):
    add_run(db)
    add_attempt(db, node='checks', status='failed', failure_class='blocked', failure_kind='dan_question')
    add_attempt(db, aid='quota', node='checks', status='failed', failure_class='quota', failure_kind='quota_exhausted')
    add_attempt(db, aid='bad', node='checks', status='failed', failure_class='bad_output', failure_kind='verification_failed')
    add_attempt(db, aid='unknown', node='checks', status='failed')
    add_attempt(db, aid='pending', node='checks', status='running')
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['counts']['gate_attempts'] == 5
    assert task['counts']['gate_failures'] == 1
    assert task['counts']['gate_holds'] == 2
    assert task['counts']['gate_unclassified_failures'] == 1
    assert task['outcomes'] == {'failed': 4, 'running': 1}

def sample(used, *, reset=1000, at='2026-01-01T00:00:00Z', source='native'):
    return json.dumps({'windows': {'300': {'used_pct': used, 'resets_at': reset}}, 'updated_at': at, 'source': source})


def test_account_quota_changes_are_observations_never_task_consumption(db):
    add_run(db)
    add_run(db, run='r2', task='t2')
    for aid, run in [('a1', 'r1'), ('a2', 'r2')]:
        add_attempt(db, aid=aid, run=run, usage_start_json=sample(10), usage_settle_json=sample(15, at='2026-01-01T00:00:10Z'))
    report = reporting().build_usage_report(db, task_id='t1')
    task = report['tasks'][0]
    assert task['quota_consumption'] is None
    observation = task['quota_observations'][0]
    assert observation['scope'] == 'account'
    assert observation['observed_change_pct'] == 5
    assert observation['overlaps_other_attempts'] is True
    assert observation['attributable_to_attempt'] is False
    assert observation['pool_id'] == 'codex-subscription'


@pytest.mark.parametrize('start,end,crossing,reason', [
    (sample(90), sample(5, reset=2000, at='2026-01-01T00:00:10Z'), None, 'reset_crossing'),
    (sample(10), sample(15, at='2026-01-01T00:00:10Z'), 1, 'reset_crossing'),
    (sample(20), sample(15, at='2026-01-01T00:00:10Z'), 0, 'counter_decreased'),
    (None, sample(15), None, 'missing_sample'),
    (sample(10), sample(101, at='2026-01-01T00:00:10Z'), None, 'invalid_window'),
    (sample(10), sample(15, source='bridge', at='2026-01-01T00:00:10Z'), None, 'source_changed'),
    (sample(10), sample(15), None, 'invalid_observation_interval'),
])
def test_uncomparable_quota_snapshots_do_not_manufacture_deltas(db, start, end, crossing, reason):
    add_run(db)
    add_attempt(db, usage_start_json=start, usage_settle_json=end, reset_crossing=crossing)
    observation = reporting().build_usage_report(db)['tasks'][0]['quota_observations'][0]
    assert observation['observed_change_pct'] is None
    assert observation['unavailable_reason'] == reason


def test_costs_stay_per_pool_and_require_recorded_facts(db):
    add_run(db)
    add_attempt(db, tokens=split(), cost=2)
    add_attempt(db, aid='pool2', tokens=split(), cost=5, pool='api')
    add_attempt(db, aid='unpriced', tokens=split(), cost=None)
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['measurements']['cost_units']['known_sum'] is None
    assert 'mixed_cost_pools' in task['issues']
    assert sorted(g['measurements']['cost_units']['known_sum'] for g in task['routes']) == [70, 175]
    unpriced_pool = next(g for g in task['routes'] if g['pool_id'] == 'codex-subscription')
    assert unpriced_pool['measurements']['cost_units']['complete'] is False


def test_snapshot_remains_consistent_and_refuses_writes(db):
    add_run(db)
    add_attempt(db)
    api = reporting()
    with api.read_snapshot(db) as conn:
        assert conn.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        writer = sqlite3.connect(db)
        # A WAL writer commits while the report retains its old snapshot.
        writer.execute("UPDATE attempts SET status='failed'")
        writer.commit()
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == 'succeeded'
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM attempts')
        writer.close()
    assert api.build_usage_report(db)['tasks'][0]['outcomes'] == {'failed': 1}


def test_report_leaves_controller_owned_database_unchanged(tmp_path):
    from maestro.control.store import ControlStore

    db = tmp_path / 'control.sqlite3'
    store = ControlStore.open_controller(db)
    try:
        before = list(store.conn.iterdump())
        head = store.head_sequence()
        api = reporting()
        assert api.build_usage_report(db)['tasks'] == []
        assert list(store.conn.iterdump()) == before
        assert store.head_sequence() == head
        assert store.conn.execute('SELECT count(*) FROM writer_leases').fetchone()[0] == 1
    finally:
        store.close()


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        reporting().build_usage_report(path)
    assert not path.exists()


def test_current_node_states_are_separate_from_attempt_history(db):
    add_run(db)
    add_attempt(db, status='failed')
    add_attempt(db, aid='retry', status='succeeded')
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO node_states VALUES ('r1', 'looks_like_gate', 'succeeded', 'retry')")
        conn.execute("INSERT INTO node_states VALUES ('r1', 'writer_name', 'skipped', NULL)")
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['counts']['implementer_cycles'] == 2
    assert task['counts']['review_rounds'] == 0
    assert task['node_phases'] == {'skipped': 1, 'succeeded': 1}
    assert task['node_states'] == [
        {'run_id': 'r1', 'node_id': 'looks_like_gate', 'phase': 'succeeded', 'last_attempt_id': 'retry'},
        {'run_id': 'r1', 'node_id': 'writer_name', 'phase': 'skipped', 'last_attempt_id': None},
    ]


def test_malformed_workflow_and_route_facts_are_flagged_without_inference(db):
    add_run(db, nodes=[None, {'node_id': 'looks_like_gate', 'kind': 'agent', 'agent_definition_ref': ['implementer']}])
    add_attempt(db, tokens=split(), binding_json=json.dumps({'relative_cost': 2, 'usage_pool_id': ['pool']}))
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['counts']['unclassified_attempts'] == 1
    assert task['measurements']['cost_units']['known_sum'] is None
    assert task['issues']


def test_cost_requires_complete_recorded_route_and_ignores_current_policy(db):
    add_run(db)
    add_attempt(db, tokens=split(), backend_id=None)
    add_attempt(db, aid='unpriced', tokens=split(), binding_json=json.dumps({
        'usage_pool_id': 'codex-subscription', 'selection_reason': 'relative_cost=2',
    }))
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['measurements']['cost_units']['known_sum'] is None
    assert task['measurements']['cost_units']['measured_attempts'] == 0


def test_recorded_pool_and_scoped_observations_remain_distinct(db):
    add_run(db)
    start = json.loads(sample(10))
    end = json.loads(sample(15, at='2026-01-01T00:00:10Z'))
    start['pools'] = {'other-pool': {'300': {'used_pct': 40, 'resets_at': 1000}}}
    end['pools'] = {'other-pool': {'300': {'used_pct': 42, 'resets_at': 1000}}}
    start['scoped_windows'] = {'opus': {'10080': {'used_pct': 60, 'resets_at': 1000}}}
    end['scoped_windows'] = {'opus': {'10080': {'used_pct': 63, 'resets_at': 1000}}}
    add_attempt(db, usage_start_json=json.dumps(start), usage_settle_json=json.dumps(end))
    observations = reporting().build_usage_report(db)['tasks'][0]['quota_observations']
    assert [(o['scope'], o['pool_id'], o['window_minutes'], o['observed_change_pct']) for o in observations] == [
        ('account', 'codex-subscription', '300', 5),
        ('pool', 'other-pool', '300', 2),
        ('opus', 'codex-subscription', '10080', 3),
    ]
    assert all(o['attributable_to_attempt'] is False for o in observations)


def test_renderer_is_stable_json_with_coverage_and_unknowns(db):
    add_run(db)
    add_attempt(db, tokens=split(fresh=0, cache=0, output=0), wall=0, cost=None)
    api = reporting()
    report = api.build_usage_report(db)
    rendered = api.render_usage_report(report)
    assert json.loads(rendered) == report
    assert rendered == api.render_usage_report(api.build_usage_report(db))
    assert 'null' in rendered
    assert report['tasks'][0]['measurements']['tokens']['known_sum'] == 0


def test_saved_p12_replay_matches_hand_counts_and_numeric_coverage(db):
    fixture = json.loads((Path(__file__).parent / 'fixtures/usage_report/p12-07-replay.json').read_text())
    add_run(db, run=fixture['run_id'], task=fixture['task_id'],
            status=fixture['run_status'], nodes=fixture['nodes'])
    for row in fixture['attempts']:
        add_attempt(db, run=fixture['run_id'], cost=None, **row)
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['run_statuses'] == {'succeeded': 1}
    assert task['attempts'] == 14
    assert {key: task['counts'][key] for key in ('implementer_cycles', 'review_rounds', 'gate_attempts', 'gate_failures', 'gate_holds')} == {
        'implementer_cycles': 3, 'review_rounds': 3, 'gate_attempts': 5,
        'gate_failures': 0, 'gate_holds': 2,
    }
    assert task['outcomes'] == {'failed': 4, 'succeeded': 10}
    # Hand sums of the five recorded Codex splits; output already includes reasoning.
    for metric, total in {'fresh_input': 344855, 'cache_read': 4336896,
                          'cache_write': 0, 'output': 37030, 'tokens': 4718781}.items():
        assert task['measurements'][metric] == {
            'known_sum': total, 'measured_attempts': 5, 'total_attempts': 14, 'complete': False,
        }
    assert task['measurements']['wall_time_sec'] == {
        'known_sum': 2031, 'measured_attempts': 6, 'total_attempts': 14, 'complete': False,
    }
    assert all(route['measurements']['cost_units']['known_sum'] is None for route in task['routes'])
    assert task['measurements']['cost_units']['known_sum'] is None
    assert task['quota_consumption'] is None
    assert any(o['unavailable_reason'] == 'missing_sample' for o in task['quota_observations'])
    json.dumps(task, allow_nan=False)


def test_unavailable_measurements_are_named_per_attempt(db):
    add_run(db)
    add_attempt(db, tokens=split(fresh=None), wall=None, cost=None)
    detail = reporting().build_usage_report(db)['tasks'][0]['attempt_details'][0]
    assert detail['attempt_id'] == 'a1'
    assert detail['role'] == 'implementer'
    assert detail['missing_measurements'] == ['cost_units', 'fresh_input', 'tokens', 'wall_time_sec']
    assert detail['measurements']['cache_read'] == 20
    assert 'missing_relative_cost' in detail['issues']


def test_numeric_overflow_cannot_escape_as_an_infinite_measurement(db):
    add_run(db)
    add_attempt(db, tokens=split(), wall=1e308, cost=1e308)
    add_attempt(db, aid='second', tokens=split(), wall=1e308, cost=1e308)
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['measurements']['cost_units']['known_sum'] is None
    assert task['measurements']['wall_time_sec']['known_sum'] is None
    assert task['measurements']['wall_time_sec']['complete'] is False
    assert 'invalid_cost_units' in task['issues']
    json.dumps(task, allow_nan=False)


def test_derived_integer_token_total_is_validated_before_cost_conversion(db):
    add_run(db)
    add_attempt(db, tokens=split(10**308, 10**308, 10**308, 10**308), cost=2.0)
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['measurements']['tokens']['known_sum'] is None
    assert task['measurements']['cost_units']['known_sum'] is None
    assert 'invalid_tokens' in task['issues']
    json.dumps(task, allow_nan=False)


@pytest.mark.parametrize('field,value,reason', [
    ('source', float('nan'), 'invalid_sample_metadata'),
    ('window', 'garbage', 'invalid_window'),
    ('window', '0', 'invalid_window'),
    ('read_at', float('inf'), 'invalid_sample_metadata'),
])
def test_malformed_quota_envelopes_remain_json_and_unavailable(db, field, value, reason):
    add_run(db)
    start, end = json.loads(sample(10)), json.loads(sample(15, at='2026-01-01T00:00:10Z'))
    if field == 'window':
        for document in (start, end):
            document['windows'] = {value: document['windows']['300']}
    else:
        start[field] = value
        end[field] = value
    add_attempt(db, usage_start_json=json.dumps(start), usage_settle_json=json.dumps(end))
    api = reporting()
    report = api.build_usage_report(db)
    observation = report['tasks'][0]['quota_observations'][0]
    assert observation['observed_change_pct'] is None
    assert observation['unavailable_reason'] == reason
    json.loads(api.render_usage_report(report))


def test_recorded_w3_envelope_provenance_is_retained_without_claiming_freshness(db):
    add_run(db)
    start, end = json.loads(sample(10)), json.loads(sample(15, at='2026-01-01T00:00:10Z'))
    for document in (start, end):
        document.update(observed_at=document['updated_at'], staleness_bound=300,
                        pool_id='codex-subscription', read_at=document['updated_at'])
    add_attempt(db, usage_start_json=json.dumps(start), usage_settle_json=json.dumps(end))
    observation = reporting().build_usage_report(db)['tasks'][0]['quota_observations'][0]
    for name, original in [('start', start), ('settle', end)]:
        assert {field: observation[name][field] for field in ('observed_at', 'staleness_bound', 'pool_id')} == {
            field: original[field] for field in ('observed_at', 'staleness_bound', 'pool_id')
        }
    assert 'freshness_unverified' in observation['uncertainty']
    assert observation['observed_change_pct'] == 5


@pytest.mark.parametrize('version,expected,flag', [
    (1, 1, None), (7, 7, None), (None, None, 'rubric_version_missing'),
    (True, None, 'rubric_version_invalid'), (0, None, 'rubric_version_invalid'),
    ('1', None, 'rubric_version_invalid'),
])
def test_review_report_preserves_capture_identity_version_and_diff_flags(db, version, expected, flag):
    add_run(db)
    add_attempt(db)
    w2_event(db, 'ReviewComplexityObserved', {
        'ledger_id': 'review:a1', 'complexity': 3, 'rubric_version': version,
        'diff_stats': {'flags': ['diff_unavailable', 42, 'diff_unavailable']},
    })
    review = reporting().build_usage_report(db)['tasks'][0]['complexity_reviews'][0]
    assert review['ledger_id'] == 'review:a1'
    assert review['rubric_version'] == expected
    assert review['diff_stats']['flags'] == ['diff_unavailable']
    assert review['diff_stats']['files_changed'] is None
    assert review['flags'] == ([flag] if flag else [])


def test_mixed_review_origins_retain_missing_observation_and_spend_once(db):
    add_run(db)
    add_attempt(db, node='writer_name', tokens=split(), wall=4)
    w2_event(db, 'ExtraStepObserved', {
        'ledger_id': 'attempt:a1', 'observed_cause': 'missed_earlier',
        'observed_causes': ['missed_earlier', None], 'fault': 'unclassified',
        'context': {'role': 'reviewer'}, 'flags': ['multiple_faults', 'cause_missing'],
    })
    task = reporting().build_usage_report(db)['tasks'][0]
    assert task['blame_ledger'][0]['observed_causes'] == ['missed_earlier', None]
    assert task['blame_ledger'][0]['fault'] == 'unclassified'
    assert task['faults'][0]['attempts'] == 1
    assert task['faults'][0]['measurements']['tokens']['known_sum'] == 35
    assert task['faults'][0]['measurements']['wall_time_sec']['known_sum'] == 4


@pytest.mark.parametrize('second,captured_fault,captured_flags,revised_fault,expected', [
    ('fixed_incompletely_out_of_scope', 'reviewer', [], 'planner', 'unclassified'),
    ('not_fixed', 'unclassified', ['multiple_faults'], 'reviewer', 'reviewer'),
])
def test_report_remaps_all_review_origins_and_retains_captured_allocation(db, monkeypatch,
        second, captured_fault, captured_flags, revised_fault, expected):
    from maestro.workflows import telemetry
    add_run(db)
    add_attempt(db, node='writer_name', tokens=split())
    w2_event(db, 'ExtraStepObserved', {
        'ledger_id': 'attempt:a1', 'observed_cause': 'missed_earlier',
        'observed_causes': ['missed_earlier', second], 'fault': captured_fault,
        'flags': captured_flags, 'context': {'role': 'reviewer'},
    })
    monkeypatch.setattr(telemetry, 'CAUSE_FAULT', {**telemetry.CAUSE_FAULT, second: revised_fault})
    task = reporting().build_usage_report(db)['tasks'][0]
    row = task['blame_ledger'][0]
    assert row['fault'] == expected
    assert row['captured_fault'] == captured_fault
    assert row['captured_flags'] == captured_flags
    assert ('multiple_faults' in row['flags']) == (expected == 'unclassified')
    assert task['faults'][0]['fault'] == expected
    assert task['faults'][0]['measurements']['tokens']['known_sum'] == 35


def test_contextual_causes_preserve_roles_and_remap_without_duplicating_spend(db, monkeypatch):
    from maestro.workflows import telemetry
    add_run(db)
    add_attempt(db, node='writer_name', tokens=split(), wall=4)
    observations = [dict(cause='no_sentinel', role='implementer'),
                    dict(cause='garbage_output', role='reviewer')]
    w2_event(db, 'ExtraStepObserved', {
        'ledger_id': 'attempt:a1', 'observed_cause': 'garbage_output',
        'observed_causes': ['no_sentinel', 'garbage_output'], 'cause_observations': observations,
        'fault': 'unclassified', 'flags': ['multiple_faults'],
        'trigger_attempt_id': 'prior-writer', 'context': {'role': 'reviewer'},
    })
    task = reporting().build_usage_report(db)['tasks'][0]
    [row] = task['blame_ledger']
    assert row['fault'] == 'unclassified'
    assert row['role'] == 'reviewer'
    assert row['cause_observations'] == observations
    [allocation] = task['faults']
    assert allocation['attempts'] == 1
    assert allocation['measurements']['tokens']['known_sum'] == 35
    assert allocation['measurements']['wall_time_sec']['known_sum'] == 4
    monkeypatch.setattr(telemetry, 'CAUSE_FAULT', {
        **telemetry.CAUSE_FAULT, 'no_sentinel': 'maestro', 'garbage_output': 'maestro'})
    revised = reporting().build_usage_report(db)['tasks'][0]
    assert revised['blame_ledger'][0]['fault'] == 'maestro'
    assert revised['blame_ledger'][0]['captured_fault'] == 'unclassified'
    assert 'multiple_faults' not in revised['blame_ledger'][0]['flags']
    assert revised['faults'][0]['attempts'] == 1
    assert revised['faults'][0]['measurements']['tokens']['known_sum'] == 35


@pytest.mark.parametrize('contexts', [
    [None], 'not a list', [{'cause': 'missed_earlier'}, 17],
    [{'cause': 'missed_earlier', 'unexpected': float('nan')}, {'cause': ['unknown']}],
])
def test_malformed_contextual_evidence_stays_unknown_and_renderable(db, contexts):
    add_run(db)
    add_attempt(db, tokens=split())
    w2_event(db, 'ExtraStepObserved', {
        'observed_cause': 'missed_earlier', 'cause_observations': contexts,
        'observed_causes': ['missed_earlier'], 'fault': 'reviewer',
    })
    report = reporting().build_usage_report(db)
    [task] = report['tasks']
    [row] = task['blame_ledger']
    assert row['fault'] == 'unclassified'
    assert 'cause_context_invalid' in row['flags']
    assert task['faults'][0]['measurements']['tokens']['known_sum'] == 35
    reporting().render_usage_report(report)


def test_fault_accounting_covers_tasks_roles_routes_and_partial_mixed_pools(db):
    add_run(db)
    add_run(db, run='r2', task='t2')
    for aid, run, node, tokens, pool in [
        ('writer', 'r1', 'looks_like_gate', split(), 'pool-a'),
        ('review', 'r1', 'writer_name', split(1, 0, 0, 1), 'pool-b'),
        ('gate', 'r1', 'checks', None, 'pool-a'),
        ('other-task', 'r2', 'writer_name', split(2, 0, 0, 2), 'pool-a'),
    ]:
        add_attempt(db, aid=aid, run=run, node=node, tokens=tokens, wall=3, pool=pool)
        w2_event(db, 'ExtraStepObserved', {'observed_cause': 'escaped_defect'}, aid=aid, run=run)
    tasks = {t['task_id']: t for t in reporting().build_usage_report(db)['tasks']}
    [fault] = tasks['t1']['faults']
    assert fault['fault'] == 'reviewer'
    assert fault['attempts'] == fault['ledger_rows'] == 3
    assert fault['measurements']['tokens'] == {
        'known_sum': 37, 'measured_attempts': 2, 'total_attempts': 3, 'complete': False}
    assert fault['measurements']['wall_time_sec']['known_sum'] == 9
    assert fault['measurements']['cost_units']['known_sum'] is None
    routes = {r['role']: r for r in fault['routes']}
    assert set(routes) == {'implementer', 'reviewer', 'gate'}
    assert routes['implementer']['measurements']['cost_units']['known_sum'] == 70
    assert routes['reviewer']['measurements']['cost_units']['known_sum'] == 4
    assert routes['gate']['measurements']['tokens']['known_sum'] is None
    assert tasks['t2']['faults'][0]['measurements']['tokens']['known_sum'] == 4
    assert all(t['quota_consumption'] is None and t['faults'][0]['quota_consumption'] is None
               for t in tasks.values())


def test_usage_cli_reads_legacy_schema_and_handles_filters_and_errors(db, tmp_path, capsys):
    from maestro import cli
    add_run(db)
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE attempts')
        conn.execute('CREATE TABLE attempts(attempt_id TEXT, run_id TEXT, node_id TEXT, status TEXT)')
        conn.execute("INSERT INTO attempts VALUES ('old', 'r1', 'looks_like_gate', 'succeeded')")
        before = tuple(conn.iterdump())
    assert cli.main(['report', 'usage', '--database', str(db)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['tasks'][0]['measurements']['tokens']['known_sum'] is None
    assert report['tasks'][0]['blame_ledger'] == []
    assert cli.main(['report', 'usage', '--database', str(db), '--task', 'absent']) == 0
    assert json.loads(capsys.readouterr().out)['tasks'] == []
    with sqlite3.connect(db) as conn:
        assert tuple(conn.iterdump()) == before
    invalid = tmp_path / 'invalid.sqlite3'
    invalid.write_text('this is not SQLite')
    assert cli.main(['report', 'usage', '--database', str(invalid)]) == 1
    assert 'usage report:' in capsys.readouterr().err
    assert invalid.read_text() == 'this is not SQLite'
    with pytest.raises(SystemExit) as error:
        cli.main(['report', 'usage'])
    assert error.value.code == 2
