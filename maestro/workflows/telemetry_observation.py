"""Pure W2 adapters for typed workflow observations.

No storage, verdict, retries or text classification here. Runtime observers can
pass these facts to telemetry_capture using the controller's existing connection.
Missing evidence stays unclassified; a review with mixed faults has one unknown
allocation rather than assigning the same attempt's spend to several roles.
"""
from __future__ import annotations

from collections.abc import Mapping

from maestro.workflows import telemetry


def cause_observation(cause=None, *, role=None, rebriefed_role=None, gate_admitted=None,
                      causes=(), flags=(), cause_observations=()) -> dict:
    context = dict(role=role, rebriefed_role=rebriefed_role, gate_admitted=gate_admitted)
    fact = telemetry.extra_step(cause, **context)
    observations = tuple(causes)
    facts = [] if cause_observations else [telemetry.extra_step(value, **context) for value in observations]
    for observed in cause_observations:
        facts.append(telemetry.extra_step(observed.get('cause'),
            role=observed.get('role'), rebriefed_role=observed.get('rebriefed_role'),
            gate_admitted=observed.get('gate_admitted')))
    all_flags = set(flags) | set(fact['flags'])
    for observed in facts:
        all_flags.update(observed['flags'])
    faults = {f['fault'] for f in facts} | {fact['fault']}
    if len(faults) > 1:
        all_flags.add('multiple_faults')
    fault = 'unclassified' if 'multiple_faults' in all_flags else fact['fault']
    result = {'cause': cause, 'fault': fault, **context,
              'causes': observations, 'flags': sorted(all_flags)}
    if cause_observations:
        result['cause_observations'] = list(cause_observations)
    return result


def result_observation(node, result, *, gate_admitted=None) -> dict | None:
    """Observe an envelope without changing it or guessing from its detail text.

Gate and operator causes need typed evidence supplied by their own observers.
An unannotated failure is unknown. Every review after round one is extra, even an
approval whose origins are absent; its unknown allocation remains visible.
"""
    role = node.agent_definition_ref if node.kind == 'agent' else (
        'gate' if node.handler_ref == 'maestro.handlers.verification' else node.handler_ref)
    outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
    context = dict(role=role, gate_admitted=gate_admitted)
    if (node.handler_ref == 'maestro.handlers.merge' and result.status == 'failed'
            and outputs.get('stage') == 'verify'):
        evidence = outputs.get('evidence')
        evidence = evidence if isinstance(evidence, (tuple, list)) else ()
        causes = tuple('escaped_defect' if r.get('extra_step_cause') == 'check_failed'
                       else r.get('extra_step_cause') for r in evidence
                       if isinstance(r, Mapping) and r.get('outcome') != 'passed')
        return cause_observation(causes[0] if causes else None, causes=causes, **context)
    if 'extra_step_cause' in outputs:
        return cause_observation(outputs['extra_step_cause'], **context)
    if outputs.get('dan_questions'):
        return cause_observation('spec_gap', **context)
    failure = outputs.get('failure')
    kind = failure.get('kind') if isinstance(failure, Mapping) else None
    typed_cause = {
        'garbage_output': 'garbage_output', 'no_sentinel': 'no_sentinel',
        'empty_output': 'garbage_output', 'malformed_tool_calls': 'garbage_output',
        'worker_crashed': 'worker_crashed', 'controller_error': 'controller',
        'quota_exhausted': 'quota', 'rate_limited': 'rate_limit',
        'provider_unavailable': 'endpoint',
    }.get(kind) if isinstance(kind, str) else None
    if typed_cause and result.status in ('failed', 'uncertain'):
        return cause_observation(typed_cause, **context)
    if node.handler_ref == 'maestro.handlers.verification' and result.status == 'failed':
        evidence = outputs.get('evidence')
        evidence = evidence if isinstance(evidence, (tuple, list)) else ()
        causes = tuple(r.get('extra_step_cause') for r in evidence
                       if isinstance(r, Mapping) and r.get('outcome') != 'passed')
        return cause_observation(causes[0] if causes else None, causes=causes, **context)
    if result.status == 'uncertain':
        return cause_observation('controller', **context)
    review = outputs.get('review')
    if isinstance(review, Mapping):
        round_no = review.get('round')
        if type(round_no) is int and round_no >= 2:
            records = review.get('finding_records')
            records = records if isinstance(records, (tuple, list)) else ()
            origins = tuple(r.get('origin') for r in records
                            if isinstance(r, Mapping) and r.get('severity') == 'blocking')
            return cause_observation(origins[0] if origins else None, causes=origins, **context)
    if result.status not in ('failed', 'uncertain'):
        return None
    cause = 'unclassified' if kind is not None else None
    return cause_observation(cause, **context)


def event_observation(event, *, role=None, gate_admitted=None) -> dict | None:
    """Read existing controller question/ruling events; notes never decide blame.

The caller supplies the immutable role of the attempt that asked. Ruling events
without that linkage cannot guess the role from a task or command family.
"""
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    cause = None
    flags = []
    ruling = payload.get('ruling')
    if event.type in ('DenyListHitRuled', 'PermissionRuled') and not isinstance(ruling, str):
        flags.append('ruling_invalid' if ruling is not None else 'ruling_missing')
        ruling = None
    if event.type == 'DanQuestionAsked':
        cause = 'spec_gap'
    elif event.type == 'DenyListHitRuled':
        cause = {'approved': 'deny_approved', 'disapproved': 'deny_disapproved'}.get(ruling)
    elif event.type == 'PermissionRuled':
        cause = {'allowed': 'permission_allowed', 'declined': 'permission_declined'}.get(ruling)
    elif event.type not in ('DenyListHitAsked', 'PermissionAsked', 'PermissionRefused'):
        return None
    return {**cause_observation(cause, role=role, gate_admitted=gate_admitted, flags=flags),
            'source_event_id': event.event_id, 'source_sequence': event.sequence}
