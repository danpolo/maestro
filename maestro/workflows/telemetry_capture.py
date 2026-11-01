"""Best-effort append-only W2 capture through the controller's existing event store.

One logical extra-step row per attempt (or explicit occurrence with no attempt).
Changed observations append amendments under that same ledger id. Repeated callbacks
are no-ops. No migrations, writer acquisition, scheduling or verdict decisions here.
Future W6/W11/W13 observers call these same seams when their behavior is built.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from maestro.workflows import telemetry

EXTRA_STEP_EVENT = 'ExtraStepObserved'
REVIEW_EVENT = 'ReviewComplexityObserved'
GOAL_IMPACT_EVENT = 'GoalImpactObserved'


@contextmanager
def _transaction(store, conn):
    if conn is None:
        with store.transaction() as tx:
            yield tx
        return
    conn.execute('SAVEPOINT w2_capture')
    try:
        yield conn
    except Exception:
        conn.execute('ROLLBACK TO w2_capture')
        raise
    finally:
        conn.execute('RELEASE w2_capture')


def _append(store, event_type, payload, *, run_id, node_id, attempt_id, at=None, conn=None) -> bool:
    try:
        # The current store controller owns its single connection. Comparison and append
        # share its transaction so callback duplication cannot interleave a new row.
        with _transaction(store, conn) as conn:
            rows = conn.execute(
                'SELECT payload FROM events WHERE type = ? AND run_id = ? ORDER BY sequence DESC',
                (event_type, run_id),
            )
            for row in rows:
                old = json.loads(row['payload'])
                if old.get('ledger_id') == payload['ledger_id']:
                    if old == payload:
                        return True
            store.append_event(event_type, node_id, run_id=run_id, attempt_id=attempt_id,
                               payload=payload, recorded_at=at, conn=conn)
        return True
    except Exception:  # noqa: BLE001 — diagnostics must never change workflow behavior
        return False


def record_extra_step(store, *, run_id, node_id, attempt_id=None, cause=None,
                      role=None, rebriefed_role=None, gate_admitted=None,
                      occurrence=None, flags=(), causes=(), at=None, conn=None,
                      trigger_attempt_id=None, source_event_id=None,
                      cause_observations=()) -> bool:
    """Observe a cause, ignoring any blame supplied by an agent.

An occurrence key is required when no attempt exists (e.g. one route wait episode).
Unknown/missing causes remain explicit; absence never becomes a guessed fault.
"""
    try:
        if not attempt_id and not occurrence:
            return False
        fact = telemetry.extra_step(cause, role=role, rebriefed_role=rebriefed_role,
                                    gate_admitted=gate_admitted)
        observed_causes = [c[:160] if isinstance(c, str) else None for c in causes]
        observations = [] if cause_observations else [telemetry.extra_step(c, role=role, rebriefed_role=rebriefed_role,
                                             gate_admitted=gate_admitted) for c in observed_causes]
        contexts = []
        for observed in cause_observations:
            item = {key: observed.get(key) for key in
                    ('cause', 'role', 'rebriefed_role', 'gate_admitted')}
            if isinstance(item['cause'], str):
                item['cause'] = item['cause'][:160]
            else:
                item['cause'] = None
            contexts.append(item)
            observations.append(telemetry.extra_step(item['cause'], role=item['role'],
                rebriefed_role=item['rebriefed_role'], gate_admitted=item['gate_admitted']))
        combined_flags = set(fact['flags']) | set(flags)
        for observation in observations:
            combined_flags.update(observation['flags'])
        if len({o['fault'] for o in observations} | {fact['fault']}) > 1:
            combined_flags.add('multiple_faults')
        if 'multiple_faults' in combined_flags:
            fact['fault'] = 'unclassified'
        payload = {**fact, 'ledger_id': f'attempt:{attempt_id}' if attempt_id else str(occurrence),
                   'node_id': node_id, 'role': role,
                   'mapping_version': telemetry.MAPPING_VERSION,
                   'observed_cause': cause[:160] if isinstance(cause, str) else None,
                   'context': {'role': role, 'rebriefed_role': rebriefed_role,
                               'gate_admitted': gate_admitted},
                   'observed_causes': observed_causes,
                   'flags': sorted(combined_flags)}
        if contexts:
            payload['cause_observations'] = contexts
        if trigger_attempt_id:
            payload['trigger_attempt_id'] = trigger_attempt_id
        if source_event_id:
            payload['source_event_id'] = source_event_id
        return _append(store, EXTRA_STEP_EVENT, payload, run_id=run_id, node_id=node_id,
                       attempt_id=attempt_id, at=at, conn=conn)
    except Exception:  # noqa: BLE001
        return False


def record_goal_impact(store, *, run_id, node_id, attempt_id, goal, impact, evidence=(),
                       at=None) -> bool:
    """GDT-6 (R58): a goal impact observed by an approved review, `unattributed` with its
    provenance. No fault is assigned: attribution follows evidence (W12), never a rule
    that blames the planner for a miss."""
    try:
        payload = {'ledger_id': f'goal:{goal}:review:{attempt_id}', 'goal': goal,
                   'impact': impact, 'attribution': 'unattributed',
                   'provenance': {'run_id': run_id, 'node_id': node_id,
                                  'attempt_id': attempt_id, 'evidence': list(evidence)}}
        return _append(store, GOAL_IMPACT_EVENT, payload, run_id=run_id, node_id=node_id,
                       attempt_id=attempt_id, at=at)
    except Exception:  # noqa: BLE001
        return False


#: The keys an attribution amendment adds to the observation (GDT-12, R58).
ATTRIBUTION_KEYS = ('attribution_cause', 'attribution_rule', 'attribution_evidence',
                    'attributed_at', 'mapping_version')


def record_goal_attribution(store, observation, fact, *, at) -> bool:
    """GDT-12 (R58, R68): W12's evidence-based attribution of a goal-impact observation,
    appended under the observation's own ledger id. The payload is the observation (its
    first row, the provenance, without any earlier amendment's keys) plus the attribution;
    the first row is never rewritten, and an identical amendment is a no-op (`_append`).
    `fact`: `{attribution, cause, rule, evidence}`."""
    try:
        base = {k: v for k, v in observation.items() if k not in ATTRIBUTION_KEYS}
        provenance = base.get('provenance') or {}
        payload = {**base, 'attribution': fact['attribution'],
                   'attribution_cause': fact.get('cause'),
                   'attribution_rule': fact.get('rule'),
                   'attribution_evidence': [str(e) for e in fact.get('evidence') or ()],
                   'attributed_at': at, 'mapping_version': telemetry.MAPPING_VERSION}
        return _append(store, GOAL_IMPACT_EVENT, payload, run_id=provenance.get('run_id'),
                       node_id=provenance.get('node_id') or 'goal',
                       attempt_id=provenance.get('attempt_id'), at=at)
    except Exception:  # noqa: BLE001
        return False


def record_review(store, *, run_id, node_id, attempt_id, document,
                  tree=None, base=None, head=None, at=None) -> bool:
    try:
        score = telemetry.review_complexity(document)
        payload = {**score, 'ledger_id': f'review:{attempt_id}', 'node_id': node_id,
                   'diff_stats': telemetry.diff_stats(tree, base, head)}
        return _append(store, REVIEW_EVENT, payload, run_id=run_id, node_id=node_id,
                       attempt_id=attempt_id, at=at)
    except Exception:  # noqa: BLE001
        return False
