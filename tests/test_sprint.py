"""Synthetic sprint control through the real shared mailbox and controller store."""
from datetime import datetime, timezone

import pytest

from maestro import config, state
from maestro.control.store import ControlStore
from maestro.hitl import commands, shared_bot, telegram
from tests.workflows.test_w2_runtime import runtime


NOW = 2_000_000_000.0


def stamp(at=NOW):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@pytest.fixture
def sprint_project(tmp_path, monkeypatch):
    repo = tmp_path / 'alpha'
    repo.mkdir()
    orch = repo / '.orchestrator'
    orch.mkdir()
    (repo / 'project.yaml').write_text('project: {name: alpha}\n')
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'shared'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'synthetic')
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '42')
    monkeypatch.setattr(state, 'STATE_JSON', orch / 'state.json')
    monkeypatch.setattr(state, 'CONTROL_DB', orch / 'control.sqlite3')
    monkeypatch.setattr(state, 'JOURNAL', orch / 'journal.ndjson')
    monkeypatch.setattr(config, 'PROJECT_YAML', repo / 'project.yaml')
    monkeypatch.setattr(commands, 'REPO', repo)
    monkeypatch.setattr(commands, '_LISTENER_QUEUE', None)
    monkeypatch.setattr(commands, '_getUpdates_offset', 0)
    monkeypatch.setattr(commands.time, 'time', lambda: NOW)
    ControlStore.open_controller(state.CONTROL_DB).close()
    state._reset_control_store()
    state.write_state({'in_flight': [], 'sprint': {'weeks': {'codex-subscription': NOW + 3600}}})
    shared_bot.register_project(repo)
    sent = []
    monkeypatch.setattr(commands, 'notify_telegram', sent.append)
    # Every notice/API boundary is synthetic, including older refresh tests.
    class Unsent:
        ok = False
    monkeypatch.setattr(telegram.requests, 'post', lambda *a, **kw: Unsent())
    monkeypatch.setattr(telegram, '_tg_api', lambda *a, **kw: None)
    yield repo, sent
    state._reset_control_store()


def deliver(repo, uid, verb):
    update = {'update_id': uid, 'message': {'chat': {'id': 42},
        'text': f'/sprint alpha {verb}'}}
    errors = []
    shared_bot.poll_once(lambda _: [update], lambda text: errors.append(text) or True)
    assert not errors
    commands.poll_control_commands([])
    assert shared_bot.local_updates(repo) == []


def graph_routing(repo, monkeypatch, *, weekly=10, reset=3600, signals=None):
    from maestro import orchestrator, reset_signals
    from maestro.backends.base import WindowUsage
    from maestro.usage_sources import PoolUsage
    from tests.test_model_graph_routing import _provider_tables
    from tests.backends.test_router import pair_catalog

    (repo / 'project.yaml').write_text(_provider_tables(repo))
    monkeypatch.setattr(orchestrator, 'REPO', repo)
    monkeypatch.setattr(reset_signals.ResetSignalSource, 'read', lambda _: signals)
    routing = orchestrator._graph_routing()
    routing.catalog = pair_catalog('codex')
    routing.usage_reader = lambda binding: PoolUsage(pool_id=binding.usage_pool_id,
        source='synthetic', updated_at=stamp(),
        windows={300: WindowUsage(10, NOW + 1800),
                 10080: WindowUsage(weekly, NOW + reset)})
    return routing


def test_graph_refresh_persists_current_weekly_sprint_with_frozen_inputs(sprint_project,
                                                                      monkeypatch):
    repo, _ = sprint_project
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    saved = state.read_state()['sprint']
    decision = saved.get('decisions', {}).get('codex-subscription', {})
    assert decision.get('active') is True, 'the production refresh must decide a behind-pace sprint'
    assert decision['reason'] == 'weekly_reset'
    assert decision['deadline'] == NOW + 3600
    assert decision['inputs']['weekly']['used_pct'] == 10
    assert decision['inputs']['weekly']['observed_at'] == stamp()
    assert decision['inputs']['config']['enabled'] is True
    state._reset_control_store()
    assert state.read_state()['sprint'] == saved


def target_routing(repo, monkeypatch, *, lane='forecast'):
    from maestro.backends.base import WindowUsage
    from maestro.reset_signals import ResetSignals, ResetForecast
    from maestro.usage_sources import PoolUsage
    from tests.backends.test_router import pair_catalog

    signals = ResetSignals(stamp(), 300,
        forecast=ResetForecast(70, 90, 'high') if lane == 'forecast' else None,
        active_signal={'deadline': stamp(NOW+1800)} if lane == 'announced' else None,
        banked_signal={'banked_state': 'available', 'credits': 1,
                       'expires_at': stamp(NOW+1800)} if lane == 'banked' else None)
    routing = graph_routing(repo, monkeypatch, signals=signals)
    routing.catalog = pair_catalog('claude', 'codex')
    routing.policy = routing.policy.with_objective('pace')
    def usage(binding):
        codex = binding.usage_pool_id == 'codex-subscription'
        return PoolUsage(pool_id=binding.usage_pool_id, source='synthetic', updated_at=stamp(),
            windows={300: WindowUsage(10, NOW+1800),
                     10080: WindowUsage(20 if codex else 0 if lane == 'weekly' else 30,
                         NOW+(23*3600 if codex and lane == 'weekly' else (4 if codex else 2)*86400))})
    routing.usage_reader = usage
    routing.refresh_pace(NOW)
    return routing


@pytest.mark.parametrize('action', ['off', 'stop'])
def test_real_factory_routes_by_active_weekly_target_and_immediate_control(
        sprint_project, monkeypatch, action):
    from maestro.workflows.routing import resolve
    from tests.backends.test_router import agent_node
    repo, _ = sprint_project
    routing = target_routing(repo, monkeypatch)
    observations = {pool: resource.observation.to_dict()
                    for pool, resource in routing.resources.pools.items()}
    result = resolve(routing, agent_node(), None, now_epoch=NOW)
    assert result.backend_id == 'codex', 'an active forecast must raise the real pool target'
    weekly = next(s for s in result.pace_decision.stages if s.name == 'weekly_pace')
    assert weekly.inputs['sprints']['codex-subscription']['active'] is True
    assert weekly.inputs['pool_paces']['codex-subscription'] == 80
    assert weekly.inputs['observed_pool_paces']['codex-subscription'] == 22.9

    # A control invalidates the old decision before quota refresh rebuilds target_pct.
    deliver(repo, 1, action)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['target_pct'] == 100
    stopped = resolve(routing, agent_node(), None, now_epoch=NOW)
    assert stopped.backend_id == 'claude'
    assert weekly.inputs['sprints']['codex-subscription']['active'] is True
    assert {pool: resource.observation.to_dict()
            for pool, resource in routing.resources.pools.items()} == observations
    state._reset_control_store()
    assert resolve(routing, agent_node(), None, now_epoch=NOW).backend_id == 'claude'
    deliver(repo, 2, 'on')
    routing.refresh_pace(NOW)
    assert resolve(routing, agent_node(), None, now_epoch=NOW).backend_id == 'codex'


