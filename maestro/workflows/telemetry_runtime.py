"""Best-effort runtime observers; immutable attempts and revisions supply context.

Only typed facts are observed. No workflow decision, ownership, or retry lives here.
The store event observer runs inside its caller's savepoint, sharing the connection.
"""
from __future__ import annotations

import json

from maestro.workflows import telemetry_capture as capture
from maestro.workflows import telemetry_observation as observation


def attempt_node(store, attempt_id):
    from maestro.workflows.compiler import load_revision

    row = store.conn.execute(
        'SELECT a.*, r.workflow_id, r.workflow_revision FROM attempts a '
        'JOIN task_runs r ON r.run_id=a.run_id WHERE a.attempt_id=?', (attempt_id,)
    ).fetchone()
    if row is None:
        return None, None
    revision = load_revision(store, row['workflow_id'], row['workflow_revision'])
    return row, revision.node(row['node_id']) if revision else None


def record_fact(store, row, fact, *, at=None, conn=None, preserve_inherited=False):
    old = latest_extra(store, row['run_id'], row['attempt_id'])
    # A missing provisional claim cause can be resolved by a later typed result.
    # Concrete causal facts survive alongside any new result observations.
    if (preserve_inherited and old.get('observed_cause') not in (None, 'unclassified')):
        context = old.get('context') or {}
        inherited = old.get('cause_observations') or [
            dict(cause=cause, **context) for cause in
            (old.get('observed_causes') or [old.get('observed_cause')])]
        current = fact.get('cause_observations') or [
            {key: fact.get(key) for key in ('role', 'rebriefed_role', 'gate_admitted')}
            | {'cause': cause} for cause in (fact.get('causes') or [fact.get('cause')])]
        observed = []
        for item in [*inherited, *current]:
            if item not in observed:
                observed.append(item)
        primary = fact.get('cause') if fact.get('cause') is not None else old.get('observed_cause')
        primary_context = ({key: fact.get(key) for key in ('role', 'rebriefed_role', 'gate_admitted')}
                           if fact.get('cause') is not None else context)
        fact = observation.cause_observation(primary, **primary_context,
            cause_observations=observed, flags=set(old.get('flags') or ()) | set(fact.get('flags') or ()))
        # The legacy cause list is display provenance. Contextual observations below
        # are authoritative when roles differ between the trigger and this result.
        fact['causes'] = tuple(item['cause'] for item in observed)
    return capture.record_extra_step(
        store, run_id=row['run_id'], node_id=row['node_id'], attempt_id=row['attempt_id'],
        cause=fact['cause'], role=fact.get('role'), rebriefed_role=fact.get('rebriefed_role'),
        gate_admitted=fact.get('gate_admitted'), causes=fact.get('causes', ()),
        flags=fact.get('flags', ()), at=at, conn=conn,
        trigger_attempt_id=fact.get('trigger_attempt_id') or old.get('trigger_attempt_id'),
        source_event_id=fact.get('source_event_id') or old.get('source_event_id'),
        cause_observations=fact.get('cause_observations') or (),
    )


def latest_extra(store, run_id, attempt_id):
    row = store.conn.execute(
        'SELECT payload FROM events WHERE type=? AND run_id=? AND attempt_id=? '
        'ORDER BY sequence DESC LIMIT 1', (capture.EXTRA_STEP_EVENT, run_id, attempt_id)
    ).fetchone()
    return json.loads(row['payload']) if row else {}


def observe_route_wait(run, node, resolved, *, at):
    """A wait episode ends at the next claim, survives restart, and spends nothing."""
    try:
        boundary = run.store.conn.execute(
            'SELECT MAX(sequence) FROM events WHERE run_id=? AND type=? AND '
            'json_extract(payload, "$.node_id")=?',
            (run.run_id, 'NodeAttemptClaimed', node.node_id),
        ).fetchone()[0] or 0
        # Concurrency saturation and an unspecified paused pool prove no quota cause.
        typed = {'quota_exhausted': 'quota'}
        temporal = {'quota_exhausted', 'pool_saturated', 'pool_paused'}
        reasons = tuple(r.reason for r in resolved.rejected_routes if r.reason in temporal)
        reasons = reasons or (resolved.reason,)
        causes = tuple(dict.fromkeys(typed.get(reason) for reason in reasons))
        capture.record_extra_step(
            run.store, run_id=run.run_id, node_id=node.node_id,
            occurrence=f'wait:{node.node_id}:{boundary}', cause=causes[0], causes=causes,
            role=node.agent_definition_ref, flags=[f'route_wait:{r}' for r in sorted(set(reasons))],
            at=at,
        )
    except Exception:
        pass


