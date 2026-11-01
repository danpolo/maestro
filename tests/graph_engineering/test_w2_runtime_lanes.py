"""W2 emission through the existing private graph harness and operator channels."""
import pytest

import maestro.state
from maestro.usage_report import build_usage_report

from . import test_deny_list_ask as deny
from . import test_permission_ask as permission
from . import test_end_to_end as end
from .test_failure_ladder import _patch_fixture_cli_driver


@pytest.fixture(autouse=True)
def fresh_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


def task_report():
    return build_usage_report(maestro.state.control_store().path, task_id='TASK-001')['tasks'][0]


def test_one_happy_graph_has_no_extra_step_allocations(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, end._CommittingAgent())
    result = run_lane(end._happy_scenario('w2_no_extra_happy_path'))
    assert result.returncode == 0
    task = task_report()
    assert task['run_statuses'] == {'succeeded': 1}
    assert task['blame_ledger'] == []
    assert task['counts']['implementer_cycles'] == task['counts']['review_rounds'] == 1
    assert task['counts']['gate_attempts'] == 1


@pytest.mark.parametrize('answer,cause,fault', [
    (0, 'permission_allowed', 'maestro'), (1, 'permission_declined', 'implementer'),
])
def test_permission_channel_poll_and_rerun_keep_cause(run_lane, monkeypatch, answer, cause, fault):
    agent = permission._AskingAgent([permission.REBASE])
    dan = end._DanAnswers([('tap', answer)])
    permission._lane(run_lane, monkeypatch, f'w2_permission_{answer}', agent, dan)
    task = task_report()
    attempts = [a for a in task['attempt_details'] if a['node_id'] == 'implementer']
    assert len(attempts) == 2
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] in {a['attempt_id'] for a in attempts}]
    assert len(rows) == 2
    assert {r['extra_step_cause'] for r in rows} == {cause}
    assert {r['fault'] for r in rows} == {fault}
    assert rows[1]['trigger_attempt_id'] == rows[0]['attempt_id']


@pytest.mark.parametrize('answer,cause,fault', [
    (0, 'deny_approved', 'maestro'), (1, 'deny_disapproved', 'implementer'),
])
def test_deny_channel_poll_and_gate_rework_keep_cause(run_lane, monkeypatch, answer, cause, fault):
    agent = deny._WritingAgent([{deny.STATE_FILE: deny.DELETE}, {deny.STATE_FILE: deny.CLEAN}])
    dan = deny._Dan([('tap', answer)], agent)
    deny._lane(run_lane, monkeypatch, f'w2_deny_{answer}', agent, dan)
    task = task_report()
    asked = [r for r in task['blame_ledger'] if r['extra_step_cause'] == cause]
    assert len(asked) >= 2
    assert {r['fault'] for r in asked} == {fault}
    assert len({r['ledger_id'] for r in task['blame_ledger']}) == len(task['blame_ledger'])


def test_gate_rework_links_all_reexecuted_nodes_to_the_actual_failed_gate(run_lane, monkeypatch):
    agent = end._ReviewingAgent(['approved'])
    _patch_fixture_cli_driver(monkeypatch, agent)
    check = {'id': 'V1', 'kind': 'auto', 'cmd': f"grep -q 'round 2' {end.FEATURE}"}
    result = run_lane(end._happy_scenario('w2_gate_cross_node', tasks=[end._task(
        'TASK-001', short_desc='add the feature file', verifications=[check])]))
    assert result.returncode == 0
    task = task_report()
    [failed_gate] = [a for a in task['attempt_details']
                     if a['node_id'] == 'gate' and a['status'] == 'failed']
    attempts = [a for a in task['attempt_details'] if a['node_id'] in ('implementer', 'gate')]
    reworks = [r for r in task['blame_ledger']
               if r['attempt_id'] != failed_gate['attempt_id']
               and r['attempt_id'] in {a['attempt_id'] for a in attempts}]
    assert len(reworks) == 2
    assert {r['extra_step_cause'] for r in reworks} == {'check_failed'}
    assert {r['trigger_attempt_id'] for r in reworks} == {failed_gate['attempt_id']}
    assert {r['fault'] for r in reworks} == {'implementer'}