@pytest.mark.parametrize('damage', ['inactive', 'foreign_pool', 'future_week', 'invalid_target',
    'future_evaluation', 'stale_signal', 'future_signal', 'missing_signal',
    'unknown_weekly', 'expired_weekly', 'malformed_weekly', 'future_observation',
    'stale_observation', 'full_five_hour', 'unknown_five_hour', 'paused_pool', 'busy_pool'])
def test_active_target_never_repairs_unusable_evidence_or_admission(
        sprint_project, monkeypatch, damage):
    from dataclasses import replace
    from maestro.backends import catalog as cat
    from maestro.workflows.routing import resolve
    from tests.backends.test_router import agent_node
    repo, _ = sprint_project
    routing = target_routing(repo, monkeypatch)
    document = state.read_state()
    decision = document['sprint']['decisions']['codex-subscription']
    pool = routing.resources.pool('codex-subscription')
    windows = dict(pool.observation.windows)
    if damage == 'inactive':
        decision['active'] = False
    elif damage == 'foreign_pool':
        decision['pool_id'] = 'claude-subscription'
    elif damage == 'future_week':
        decision['week'] += 604800
    elif damage == 'invalid_target':
        decision['target_pct'] = True
    elif damage == 'future_evaluation':
        decision['inputs']['now_epoch'] = NOW+1
    elif damage in ('stale_signal', 'future_signal'):
        decision['inputs']['signals']['observed_at'] = stamp(NOW-301 if damage == 'stale_signal' else NOW+1)
    elif damage == 'missing_signal':
        decision['inputs']['signals'] = None
    elif damage == 'unknown_weekly':
        windows.pop(10080)
    elif damage == 'expired_weekly':
        windows[10080] = cat.QuotaWindow(10080, 20, NOW-1)
    elif damage == 'malformed_weekly':
        windows[10080] = cat.QuotaWindow(10080, -1, NOW+4*86400)
    elif damage in ('future_observation', 'stale_observation'):
        pool.observation = replace(pool.observation,
            observed_at=stamp(NOW+1 if damage == 'future_observation' else NOW-901))
    elif damage == 'full_five_hour':
        windows[300] = cat.QuotaWindow(300, 100, NOW+1800)
    elif damage == 'unknown_five_hour':
        windows.pop(300)
    elif damage == 'paused_pool':
        pool.pause(until=NOW+1800, reason='operator')
    elif damage == 'busy_pool':
        pool.active = pool.concurrency_limit
    pool.observation = replace(pool.observation, windows=windows)
    state.write_state(document)
    assert resolve(routing, agent_node(), None, now_epoch=NOW).backend_id == 'claude', damage


@pytest.mark.parametrize('lane', ['weekly', 'announced', 'forecast', 'banked'])
def test_reasoned_target_and_skip_button_change_actual_selection_without_quota_refresh(
        sprint_transport, monkeypatch, lane):
    from maestro.workflows.routing import resolve
    from tests.backends.test_router import agent_node
    repo, posts, _ = sprint_transport
    routing = target_routing(repo, monkeypatch, lane=lane)
    assert resolve(routing, agent_node(), None, now_epoch=NOW).backend_id == 'codex'
    token = delivered_token(posts)
    commands._dispatch_delivery(sprint_callback(1, token), [], '42')
    assert not state.read_state()['sprint']['decisions']['codex-subscription']['active']
    assert resolve(routing, agent_node(), None, now_epoch=NOW).backend_id == 'claude'


def test_active_target_cannot_override_measured_quality_or_unknown_headroom(
        sprint_project, monkeypatch):
    from dataclasses import replace
    from maestro.backends import catalog as cat
    from maestro.workflows.routing import resolve
    from tests.backends.test_router import agent_node
    repo, _ = sprint_project
    routing = target_routing(repo, monkeypatch)
    original = resolve(routing, agent_node(), None, now_epoch=NOW)
    for entry, model in routing.catalog.models():
        for pair in entry.pairs(model):
            native, _ = entry.resolve_effort(model, pair.effort)
            shape = replace(original.shape_key, backend_id=entry.backend_id,
                            model_id=model, effort=native or 'unsupported')
            for i in range(10):
                routing.statistics.record(shape, success=i < (5 if entry.backend_id == 'codex' else 10))
    guarded = resolve(routing, agent_node(), None, now_epoch=NOW)
    assert guarded.backend_id == 'claude'
    assert any(r.reason == 'evidence_worse' and r.backend_id == 'codex'
               for r in guarded.rejected_routes)
    for resource in routing.resources.pools.values():
        resource.observation = replace(resource.observation,
            windows={10080: resource.observation.weekly})
    waiting = resolve(routing, agent_node(), None, now_epoch=NOW)
    assert not waiting.is_bound and waiting.disposition == 'wait' and waiting.reason == 'quota_unknown'


def test_active_target_keeps_nonpace_selection_unchanged(sprint_project, monkeypatch):
    from maestro.workflows.routing import resolve
    from tests.backends.test_router import agent_node
    repo, _ = sprint_project
    routing = target_routing(repo, monkeypatch)
    routing.policy = routing.policy.with_objective('paid_efficiency')
    active = resolve(routing, agent_node(), None, now_epoch=NOW)
    deliver(repo, 1, 'off')
    inactive = resolve(routing, agent_node(), None, now_epoch=NOW)
    assert (active.backend_id, active.model_id, active.requested_effort) == (
        inactive.backend_id, inactive.model_id, inactive.requested_effort)
    assert active.pace_decision is inactive.pace_decision is None


