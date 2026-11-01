"""Pure W2 observations over actual result envelopes; no verdict or storage changes."""
import importlib
import importlib.util

import pytest

from maestro.workflows.models import NodeResult, NodeSpec


def observer():
    assert importlib.util.find_spec('maestro.workflows.telemetry_observation'), 'W2 result observer missing'
    return importlib.import_module('maestro.workflows.telemetry_observation')


def node(role='implementer'):
    return NodeSpec(node_id='n', kind='agent', handler_ref='maestro.handlers.agent',
                    agent_definition_ref=role)


def result(status='succeeded', outputs=None, **kwargs):
    return NodeResult(run_id='r', node_id='n', attempt_id='a', status=status,
                      outputs=outputs or {}, **kwargs)


def test_normal_success_has_no_extra_step_and_does_not_mutate_result():
    envelope = result()
    before = envelope.to_dict()
    assert observer().result_observation(node(), envelope) is None
    assert envelope.to_dict() == before


@pytest.mark.parametrize('kind,cause,fault,context', [
    ('garbage_output', 'garbage_output', 'implementer', {}),
    ('worker_crashed', 'worker_crashed', 'harness', {}),
    ('quota_exhausted', 'quota', 'none', {'gate_admitted': False}),
    ('rate_limited', 'rate_limit', 'router', {'gate_admitted': True}),
    ('provider_unavailable', 'endpoint', 'unclassified', {}),
    ('unknown', 'unclassified', 'unclassified', {}),
])
def test_typed_failures_map_without_reading_fault_claims_or_free_text(kind, cause, fault, context):
    envelope = result('failed', {'failure': {'kind': kind}, 'fault': 'planner'},
                      detail='planner broke the check; spec_gap')
    observation = observer().result_observation(node(), envelope, **context)
    assert observation['cause'] == cause
    assert observation['fault'] == fault


def test_gate_failure_without_observed_cause_does_not_guess_planner_from_text():
    gate = NodeSpec(node_id='n', kind='function', handler_ref='maestro.handlers.verification')
    observation = observer().result_observation(gate, result('failed', detail='wrong path, tool missing'))
    assert observation['fault'] == 'unclassified'
    assert observation['flags']


def test_staged_mixed_check_causes_preserve_one_unknown_allocation():
    merge = NodeSpec(node_id='merge', kind='function', handler_ref='maestro.handlers.merge')
    envelope = result('failed', outputs={'stage': 'verify', 'extra_step_cause': 'escaped_defect',
        'evidence': [
            {'outcome': 'failed', 'extra_step_cause': 'check_failed'},
            {'outcome': 'failed', 'extra_step_cause': 'environment_missing'},
            {'outcome': 'passed', 'extra_step_cause': None},
        ]})
    before = envelope.to_dict()
    observation = observer().result_observation(merge, envelope)
    assert observation['causes'] == ('escaped_defect', 'environment_missing')
    assert observation['fault'] == 'unclassified'
    assert 'multiple_faults' in observation['flags']
    assert envelope.to_dict() == before


@pytest.mark.parametrize('origins,fault,flags', [
    (['missed_earlier', 'fixed_incompletely_out_of_scope'], 'reviewer', []),
    (['not_fixed', 'missed_earlier'], 'unclassified', ['multiple_faults']),
    (['missed_earlier', None], 'unclassified', ['cause_missing', 'multiple_faults']),
    ([], 'unclassified', ['cause_missing']),
])
def test_later_review_preserves_all_origins_without_allocating_one_attempt_twice(origins, fault, flags):
    records = [{'severity': 'blocking', 'origin': origin} for origin in origins]
    envelope = result(outputs={'review': {'round': 2, 'finding_records': records}})
    observation = observer().result_observation(node('reviewer'), envelope)
    assert observation['fault'] == fault
    assert observation['flags'] == flags
    assert observation['causes'] == tuple(origins)


def test_unknown_explicit_cause_and_missing_dynamic_context_stay_flagged():
    envelope = result('failed', {'extra_step_cause': 'agent_blames_planner'})
    observation = observer().result_observation(node(), envelope)
    assert observation['cause'] == 'agent_blames_planner'
    assert observation['fault'] == 'unclassified'
    assert observation['flags'] == ['cause_unknown']