@pytest.mark.parametrize('verdict,fault', [('poor_agent', 'implementer'), ('bad_graph', 'planner')])
def test_judged_diagnosis_and_final_run_share_durable_cause(run_lane, monkeypatch, verdict, fault):
    agent = end._DiagnosingAgent(['changes_requested', 'approved'], {
        'verdict': verdict, 'reason': 'fixture repair',
        'brief_addenda': {'implementer': end.DIAGNOSIS},
    })
    end._diagnosis_lane(run_lane, monkeypatch, 'w2_diagnosis_' + verdict, agent)
    task = task_report()
    [diagnosis] = [a for a in task['attempt_details'] if a['node_id'] == 'diagnose']
    final_run = maestro.state.control_store().conn.execute(
        'SELECT run_id FROM task_runs WHERE workflow_revision=2').fetchone()['run_id']
    [first_writer] = [a for a in task['attempt_details']
                     if a['node_id'] == 'implementer' and a['run_id'] != final_run]
    [final_writer] = [a for a in task['attempt_details']
                     if a['node_id'] == 'implementer' and a['run_id'] == final_run]
    [diagnosis_row] = [r for r in task['blame_ledger'] if r['attempt_id'] == diagnosis['attempt_id']]
    [final_row] = [r for r in task['blame_ledger'] if r['attempt_id'] == final_writer['attempt_id']]
    assert diagnosis_row['extra_step_cause'] == final_row['extra_step_cause'] == verdict
    assert diagnosis_row['fault'] == final_row['fault'] == fault
    assert diagnosis_row['trigger_attempt_id'] != first_writer['attempt_id']  # failed review opened diagnosis
    assert final_row['trigger_attempt_id'] == diagnosis['attempt_id']
    assert diagnosis_row['run_id'] != final_row['run_id']
    assert len({r['ledger_id'] for r in task['blame_ledger']}) == len(task['blame_ledger'])


def test_refused_diagnosis_records_unknown_cause_without_believing_verdict(run_lane, monkeypatch):
    agent = end._DiagnosingAgent(['changes_requested'], {
        'verdict': 'poor_agent', 'reason': 'claims a fault but rebriefs nobody',
    })
    end._diagnosis_lane(run_lane, monkeypatch, 'w2_refused_diagnosis', agent)
    task = task_report()
    [diagnosis] = [a for a in task['attempt_details'] if a['node_id'] == 'diagnose']
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == diagnosis['attempt_id']]
    assert row['extra_step_cause'] == row['fault'] == 'unclassified'
    assert 'diagnosis_refused' in row['flags']


def test_typed_quota_failure_and_real_reroute_keep_original_trigger(run_lane, monkeypatch):
    from .test_failure_ladder import _patch_two_drivers
    _patch_two_drivers(monkeypatch, end._QuotaSpent(), end._CommittingAgent())
    result = run_lane(end._happy_scenario('w2_quota_reroute', routing=end._two_strong_pools))
    assert result.returncode == 0
    task = task_report()
    [first, rerouted] = [a for a in task['attempt_details'] if a['node_id'] == 'implementer']
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == rerouted['attempt_id']]
    assert row['extra_step_cause'] == 'quota'
    assert row['fault'] == 'unclassified'  # no W5 5h admission decision exists
    assert row['trigger_attempt_id'] == first['attempt_id']
    assert 'fault_context_missing' in row['flags']
    assert task['quota_consumption'] is None


def test_final_review_retains_diagnosed_cause_and_missing_origins(run_lane, monkeypatch):
    agent = end._DiagnosingAgent(['changes_requested', 'approved'], {
        'verdict': 'poor_agent', 'reason': 'fixture repair',
        'brief_addenda': {'implementer': end.DIAGNOSIS},
    })
    end._diagnosis_lane(run_lane, monkeypatch, 'w2_final_review_observations', agent)
    task = task_report()
    final_run = maestro.state.control_store().conn.execute(
        'SELECT run_id FROM task_runs WHERE workflow_revision=2').fetchone()['run_id']
    [review] = [a for a in task['attempt_details']
                if a['node_id'] == 'proof_review' and a['run_id'] == final_run]
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == review['attempt_id']]
    assert row['extra_step_cause'] == 'poor_agent'
    assert row['observed_causes'] == ['poor_agent', None]
    assert row['fault'] == 'unclassified'
    assert {'multiple_faults', 'cause_missing'} <= set(row['flags'])
    assert len({r['ledger_id'] for r in task['blame_ledger']}) == len(task['blame_ledger'])