def observe_claim(store, event, *, conn):
    row, node = attempt_node(store, event.attempt_id)
    if row is None or node is None:
        return
    previous = conn.execute(
        'SELECT attempt_id FROM attempts WHERE run_id=? AND node_id=? AND rowid < '
        '(SELECT rowid FROM attempts WHERE attempt_id=?) ORDER BY rowid DESC LIMIT 1',
        (event.run_id, row['node_id'], event.attempt_id)
    ).fetchone()
    trigger = previous['attempt_id'] if previous else None
    boundary = conn.execute(
        'SELECT MAX(sequence) FROM events WHERE run_id=? AND type=? AND sequence<? '
        'AND json_extract(payload, "$.node_id")=?',
        (event.run_id, 'NodeAttemptClaimed', event.sequence, row['node_id']),
    ).fetchone()[0] or 0
    # A gate retry may re-run a different writer: the pending override records its cause.
    for phase in conn.execute(
        'SELECT attempt_id,payload FROM events WHERE run_id=? AND type=? AND sequence<? '
        'AND sequence>? ORDER BY sequence DESC',
        (event.run_id, 'NodePhaseChanged', event.sequence, boundary)
    ):
        payload = json.loads(phase['payload'])
        if (payload.get('node_id') == row['node_id'] and payload.get('override')
                and payload.get('phase') == 'pending'):
            trigger = payload.get('trigger_attempt_id') or phase['attempt_id'] or trigger
            break
    if trigger is None and node.agent_definition_ref == 'diagnoser':
        from maestro.workflows.compiler import load_revision
        revision = load_revision(store, row['workflow_id'], row['workflow_revision'])
        sources = {e.source_node_id for e in revision.edges
                   if e.target_node_id == node.node_id and e.guard_predicate == 'on_failure'}
        for source in revision.topological_order():
            if source in sources:
                state = conn.execute('SELECT phase,last_attempt_id FROM node_states '
                    'WHERE run_id=? AND node_id=?', (event.run_id, source)).fetchone()
                if state and state['phase'] == 'failed':
                    trigger = state['last_attempt_id']
                    break
    if trigger is None:
        repaired = conn.execute('SELECT payload FROM events WHERE run_id=? AND type=? '
            'AND sequence<? ORDER BY sequence DESC LIMIT 1',
            (event.run_id, 'WorkflowRepaired', event.sequence)).fetchone()
        payload = json.loads(repaired['payload']) if repaired else {}
        if node.node_id in payload.get('invalidated', ()):
            trigger = payload.get('trigger_attempt_id')
    if trigger is None:
        return
    source = conn.execute('SELECT run_id FROM attempts WHERE attempt_id=?', (trigger,)).fetchone()
    old = latest_extra(store, source['run_id'], trigger) if source else {}
    context = old.get('context') or {}
    fact = observation.cause_observation(
        old.get('observed_cause'), role=context.get('role'),
        rebriefed_role=context.get('rebriefed_role'), gate_admitted=context.get('gate_admitted'),
        causes=old.get('observed_causes') or (), flags=old.get('flags') or (),
        cause_observations=old.get('cause_observations') or (),
    )
    fact.update(trigger_attempt_id=trigger, source_event_id=event.event_id)
    record_fact(store, row, fact, at=event.recorded_at, conn=conn)


def observe_diagnosis(run, row, node, proposal, plan, refusal, *, at):
    """A verdict becomes telemetry only after the controller judges its actual patch."""
    if node.agent_definition_ref != 'diagnoser':
        return
    cause, roles, flags = None, set(), []
    if plan is not None and refusal is None:
        cause = proposal.verdict
        if cause == 'poor_agent':
            failing = run._failing_node(node)
            blamable = run._blamable(failing) if failing else set()
            for replaced in proposal.replace_nodes:
                if replaced.node_id in blamable:
                    original = run.revision.node(replaced.node_id)
                    roles.add(original.agent_definition_ref if original.kind == 'agent' else None)
            if len(roles) > 1:
                flags.append('multiple_faults')
    else:
        flags.append('diagnosis_refused')
    fact = observation.cause_observation(cause, role='diagnoser',
        rebriefed_role=next(iter(roles)) if len(roles) == 1 else None, flags=flags)
    if len(roles) > 1:
        fact['cause_observations'] = [dict(cause=cause, role='diagnoser',
            rebriefed_role=role, gate_admitted=None) for role in sorted(roles, key=str)]
    record_fact(run.store, row, fact, at=at)


def observe_result(run, row, node, result, *, at):
    try:
        fact = observation.result_observation(node, result)
        if fact is not None:
            record_fact(run.store, row, fact, at=at, preserve_inherited=True)
    except Exception:  # independent observations fail independently
        pass
    try:
        review = (result.outputs or {}).get('review')
        if node.agent_definition_ref == 'reviewer' and isinstance(review, dict):
            manifest = {}
            try:
                if row['manifest_artifact_id'] and run.artifacts:
                    manifest = json.loads(run.artifacts.read(row['manifest_artifact_id']))
                if not isinstance(manifest, dict):
                    manifest = {}
            except Exception:
                pass  # the score is available even when its diff subject is not
            capture.record_review(
                run.store, run_id=row['run_id'], node_id=row['node_id'],
                attempt_id=row['attempt_id'], document=review, tree=run.worktree_for(node),
                base=run.revision.code_base_sha, head=manifest.get('reviewed_commit'), at=at,
            )
            observe_goal_progress(run, row, result, review, at=at)
    except Exception:  # telemetry never changes ingest or a verdict
        pass


