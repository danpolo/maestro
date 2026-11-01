"""W2 evidence contracts: observed causes, harmless unknowns and real Git stats."""
import importlib
import importlib.util
import subprocess

import pytest


def telemetry():
    assert importlib.util.find_spec('maestro.workflows.telemetry'), 'W2 telemetry contract missing'
    return importlib.import_module('maestro.workflows.telemetry')


@pytest.mark.parametrize('cause,fault,context', [
    ('not_fixed', 'implementer', {}),
    ('fixed_incompletely_in_scope', 'implementer', {}),
    ('fixed_incompletely_out_of_scope', 'reviewer', {}),
    ('missed_earlier', 'reviewer', {}),
    ('introduced_by_rework', 'implementer', {}),
    ('exposed_by_rework', 'none', {}),
    ('scope_widened', 'planner', {}),
    ('check_failed', 'implementer', {}),
    ('uncommitted_tree', 'implementer', {}),
    ('check_broken', 'planner', {}),
    ('environment_missing', 'maestro', {}),
    ('garbage_output', 'reviewer', {'role': 'reviewer'}),
    ('no_sentinel', 'implementer', {'role': 'implementer'}),
    ('worker_crashed', 'harness', {}),
    ('quota', 'none', {'gate_admitted': False}),
    ('rate_limit', 'router', {'gate_admitted': True}),
    ('endpoint', 'none', {'gate_admitted': False}),
    ('poor_agent', 'prep', {'rebriefed_role': 'prep'}),
    ('bad_graph', 'planner', {}),
    ('spec_gap', 'planner', {}),
    ('deny_approved', 'maestro', {}),
    ('deny_disapproved', 'implementer', {}),
    ('permission_allowed', 'maestro', {}),
    ('permission_declined', 'diagnoser', {'role': 'diagnoser'}),
    ('manual_redo', 'prep', {}),
    ('brief_check_disagree', 'planner', {}),
    ('accept_rejected_review', 'reviewer', {}),
    ('accept_rejected_plan', 'planner', {}),
    ('escaped_defect', 'reviewer', {}),
    ('parallel_scope_overlap', 'planner', {}),
    # GDT-12 (R58/R62, mapping version 2): a served goal's miss is observed, not a fault.
    ('task_did_not_serve_goal', 'unattributed', {}),
    ('runaway_false_alarm', 'maestro', {}),
    ('controller', 'maestro', {}),
])
def test_observed_cause_maps_to_fault_through_one_table(cause, fault, context):
    record = telemetry().extra_step(cause, **context)
    assert record['extra_step_cause'] == cause
    assert record['fault'] == fault
    assert record['flags'] == []


@pytest.mark.parametrize('cause,context', [
    (None, {}), ('agent_says_planner', {}), ({'fault': 'planner'}, {}),
    ('garbage_output', {}), ('poor_agent', {}), ('quota', {}),
    ('quota', {'gate_admitted': 'yes'}),
])
def test_unknown_cause_or_missing_dynamic_evidence_is_flagged(cause, context):
    record = telemetry().extra_step(cause, **context)
    assert record['fault'] == 'unclassified'
    assert record['flags']


@pytest.mark.parametrize('value', [None, True, 0, 6, 2.5, '3', {}, float('nan')])
def test_invalid_or_missing_complexity_is_nullable_and_flagged(value):
    record = telemetry().review_complexity({'complexity': value})
    assert record['complexity'] is None
    assert record['flags']


def test_reviewer_score_is_recorded_and_rubric_covers_all_four_axes():
    api = telemetry()
    assert set(api.COMPLEXITY_RUBRIC) == {1, 2, 3, 4, 5}
    for anchor in api.COMPLEXITY_RUBRIC.values():
        assert set(anchor) == {'files', 'reach', 'new_behavior', 'risk'}
    assert api.review_complexity({'complexity': 4, 'fault': 'planner'}) == {
        'complexity': 4, 'rubric_version': 1, 'flags': []}