def test_stale_quality_override_is_not_used_for_a_later_blocked_reroute(run_lane, monkeypatch, tmp_path):
    from . import test_failure_ladder as ladder
    ladder._patch_two_drivers(monkeypatch,
        ladder._ScriptedCliDriver(ladder._EMPTY, ladder._quota(ladder._reset_in(5))),
        ladder._ScriptedCliDriver(ladder._GOOD))
    worktree = ladder._committed_repo(tmp_path / 'wt')
    result = run_lane(ladder._graph_scenario('w2_stale_override', routing=ladder._two_route_routing,
        on_poll=ladder._drive_implementer(lambda: len(ladder._settled_implementer()) >= 3,
                                         worktree=worktree, succeed_from=3)))
    assert result.returncode == 0
    first, blocked, rerouted = ladder._retry_rows()
    assert first['failure_kind'] == 'empty_output'
    assert blocked['failure_kind'] == 'quota_exhausted'
    task = task_report()
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == rerouted['attempt_id']]
    assert row['trigger_attempt_id'] == blocked['attempt_id']
    assert row['extra_step_cause'] == 'quota'
    assert row['fault'] == 'unclassified'
    assert task['quota_consumption'] is None


def test_apply_repair_refusal_amends_diagnosis_and_starts_no_final_run(run_lane, monkeypatch):
    from maestro.workflows import compiler
    def refused(*args, **kwargs):
        raise compiler.StaleRepairProposal('phases moved since planning')
    monkeypatch.setattr(compiler, 'apply_repair', refused)
    agent = end._DiagnosingAgent(['changes_requested'], {
        'verdict': 'poor_agent', 'reason': 'fixture repair',
        'brief_addenda': {'implementer': end.DIAGNOSIS},
    })
    end._diagnosis_lane(run_lane, monkeypatch, 'w2_apply_refused', agent)
    task = task_report()
    assert sum(task['run_statuses'].values()) == 1
    [attempt] = [a for a in task['attempt_details'] if a['node_id'] == 'diagnose']
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == attempt['attempt_id']]
    assert row['fault'] == 'unclassified'
    assert 'diagnosis_refused' in row['flags']
    assert attempt['status'] == 'succeeded'  # durable attempt closed before the node override
    assert maestro.state.control_store().conn.execute(
        'SELECT phase FROM node_states WHERE node_id="diagnose"').fetchone()[0] == 'failed'


@pytest.mark.parametrize('proposal', [
    {'verdict': ['poor_agent'], 'reason': 'invalid verdict'},
    {'verdict': 'unknown', 'reason': 'not a diagnosis'},
    {'verdict': 'poor_agent', 'reason': 'rebrief both actual blamable roles',
     'brief_addenda': {'implementer': end.DIAGNOSIS, 'proof_review': end.DIAGNOSIS}},
])
def test_malformed_or_multiple_role_diagnosis_cannot_assign_one_role(run_lane, monkeypatch, proposal):
    agent = end._DiagnosingAgent(['changes_requested', 'approved'], proposal)
    end._diagnosis_lane(run_lane, monkeypatch, 'w2_uncertain_diagnosis', agent)
    task = task_report()
    [attempt] = [a for a in task['attempt_details'] if a['node_id'] == 'diagnose']
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == attempt['attempt_id']]
    assert row['fault'] == 'unclassified'
    assert {'diagnosis_refused', 'multiple_faults'} & set(row['flags'])
    if 'brief_addenda' in proposal:
        assert {o['rebriefed_role'] for o in row['cause_observations']} == {'implementer', 'reviewer'}