def test_explicit_controller_observation_and_operator_reason_are_pure():
    api = observer()
    assert api.cause_observation('controller', role='gate')['fault'] == 'maestro'
    assert api.cause_observation('permission_declined', role='diagnoser')['fault'] == 'diagnoser'
    assert api.cause_observation('poor_agent')['fault'] == 'unclassified'
    assert api.cause_observation('poor_agent', rebriefed_role='prep')['fault'] == 'prep'


@pytest.mark.parametrize('kind,payload,role,cause,fault', [
    ('DanQuestionAsked', {}, 'implementer', 'spec_gap', 'planner'),
    ('DenyListHitRuled', {'ruling': 'approved'}, 'gate', 'deny_approved', 'maestro'),
    ('DenyListHitRuled', {'ruling': 'disapproved'}, 'gate', 'deny_disapproved', 'implementer'),
    ('PermissionRuled', {'ruling': 'allowed'}, 'prep', 'permission_allowed', 'maestro'),
    ('PermissionRuled', {'ruling': 'declined'}, 'prep', 'permission_declined', 'prep'),
    ('PermissionRuled', {'ruling': 'refused'}, 'prep', None, 'unclassified'),
    ('DenyListHitAsked', {}, 'gate', None, 'unclassified'),
])
def test_recorded_operator_events_use_typed_ruling_and_retain_source_identity(kind, payload, role, cause, fault):
    from maestro.control.models import Event
    event = Event(event_id='e', sequence=4, type=kind, aggregate_id='task', correlation_id='c',
                  recorded_at='2026-01-01T00:00:00Z', run_id='r', attempt_id='a', payload=payload)
    observation = observer().event_observation(event, role=role)
    assert observation['cause'] == cause
    assert observation['fault'] == fault
    assert observation['source_event_id'] == 'e'
    assert observation['source_sequence'] == 4
    assert event.payload == payload


def test_mixed_origins_capture_one_unknown_allocation_and_keeps_missing_observations(tmp_path):
    from maestro.control.store import ControlStore
    from maestro.workflows.telemetry_capture import record_extra_step
    envelope = result(outputs={'review': {'round': 2, 'finding_records': [
        {'severity': 'blocking', 'origin': 'missed_earlier'},
        {'severity': 'blocking', 'origin': None},
    ]}})
    observation = observer().result_observation(node('reviewer'), envelope)
    args = {k: observation[k] for k in ('cause', 'role', 'rebriefed_role', 'gate_admitted', 'causes', 'flags')}
    with ControlStore.open_controller(tmp_path / 'control.sqlite3') as store:
        for _ in range(2):
            assert record_extra_step(store, run_id='r', node_id='n', attempt_id='a', **args)
        events = store.events()
        assert len(events) == 1
        assert events[0].payload['fault'] == 'unclassified'
        assert events[0].payload['observed_causes'] == ['missed_earlier', None]


def test_real_later_review_parser_bad_output_keeps_own_role_attribution(tmp_path):
    from maestro.workflows.handlers import AttemptInbox, HandlerContext, adopt_review
    reviewer = node('reviewer')
    ctx = HandlerContext(run_id='r', task_id='t', node=reviewer, attempt_id='a',
                         inbox=AttemptInbox(tmp_path / 'inbox', 'a'), workspace=tmp_path,
                         inputs={'review_round': 2})
    adopted = adopt_review(ctx, result())  # review.json absent, despite clean session exit
    assert adopted.outputs['failure']['kind'] == 'garbage_output'
    observation = observer().result_observation(reviewer, adopted)
    assert observation['cause'] == 'garbage_output'
    assert observation['fault'] == 'reviewer'


def test_later_review_question_keeps_spec_gap_observation():
    envelope = result(outputs={'review': {'round': 2, 'finding_records': []},
                               'dan_questions': [{'question': 'Which requirement?'}]})
    observation = observer().result_observation(node('reviewer'), envelope)
    assert observation['cause'] == 'spec_gap'
    assert observation['fault'] == 'planner'


@pytest.mark.parametrize('kind', ['PermissionRuled', 'DenyListHitRuled'])
@pytest.mark.parametrize('ruling', [[], {}, True])
def test_malformed_recorded_ruling_remains_flagged_unknown(kind, ruling):
    from maestro.control.models import Event
    event = Event(event_id='e', sequence=1, type=kind, aggregate_id='t', correlation_id='c',
                  recorded_at='2026-01-01T00:00:00Z', payload={'ruling': ruling})
    observation = observer().event_observation(event, role='prep')
    assert observation['fault'] == 'unclassified'
    assert 'ruling_invalid' in observation['flags']