@pytest.mark.parametrize('waiting', [False, True])
def test_actual_sprint_selection_and_budget_result_are_durable_across_restart(
        sprint_project, runtime, monkeypatch, tmp_path, waiting):
    import json
    import os
    from maestro.control import resources
    from maestro.control.resources import HostReservationLedger
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.runner import WorkflowRunner
    from tests.graph_engineering.conftest import FixtureQueries
    repo, _ = sprint_project
    store, make = runtime
    monkeypatch.setenv('MAESTRO_HOST_LEDGER', str(tmp_path / 'target-host.json'))
    # Its default clock was captured before sprint_project froze time.time.
    def ledger(*args, **kwargs):
        return HostReservationLedger(*args, clock=lambda: NOW, **kwargs)
    monkeypatch.setattr(resources, 'HostReservationLedger', ledger)
    routing = target_routing(repo, monkeypatch)
    routing.queries = FixtureQueries()
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-target', 'kind': 'auto', 'cmd': 'true'}]})
    routing.version_reader = lambda _: 'synthetic'
    if waiting:
        held = ledger().reserve_budget('codex-subscription', {'account:300': 82},
            capacity={'account:300': 82}, resets={'account:300': NOW+1800},
            project_id='competing-project', run_id='competing-attempt', writer_pid=os.getpid())
        assert held.admission.admitted
    run, attempt, node = make(routing=routing)
    with ControlStore.open_client(store.path) as reopened:
        events = [e for e in reopened.events(aggregate_id=run.run_id)
                  if e.type == 'PaceDecisionRecorded']
        assert len(events) == 1
        payload = events[0].payload
        decision = payload['decision']
        weekly = next(s for s in decision['stages'] if s['name'] == 'weekly_pace')
        assert weekly['inputs']['sprint_targets']['codex-subscription'] == 100
        assert weekly['inputs']['observed_pool_paces']['codex-subscription'] == 22.9
        assert weekly['inputs']['pool_paces']['codex-subscription'] == 80
        assert weekly['inputs']['sprints']['codex-subscription']['reason'] == 'forecast_reset'
        assert decision['pools']['codex-subscription']['observation']['windows']['10080']['used_pct'] == 20
        assert decision['selected']['backend_id'] == 'codex'
        assert decision['stages'][-1]['inputs']['capacity']['account:300'] == 82
        if waiting:
            assert attempt is None and payload['outcome'] == 'wait'
            assert decision['stages'][-1]['result']['reason'] == 'budget_exhausted'
        else:
            assert attempt['backend_id'] == 'codex' and payload['outcome'] == 'admitted'
            assert json.loads(attempt['binding_json'])['pace_decision'] == decision
    deliver(repo, 1, 'stop')
    state._reset_control_store()
    restarted = WorkflowRunner(store, run.revision, run_id=run.run_id, repo=run.repo,
        routing=routing, dispatcher=run.dispatcher, artifacts=run.artifacts, is_alive=lambda _: True)
    restarted.tick()
    assert [e.payload for e in store.events(aggregate_id=run.run_id)
            if e.type == 'PaceDecisionRecorded'][0] == payload
    actual = store.conn.execute('SELECT * FROM attempts WHERE run_id=?', (run.run_id,)).fetchone()
    assert actual['backend_id'] == ('claude' if waiting else 'codex')
    if not waiting:
        assert actual['attempt_id'] == attempt['attempt_id']
        assert json.loads(actual['binding_json'])['pace_decision'] == decision


@pytest.mark.parametrize('lane,reason,deadline', [
    ('announced', 'announced_reset', NOW + 1800),
    ('forecast', 'forecast_reset', None), ('banked', 'banked_credit', NOW + 1800),
])
def test_current_reset_signals_drive_distinct_real_refresh_decisions(sprint_project, monkeypatch,
                                                                   lane, reason, deadline):
    from maestro.reset_signals import ResetSignals, ResetForecast
    repo, _ = sprint_project
    signals = ResetSignals(stamp(), 300,
        active_signal={'deadline': stamp(NOW + 1800)} if lane == 'announced' else None,
        banked_signal={'banked_state': 'available', 'credits': 1,
                       'expires_at': stamp(NOW + 1800)} if lane == 'banked' else None,
        forecast=ResetForecast(70, 90, 'high') if lane == 'forecast' else None)
    routing = graph_routing(repo, monkeypatch, weekly=95, signals=signals)
    routing.refresh_pace(NOW)
    decision = state.read_state()['sprint']['decisions']['codex-subscription']
    assert decision['active'] is True, 'a current reset signal must drive the production sprint'
    assert decision['reason'] == reason
    assert decision['deadline'] == deadline
    assert decision['inputs']['signals']['observed_at'] == stamp()
    assert decision['inputs']['signals']['forecast_is_announcement'] is False
    assert decision['activate_credit'] is (lane == 'banked')
    assert routing.resources.pool('codex-subscription').observation.five_hour.used_pct == 10
    if lane == 'forecast':
        assert decision['inputs']['signals']['forecast']['p24'] == 70
        record = state.read_state()['sprint']['forecast_sprints'][0]
        assert record['real_reset_observed'] is None


@pytest.mark.parametrize('damage', ['stale', 'future', 'expired_announcement', 'far_announcement',
    'malformed_announcement', 'spent_credit', 'expired_credit', 'no_credit', 'low_confidence',
    'low_odds', 'boolean_odds', 'nan_odds', 'history_only'])
def test_unusable_or_historical_signals_never_promote_to_current_capacity(damage):
    from maestro.sprint import evaluate, SprintConfig
    from maestro.reset_signals import ResetSignals, ResetForecast
    from maestro.backends.catalog import QuotaObservation, QuotaWindow
    observation = QuotaObservation(pool_id='codex-subscription', observed_at=stamp(),
        windows={10080: QuotaWindow(10080, 95, NOW + 3600)})
    observed = NOW-301 if damage == 'stale' else NOW+1 if damage == 'future' else NOW
    active, banked, forecast = None, None, None
    if damage in ('stale', 'future'):
        forecast = ResetForecast(99, 100, 'high')
    if damage.endswith('announcement'):
        active = {'deadline': {'expired_announcement': stamp(NOW-1),
            'far_announcement': stamp(NOW+86401), 'malformed_announcement': 123}[damage]}
    if damage.endswith('credit'):
        banked = {'banked_state': 'spent' if damage == 'spent_credit' else 'available',
            'credits': 0 if damage == 'no_credit' else 1,
            'expires_at': stamp(NOW-1 if damage == 'expired_credit' else NOW+1800)}
    if damage in ('low_confidence', 'low_odds', 'boolean_odds', 'nan_odds'):
        forecast = ResetForecast({'low_odds': 69.9, 'boolean_odds': True,
            'nan_odds': float('nan')}.get(damage, 99), 100,
            'medium' if damage == 'low_confidence' else 'high')
    signals = ResetSignals(stamp(observed), 300, active, banked, forecast, events=({
        'event_kind': 'banked_grant', 'banked_state': 'available', 'credits': 1,
        'expires_at': stamp(NOW+1800), 'deadline': stamp(NOW+1800)},))
    decision = evaluate('codex-subscription', observation, config=SprintConfig(),
        now_epoch=NOW, signals=signals)
    assert not decision.active and decision.reason == 'not_behind_pace'
    assert decision.target_pct is None and decision.activate_credit is False