@pytest.mark.parametrize('origin,fault', [
    ('not_fixed', 'implementer'), ('fixed_incompletely_in_scope', 'implementer'),
    ('fixed_incompletely_out_of_scope', 'reviewer'), ('missed_earlier', 'reviewer'),
    ('introduced_by_rework', 'implementer'), ('exposed_by_rework', 'none'),
    ('scope_widened', 'planner'),
])
def test_each_later_review_origin_is_one_graph_runtime_allocation(run_lane, monkeypatch, origin, fault):
    from maestro.workflows import failures
    from . import conftest as lane_harness
    monkeypatch.setattr(lane_harness, 'MAX_POLLS', 24)
    agent = end._OriginAgent([
        ('changes_requested', [{'finding': 'first defect', 'severity': 'blocking'}]),
        ('changes_requested', [{'finding': 'observed defect', 'severity': 'blocking',
                                'origin': origin, 'prior_finding': 'r1-f1'}]),
        ('approved', []),
    ], {'verdict': 'poor_agent', 'reason': 'repair',
        'brief_addenda': {'implementer': end.DIAGNOSIS}})
    monkeypatch.setattr(failures, 'QUALITY_ATTEMPTS', 2)
    _patch_fixture_cli_driver(monkeypatch, agent)
    result = run_lane(end._happy_scenario('w2_origin_' + origin,
                                         on_poll=end._drive_until_the_newest_run_settles))
    assert result.returncode == 0
    store = maestro.state.control_store()
    second = store.conn.execute('SELECT attempt_id FROM attempts WHERE node_id="proof_review" '
                                'ORDER BY rowid LIMIT 1 OFFSET 1').fetchone()['attempt_id']
    task = task_report()
    [row] = [r for r in task['blame_ledger'] if r['attempt_id'] == second]
    assert row['extra_step_cause'] == origin
    assert row['fault'] == fault
    assert row['observed_causes'] == [origin]
    assert len({r['ledger_id'] for r in task['blame_ledger']}) == len(task['blame_ledger'])


def test_diagnostic_observer_failure_cannot_prevent_the_repaired_run(run_lane, monkeypatch):
    from maestro.workflows import telemetry_runtime
    def failed(*args, **kwargs):
        raise OSError('diagnostic capture unavailable')
    monkeypatch.setattr(telemetry_runtime, 'observe_diagnosis', failed)
    agent = end._DiagnosingAgent(['changes_requested', 'approved'], {
        'verdict': 'poor_agent', 'reason': 'fixture repair',
        'brief_addenda': {'implementer': end.DIAGNOSIS},
    })
    end._diagnosis_lane(run_lane, monkeypatch, 'w2_diagnosis_capture_failure', agent)
    assert task_report()['run_statuses'] == {'canceled': 1, 'succeeded': 1}


def test_real_question_poll_and_reexecution_keep_spec_gap(run_lane, monkeypatch):
    agent = end._AskingAgent(['approved'], 'implementer')
    dan = end._DanAnswers([('tap', 0)])
    end._question_lane(run_lane, monkeypatch, 'w2_question', agent, dan)
    rows = [r for r in task_report()['blame_ledger'] if r['extra_step_cause'] == 'spec_gap']
    assert len(rows) >= 2
    assert {r['fault'] for r in rows} == {'planner'}


def test_real_dead_worker_recovery_is_one_allocation_per_attempt(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, end._CommittingAgent())
    result = run_lane(end._happy_scenario('w2_dead_worker', on_poll=end._crash_the_first_implementer))
    assert result.returncode == 0
    task = task_report()
    rows = [r for r in task['blame_ledger'] if r['extra_step_cause'] == 'worker_crashed']
    assert len(rows) == 2
    assert {r['fault'] for r in rows} == {'harness'}
    assert rows[1]['trigger_attempt_id'] == rows[0]['attempt_id']


@pytest.mark.parametrize('reason,fault', [('manual_redo', 'prep'), ('brief_check_disagree', 'planner')])
def test_existing_manual_redo_channel_keeps_explicit_reason(run_lane, monkeypatch, reason, fault):
    from . import test_g7_parity as manual
    _patch_fixture_cli_driver(monkeypatch, manual._PrepAgent())
    dan = manual._ActionDan(['redo ' + reason, 'done'])
    result = run_lane(manual._manual_lane('w2_manual_' + reason, monkeypatch, dan))
    assert result.returncode == 0
    rows = [r for r in task_report()['blame_ledger'] if r['extra_step_cause'] == reason]
    assert len(rows) >= 2
    assert {r['fault'] for r in rows} == {fault}