def observe_goal_progress(run, row, result, review, *, at):
    """GDT-6 (R58): an approved review that saw `no_progress_unexpected` on a served goal
    is an unattributed observation with its provenance, never an automatic planner fault
    (W11a's `task_did_not_serve_goal` rows stay readable for history). Keyed by goal and
    review, so it stands beside the reviewer attempt's own row."""
    if result.status != 'succeeded' or str(review.get('verdict', '')).lower() != 'approved':
        return
    for record in review.get('goal_progress') or ():
        if record.get('served') and record.get('impact') == 'no_progress_unexpected':
            capture.record_goal_impact(
                run.store, run_id=row['run_id'], node_id=row['node_id'],
                attempt_id=row['attempt_id'], goal=record['goal'],
                impact=record['impact'], evidence=record.get('evidence') or (), at=at,
            )


def observe_dead_worker(store, attempt_id, *, at):
    try:
        row, node = attempt_node(store, attempt_id)
        if row is not None:
            record_fact(store, row, observation.cause_observation('worker_crashed'), at=at)
    except Exception:
        pass


def operator_rulings(conn, event, *, attempt_id, role):
    """Retain every answer for the asking attempt; unresolved requests stay unknown.

    One allocation covers the attempt's spend. Opposite rulings cannot be allocated
    to a last answer, and several same-fault answers do not multiply that spend.
    """
    from types import SimpleNamespace

    causes, flags, answered = [], set(), set()
    requests = set()
    for family in ('DenyListHit', 'Permission'):
        for prior in conn.execute(
            'SELECT attempt_id,payload FROM events WHERE run_id=? AND type=? AND sequence<=?',
            (event.run_id, family + 'Asked', event.sequence),
        ):
            payload = json.loads(prior['payload'])
            if (prior['attempt_id'] or payload.get('attempt_id')) == attempt_id:
                requests.update((family, request) for request in payload.get('request_ids') or ())
        for prior in conn.execute(
            'SELECT event_id,payload FROM events WHERE run_id=? AND type=? AND sequence<=? '
            'ORDER BY sequence', (event.run_id, family + 'Ruled', event.sequence),
        ):
            payload = json.loads(prior['payload'])
            request = (family, payload.get('request_id'))
            if request in requests:
                fact = observation.event_observation(SimpleNamespace(
                    type=family + 'Ruled', payload=payload, event_id=prior['event_id'],
                    sequence=0), role=role)
                causes.append(fact['cause'])
                flags.update(fact['flags'])
                answered.add(request)
    questions = conn.execute(
        'SELECT attempt_id,payload FROM events WHERE run_id=? AND type=? AND sequence<=?',
        (event.run_id, 'DanQuestionAsked', event.sequence))
    if any((prior['attempt_id'] or json.loads(prior['payload']).get('attempt_id')) == attempt_id
           for prior in questions):
        causes.append('spec_gap')
    if requests - answered:
        causes.append(None)
        flags.add('ruling_pending')
    return observation.cause_observation(
        causes[0] if causes else None, causes=causes, role=role, flags=flags)


def observe_event(store, event, *, conn):
    """Operator rulings inherit the asking attempt through the recorded request id."""
    if event.type == 'NodeAttemptClaimed':
        observe_claim(store, event, conn=conn)
        return
    supported = ('DanQuestionAsked', 'DenyListHitAsked', 'DenyListHitRuled',
                 'PermissionAsked', 'PermissionRuled', 'PermissionRefused', 'ControllerError',
                 'WorkflowRepairRefused')
    if event.type not in supported:
        return
    attempt_id = event.attempt_id or event.payload.get('attempt_id')
    if not attempt_id and event.type in ('DenyListHitRuled', 'PermissionRuled'):
        asked_type = 'DenyListHitAsked' if event.type == 'DenyListHitRuled' else 'PermissionAsked'
        request = event.payload.get('request_id')
        for prior in conn.execute(
            'SELECT attempt_id,payload FROM events WHERE run_id=? AND type=? '
            'AND sequence<? ORDER BY sequence DESC', (event.run_id, asked_type, event.sequence)
        ):
            payload = json.loads(prior['payload'])
            if request and request in (payload.get('request_ids') or []):
                attempt_id = prior['attempt_id'] or payload.get('attempt_id')
                break
    if not attempt_id:
        return
    row, node = attempt_node(store, attempt_id)
    if row is None or node is None:
        return
    role = node.agent_definition_ref if node.kind == 'agent' else node.handler_ref
    fact = (observation.cause_observation('controller', role=role)
            if event.type == 'ControllerError' else observation.event_observation(event, role=role))
    if event.type == 'WorkflowRepairRefused' and node.agent_definition_ref == 'diagnoser':
        fact = observation.cause_observation(None, role=role, flags=['diagnosis_refused'])
    if event.type in ('DenyListHitAsked', 'PermissionAsked', 'DenyListHitRuled',
                      'PermissionRuled', 'DanQuestionAsked'):
        fact = operator_rulings(conn, event, attempt_id=attempt_id, role=role)
        fact['source_event_id'] = event.event_id
    if fact is not None:
        record_fact(store, row, fact, at=event.recorded_at, conn=conn)