def test_forecasts_change_only_codex_target_and_report_observed_reset_or_unknown(
        sprint_project, monkeypatch):
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    from maestro.reset_signals import ResetSignals, ResetForecast
    from maestro.sprint import evaluate, SprintConfig
    repo, _ = sprint_project
    signals = ResetSignals(stamp(), 300, forecast=ResetForecast(90, 100, 'high'))
    routing = graph_routing(repo, monkeypatch, weekly=95, signals=signals)
    routing.refresh_pace(NOW)
    saved = state.read_state()['sprint']
    assert len(saved['forecast_sprints']) == 1
    routing.refresh_pace(NOW)
    assert len(state.read_state()['sprint']['forecast_sprints']) == 1
    observation = routing.resources.pool('codex-subscription').observation
    other = evaluate('other-pool', observation, config=SprintConfig(), now_epoch=NOW, signals=signals)
    assert other.reason == 'not_behind_pace' and not other.active
    # A fresh window with a materially advanced deadline is actual rollover
    # evidence. No event history or missing reader is relabelled a reset.
    routing.resources.pool('codex-subscription').observation = replace(observation,
        observed_at=stamp(NOW+60), windows={10080: QuotaWindow(10080, 0, NOW+604800)})
    routing.refresh_pace(NOW+60)
    matched = state.read_state()['sprint']['forecast_sprints'][0]
    assert matched['real_reset_observed'] is True
    assert matched['reset_observed_at'] == stamp(NOW+60)
    assert saved['forecast_sprints'][0]['real_reset_observed'] is None
    assert saved['decisions']['codex-subscription']['deadline'] is None


@pytest.mark.parametrize('weekly,reset,active,reason', [
    (10, 3600, True, 'weekly_reset'), (95, 3600, False, 'not_behind_pace'),
    (10, 86400, True, 'weekly_reset'), (10, 86401, False, 'outside_reset_window'),
    (10, -1, False, 'weekly_unknown'), (10, 604801, False, 'weekly_unknown'),
    (float('nan'), 3600, False, 'weekly_unknown'), (True, 3600, False, 'weekly_unknown'),
])
def test_weekly_boundaries_remain_reasoned_without_inventing_quota(sprint_project, monkeypatch,
                                                                weekly, reset, active, reason):
    repo, _ = sprint_project
    routing = graph_routing(repo, monkeypatch, weekly=weekly, reset=reset)
    routing.refresh_pace(NOW)
    decision = state.read_state()['sprint']['decisions']['codex-subscription']
    assert decision['active'] is active and decision['reason'] == reason
    assert decision['target_pct'] == (100 if active else None)


def test_off_stop_on_and_reset_rearm_survive_controller_reconstruction(sprint_project, monkeypatch):
    repo, _ = sprint_project
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    deliver(repo, 1, 'stop')
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active'] is False
    state._reset_control_store()
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['reason'] == 'stopped_this_week'
    deliver(repo, 2, 'on')
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active']
    deliver(repo, 3, 'off')
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['reason'] == 'skipped_this_week'
    # A small corrected deadline is still the skipped week.
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    resource = routing.resources.pool('codex-subscription')
    resource.observation = replace(resource.observation, windows={
        **resource.observation.windows, 10080: QuotaWindow(10080, 10, NOW + 3900)})
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['reason'] == 'skipped_this_week'
    # The next actually observed weekly window re-arms without /on.
    next_now = NOW + 604800
    resource.observation = replace(resource.observation, observed_at=stamp(next_now), windows={
        10080: QuotaWindow(10080, 10, next_now + 3600)})
    routing.pace_probe_at['codex'] = next_now
    state._reset_control_store()
    routing.refresh_pace(next_now)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active']


def test_second_stop_keeps_a_pool_stopped_earlier_the_same_week():
    from maestro import sprint
    document = {'weeks': {'A': NOW + 5000, 'B': NOW + 6000},
                'decisions': {'A': {'active': True, 'week': NOW + 5000},
                              'B': {'active': False, 'week': NOW + 6000}}}
    first = sprint.control(document, 'stop', now_epoch=NOW)
    first['decisions']['B'] = {'active': True, 'week': NOW + 6000}
    second = sprint.control(first, 'stop', now_epoch=NOW + 60)
    assert second['stopped_weeks'] == {'A': NOW + 5000, 'B': NOW + 6000}
    # Pools still awaiting their first week also accumulate across stops.
    document = {'weeks': {}, 'decisions': {'A': {'active': True}, 'B': {'active': False}}}
    first = sprint.control(document, 'stop', now_epoch=NOW)
    first['decisions']['B'] = {'active': True}
    assert sprint.control(first, 'stop', now_epoch=NOW)['stopped_pools'] == ['A', 'B']


def test_unchanged_refresh_does_not_rewrite_state(sprint_project, monkeypatch):
    from maestro import sprint
    repo, _ = sprint_project
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    writes = []
    original = state.write_state
    monkeypatch.setattr(state, 'write_state', lambda doc: writes.append(doc) or original(doc))
    for step in (30, 60, 90):
        sprint.refresh(routing.resources, config=sprint.SprintConfig(), now_epoch=NOW + step)
    assert writes == [], 'an unchanged decision must not append a full state event every poll'
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active'] is True


def test_invalid_sprint_config_is_journaled_once_and_never_stops_the_refresh(
        sprint_project, monkeypatch):
    from maestro import orchestrator
    repo, _ = sprint_project
    routing = graph_routing(repo, monkeypatch)
    with (repo / 'project.yaml').open('a') as handle:
        handle.write('sprint: [not, a, mapping]\n')
    journal = []
    monkeypatch.setattr(orchestrator, 'append_journal', lambda *args: journal.append(args))
    routing.refresh_pace(NOW)
    routing.pace_probe_at.clear()
    routing.refresh_pace(NOW + 30)
    assert [entry[0] for entry in journal] == ['sprint_refresh_unavailable']


def test_off_before_any_observation_binds_one_week_and_permanent_disable_is_distinct(
        sprint_project, monkeypatch):
    repo, sent = sprint_project
    document = state.read_state()
    document['sprint'] = {}
    state.write_state(document)
    deliver(repo, 1, 'off')
    assert state.read_state()['sprint']['skip_pending'] is True
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['reason'] == 'skipped_this_week'
    with (repo / 'project.yaml').open('a') as stream:
        stream.write('sprint: {enabled: false}\n')
    deliver(repo, 2, 'on')
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['reason'] == 'config_disabled'
    deliver(repo, 3, 'status')
    assert 'disabled by config' in sent[-1]
    assert 'sprint: {enabled: false}' in (repo / 'project.yaml').read_text()