@pytest.mark.parametrize('reason,fault', [
    ('accept_rejected_review', 'reviewer'), ('accept_rejected_plan', 'planner'),
])
def test_existing_acceptance_channel_keeps_explicit_rejection_reason(run_lane, monkeypatch, reason, fault):
    from maestro import taskgraph
    from .test_dispatch_wiring import _attempts

    _patch_fixture_cli_driver(monkeypatch, end._RevisingAgent())
    dan = end._Dan(['fail ' + reason])
    result = run_lane(end._dan_lane('w2_accept_' + reason, monkeypatch, dan))
    assert result.returncode == 0
    rows = [r for r in task_report()['blame_ledger'] if r['extra_step_cause'] == reason]
    assert len(rows) == 1, 'one failed observation; no rebrief of the integrated execution'
    assert fault in {r['fault'] for r in rows}
    assert {r['fault'] for r in rows} <= {fault, 'unclassified'}
    for row in rows:
        if row['fault'] == 'unclassified':
            assert {'multiple_faults', 'cause_missing'} <= set(row['flags'])
    graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(result.repo)))
    [repair] = [s for s in graph.specs.values() if s.legacy_fields.get('repairs_task') == 'TASK-001']
    assert reason in repair.goal and repair.legacy_fields['repair_subject'] == dan.snapshots[0]
    assert maestro.state.control_store().task(repair.task_id) is not None
    assert len([a for a in _attempts() if a['node_id'] == 'implementer']) == 1
    assert len([a for a in _attempts() if a['node_id'] == 'merge']) == 1
    assert task_report()['run_statuses'] == {'failed': 1}


def test_real_controller_error_after_claim_records_maestro_fault(run_lane, monkeypatch):
    from maestro.workflows import runner
    monkeypatch.setattr(runner.WorkflowRunner, '_record_routing_decision', end._sql_typo)
    result = run_lane(end._graph_scenario('w2_controller_error'))
    assert result.returncode == 0
    [row] = task_report()['blame_ledger']
    assert row['extra_step_cause'] == 'controller'
    assert row['fault'] == 'maestro'


@pytest.mark.parametrize('kind,cause,fault', [
    ('quota', 'quota', 'unclassified'), ('rate_limit', 'rate_limit', 'unclassified'),
    ('no_sentinel', 'no_sentinel', 'implementer'),
])
def test_worker_classification_reaches_runtime_ledger_once(run_lane, monkeypatch, kind, cause, fault):
    from maestro.backends.base import Completion, ExitVerdict
    from . import test_failure_ladder as ladder
    completion = Completion(text='an answer without a sentinel', returncode=0, timed_out=False)
    verdict = ExitVerdict(kind='ok')
    if kind in ('quota', 'rate_limit'):
        completion = Completion(text='', returncode=1, timed_out=False)
        verdict = ExitVerdict(kind='quota_exhausted', reset_at='2099-01-01T00:00:00Z',
                              evidence='429 rate limit' if kind == 'rate_limit' else 'quota spent')
    ladder._patch_fixture_cli_driver(monkeypatch, ladder._FakeCliDriver(completion, verdict))
    _, first, _ = ladder._settled_attempt(run_lane, 'w2_worker_' + kind)
    rows = [r for r in task_report()['blame_ledger'] if r['attempt_id'] == first['attempt_id']]
    assert len(rows) == 1
    assert rows[0]['extra_step_cause'] == cause
    assert rows[0]['fault'] == fault


def test_empty_output_retry_keeps_actual_worker_cause(run_lane, monkeypatch, tmp_path):
    from . import test_failure_ladder as ladder
    ladder._patch_fixture_cli_driver(monkeypatch, ladder._ScriptedCliDriver(ladder._EMPTY))
    tree = ladder._committed_repo(tmp_path / 'wt')
    _, first, result = ladder._settled_attempt(run_lane, 'w2_empty_output', worktree=tree)
    assert result.outputs['failure']['kind'] == first['failure_kind'] == 'empty_output'
    task = task_report()
    writer_ids = {a['attempt_id'] for a in task['attempt_details'] if a['node_id'] == 'implementer'}
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] in writer_ids]
    assert len(rows) == len(writer_ids) > 1
    assert {r['extra_step_cause'] for r in rows} == {'garbage_output'}
    assert {r['fault'] for r in rows} == {'implementer'}