def test_diff_crosscheck_records_real_frozen_git_facts(tmp_path):
    api = telemetry()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'W2 fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (tmp_path / 'old.py').write_text('one\ntwo\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (tmp_path / 'old.py').write_text('one\nthree\nfour\n')
    (tmp_path / 'binary').write_bytes(b'\x00\x01')
    git('add', '.')
    git('commit', '-qm', 'changed')
    head = git('rev-parse', 'HEAD')
    facts = api.diff_stats(tmp_path, base, head)
    assert facts['base_sha'] == base and facts['head_sha'] == head
    assert facts['files_changed'] == 2
    assert facts['insertions'] == 2 and facts['deletions'] == 1
    assert facts['binary_files'] == 1
    assert facts['flags'] == []
    # The cross-check is bound to immutable commits, not the live working diff.
    (tmp_path / 'old.py').write_text('uncommitted\n')
    assert api.diff_stats(tmp_path, base, head) == facts
    assert api.diff_stats(tmp_path, 'missing-ref', head)['files_changed'] is None


def test_missing_tree_keeps_diff_unknown():
    assert telemetry().diff_stats(None, None, None)['files_changed'] is None


def capture():
    assert importlib.util.find_spec('maestro.workflows.telemetry_capture'), 'W2 capture seam missing'
    return importlib.import_module('maestro.workflows.telemetry_capture')


def test_capture_is_append_only_idempotent_and_keeps_unknown_evidence(tmp_path):
    from maestro.control.store import ControlStore
    api = capture()
    with ControlStore.open_controller(tmp_path / 'control.sqlite3') as store:
        args = dict(run_id='r', node_id='n', attempt_id='a', cause='unrecognized', role='reviewer')
        assert api.record_extra_step(store, **args)
        assert api.record_extra_step(store, **args)
        first = store.events()
        assert len(first) == 1
        assert first[0].payload['extra_step_cause'] == 'unclassified'
        assert first[0].payload['observed_cause'] == 'unrecognized'
        assert first[0].payload['flags']
        assert api.record_extra_step(store, **{**args, 'cause': 'missed_earlier'})
        events = store.events()
        assert len(events) == 2
        assert events[0] == first[0]
        assert events[1].payload['ledger_id'] == first[0].payload['ledger_id']
        assert events[1].payload['fault'] == 'reviewer'
        # A callback from before the correction can be redelivered after a restart.
        assert api.record_extra_step(store, **args)
        assert store.events() == events


def test_telemetry_capture_failure_is_harmless_and_never_claims_another_writer(tmp_path):
    from maestro.control.store import ControlStore
    api = capture()
    with ControlStore.open_controller(tmp_path / 'control.sqlite3') as controller:
        with ControlStore.open_client(tmp_path / 'control.sqlite3') as client:
            assert api.record_extra_step(client, run_id='r', node_id='n', attempt_id='a', cause='controller') is False
        assert controller.events() == []


def test_one_of_each_extra_step_has_one_durable_row_with_its_mapped_fault(tmp_path):
    from maestro.control.store import ControlStore
    from maestro.workflows.review_origins import CAUSE_FAULT
    api = capture()
    with ControlStore.open_controller(tmp_path / 'control.sqlite3') as store:
        for i, cause in enumerate(CAUSE_FAULT):
            if cause == 'unclassified':
                continue
            kwargs = dict(run_id='r', node_id=f'n{i}', attempt_id=f'a{i}', cause=cause,
                          role='reviewer', rebriefed_role='implementer', gate_admitted=False)
            assert api.record_extra_step(store, **kwargs)
            assert api.record_extra_step(store, **kwargs)
        events = store.events()
        assert len(events) == len(CAUSE_FAULT) - 1
        assert len({e.payload['ledger_id'] for e in events}) == len(events)
        for event in events:
            payload = event.payload
            assert payload['fault'] == telemetry().extra_step(
                payload['extra_step_cause'], role='reviewer', rebriefed_role='implementer', gate_admitted=False)['fault']
            assert payload['flags'] == []


def test_complexity_capture_keeps_missing_score_without_rejecting_review(tmp_path):
    from maestro.control.store import ControlStore
    api = capture()
    with ControlStore.open_controller(tmp_path / 'control.sqlite3') as store:
        assert api.record_review(store, run_id='r', node_id='n', attempt_id='a', document={})
        assert api.record_review(store, run_id='r', node_id='n', attempt_id='a', document={})
        assert len(store.events()) == 1
        facts = store.events()[0].payload
        assert facts['complexity'] is None and facts['flags'] == ['complexity_missing']
        assert facts['diff_stats']['files_changed'] is None