@pytest.mark.parametrize('block', [False, {'enabled': 1}, {'window_hours': 0},
    {'window_hours': float('inf')}, {'behind_margin_pct': True}, {'forecast_p24': 101},
    {'forecast_confidence': 'certain'}, {'unknown': 1}])
def test_doctor_rejects_invalid_sprint_config(block):
    from maestro.model_policy import role_shape_issues
    assert any('sprint' in issue for issue in role_shape_issues({'sprint': block}))


def test_sprint_effective_config_inherits_global_values_without_mutation(sprint_project, monkeypatch):
    repo, _ = sprint_project
    global_config = {'sprint': {'window_hours': 12, 'behind_margin_pct': 8}}
    monkeypatch.setattr(config, 'load_global_config', lambda: global_config)
    with (repo / 'project.yaml').open('a') as stream:
        stream.write('sprint: {forecast_p24: 80}\n')
    effective = config.sprint_config()
    assert (effective.enabled, effective.window_hours, effective.behind_margin_pct,
            effective.forecast_p24) == (True, 12, 8, 80)
    assert global_config == {'sprint': {'window_hours': 12, 'behind_margin_pct': 8}}


def test_sprint_off_is_a_durable_one_week_mailbox_control_and_replay_cannot_skip_next_week(
        sprint_project):
    repo, sent = sprint_project
    deliver(repo, 1, 'off')
    saved = state.read_state()['sprint']
    assert saved.get('skipped_weeks') == {'codex-subscription': NOW + 3600}, (
        '/sprint off must persist the observed current week through the real command handler')
    assert 'sprint' in commands.COMMANDS and '/sprint' in commands.HELP_TEXT
    state._reset_control_store()
    assert state.read_state()['sprint'] == saved
    row = state.control_store().conn.execute('SELECT status FROM command_inbox '
        'WHERE idempotency_key=?', ('telegram:update:1',)).fetchone()
    assert row['status'] == 'committed'
    changed = state.read_state()
    changed['sprint']['weeks']['codex-subscription'] += 604800
    state.write_state(changed)
    deliver(repo, 2, 'on')
    # A duplicate update bypasses execution even after restart and a new week.
    commands._dispatch_delivery({'update_id': 1, 'message': {
        'chat': {'id': 42}, 'text': '/sprint off'}}, [], '42')
    assert not state.read_state()['sprint'].get('skipped_weeks')
    assert sent


@pytest.mark.parametrize('action', ['off', 'stop'])
def test_mixed_pool_unknown_week_controls_bind_first_week_then_rearm(
        sprint_project, monkeypatch, action):
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    from maestro.reset_signals import ResetSignals, ResetForecast
    repo, _ = sprint_project
    signals = ResetSignals(stamp(), 300, forecast=ResetForecast(90, 100, 'high'))
    routing = graph_routing(repo, monkeypatch, signals=signals)
    routing.refresh_pace(NOW)
    resource = routing.resources.pool('codex-subscription')
    # One pool is known, the active forecast pool has never observed a week.
    other = replace(resource, pool_id='other-pool')
    routing.resources.pools['other-pool'] = other
    resource.observation = replace(resource.observation, windows={})
    document = state.read_state()
    document['sprint']['weeks'] = {'other-pool': NOW + 3600}
    state.write_state(document)
    routing.pace_probe_at['codex'] = NOW
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active']
    deliver(repo, 1, action)
    state._reset_control_store()
    routing.refresh_pace(NOW)
    assert not state.read_state()['sprint']['decisions']['codex-subscription']['active'], (
        'the active unknown-week pool must stay suppressed alongside the known pool')
    resource.observation = replace(resource.observation, windows={
        10080: QuotaWindow(10080, 10, NOW + 3600)})
    routing.refresh_pace(NOW)
    field = 'skipped_weeks' if action == 'off' else 'stopped_weeks'
    assert state.read_state()['sprint'].get(field, {}).get('codex-subscription') == NOW + 3600
    state._reset_control_store()
    resource.observation = replace(resource.observation, windows={
        10080: QuotaWindow(10080, 10, NOW + 3900)})
    routing.refresh_pace(NOW)
    assert not state.read_state()['sprint']['decisions']['codex-subscription']['active']
    next_now = NOW + 604800
    resource.observation = replace(resource.observation, observed_at=stamp(next_now), windows={
        10080: QuotaWindow(10080, 10, next_now + 3600)})
    routing.pace_probe_at['codex'] = next_now
    routing.refresh_pace(next_now)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active']


def test_status_reports_an_active_signal_without_week_identity(sprint_project, monkeypatch):
    from dataclasses import replace
    from maestro.reset_signals import ResetSignals, ResetForecast
    repo, sent = sprint_project
    routing = graph_routing(repo, monkeypatch,
        signals=ResetSignals(stamp(), 300, forecast=ResetForecast(90, 100, 'high')))
    routing.refresh_pace(NOW)
    resource = routing.resources.pool('codex-subscription')
    resource.observation = replace(resource.observation, windows={})
    document = state.read_state()
    document['sprint']['weeks'] = {}
    state.write_state(document)
    routing.pace_probe_at['codex'] = NOW
    routing.refresh_pace(NOW)
    deliver(repo, 1, 'status')
    assert 'codex-subscription: forecast_reset' in sent[-1]