def test_malformed_endpoint_tool_calls_keep_actual_runtime_cause(run_lane, monkeypatch):
    import dataclasses
    from maestro.backends.model_api import ModelAPI
    from maestro.workflows import routing, worker
    from maestro.workflows.policy import default_policy
    from . import test_failure_ladder as ladder
    from .conftest import fixture_routing
    class Malformed:
        def post(self, url, **kwargs):
            return 200, {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': '{path: a.py'}}]}}]}, {}
    [entry] = routing.endpoints_for_catalog([ladder.FREE_ENDPOINT])
    monkeypatch.setattr(worker, '_model_api', lambda endpoint: ModelAPI(
        endpoint, transport=Malformed(), environ={endpoint.api_key_env: 'fixture-key'}))
    result = run_lane(ladder._graph_scenario('w2_malformed_tools',
        project_yaml={'engineering': {'runner': 'graph', 'free_tier_endpoints': [ladder.FREE_ENDPOINT]}},
        routing=lambda: fixture_routing(entries=[entry],
            policy=dataclasses.replace(default_policy(), objective='free_quality')),
        on_poll=ladder._run_the_implementer_worker()))
    assert result.returncode == 0
    first = ladder._implementer_attempts()[0]
    assert first['failure_kind'] == 'malformed_tool_calls'
    [row] = [r for r in task_report()['blame_ledger'] if r['attempt_id'] == first['attempt_id']]
    assert row['extra_step_cause'] == 'garbage_output'
    assert row['fault'] == 'implementer'


def test_reviewer_garbage_output_is_observed_without_agent_blame(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, end._ReviewingAgent([None] * 5))
    result = run_lane(end._happy_scenario('w2_garbage_review'))
    assert result.returncode == 0
    task = task_report()
    review_ids = {a['attempt_id'] for a in task['attempt_details'] if a['node_id'] == 'proof_review'}
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] in review_ids]
    assert len(rows) == len(review_ids) == 3
    assert {r['extra_step_cause'] for r in rows} == {'garbage_output'}
    assert {r['fault'] for r in rows} == {'reviewer'}


def test_real_endpoint_failure_keeps_admission_unknown(run_lane, monkeypatch):
    import dataclasses
    from maestro.backends.model_api import ModelAPI
    from maestro.workflows import routing, worker
    from maestro.workflows.policy import default_policy
    from . import test_failure_ladder as ladder
    from .conftest import fixture_routing
    class Unavailable:
        def post(self, url, **kwargs):
            return 503, {'error': {'message': 'fixture unavailable'}}, {}
    [entry] = routing.endpoints_for_catalog([ladder.FREE_ENDPOINT])
    monkeypatch.setattr(worker, '_model_api', lambda endpoint: ModelAPI(
        endpoint, transport=Unavailable(), environ={endpoint.api_key_env: 'fixture-key'}))
    result = run_lane(ladder._graph_scenario('w2_endpoint',
        project_yaml={'engineering': {'runner': 'graph', 'free_tier_endpoints': [ladder.FREE_ENDPOINT]}},
        routing=lambda: fixture_routing(entries=[entry],
            policy=dataclasses.replace(default_policy(), objective='free_quality')),
        on_poll=ladder._run_the_implementer_worker()))
    assert result.returncode == 0
    first = ladder._implementer_attempts()[0]
    [row] = [r for r in task_report()['blame_ledger'] if r['attempt_id'] == first['attempt_id']]
    assert row['extra_step_cause'] == 'endpoint'
    assert row['fault'] == 'unclassified'
    assert 'fault_context_missing' in row['flags']


@pytest.mark.parametrize('case,cause,fault', [
    ('broken', 'check_broken', 'planner'),
    ('missing_tool', 'environment_missing', 'maestro'),
    ('dirty', 'uncommitted_tree', 'implementer'),
])
def test_gate_failures_travel_the_complete_graph(run_lane, monkeypatch, case, cause, fault):
    from maestro import gates, validation
    agent = end._UncommittingAgent() if case == 'dirty' else end._CommittingAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    if case != 'dirty':
        argv = () if case == 'broken' else ('/w2-fixture/no-such-tool',)
        spec = validation.VerificationSpec(check_id='V1', acceptance_criterion_ids=('V1',),
                                          argv_or_adapter_ref=argv)
        monkeypatch.setattr(gates, 'authoritative_specs', lambda *a, **kw: [spec])
    result = run_lane(end._happy_scenario('w2_gate_' + case))
    assert result.returncode == 0
    task = task_report()
    gates_ids = {a['attempt_id'] for a in task['attempt_details'] if a['node_id'] == 'gate'}
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] in gates_ids]
    assert rows
    assert {r['extra_step_cause'] for r in rows} == {cause}
    assert {r['fault'] for r in rows} == {fault}
    assert len({r['ledger_id'] for r in rows}) == len(rows)


