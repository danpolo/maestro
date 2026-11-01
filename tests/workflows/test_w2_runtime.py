"""Production W2 emission through the controller protocol, recovery and parser."""
import json
import subprocess

import pytest

from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.workflows import handlers, runner
from maestro.workflows.models import EdgeSpec, NodeSpec, NodeResult, WorkflowRevision, RetryPolicy
from maestro.usage_report import build_usage_report


@pytest.fixture
def runtime(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    store = ControlStore.open_controller(repo / '.orchestrator/control.sqlite3')
    opened = []
    def make(role='implementer', kind='agent', handler='maestro.handlers.agent', routing=None,
             params=None, code_base_sha='', downstream=False):
        nid = f'n{len(opened)}'
        node = NodeSpec(node_id=nid, kind=kind, handler_ref=handler,
                        agent_definition_ref=role if kind == 'agent' else None,
                        permissions_ref='frozen_snapshot_reader', retry_policy=RetryPolicy(max_attempts=3),
                        params=params or {})
        tail = NodeSpec(node_id=nid + '_tail', kind='agent', handler_ref='maestro.handlers.agent',
                        agent_definition_ref='reviewer', permissions_ref='frozen_snapshot_reader',
                        retry_policy=RetryPolicy(max_attempts=3))
        revision = WorkflowRevision(workflow_id=f'w{nid}', revision=1, task_id=f't{nid}',
            task_spec_revision=1, compiler_version='fixture', policy_hash='',
            code_base_sha=code_base_sha, execution_objective='paid_efficiency',
            nodes=(node, tail) if downstream else (node,),
            edges=(EdgeSpec(nid, tail.node_id, guard_predicate='on_success'),) if downstream else (),
            terminal_requirements=(tail.node_id,) if downstream else (nid,))
        dispatch = handlers.TmuxDispatcher(runner=lambda cmd, **kw:
            subprocess.CompletedProcess(cmd, 0, '', ''))
        run = runner.start_run(store, revision, repo=repo, dispatcher=dispatch,
            artifacts=ArtifactStore(repo / '.orchestrator/artifacts', store),
            is_alive=lambda pid: True, routing=routing)
        run.tick()
        row = store.conn.execute('SELECT * FROM attempts WHERE run_id=?', (run.run_id,)).fetchone()
        opened.append(run)
        return run, row, node
    yield store, make
    store.close()


def finish(run, row, *, status='failed', outputs=None):
    inbox = run.inbox(row['attempt_id'])
    inbox.write_result(NodeResult(run_id=run.run_id, node_id=row['node_id'],
        attempt_id=row['attempt_id'], status=status, outputs=outputs or {}))
    op = run.store.intents_for_attempt(row['attempt_id'])[0]['operation_id']
    inbox.write_receipt(operation_id=op, exit_status=0 if status == 'succeeded' else 1)
    assert run.ingest_results() == [row['node_id']]
    assert run.ingest_results() == []
    return build_usage_report(run.store.path, task_id=run.revision.task_id)['tasks'][0]


@pytest.mark.parametrize('kind,cause,fault', [
    ('garbage_output', 'garbage_output', 'implementer'),
    ('no_sentinel', 'no_sentinel', 'implementer'),
    ('worker_crashed', 'worker_crashed', 'harness'),
    ('quota_exhausted', 'quota', 'unclassified'),
    ('rate_limited', 'rate_limit', 'unclassified'),
    ('provider_unavailable', 'endpoint', 'unclassified'),
    ('controller_error', 'controller', 'maestro'),
    ('unknown', 'unclassified', 'unclassified'),
])
def test_actual_result_ingest_emits_one_row_and_replay_is_noop(runtime, kind, cause, fault):
    store, make = runtime
    run, row, _ = make()
    task = finish(run, row, outputs={'failure': {'kind': kind}, 'fault': 'planner'})
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['extra_step_cause'] == cause
    assert task['blame_ledger'][0]['fault'] == fault
    assert len([e for e in store.events() if e.type == 'ExtraStepObserved']) == 1


def test_first_happy_path_has_no_extra_row(runtime):
    _, make = runtime
    run, row, _ = make()
    assert finish(run, row, status='succeeded')['blame_ledger'] == []


def test_w4_review_independence_uses_the_persisted_writer_after_restart(runtime, monkeypatch):
    from maestro.backends import catalog as cat
    from maestro.taskgraph import normalize_spec
    from tests.graph_engineering.conftest import fixture_cli_entry, fixture_routing
    store, make = runtime
    def entry(backend, family):
        return fixture_cli_entry(backend_id=backend, usage_pool_id=backend,
            model_profiles={'fixture-1': cat.ModelProfile('fixture-1', model_family=family)},
            pair_profiles=cat.migrate_pairs(('fixture-1', 'strong', 1.0)), pairs_migrated=True)
    routing = fixture_routing(entries=[entry('native', 'claude'), entry('agy', 'claude'),
                                       entry('gemini', 'gemini')], preferred_pool='native')
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w4', 'kind': 'auto', 'cmd': 'true'}]})
    run, writer, node = make(routing=routing, downstream=True)
    assert writer['backend_id'] == 'native'
    finish(run, writer, status='succeeded')
    restarted = runner.WorkflowRunner(store, run.revision, run_id=run.run_id,
        repo=run.repo, routing=routing, dispatcher=run.dispatcher, artifacts=run.artifacts)
    monkeypatch.setattr(restarted, 'risky_files_touched', lambda tree: ('reviewed', []))
    restarted.tick()
    review = store.conn.execute('SELECT binding_json FROM attempts WHERE run_id=? AND node_id=?',
                                (run.run_id, node.node_id + '_tail')).fetchone()
    assert review is not None
    binding = json.loads(review['binding_json'])
    assert binding['backend_id'] == 'gemini'
    assert binding['model_family'] == 'gemini'


def test_w4_review_counts_all_writer_attempts_after_a_retry(runtime, monkeypatch):
    from maestro.backends import catalog as cat
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.failures import FailureRecord
    from tests.graph_engineering.conftest import fixture_cli_entry, fixture_routing
    store, make = runtime
    entries = [fixture_cli_entry(backend_id=name, usage_pool_id=name,
        model_profiles={'fixture-1': cat.ModelProfile('fixture-1', model_family=name)},
        pair_profiles=cat.migrate_pairs(('fixture-1', 'strong', 1.0)), pairs_migrated=True)
        for name in ('claude', 'gemini', 'gpt')]
    routing = fixture_routing(entries=entries, preferred_pool='claude')
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w4', 'kind': 'auto', 'cmd': 'true'}]})
    run, first, node = make(routing=routing, downstream=True)
    assert first['backend_id'] == 'claude'
    finish(run, first, outputs={'failure': {'kind': 'no_sentinel'}})
    # A failed writer can leave work that the retry keeps or amends.
    with store.conn:
        store.conn.execute('UPDATE attempts SET start_sha=?, end_sha=? WHERE attempt_id=?',
                           ('base', 'draft', first['attempt_id']))
    run.lifecycle.requeue_cone(first['attempt_id'], run_id=run.run_id,
        root=node.node_id, cone=(node.node_id,), record=FailureRecord(kind='no_sentinel'))
    routing.resources = fixture_routing(entries=entries, preferred_pool='gemini').resources
    run.tick()
    second = store.conn.execute('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid DESC',
                                (run.run_id,)).fetchone()
    assert second['backend_id'] == 'gemini'
    finish(run, second, status='succeeded')
    restarted = runner.WorkflowRunner(store, run.revision, run_id=run.run_id,
        repo=run.repo, routing=routing, dispatcher=run.dispatcher, artifacts=run.artifacts)
    monkeypatch.setattr(restarted, 'risky_files_touched', lambda tree: ('reviewed', []))
    restarted.tick()
    review = store.conn.execute('SELECT binding_json FROM attempts WHERE run_id=? AND node_id=?',
                                (run.run_id, node.node_id + '_tail')).fetchone()
    assert review is not None
    assert json.loads(review['binding_json'])['model_family'] == 'gpt'


@pytest.mark.parametrize('case,cause,fault', [
    ('check', 'escaped_defect', 'reviewer'),
    ('conflict', 'parallel_scope_overlap', 'planner'),
    ('guard', 'unclassified', 'unclassified'),
])
def test_real_merge_pipeline_retains_only_observed_causes(runtime, monkeypatch, case, cause, fault):
    from maestro import validation
    import maestro.state
    store, make = runtime
    run, row, node = make(kind='function', handler='maestro.handlers.merge')
    repo = run.repo
    monkeypatch.setattr(maestro.state, 'JOURNAL', repo / '.orchestrator/journal.ndjson')
    monkeypatch.setattr(maestro.state, 'control_store', lambda: store)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'W2 merge fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / '.gitignore').write_text('.orchestrator/\n')
    (repo / 'app.py').write_text('VALUE = 1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    git('checkout', '-qb', 'impl/w2')
    (repo / 'app.py').write_text('VALUE = 2\n')
    if case == 'guard':
        (repo / '.env').write_text('FIXTURE=private\n')
    git('add', '.')
    git('commit', '-qm', 'candidate')
    git('checkout', '-q', 'main')
    if case == 'conflict':
        (repo / 'app.py').write_text('VALUE = 3\n')
        git('add', '.')
        git('commit', '-qm', 'overlapping task')
    before = git('rev-parse', 'main')
    spec = validation.VerificationSpec(check_id='V-merge', acceptance_criterion_ids=('V-merge',),
        argv_or_adapter_ref=('false',), cache_policy='never')
    ctx = handlers.HandlerContext(run_id=run.run_id, task_id=run.revision.task_id,
        node=node, attempt_id=row['attempt_id'], inbox=run.inbox(row['attempt_id']),
        workspace=repo, params={'task_id': run.revision.task_id, 'branch': 'impl/w2'},
        inputs={'specs': [spec]}, controller=run)
    result = handlers.merge_handler(ctx)
    assert result.status == 'failed'
    assert result.outputs['stage'] == {'check': 'verify', 'conflict': 'stage', 'guard': 'guard'}[case]
    assert git('rev-parse', 'main') == before
    task = finish(run, row, status=result.status, outputs=result.outputs)
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['extra_step_cause'] == cause
    assert task['blame_ledger'][0]['fault'] == fault


@pytest.mark.parametrize('handler,reason,cause,fault', [
    ('human', 'manual_redo', 'manual_redo', 'prep'),
    ('human', 'brief_check_disagree', 'brief_check_disagree', 'planner'),
    ('human', 'please redo: brief_check_disagree', 'unclassified', 'unclassified'),
    ('dan_confirm', 'accept_rejected_review', 'accept_rejected_review', 'reviewer'),
    ('dan_confirm', 'accept_rejected_plan', 'accept_rejected_plan', 'planner'),
    ('dan_confirm', 'the plan looks wrong', 'unclassified', 'unclassified'),
])
def test_real_operator_handler_retains_explicit_reason_only(runtime, handler, reason, cause, fault):
    from types import SimpleNamespace
    from maestro.hitl.verify import DanVerdict
    _, make = runtime
    run, row, node = make(kind='human' if handler == 'human' else 'function',
                           handler=f'maestro.handlers.{handler}')
    if handler == 'human':
        for args in [('init', '-q'), ('config', 'user.name', 'W2 manual fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'),
                     ('commit', '-q', '--allow-empty', '-m', 'manual subject')]:
            subprocess.run(['git', '-C', str(run.repo), *args], check=True)
    channel = SimpleNamespace(
        poll_action=lambda **kw: DanVerdict('redo', reason, 'action-q'),
        poll=lambda **kw: DanVerdict('rejected', reason, 'verify-q'))
    controller = SimpleNamespace(repo=run.repo, dan_channel=channel, task_tree_path=lambda: run.repo,
        verified_snapshot=lambda: 'frozen-subject',
        manual_step=lambda node: {'dan_action': 'printf fixture', 'post_action_cmd': 'true',
            'step_command': 'printf fixture', 'step_check_cmd': 'git cat-file -e HEAD^{commit}',
            'expected_result': 'fixture'})
    ctx = handlers.HandlerContext(run_id=run.run_id, task_id=run.revision.task_id,
        node=node, attempt_id=row['attempt_id'], inbox=run.inbox(row['attempt_id']),
        workspace=run.repo, params={'task_id': run.revision.task_id, 'dan_checks': [{'id': 'V-dan'}]},
        controller=controller)
    result = handlers.resolve(node.handler_ref)(ctx)
    assert result.status == 'failed'
    assert result.outputs['reason'] == reason
    task = finish(run, row, status=result.status, outputs=result.outputs)
    assert task['blame_ledger'][0]['extra_step_cause'] == cause
    assert task['blame_ledger'][0]['fault'] == fault


def test_invalid_review_score_survives_parser_capture_and_report(runtime, tmp_path):
    _, make = runtime
    run, row, node = make(role='reviewer')
    ctx = handlers.HandlerContext(run_id=run.run_id, task_id=run.revision.task_id,
        node=node, attempt_id=row['attempt_id'], inbox=run.inbox(row['attempt_id']),
        workspace=tmp_path, inputs={'review_round': 1})
    (tmp_path / 'review.json').write_text(json.dumps(
        {'verdict': 'approved', 'findings': [], 'complexity': 'not a score'}))
    result = handlers.adopt_review(ctx, ctx.result('succeeded'))
    task = finish(run, row, status=result.status, outputs=result.outputs)
    review = task['complexity_reviews'][0]
    assert review['complexity'] is None
    assert 'complexity_invalid' in review['flags']
    assert 'complexity_missing' not in review['flags']


def test_unreadable_manifest_retains_review_score_and_unknown_diff(runtime):
    store, make = runtime
    run, row, _ = make(role='reviewer')
    bad = run.artifacts.put_bytes(b'{bad json', type='context_manifest')
    store.conn.execute('UPDATE attempts SET manifest_artifact_id=? WHERE attempt_id=?',
                       (bad.artifact_id, row['attempt_id']))
    task = finish(run, row, status='succeeded', outputs={'review': {'round': 1, 'complexity': 3}})
    assert task['complexity_reviews'][0]['complexity'] == 3
    assert task['complexity_reviews'][0]['diff_stats']['files_changed'] is None
    assert 'diff_unavailable' in task['complexity_reviews'][0]['diff_stats']['flags']


@pytest.mark.parametrize('case', ['missing_artifact', 'non_dict_manifest', 'git_failure'])
def test_missing_diff_evidence_does_not_drop_the_review_or_change_verdict(runtime, case):
    store, make = runtime
    run, row, _ = make(role='reviewer', code_base_sha='absent-base')
    content = b'[]' if case == 'non_dict_manifest' else b'{"reviewed_commit":"absent-head"}'
    manifest = run.artifacts.put_bytes(content, type='context_manifest')
    if case == 'missing_artifact':
        run.artifacts.path_for(manifest.content_hash).unlink()
    store.conn.execute('UPDATE attempts SET manifest_artifact_id=? WHERE attempt_id=?',
                       (manifest.artifact_id, row['attempt_id']))
    task = finish(run, row, status='succeeded', outputs={'review': {'round': 1, 'complexity': 2}})
    [review] = task['complexity_reviews']
    assert review['complexity'] == 2
    assert all(review['diff_stats'][key] is None for key in
               ('files_changed', 'insertions', 'deletions', 'binary_files'))
    assert review['diff_stats']['flags'] == ['diff_unavailable']
    assert task['attempt_details'][0]['status'] == 'succeeded'


@pytest.mark.parametrize('blocked,cause', [('saturated', 'unclassified'), ('quota', 'quota')])
def test_real_route_wait_is_one_durable_episode_without_spend(runtime, blocked, cause):
    import time
    from datetime import datetime, timezone
    from maestro.backends.catalog import QuotaObservation, QuotaWindow
    from maestro.taskgraph import normalize_spec
    from tests.graph_engineering.conftest import fixture_routing
    store, make = runtime
    routing = fixture_routing()
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w2', 'kind': 'auto', 'cmd': 'true'}]})
    pool = routing.resources.pool('fixture-pool')
    if blocked == 'saturated':
        pool.active = pool.concurrency_limit
    else:
        pool.observation = QuotaObservation('fixture-pool',
            {300: QuotaWindow(300, 99, time.time() + 3600)},
            observed_at=datetime.now(timezone.utc).isoformat())
    run, row, node = make(routing=routing)
    assert row is None
    for _ in range(3):
        assert node.node_id in run.tick().waiting
    restarted = runner.WorkflowRunner(store, run.revision, run_id=run.run_id,
        repo=run.repo, routing=routing, dispatcher=run.dispatcher, artifacts=run.artifacts)
    assert node.node_id in restarted.tick().waiting
    task = build_usage_report(store.path)['tasks'][0]
    assert len(task['blame_ledger']) == 1
    wait = task['blame_ledger'][0]
    assert wait['attempt_id'] is None
    assert wait['extra_step_cause'] == cause
    assert wait['fault'] == 'unclassified'  # W5 admission evidence is unavailable
    assert task['faults'][0]['attempts'] == 0
    assert len([e for e in store.events() if e.type == 'ExtraStepObserved']) == 1


def test_ineligible_routes_do_not_hide_the_actual_temporal_quota_wait(runtime):
    import time
    from datetime import datetime, timezone
    from maestro.backends.catalog import QuotaObservation, QuotaWindow, migrate_pairs
    from maestro.taskgraph import normalize_spec
    from tests.graph_engineering.conftest import fixture_routing, fixture_cli_entry
    store, make = runtime
    routing = fixture_routing(entries=[fixture_cli_entry(), fixture_cli_entry(
        backend_id='weak', usage_pool_id='weak-pool',
        pair_profiles=migrate_pairs(('fixture-1', 'light', 1.0)), pairs_migrated=True)])
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w2', 'kind': 'auto', 'cmd': 'true'}]})
    routing.resources.pool('fixture-pool').observation = QuotaObservation('fixture-pool',
        {300: QuotaWindow(300, 99, time.time() + 3600)},
        observed_at=datetime.now(timezone.utc).isoformat())
    run, row, _ = make(routing=routing)
    assert row is None
    [wait] = build_usage_report(store.path)['tasks'][0]['blame_ledger']
    assert wait['observed_causes'] == ['quota']
    assert wait['extra_step_cause'] == 'quota'
    assert 'multiple_faults' not in wait['flags']


def test_a_wait_after_the_next_claim_is_a_new_episode(runtime):
    from maestro.taskgraph import normalize_spec
    from tests.graph_engineering.conftest import fixture_routing
    store, make = runtime
    routing = fixture_routing()
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w2', 'kind': 'auto', 'cmd': 'true'}]})
    pool = routing.resources.pool('fixture-pool')
    pool.active = pool.concurrency_limit
    run, row, node = make(routing=routing)
    assert row is None
    pool.active = 0
    run.tick()
    first = store.conn.execute('SELECT * FROM attempts WHERE run_id=?', (run.run_id,)).fetchone()
    finish(run, first, outputs={'failure': {'kind': 'quota_exhausted',
                                          'reset_at': '2099-01-01T00:00:00Z'}})
    for _ in range(3):
        assert node.node_id in run.tick().waiting
    task = build_usage_report(store.path)['tasks'][0]
    waits = [r for r in task['blame_ledger'] if r['attempt_id'] is None]
    assert len(waits) == 2
    assert waits[0]['ledger_id'] != waits[1]['ledger_id']
    assert len(task['attempt_details']) == 1


@pytest.mark.parametrize('case,cause,fault', [
    ('nonzero', 'check_failed', 'implementer'),
    ('no_command', 'check_broken', 'planner'),
    ('wrong_cwd', 'check_broken', 'planner'),
    ('missing_tool', 'environment_missing', 'maestro'),
    ('no_tests', 'check_broken', 'planner'),
    ('mixed', 'check_failed', 'unclassified'),
])
def test_real_gate_check_execution_retains_typed_cause(runtime, monkeypatch, case, cause, fault):
    import sys
    from maestro import gates, validation
    _, make = runtime
    run, row, node = make(kind='function', handler='maestro.handlers.verification')
    args = dict(check_id='V-w2', acceptance_criterion_ids=('V-w2',),
                argv_or_adapter_ref=(sys.executable, '-c',
                    'import sys; print("environment_missing bad_graph"); sys.exit(1)'))
    if case == 'no_command':
        args['argv_or_adapter_ref'] = ()
    elif case == 'wrong_cwd':
        args['cwd_scope'] = 'does-not-exist'
    elif case == 'missing_tool':
        args['argv_or_adapter_ref'] = (str(run.repo / 'absent-tool'),)
    elif case == 'no_tests':
        args['argv_or_adapter_ref'] = (sys.executable, '-m', 'pytest', '-q', str(run.repo))
    spec = validation.VerificationSpec(**args)
    specs = [spec]
    if case == 'mixed':
        specs.append(validation.VerificationSpec(check_id='V-broken',
            argv_or_adapter_ref=('true',), cwd_scope='absent-directory',
            acceptance_criterion_ids=('V-broken',)))
    monkeypatch.setattr(gates, 'authoritative_specs', lambda *a, **kw: specs)
    ctx = handlers.HandlerContext(run_id=run.run_id, task_id=run.revision.task_id,
        node=node, attempt_id=row['attempt_id'], inbox=run.inbox(row['attempt_id']),
        workspace=run.repo, worktree=run.repo, params={'task_id': run.revision.task_id})
    result = handlers.verification_handler(ctx)
    assert result.status == 'failed'
    first = validation.VerificationEvidence.from_row(result.outputs['evidence'][0])
    assert first.extra_step_cause == cause
    task = finish(run, row, status=result.status, outputs=result.outputs)
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['extra_step_cause'] == cause
    assert task['blame_ledger'][0]['fault'] == fault
    if case == 'mixed':
        assert 'multiple_faults' in task['blame_ledger'][0]['flags']


def test_real_dirty_tree_gate_records_implementer_cause(runtime):
    _, make = runtime
    run, row, node = make(kind='function', handler='maestro.handlers.verification')
    subprocess.run(['git', 'init', '-q', str(run.repo)], check=True)
    (run.repo / 'uncommitted.py').write_text('changed\n')
    ctx = handlers.HandlerContext(run_id=run.run_id, task_id=run.revision.task_id,
        node=node, attempt_id=row['attempt_id'], inbox=run.inbox(row['attempt_id']),
        workspace=run.repo, worktree=run.repo, params={'task_id': run.revision.task_id})
    result = handlers.verification_handler(ctx)
    assert result.status == 'failed'
    task = finish(run, row, status=result.status, outputs=result.outputs)
    assert task['blame_ledger'][0]['extra_step_cause'] == 'uncommitted_tree'
    assert task['blame_ledger'][0]['fault'] == 'implementer'


def test_real_routed_claim_and_restart_keep_price_when_current_catalog_changes(runtime):
    from dataclasses import replace
    from maestro.backends.catalog import CapabilityCatalog
    from maestro.taskgraph import normalize_spec
    from tests.graph_engineering.conftest import fixture_routing
    store, make = runtime
    routing = fixture_routing()
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w2', 'kind': 'auto', 'cmd': 'true'}]})
    routing.token_reader = lambda *a: {'fresh_input': 10, 'cache_read': 20,
                                     'cache_write': 0, 'output': 5}
    run, row, _ = make(routing=routing)
    assert row is not None
    frozen = row['binding_json']
    assert json.loads(frozen)['relative_cost'] == 1.0
    entry = routing.catalog.entry('fixture_cli')
    routing.catalog = CapabilityCatalog.of([replace(entry,
        pair_profiles={key: replace(pair, relative_cost=9.0)
                       for key, pair in entry.pair_profiles.items()})])
    restarted = runner.WorkflowRunner(store, run.revision, run_id=run.run_id,
        repo=run.repo, routing=routing, dispatcher=run.dispatcher,
        artifacts=run.artifacts, is_alive=lambda pid: True)
    task = finish(restarted, row, status='succeeded')
    assert store.conn.execute('SELECT binding_json FROM attempts WHERE attempt_id=?',
                             (row['attempt_id'],)).fetchone()[0] == frozen
    assert task['measurements']['tokens']['known_sum'] == 35
    assert task['measurements']['cost_units']['known_sum'] == 35


def test_real_reviewer_claim_diff_ignores_changed_current_head(runtime):
    from maestro.backends.catalog import migrate_pairs
    from maestro.taskgraph import normalize_spec
    from tests.graph_engineering.conftest import fixture_cli_entry, fixture_routing
    store, make = runtime
    repo = store.path.parent.parent
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'W2 runtime fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / '.gitignore').write_text('.orchestrator/\n')
    (repo / 'app.py').write_text('first\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (repo / 'app.py').write_text('first\nreviewed\n')
    git('add', '.')
    git('commit', '-qm', 'review subject')
    subject = git('rev-parse', 'HEAD')
    routing = fixture_routing(entries=[fixture_cli_entry(
        pair_profiles=migrate_pairs(('fixture-1', 'strong', 1.0)))])
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-w2', 'kind': 'auto', 'cmd': 'true'}]})
    run, row, _ = make(role='reviewer', routing=routing, code_base_sha=base,
                       params={'worktree': str(repo)})
    assert row is not None
    manifest = json.loads(run.artifacts.read(row['manifest_artifact_id']))
    assert manifest['reviewed_commit'] == subject
    (repo / 'app.py').write_text('unrelated later change\n' * 10)
    git('add', '.')
    git('commit', '-qm', 'later HEAD')
    task = finish(run, row, status='succeeded', outputs={'review': {'round': 1, 'complexity': 2}})
    diff = task['complexity_reviews'][0]['diff_stats']
    assert diff['base_sha'] == base
    assert diff['head_sha'] == subject
    assert diff['head_sha'] != git('rev-parse', 'HEAD')
    assert (diff['files_changed'], diff['insertions'], diff['deletions']) == (1, 1, 0)


@pytest.mark.parametrize('origins,fault', [(['missed_earlier'], 'reviewer'),
    (['not_fixed', 'introduced_by_rework'], 'implementer'),
    (['not_fixed', 'missed_earlier'], 'unclassified')])
def test_real_later_review_keeps_origins_and_one_attempt(runtime, origins, fault):
    _, make = runtime
    run, row, _ = make(role='reviewer')
    task = finish(run, row, status='succeeded', outputs={'review': {'round': 2,
        'complexity': 3, 'finding_records': [{'severity': 'blocking', 'origin': c} for c in origins]}})
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['fault'] == fault
    assert task['blame_ledger'][0]['observed_causes'] == origins
    assert task['complexity_reviews'][0]['complexity'] == 3


@pytest.mark.parametrize('kind,ruling,cause,fault', [
    ('DenyListHitRuled', 'approved', 'deny_approved', 'maestro'),
    ('DenyListHitRuled', 'disapproved', 'deny_disapproved', 'implementer'),
    ('PermissionRuled', 'allowed', 'permission_allowed', 'maestro'),
    ('PermissionRuled', 'declined', 'permission_declined', 'implementer')])
def test_controller_operator_events_amend_asking_attempt(runtime, kind, ruling, cause, fault):
    store, make = runtime
    run, row, _ = make()
    asked = 'DenyListHitAsked' if kind.startswith('Deny') else 'PermissionAsked'
    store.append_event(asked, run.revision.task_id, run_id=run.run_id,
        attempt_id=row['attempt_id'], payload={'node_id': row['node_id'], 'request_ids': ['q']})
    store.append_event(kind, run.revision.task_id, run_id=run.run_id,
        payload={'request_id': 'q', 'ruling': ruling})
    task = build_usage_report(store.path)['tasks'][0]
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['attempt_id'] == row['attempt_id']
    assert task['blame_ledger'][0]['extra_step_cause'] == cause
    assert task['blame_ledger'][0]['fault'] == fault


@pytest.mark.parametrize('family,rulings', [
    ('DenyListHit', ('approved', 'disapproved')),
    ('Permission', ('allowed', 'declined')),
])
def test_multiple_operator_rulings_keep_contrary_evidence_and_one_allocation(runtime, family, rulings):
    store, make = runtime
    run, row, _ = make()
    store.append_event(family + 'Asked', run.revision.task_id, run_id=run.run_id,
        attempt_id=row['attempt_id'], payload={'request_ids': ['q1', 'q2']})
    for request, ruling in zip(('q1', 'q2'), rulings):
        store.append_event(family + 'Ruled', run.revision.task_id, run_id=run.run_id,
            payload={'request_id': request, 'ruling': ruling})
    task = build_usage_report(store.path)['tasks'][0]
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['fault'] == 'unclassified'
    assert 'multiple_faults' in task['blame_ledger'][0]['flags']
    assert len(task['blame_ledger'][0]['observed_causes']) == 2


def test_mixed_question_families_and_later_failure_keep_all_evidence_after_restart(runtime):
    store, make = runtime
    run, row, _ = make()
    for family, request, ruling in [('DenyListHit', 'deny-q', 'approved'),
                                    ('Permission', 'permission-q', 'declined')]:
        store.append_event(family + 'Asked', run.revision.task_id, run_id=run.run_id,
            attempt_id=row['attempt_id'], payload={'request_ids': [request]})
        store.append_event(family + 'Ruled', run.revision.task_id, run_id=run.run_id,
            payload={'request_id': request, 'ruling': ruling})
    store.append_event('DanQuestionAsked', run.revision.task_id, run_id=run.run_id,
        attempt_id=row['attempt_id'], payload={'request_id': 'spec-q'})
    task = finish(run, row, outputs={'failure': {'kind': 'no_sentinel'}})
    [fact] = task['blame_ledger']
    assert set(fact['observed_causes']) == {
        'deny_approved', 'permission_declined', 'spec_gap', 'no_sentinel'}
    assert fact['fault'] == 'unclassified'
    assert 'multiple_faults' in fact['flags']
    old_callback = next(e for e in store.events() if e.type == 'PermissionRuled')
    before = [tuple(event) for event in store.conn.execute('SELECT * FROM events ORDER BY sequence')]
    store.close()
    reopened = ControlStore.open_controller(store.path)
    try:
        restored = runner.WorkflowRunner(reopened, run.revision, run_id=run.run_id, repo=run.repo,
            dispatcher=run.dispatcher, artifacts=ArtifactStore(run.repo / '.orchestrator/artifacts', reopened),
            is_alive=lambda pid: True)
        assert restored.ingest_results() == []
        from maestro.workflows.telemetry_runtime import observe_event
        with reopened.transaction() as conn:
            observe_event(reopened, old_callback, conn=conn)
            observe_event(reopened, old_callback, conn=conn)
        assert build_usage_report(reopened.path)['tasks'][0]['blame_ledger'] == [fact]
        assert [tuple(event) for event in reopened.conn.execute('SELECT * FROM events ORDER BY sequence')] == before
    finally:
        reopened.close()


def test_standalone_recovery_observes_dead_worker_once(runtime):
    from maestro.control.reconcile import Reconciler
    store, make = runtime
    run, row, _ = make()
    run.lifecycle.start_attempt(row['attempt_id'], run_id=run.run_id, writer_pid=987654)
    recovery = Reconciler(store, attempts_root=handlers.attempts_root(run.repo), is_alive=lambda pid: False)
    assert len(recovery.reconcile()) == 1
    assert len(recovery.reconcile()) == 0
    task = build_usage_report(store.path)['tasks'][0]
    assert len(task['blame_ledger']) == 1
    assert task['blame_ledger'][0]['extra_step_cause'] == 'worker_crashed'


def test_failed_capture_does_not_change_successful_ingest(runtime, monkeypatch):
    from maestro.workflows import telemetry_capture
    _, make = runtime
    run, row, _ = make(role='reviewer')
    def broken(*args, **kw):
        raise OSError('telemetry offline')
    monkeypatch.setattr(telemetry_capture, 'record_review', broken)
    monkeypatch.setattr(telemetry_capture, 'record_extra_step', broken)
    task = finish(run, row, status='succeeded', outputs={'review': {'round': 2, 'complexity': 4}})
    assert run.phases()[row['node_id']] == 'succeeded'
    assert task['blame_ledger'] == []


@pytest.mark.parametrize('score', [4, None, True, '4', 6])
def test_real_review_parser_keeps_nullable_score_without_deciding_verdict(tmp_path, score):
    node = NodeSpec(node_id='review', kind='agent', handler_ref='maestro.handlers.agent',
                    agent_definition_ref='reviewer')
    ctx = handlers.HandlerContext(run_id='r', task_id='t', node=node, attempt_id='a',
        inbox=handlers.AttemptInbox(tmp_path / 'inbox', 'a'), workspace=tmp_path,
        inputs={'review_round': 1})
    (tmp_path / 'review.json').write_text(json.dumps({'verdict': 'approved', 'findings': [], 'complexity': score}))
    result = handlers.adopt_review(ctx, ctx.result('succeeded'))
    assert result.status == 'succeeded'
    assert result.outputs['review']['complexity'] == (4 if type(score) is int and score == 4 else None)
    assert result.outputs['review']['rubric_version'] == 1


def test_reviewer_brief_asks_for_fixed_after_the_fact_rubric(tmp_path):
    from maestro.workflows.worker import task_header
    from maestro.workflows.telemetry import complexity_prompt
    node = NodeSpec(node_id='review', kind='agent', handler_ref='maestro.handlers.agent',
                    agent_definition_ref='reviewer')
    ctx = handlers.HandlerContext(run_id='r', task_id='t', node=node, attempt_id='a',
        inbox=handlers.AttemptInbox(tmp_path / 'inbox', 'a'), workspace=tmp_path, inputs={})
    assert complexity_prompt() in task_header(ctx)


def test_operator_capture_failure_rolls_back_only_diagnostics(runtime, monkeypatch):
    from maestro.workflows import telemetry_runtime
    store, make = runtime
    run, row, _ = make()
    def broken(store, event, *, conn):
        store.append_event('ExtraStepObserved', 'n', run_id=run.run_id,
            attempt_id=row['attempt_id'], payload={'ledger_id': 'partial'}, conn=conn)
        raise OSError('offline')
    monkeypatch.setattr(telemetry_runtime, 'observe_event', broken)
    event = store.append_event('PermissionAsked', 't', run_id=run.run_id,
        attempt_id=row['attempt_id'], payload={'request_ids': ['q']})
    assert store.events()[-1].event_id == event.event_id
    assert not [e for e in store.events() if e.type == 'ExtraStepObserved']


def test_successful_rework_keeps_trigger_cause_and_link(runtime):
    from maestro.workflows.failures import FailureRecord
    store, make = runtime
    run, first, node = make()
    finish(run, first, outputs={'failure': {'kind': 'no_sentinel'}})
    run.lifecycle.requeue_cone(first['attempt_id'], run_id=run.run_id,
        root=node.node_id, cone=(node.node_id,), record=FailureRecord(kind='no_sentinel'))
    run.tick()
    second = store.conn.execute('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid DESC',
                               (run.run_id,)).fetchone()
    assert second['attempt_id'] != first['attempt_id']
    task = finish(run, second, status='succeeded')
    assert len(task['blame_ledger']) == 2
    rework = next(r for r in task['blame_ledger'] if r['attempt_id'] == second['attempt_id'])
    assert rework['extra_step_cause'] == 'no_sentinel'
    assert rework['trigger_attempt_id'] == first['attempt_id']


@pytest.mark.parametrize('prior_tail', [False, True])
def test_reset_downstream_claim_uses_actual_trigger_after_runner_restart(runtime, prior_tail):
    from maestro.workflows.failures import FailureRecord
    store, make = runtime
    run, first, node = make(downstream=True)
    tail = node.node_id + '_tail'
    if prior_tail:
        finish(run, first, status='succeeded')
        run.tick()
        previous_tail = store.conn.execute('SELECT * FROM attempts WHERE run_id=? AND node_id=?',
                                           (run.run_id, tail)).fetchone()
        finish(run, previous_tail, outputs={'failure': {'kind': 'garbage_output'}})
        run.lifecycle.requeue_cone(previous_tail['attempt_id'], run_id=run.run_id,
            root=node.node_id, cone=(node.node_id, tail), record=FailureRecord(kind='garbage_output'))
        run.tick()
        first = store.conn.execute('SELECT * FROM attempts WHERE run_id=? AND node_id=? ORDER BY rowid DESC',
                                   (run.run_id, node.node_id)).fetchone()
    finish(run, first, outputs={'failure': {'kind': 'no_sentinel'}})
    # The reset trigger is the writer even if the descendant has its own prior attempt.
    run.lifecycle.requeue_cone(first['attempt_id'], run_id=run.run_id, root=node.node_id,
        cone=(node.node_id, tail), record=FailureRecord(kind='no_sentinel'))
    restarted = run.spawn(run.run_id, run.revision)
    restarted.tick()
    second = store.conn.execute('SELECT * FROM attempts WHERE run_id=? AND node_id=? ORDER BY rowid DESC',
                                (run.run_id, node.node_id)).fetchone()
    finish(restarted, second, status='succeeded')
    restarted.tick()
    claimed_tail = store.conn.execute('SELECT * FROM attempts WHERE run_id=? AND node_id=? ORDER BY rowid DESC',
                                      (run.run_id, tail)).fetchone()
    assert claimed_tail is not None
    if prior_tail:
        assert claimed_tail['attempt_id'] != previous_tail['attempt_id']
    task = build_usage_report(store.path, task_id=run.revision.task_id)['tasks'][0]
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] == claimed_tail['attempt_id']]
    assert len(rows) == 1
    assert rows[0]['trigger_attempt_id'] == first['attempt_id']
    assert rows[0]['extra_step_cause'] == 'no_sentinel'
    assert rows[0]['fault'] == ('unclassified' if prior_tail else 'implementer')
    if prior_tail:
        assert rows[0]['observed_causes'] == ['garbage_output', 'no_sentinel']
        assert 'multiple_faults' in rows[0]['flags']
    before = len([e for e in store.events() if e.type == 'ExtraStepObserved'])
    restarted.tick()
    assert len([e for e in store.events() if e.type == 'ExtraStepObserved']) == before


def test_capture_internal_write_error_leaves_no_partial_event(runtime, monkeypatch):
    store, make = runtime
    run, row, _ = make()
    real_append = store.append_event
    def broken(kind, *args, **kw):
        event = real_append(kind, *args, **kw)
        if kind == 'ExtraStepObserved':
            raise OSError('disk failed after insert')
        return event
    monkeypatch.setattr(store, 'append_event', broken)
    real_append('PermissionAsked', 't', run_id=run.run_id,
        attempt_id=row['attempt_id'], payload={'request_ids': ['q']})
    assert [e.type for e in store.events()][-1] == 'PermissionAsked'
    assert not [e for e in store.events() if e.type == 'ExtraStepObserved']


def test_uncertain_result_records_controller_cause(runtime):
    _, make = runtime
    run, row, _ = make()
    task = finish(run, row, status='uncertain')
    assert task['blame_ledger'][0]['extra_step_cause'] == 'controller'
    assert task['blame_ledger'][0]['fault'] == 'maestro'


def test_usage_cli_reports_owned_wal_readonly_and_filters_task(runtime, monkeypatch, capsys):
    from maestro import cli
    store, make = runtime
    run, row, _ = make()
    finish(run, row, outputs={'failure': {'kind': 'no_sentinel'}})
    make(role='reviewer')
    before = tuple(store.conn.iterdump())
    monkeypatch.setattr(ControlStore, 'open_controller', lambda *a, **kw: pytest.fail('writer acquired'))
    assert cli.main(['report', 'usage', '--database', str(store.path), '--task', run.revision.task_id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [t['task_id'] for t in output['tasks']] == [run.revision.task_id]
    assert output['tasks'][0]['blame_ledger'][0]['fault'] == 'implementer'
    assert tuple(store.conn.iterdump()) == before


def test_usage_cli_subprocess_and_missing_database(runtime, tmp_path):
    import os
    import sys
    store, make = runtime
    run, row, _ = make()
    finish(run, row, outputs={'failure': {'kind': 'worker_crashed'}})
    before = tuple(store.conn.iterdump())
    cmd = [sys.executable, '-m', 'maestro.cli', 'report', 'usage', '--database', str(store.path)]
    result = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['tasks'][0]['blame_ledger'][0]['fault'] == 'harness'
    assert tuple(store.conn.iterdump()) == before
    missing = tmp_path / 'missing.sqlite3'
    result = subprocess.run(cmd[:-1] + [str(missing)], capture_output=True, text=True)
    assert result.returncode == 1
    assert not missing.exists()


def test_rework_fault_uses_triggering_role_not_the_role_spending_tokens(runtime):
    from maestro.control.models import now_iso
    from maestro.workflows.telemetry_capture import record_extra_step
    store, make = runtime
    run, row, _ = make(role='implementer')
    record_extra_step(store, run_id=run.run_id, node_id=row['node_id'],
        attempt_id=row['attempt_id'], cause='permission_declined', role='reviewer',
        trigger_attempt_id='prior-review', at=now_iso())
    task = build_usage_report(store.path)['tasks'][0]
    assert task['blame_ledger'][0]['role'] == 'implementer'
    assert task['blame_ledger'][0]['fault'] == 'reviewer'


@pytest.mark.parametrize('owner,cause,context,fault', [
    ('W6', 'brief_check_disagree', {}, 'planner'),
    ('W11', 'task_did_not_serve_goal', {}, 'unattributed'),   # mapping v2 (GDT-12)
    ('W13', 'runaway_false_alarm', {}, 'maestro'),
    ('W13', 'poor_agent', {'rebriefed_role': 'reviewer'}, 'reviewer'),
])
def test_future_owners_can_record_observations_without_product_behavior(runtime, owner, cause, context, fault):
    from maestro.workflows.telemetry_capture import record_extra_step
    store, make = runtime
    run, row, _ = make()
    task = finish(run, row, status='succeeded')
    assert task['blame_ledger'] == []
    tables = ('attempts', 'node_states', 'task_runs', 'workflow_revisions', 'operation_intents')
    before = {table: [tuple(r) for r in store.conn.execute(f'SELECT * FROM {table}')]
              for table in tables}
    kwargs = dict(run_id=run.run_id, node_id=row['node_id'], cause=cause,
                  occurrence=f'future:{owner}:observed-event', **context)
    assert record_extra_step(store, **kwargs)
    assert record_extra_step(store, **kwargs)
    task = build_usage_report(store.path)['tasks'][0]
    [fact] = task['blame_ledger']
    assert fact['fault'] == fault
    assert fact['attempt_id'] is None
    assert task['faults'][0]['attempts'] == 0
    assert task['faults'][0]['measurements']['tokens']['known_sum'] is None
    assert {table: [tuple(r) for r in store.conn.execute(f'SELECT * FROM {table}')]
            for table in tables} == before
    assert len([e for e in store.events() if e.type == 'ExtraStepObserved']) == 1