@pytest.mark.parametrize('crash', ['before_write', 'after_write', 'before_settle', 'newer_control'])
def test_claimed_sprint_control_recovers_without_reinterpreting_its_week(
        sprint_project, monkeypatch, crash):
    repo, _ = sprint_project
    original_write = commands.write_state
    def failed_write(document):
        if crash == 'after_write':
            original_write(document)
        raise OSError('synthetic controller crash')
    update = {'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '/sprint off'}}
    shared_bot.poll_once(lambda _: [{**update, 'message': {
        **update['message'], 'text': '/sprint alpha off'}}], lambda _: True)
    with monkeypatch.context() as patch:
        if crash == 'before_settle':
            patch.setattr(commands, '_settle_control_command',
                          lambda _: (_ for _ in ()).throw(OSError('synthetic settle crash')))
        else:
            patch.setattr(commands, 'write_state', failed_write)
        commands.poll_control_commands([])
    row = state.control_store().conn.execute('SELECT * FROM command_inbox WHERE '
        'idempotency_key=?', ('telegram:update:1',)).fetchone()
    assert row['status'] == 'processing'
    original_payload = row['payload']
    # Genuine crash recovery: release controller handle and reconstruct from disk.
    state._reset_control_store()
    changed = state.read_state()
    changed['sprint']['weeks']['codex-subscription'] = NOW + 604800 + 3600
    state.write_state(changed)
    if crash == 'newer_control':
        # An older unresolved mailbox delivery cannot undo a later accepted /on.
        commands._dispatch_delivery({'update_id': 2, 'message': {
            'chat': {'id': 42}, 'text': '/sprint on'}}, [], '42')
    commands.poll_control_commands([])
    recovered = state.control_store().conn.execute('SELECT * FROM command_inbox WHERE '
        'idempotency_key=?', ('telegram:update:1',)).fetchone()
    assert recovered['status'] == 'committed', 'a claimed sprint command must recover after restart'
    assert recovered['payload'] == original_payload
    saved = state.read_state()['sprint']
    if crash == 'newer_control':
        assert not saved.get('skipped_weeks')
    else:
        assert saved['skipped_weeks'] == {'codex-subscription': NOW + 3600}
    assert shared_bot.local_updates(repo) == []
    # Settled replay after the reset also cannot reinterpret the old delivery.
    before = state.read_state()['sprint']
    commands._dispatch_delivery(update, [], '42')
    assert state.read_state()['sprint'] == before


@pytest.fixture
def sprint_transport(sprint_project, monkeypatch):
    repo, _ = sprint_project
    posts, api = [], []
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '7:synthetic')
    def post(url, **kwargs):
        data = kwargs['data']
        posts.append(data)
        class Delivered:
            ok = True
            def json(self):
                return {'ok': True, 'result': {'message_id': 100 + len(posts)}}
        return Delivered()
    monkeypatch.setattr(telegram.requests, 'post', post)
    monkeypatch.setattr(telegram, '_tg_api', lambda method, data: api.append((method, data)))
    return repo, posts, api


@pytest.mark.parametrize('lane,explanation', [
    ('weekly', 'weekly reset'), ('announced', 'announced'),
    ('forecast', 'forecast'), ('banked', 'activate'),
])
def test_real_refresh_sends_one_reasoned_start_notice_with_week_bound_button(
        sprint_transport, monkeypatch, lane, explanation):
    import json
    from maestro.reset_signals import ResetSignals, ResetForecast
    repo, posts, _ = sprint_transport
    signals = ResetSignals(stamp(), 300,
        active_signal={'deadline': stamp(NOW+1800)} if lane == 'announced' else None,
        banked_signal={'banked_state': 'available', 'credits': 1,
                      'expires_at': stamp(NOW+1800)} if lane == 'banked' else None,
        forecast=ResetForecast(90, 100, 'high') if lane == 'forecast' else None)
    routing = graph_routing(repo, monkeypatch, weekly=10 if lane == 'weekly' else 95,
                            signals=signals)
    routing.refresh_pace(NOW)
    assert len(posts) == 1, 'the actual sprint start must deliver its operator notice'
    assert posts[0]['text'].startswith('[alpha] ')
    assert explanation in posts[0]['text'].lower()
    if lane == 'forecast':
        assert 'announced' not in posts[0]['text'].lower()
    button = json.loads(posts[0]['reply_markup'])['inline_keyboard'][0][0]
    assert button['text'] == 'Skip this week'
    token = button['callback_data'].removeprefix('sprint:skip:')
    record = state.read_state()['sprint']['notices'][token]
    assert record['pool_id'] == 'codex-subscription'
    assert record['week'] == NOW + 3600
    assert record['message_id'] == 101
    assert record['project_key'] == shared_bot.project_key(repo)
    assert record['chat_id'] == '42'
    state._reset_control_store()
    routing.refresh_pace(NOW)
    assert len(posts) == 1, 'restart and repeated refresh preserve the delivered episode'


def sprint_callback(uid, token, *, mid=101, chat=42, tag='alpha', source=7):
    return {'update_id': uid, 'callback_query': {'id': f'cq-{uid}',
        'data': f'sprint:skip:{token}', 'message': {'message_id': mid,
        'chat': {'id': chat}, 'text': f'[{tag}] Sprint started',
        'from': {'is_bot': True, 'id': source}}}}


def test_real_skip_callback_routes_exact_project_message_episode_and_rearms(
        sprint_transport, monkeypatch):
    import copy
    import json
    import queue
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    repo, posts, api = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    token = json.loads(posts[0]['reply_markup'])['inline_keyboard'][0][0][
        'callback_data'].removeprefix('sprint:skip:')
    # Duplicate token in another registered project still requires exact message ownership.
    beta = repo.parent / 'beta'
    (beta / '.orchestrator').mkdir(parents=True)
    (beta / 'project.yaml').write_text('project: {name: beta}\n')
    shared_bot.register_project(beta)
    beta_state = copy.deepcopy(state.read_state())
    beta_record = beta_state['sprint']['notices'][token]
    beta_record.update(project_key=shared_bot.project_key(beta), message_id=202)
    shared_bot._atomic_json(beta / '.orchestrator/state.json', beta_state)
    update = sprint_callback(1, token)
    errors = []
    shared_bot.poll_once(lambda _: [update], lambda text: errors.append(text) or True)
    assert not errors, 'the shared callback registry must accept a delivered sprint button'
    assert not shared_bot.local_updates(beta)
    before = state.read_state()['sprint']
    inbox = queue.Queue()
    commands._listen_once(commands._fetch_updates, inbox)
    assert inbox.qsize() == 1, 'the listener must queue sprint taps for the controller'
    assert state.read_state()['sprint'] == before, 'the listener owns no sprint state writes'
    monkeypatch.setattr(commands, '_LISTENER_QUEUE', inbox)
    commands.poll_control_commands([])
    saved = state.read_state()['sprint']
    assert saved['skipped_weeks'] == {'codex-subscription': NOW + 3600}
    assert saved['notices'][token]['skipped'] is True
    assert not saved['decisions']['codex-subscription']['active']
    assert shared_bot.local_updates(repo) == []
    assert any(method == 'answerCallbackQuery' for method, _ in api)
    state._reset_control_store()
    commands._dispatch_delivery(update, [], '42')
    assert state.read_state()['sprint'] == saved
    # A different delivery of the same old button also cannot skip the next week.
    resource = routing.resources.pool('codex-subscription')
    next_now = NOW + 604800
    resource.observation = replace(resource.observation, observed_at=stamp(next_now),
        windows={10080: QuotaWindow(10080, 10, next_now + 3600)})
    routing.pace_probe_at['codex'] = next_now
    monkeypatch.setattr(commands.time, 'time', lambda: next_now)
    routing.refresh_pace(next_now)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active']
    next_week = state.read_state()['sprint']
    commands._dispatch_delivery(sprint_callback(2, token), [], '42')
    assert state.read_state()['sprint'] == next_week
    assert json.loads((beta / '.orchestrator/state.json').read_text()) == beta_state