@pytest.mark.parametrize('stage', ['gate', 'staging'])
@pytest.mark.parametrize('case,cause,fault', [
    ('missing', 'environment_missing', 'maestro'),
    ('empty_tests', 'check_broken', 'planner'),
    ('compound', None, 'unclassified'),
])
def test_normal_roadmap_shell_checks_keep_observed_faults(
        run_lane, monkeypatch, tmp_path, stage, case, cause, fault):
    import dataclasses
    import shlex
    import sys
    from maestro.validation import CheckRunner
    _patch_fixture_cli_driver(monkeypatch, end._CommittingAgent())
    empty = tmp_path / 'empty tests'
    empty.mkdir()
    command = {
        'missing': shlex.quote(str(tmp_path / 'absent executable')),
        'empty_tests': f'{shlex.quote(sys.executable)} -m pytest -q {shlex.quote(str(empty))}',
        'compound': 'true && /w2-fixture/no-such-tool',
    }[case]
    check = {'id': 'V1', 'kind': 'auto', 'cmd': command}
    if stage == 'staging':
        # Execute the ordinary ROADMAP check only on the actual staged tree.
        original = CheckRunner.run
        def staged_check(self, spec, tree, **kwargs):
            if 'staging' not in str(tree):
                spec = dataclasses.replace(spec, argv_or_adapter_ref=('true',), cache_policy='never')
            return original(self, spec, tree, **kwargs)
        monkeypatch.setattr(CheckRunner, 'run', staged_check)
    result = run_lane(end._happy_scenario(f'w2_shell_{stage}_{case}', tasks=[end._task(
        'TASK-001', short_desc='add the feature file', verifications=[check])]))
    assert result.returncode == 0
    task = task_report()
    node_id = 'gate' if stage == 'gate' else 'merge'
    ids = {a['attempt_id'] for a in task['attempt_details'] if a['node_id'] == node_id}
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] in ids]
    assert rows
    assert {r['extra_step_cause'] for r in rows} == {cause or 'unclassified'}
    assert {r['fault'] for r in rows} == {fault}
    if cause is None:
        assert all('cause_missing' in r['flags'] for r in rows)
    assert len({r['ledger_id'] for r in rows}) == len(rows)


@pytest.mark.parametrize('case,cause,fault', [
    ('check', 'escaped_defect', 'reviewer'),
    ('conflict', 'parallel_scope_overlap', 'planner'),
])
def test_real_integration_failures_travel_the_complete_graph(run_lane, monkeypatch, case, cause, fault):
    from maestro.validation import CheckRunner
    _patch_fixture_cli_driver(monkeypatch, end._CommittingAgent())
    # The existing check executes on the staged tree; its command fails only there.
    original = CheckRunner.run
    def staged_failure(self, spec, tree, **kwargs):
        if case == 'check' and 'staging' in str(tree):
            import dataclasses
            spec = dataclasses.replace(spec, argv_or_adapter_ref=('false',), cache_policy='never')
        return original(self, spec, tree, **kwargs)
    if case == 'check':
        monkeypatch.setattr(CheckRunner, 'run', staged_failure)
    changed = []
    def drive(run):
        if case == 'conflict' and not changed and end._node_phase('gate') == 'succeeded':
            (run.repo / end.FEATURE).write_text('other task changed this path\n')
            end._git(run.repo, 'add', end.FEATURE)
            end._git(run.repo, 'commit', '-qm', 'overlapping task')
            changed.append(True)
        end._drive_every_worker(run)
    result = run_lane(end._happy_scenario('w2_merge_' + case, on_poll=drive))
    assert result.returncode == 0
    task = task_report()
    merge_ids = {a['attempt_id'] for a in task['attempt_details'] if a['node_id'] == 'merge'}
    rows = [r for r in task['blame_ledger'] if r['attempt_id'] in merge_ids]
    assert rows
    assert {r['extra_step_cause'] for r in rows} == {cause}
    assert {r['fault'] for r in rows} == {fault}
    assert len({r['ledger_id'] for r in rows}) == len(rows)