def delivered_token(posts):
    import json
    return json.loads(posts[-1]['reply_markup'])['inline_keyboard'][0][0][
        'callback_data'].removeprefix('sprint:skip:')


def test_older_claimed_callback_cannot_undo_newer_on(sprint_transport, monkeypatch):
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    update = sprint_callback(1, delivered_token(posts))
    shared_bot.poll_once(lambda _: [update], lambda _: True)
    with monkeypatch.context() as patch:
        patch.setattr(commands, 'write_state', lambda _: (_ for _ in ()).throw(OSError('crash')))
        commands.poll_control_commands([])
    state._reset_control_store()
    commands._dispatch_delivery({'update_id': 2, 'message': {
        'chat': {'id': 42}, 'text': '/sprint on'}}, [], '42')
    commands.poll_control_commands([])
    assert not state.read_state()['sprint'].get('skipped_weeks'), (
        'an older claimed button cannot reverse the later accepted operator control')
    row = state.control_store().conn.execute('SELECT status FROM command_inbox '
        'WHERE idempotency_key=?', ('telegram:update:1',)).fetchone()
    assert row['status'] == 'committed'


def test_changed_telegram_source_gets_its_own_notice_episode(sprint_transport, monkeypatch):
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    first = delivered_token(posts)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '8:synthetic')
    routing.refresh_pace(NOW)
    assert len(posts) == 2, 'a new shared bot cannot reuse the old bot message ownership'
    assert delivered_token(posts) != first


def test_unseen_callback_for_a_different_pool_is_not_discarded_by_later_pool_receipt(
        sprint_transport, monkeypatch):
    from dataclasses import replace
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    first = delivered_token(posts)
    resource = routing.resources.pool('codex-subscription')
    routing.resources.pools['other-pool'] = replace(resource, pool_id='other-pool')
    routing.refresh_pace(NOW)
    second = state.read_state()['sprint']['active_notices']['other-pool']
    shared_bot.poll_once(lambda _: [sprint_callback(1, first)], lambda _: True)
    with monkeypatch.context() as patch:
        patch.setattr(commands, 'write_state', lambda _: (_ for _ in ()).throw(OSError('crash')))
        commands.poll_control_commands([])
    commands._dispatch_delivery(sprint_callback(2, second, mid=102), [], '42')
    state._reset_control_store()
    commands.poll_control_commands([])
    assert state.read_state()['sprint']['skipped_weeks'] == {
        'codex-subscription': NOW+3600, 'other-pool': NOW+3600}, (
        'different pools are independent controls even when one controller delivery was interrupted')


def test_older_global_on_recovers_other_pools_while_preserving_newer_pool_skip(
        sprint_transport, monkeypatch):
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    token = delivered_token(posts)
    document = state.read_state()
    document['sprint']['weeks']['other-pool'] = NOW+3600
    document['sprint']['stopped_weeks'] = {'other-pool': NOW+3600}
    state.write_state(document)
    shared_bot.poll_once(lambda _: [{'update_id': 1, 'message': {
        'chat': {'id': 42}, 'text': '/sprint alpha on'}}], lambda _: True)
    with monkeypatch.context() as patch:
        patch.setattr(commands, 'write_state', lambda _: (_ for _ in ()).throw(OSError('crash')))
        commands.poll_control_commands([])
    commands._dispatch_delivery(sprint_callback(2, token), [], '42')
    state._reset_control_store()
    commands.poll_control_commands([])
    saved = state.read_state()['sprint']
    assert not saved.get('stopped_weeks'), 'the older on still applies to independent pools'
    assert saved['skipped_weeks'] == {'codex-subscription': NOW+3600}


@pytest.mark.parametrize('crash', ['before_write', 'after_write', 'before_settle', 'after_reset'])
def test_claimed_callback_recovers_real_mailbox_without_skipping_a_later_week(
        sprint_transport, monkeypatch, crash):
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    token = delivered_token(posts)
    shared_bot.poll_once(lambda _: [sprint_callback(1, token)], lambda _: True)
    original_write = commands.write_state
    def fail(document):
        if crash == 'after_write':
            original_write(document)
        raise OSError('synthetic callback controller crash')
    with monkeypatch.context() as patch:
        if crash == 'before_settle':
            patch.setattr(commands, '_settle_control_command',
                          lambda _: (_ for _ in ()).throw(OSError('settle crash')))
        else:
            patch.setattr(commands, 'write_state', fail)
        commands.poll_control_commands([])
    assert state.control_store().claimed_commands()
    assert shared_bot.local_updates(repo)
    state._reset_control_store()
    if crash == 'after_reset':
        next_now = NOW + 604800
        resource = routing.resources.pool('codex-subscription')
        resource.observation = replace(resource.observation, observed_at=stamp(next_now),
            windows={10080: QuotaWindow(10080, 10, next_now+3600)})
        routing.pace_probe_at['codex'] = next_now
        monkeypatch.setattr(commands.time, 'time', lambda: next_now)
        routing.refresh_pace(next_now)
    commands.poll_control_commands([])
    saved = state.read_state()['sprint']
    assert not state.control_store().claimed_commands()
    assert not shared_bot.local_updates(repo)
    if crash == 'after_reset':
        assert not saved.get('skipped_weeks')
        assert saved['decisions']['codex-subscription']['active']
    else:
        assert saved['notices'][token]['skipped']
        assert saved['skipped_weeks'] == {'codex-subscription': NOW+3600}


def test_unknown_week_button_binds_only_its_pool_then_rearms(sprint_transport, monkeypatch):
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    from maestro.reset_signals import ResetSignals, ResetForecast
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch,
        signals=ResetSignals(stamp(), 300, forecast=ResetForecast(90, 100, 'high')))
    routing.refresh_pace(NOW)
    resource = routing.resources.pool('codex-subscription')
    routing.resources.pools['other-pool'] = replace(resource, pool_id='other-pool')
    resource.observation = replace(resource.observation, windows={})
    document = state.read_state()
    document['sprint']['weeks'] = {}
    state.write_state(document)
    routing.pace_probe_at['codex'] = NOW
    routing.refresh_pace(NOW)
    current = state.read_state()['sprint']
    token = current['active_notices']['codex-subscription']
    record = current['notices'][token]
    assert record['week'] is None
    shared_bot.poll_once(lambda _: [sprint_callback(1, token, mid=record['message_id'])], lambda _: True)
    commands.poll_control_commands([])
    assert state.read_state()['sprint']['skip_pending_pools'] == ['codex-subscription']
    assert state.read_state()['sprint']['decisions']['other-pool']['active']
    state._reset_control_store()
    resource.observation = replace(resource.observation, windows={
        10080: QuotaWindow(10080, 10, NOW+3600)})
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['skipped_weeks'] == {'codex-subscription': NOW+3600}
    next_now = NOW+604800
    resource.observation = replace(resource.observation, observed_at=stamp(next_now), windows={
        10080: QuotaWindow(10080, 10, next_now+3600)})
    routing.pace_probe_at['codex'] = next_now
    routing.refresh_pace(next_now)
    assert state.read_state()['sprint']['decisions']['codex-subscription']['active']


@pytest.mark.parametrize('damage', ['message', 'source', 'chat', 'project', 'unknown', 'unbound'])
def test_shared_poller_rejects_foreign_or_unbound_sprint_callbacks(
        sprint_transport, monkeypatch, damage):
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    token = delivered_token(posts)
    if damage == 'unbound':
        document = state.read_state()
        document['sprint']['notices'][token]['message_id'] = None
        state.write_state(document)
    if damage == 'project':
        beta = repo.parent / 'beta'
        beta.mkdir()
        (beta / 'project.yaml').write_text('project: {name: beta}\n')
        shared_bot.register_project(beta)
    update = sprint_callback(1, '0'*24 if damage == 'unknown' else token,
        mid=999 if damage == 'message' else 101, source=8 if damage == 'source' else 7,
        chat=99 if damage == 'chat' else 42, tag='beta' if damage == 'project' else 'alpha')
    before = state.read_state()['sprint']
    errors = []
    shared_bot.poll_once(lambda _: [update], lambda text: errors.append(text) or True)
    assert bool(errors) is (damage != 'chat')
    assert not shared_bot.local_updates(repo)
    commands.poll_control_commands([])
    assert state.read_state()['sprint'] == before


def test_delivery_without_durable_receipt_never_controls_then_retry_binds_exact_message(
        sprint_transport, monkeypatch):
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    original = state.write_state
    def fail_receipt(document):
        if any(r['message_id'] is not None for r in document.get('sprint', {}).get('notices', {}).values()):
            raise OSError('synthetic receipt persistence crash')
        original(document)
    with monkeypatch.context() as patch:
        patch.setattr(state, 'write_state', fail_receipt)
        with pytest.raises(OSError, match='receipt persistence'):
            routing.refresh_pace(NOW)
    token = delivered_token(posts)
    state._reset_control_store()
    assert state.read_state()['sprint']['notices'][token]['message_id'] is None
    errors = []
    shared_bot.poll_once(lambda _: [sprint_callback(1, token)], lambda text: errors.append(text) or True)
    assert errors and not shared_bot.local_updates(repo)
    routing.refresh_pace(NOW)
    assert len(posts) == 2 and delivered_token(posts) == token
    shared_bot.poll_once(lambda _: [sprint_callback(2, token)], lambda text: errors.append(text) or True)
    assert len(errors) == 2 and not shared_bot.local_updates(repo)
    shared_bot.poll_once(lambda _: [sprint_callback(3, token, mid=102)], lambda _: True)
    commands.poll_control_commands([])
    assert state.read_state()['sprint']['notices'][token]['skipped']


@pytest.mark.parametrize('kind', ['text', 'button'])
def test_unrelated_later_status_does_not_discard_an_unseen_sprint_control(
        sprint_transport, monkeypatch, kind):
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch)
    routing.refresh_pace(NOW)
    update = sprint_callback(1, delivered_token(posts)) if kind == 'button' else {
        'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '/sprint alpha off'}}
    shared_bot.poll_once(lambda _: [update], lambda _: True)
    with monkeypatch.context() as patch:
        patch.setattr(commands, 'write_state', lambda _: (_ for _ in ()).throw(OSError('crash')))
        commands.poll_control_commands([])
    commands._dispatch_delivery({'update_id': 100, 'message': {
        'chat': {'id': 42}, 'text': '/sprint status'}}, [], '42')
    state._reset_control_store()
    commands.poll_control_commands([])
    assert state.read_state()['sprint']['skipped_weeks'] == {'codex-subscription': NOW+3600}


@pytest.mark.parametrize('scope', ['bot', 'chat', 'project'])
def test_claimed_text_control_rejects_changed_source_scope(sprint_transport, monkeypatch, scope):
    repo, _, _ = sprint_transport
    commands._ingest_control_command(1, '/sprint off')
    if scope == 'bot':
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '8:synthetic')
    elif scope == 'chat':
        monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '99')
    else:
        monkeypatch.setattr(commands, 'REPO', repo.parent / 'beta')
    chat = '99' if scope == 'chat' else '42'
    commands._dispatch_delivery({'update_id': 1, 'message': {
        'chat': {'id': chat}, 'text': '/sprint off'}}, [], chat)
    assert not state.read_state()['sprint'].get('skipped_weeks')


def test_forecast_notice_cannot_bind_an_expired_cached_week(sprint_transport, monkeypatch):
    from dataclasses import replace
    from maestro.backends.catalog import QuotaWindow
    from maestro.reset_signals import ResetSignals, ResetForecast
    repo, posts, _ = sprint_transport
    routing = graph_routing(repo, monkeypatch,
        signals=ResetSignals(stamp(), 300, forecast=ResetForecast(90, 100, 'high')))
    routing.refresh_pace(NOW)
    resource = routing.resources.pool('codex-subscription')
    resource.observation = replace(resource.observation, windows={})
    document = state.read_state()
    document['sprint']['weeks']['codex-subscription'] = NOW-1
    state.write_state(document)
    routing.pace_probe_at['codex'] = NOW
    routing.refresh_pace(NOW)
    token = delivered_token(posts)
    record = state.read_state()['sprint']['notices'][token]
    assert record['week'] is None, 'an expired cached week cannot own a new forecast button'
    commands._dispatch_delivery(sprint_callback(1, token, mid=record['message_id']), [], '42')
    assert state.read_state()['sprint']['skip_pending_pools'] == ['codex-subscription']
    resource.observation = replace(resource.observation, windows={
        10080: QuotaWindow(10080, 10, NOW+3600)})
    routing.refresh_pace(NOW)
    assert state.read_state()['sprint']['skipped_weeks'] == {'codex-subscription': NOW+3600}
