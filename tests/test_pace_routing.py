"""W5's public routing contract, using the shipped approved pair catalog."""
from datetime import datetime, timezone

import pytest

from maestro.backends import catalog as cat, router as rt
from maestro.workflows.policy import default_policy
from tests.backends.test_router import pair_catalog, agent_node


NOW = 2_000_000_000


def observe(resources, pool, used=10, weekly=0, *, reset=1800, age=0):
    resources.observe(cat.QuotaObservation(
        pool_id=pool, observed_at=datetime.fromtimestamp(NOW-age, timezone.utc).isoformat(),
        windows={300: cat.QuotaWindow(300, used, NOW+reset),
                 10080: cat.QuotaWindow(10080, weekly, NOW+3600)}))


def route(resources, **kwargs):
    return rt.resolve_agent(agent_node(), None, resources, pair_catalog('claude', 'codex'),
                            default_policy().with_objective('pace'), now_epoch=NOW, **kwargs)


def test_pace_is_a_supported_execution_objective():
    assert default_policy().with_objective('pace').objective == 'pace'


def test_full_five_hour_pool_cannot_win_by_weekly_pace():
    resources = cat.ResourcePoolState()
    observe(resources, 'claude-subscription', 100)
    observe(resources, 'codex-subscription', weekly=80)
    result = route(resources)
    assert result.is_bound and result.backend_id == 'codex'
    assert any(r.reason == 'quota_exhausted' for r in result.rejected_routes)
    assert result.to_dict()['pace_decision']['pools']['claude-subscription']['gate'] == 'quota_exhausted'


@pytest.mark.parametrize('age,reset', [(0, -1), (-1, 1800), (901, 1800), (0, 18001)])
def test_pace_waits_when_five_hour_headroom_is_not_current(age, reset):
    resources = cat.ResourcePoolState()
    for pool in ('claude-subscription', 'codex-subscription'):
        observe(resources, pool, reset=reset, age=age)
    result = route(resources)
    assert not result.is_bound and result.disposition == 'wait'
    assert result.reason == 'quota_unknown'
    assert result.to_dict()['pace_decision']['pools']


def test_five_hour_gate_fits_the_attempt_estimate_before_ranking():
    resources = cat.ResourcePoolState()
    observe(resources, 'claude-subscription', 91.999)
    observe(resources, 'codex-subscription', weekly=80)
    assert route(resources).backend_id == 'codex'


def test_meaningfully_worse_class_evidence_is_removed_before_weekly_pace():
    resources = cat.ResourcePoolState()
    observe(resources, 'claude-subscription')
    observe(resources, 'codex-subscription', weekly=80)
    statistics = cat.OutcomeStatistics()
    catalog = pair_catalog('claude', 'codex')
    for entry, model in catalog.models():
        for pair in entry.pairs(model):
            native, _ = entry.resolve_effort(model, pair.effort)
            key = cat.ShapeKey(entry.backend_id, model, native or 'unsupported', '1.0.0',
                               'ordinary', '', '')
            for i in range(10):
                statistics.record(key, success=(i < (5 if entry.backend_id == 'claude' else 10)))
    result = route(resources, statistics=statistics)
    assert result.backend_id == 'codex'
    assert any(r.reason == 'evidence_worse' for r in result.rejected_routes)


def test_dispatch_learns_runtime_only_from_the_exact_recorded_shape():
    import json
    from maestro.workflows.routing import DispatchRouting, resolve

    resources = cat.ResourcePoolState()
    observe(resources, 'codex-subscription')
    dispatch = DispatchRouting(
        catalog=pair_catalog('codex'), policy=default_policy().with_objective('pace'),
        queries=None, task_spec=lambda _: None, resources=resources)
    original = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    shape = original.shape_key.to_dict()
    rows = [dict(attempt_id=f'prior-{i}', status='succeeded',
                 shape_key_json=json.dumps(shape), binding_json=json.dumps({
                     'usage_pool_id': original.usage_pool_id, 'relative_cost': 7}),
                 started_at=datetime.fromtimestamp(NOW-7200-i*4000, timezone.utc).isoformat(),
                 wall_time_sec=3600) for i in range(5)]
    # Other role/template/fragment compositions must never masquerade as this shape.
    rows.append({**rows[0], 'attempt_id': 'other-template', 'wall_time_sec': 1,
                 'shape_key_json': json.dumps({**shape, 'template_version': 'different'})})
    dispatch.pace_history_reader = lambda: rows
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    candidate = next(c for c in result.pace_decision.candidates
                     if (c['model_id'], c['effort']) == (original.model_id, original.requested_effort))
    estimate = candidate['estimate']
    assert estimate['runtime_sec'] == 3600
    assert estimate['runtime_source'] == 'historical_shape_runtime'
    assert estimate['runtime_observations'] == 5
    assert estimate['burn_source'] == 'configured_initial_estimate'


def history_dispatch():
    import json
    from maestro.workflows.routing import DispatchRouting, resolve
    resources = cat.ResourcePoolState()
    observe(resources, 'codex-subscription')
    dispatch = DispatchRouting(catalog=pair_catalog('codex'),
        policy=default_policy().with_objective('pace'), queries=None,
        task_spec=lambda _: None, resources=resources)
    original = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    rows = []
    for i in range(5):
        started = NOW-7200-i*4000
        def sample(at, used):
            stamp = datetime.fromtimestamp(at, timezone.utc).isoformat()
            return json.dumps(dict(source='native', pool_id=original.usage_pool_id,
                observed_at=stamp, read_at=stamp, staleness_bound=300,
                windows={'300': {'used_pct': used, 'resets_at': started+17000}}))
        rows.append(dict(attempt_id=f'prior-{i}', status='succeeded',
            shape_key_json=json.dumps(original.shape_key.to_dict()),
            binding_json=json.dumps({'usage_pool_id': original.usage_pool_id, 'relative_cost': 7}),
            started_at=datetime.fromtimestamp(started, timezone.utc).isoformat(), wall_time_sec=3600,
            usage_start_json=sample(started, 10), usage_settle_json=sample(started+3600, 24)))
    dispatch.pace_history_reader = lambda: rows
    return dispatch, rows


def test_pool_burn_is_an_explicit_estimate_using_recorded_price_and_nonoverlap():
    from maestro.workflows.routing import resolve
    dispatch, _ = history_dispatch()
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    estimates = [c['estimate'] for c in result.pace_decision.candidates]
    assert estimates and all(e['burn_pct_per_cost'] == 2 for e in estimates)
    assert all(e['burn_source'] == 'estimated_nonoverlapping_pool_burn' for e in estimates)
    assert all(e['burn_observations'] == 5 and e['burn_total'] == 5 for e in estimates)


def test_unrelated_observation_interval_cannot_supply_an_attempt_price_denominator():
    import json
    from maestro.workflows.routing import resolve
    dispatch, rows = history_dispatch()
    for key in ('usage_start_json', 'usage_settle_json'):
        sample = json.loads(rows[0][key])
        for field in ('observed_at', 'read_at'):
            at = datetime.fromisoformat(sample[field]).timestamp()-100000
            sample[field] = datetime.fromtimestamp(at, timezone.utc).isoformat()
        sample['windows']['300']['resets_at'] -= 100000
        rows[0][key] = json.dumps(sample)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    for candidate in result.pace_decision.candidates:
        estimate = candidate['estimate']
        assert estimate['burn_pct_per_cost'] == 1
        assert estimate['burn_observations'] == 4
        assert estimate['burn_fallback'] == 'insufficient_eligible_history'
        assert estimate['burn_exclusions']['prior-0'] == 'attempt_interval_mismatch'


@pytest.mark.parametrize('damage', ['overlap', 'missing_price', 'source_changed',
                                  'stale', 'future', 'reset_crossing', 'scoped_only'])
def test_ineligible_pool_history_retains_explicit_fallback_and_coverage(damage):
    import json
    from maestro.workflows.routing import resolve
    dispatch, rows = history_dispatch()
    if damage == 'overlap':
        rows.append({**rows[0], 'attempt_id': 'concurrent', 'status': 'failed'})
    elif damage == 'missing_price':
        rows[0]['binding_json'] = json.dumps({'usage_pool_id': 'codex-subscription'})
    elif damage == 'reset_crossing':
        rows[0]['reset_crossing'] = 1
    else:
        sample = json.loads(rows[0]['usage_settle_json'])
        if damage == 'source_changed':
            sample['source'] = 'other-source'
        elif damage == 'stale':
            sample['staleness_bound'] = 0.1
            sample['observed_at'] = rows[0]['started_at']
        elif damage == 'future':
            sample['observed_at'] = datetime.fromtimestamp(NOW+1, timezone.utc).isoformat()
        elif damage == 'scoped_only':
            sample['scoped_windows'] = {'opus': sample.pop('windows')}
        rows[0]['usage_settle_json'] = json.dumps(sample)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    for candidate in result.pace_decision.candidates:
        estimate = candidate['estimate']
        assert estimate['burn_source'] == 'configured_initial_estimate'
        assert estimate['burn_observations'] == 4
        assert estimate['burn_fallback'] == 'insufficient_eligible_history'
        assert estimate['burn_exclusions']['prior-0']


@pytest.mark.parametrize('fragments', [[{}], 42, 'fragment', [None], None])
def test_malformed_historical_composition_cannot_crash_dispatch(fragments):
    import json
    from maestro.workflows.routing import resolve
    dispatch, rows = history_dispatch()
    for row in rows:
        shape = json.loads(row['shape_key_json'])
        shape['active_fragment_set'] = fragments
        row['shape_key_json'] = json.dumps(shape)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    assert all(c['estimate']['runtime_source'] == 'configured_initial_estimate'
               for c in result.pace_decision.candidates)


@pytest.mark.parametrize('source', [{'invalid': 'source'}, ['native'], 42, True])
def test_malformed_sample_source_cannot_be_learned_as_pool_burn(source):
    import json
    from maestro.workflows.routing import resolve
    dispatch, rows = history_dispatch()
    for row in rows:
        for field in ('usage_start_json', 'usage_settle_json'):
            sample = json.loads(row[field])
            sample['source'] = source
            row[field] = json.dumps(sample)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    for candidate in result.pace_decision.candidates:
        assert candidate['estimate']['burn_pct_per_cost'] == 1
        assert candidate['estimate']['burn_observations'] == 0


def test_unrepresentable_runtime_history_keeps_finite_initial_estimate():
    from maestro.workflows.routing import resolve
    dispatch, rows = history_dispatch()
    for row in rows:
        row['wall_time_sec'] = 10**400
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    assert all(c['estimate']['runtime_sec'] == 1800 for c in result.pace_decision.candidates)


def cost_evidence_dispatch():
    import json
    from dataclasses import replace
    from maestro.workflows.routing import DispatchRouting
    base = pair_catalog('codex').entry('codex')
    model = 'gpt-6.1-sol'
    entry = replace(base, available_models=(model,), model_profiles={model: base.profile(model)},
        pair_profiles={key: pair for key, pair in base.pair_profiles.items()
                       if key[0] == model and key[1] in ('low', 'medium')})
    resources = cat.ResourcePoolState()
    observe(resources, 'codex-subscription')
    dispatch = DispatchRouting(catalog=cat.CapabilityCatalog.of([entry]),
        policy=default_policy().with_objective('pace'), queries=None,
        task_spec=lambda _: None, resources=resources)
    rows = []
    for effort in ('low', 'medium'):
        shape = cat.ShapeKey('codex', model, effort, '1.0.0', 'ordinary', '', '')
        for i in range(5):
            rows.append(dict(attempt_id=f'{effort}-{i}', verified_success=True,
                shape_key_json=json.dumps(shape.to_dict()),
                binding_json=json.dumps({'usage_pool_id': 'codex-subscription', 'relative_cost': 7}),
                usage_settle_json=json.dumps({'token_split': {
                    'fresh_input': 1000 if effort == 'low' else 1,
                    'cache_read': 0, 'cache_write': 0, 'output': 0}})))
    dispatch.pace_history_reader = lambda: rows
    return dispatch, rows


def canonical_evidence_dispatch():
    import json
    from dataclasses import replace

    dispatch, rows = cost_evidence_dispatch()
    base = dispatch.catalog.entry('codex')
    model = 'gpt-6.1-sol'
    own = replace(base, model_profiles={model: replace(base.profile(model),
                                                       canonical_model_id=model)})
    alias = 'vendor-model-alias'
    peer = replace(own, backend_id='peer', usage_pool_id='peer-pool',
        available_models=(alias,), model_profiles={alias: replace(own.profile(model),
            model_id=alias, usage_pool_id='peer-pool')},
        pair_profiles={(alias, effort): replace(pair, model_id=alias)
                       for (_, effort), pair in own.pair_profiles.items()})
    dispatch.catalog = cat.CapabilityCatalog.of([own, peer])
    observe(dispatch.resources, 'codex-subscription', weekly=80)
    observe(dispatch.resources, 'peer-pool', weekly=0)
    for row in rows:
        binding = json.loads(row['binding_json'])
        binding['pace_decision'] = {'demand': {'planner_complexity': 3}}
        row['binding_json'] = json.dumps(binding)
    return dispatch, rows


@pytest.mark.parametrize('mode', ['off', 'on', 'shadow'])
def test_canonical_quality_prior_is_shared_without_borrowing_pool_costs(mode):
    import json
    from maestro.workflows.routing import resolve

    dispatch, rows = canonical_evidence_dispatch()
    dispatch.runaway_mode = mode
    dispatch.planner_complexity_reader = lambda _: 3
    for row in rows:
        if row['attempt_id'].startswith('low-'):
            row['verified_success'] = row['attempt_id'] == 'low-0'
        shape = cat.ShapeKey(**json.loads(row['shape_key_json']))
        dispatch.statistics.record(shape, success=row['verified_success'])
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.backend_id == 'peer'
    assert result.requested_effort == 'medium'
    peer = [c['evidence'] for c in result.pace_decision.candidates if c['backend_id'] == 'peer']
    assert {e['success_rate'] for e in peer} == {0.2, 1.0}
    assert all(e['cost_observations'] == 0 and e['success_per_cost'] is None for e in peer)


def test_canonical_harnesses_keep_their_own_recorded_prices_and_token_costs():
    import json
    from maestro.workflows.routing import resolve

    dispatch, rows = canonical_evidence_dispatch()
    for row in list(rows):
        shape = json.loads(row['shape_key_json'])
        shape.update(backend_id='peer', model_id='vendor-model-alias')
        binding = json.loads(row['binding_json'])
        binding.update(usage_pool_id='peer-pool', relative_cost=0.0001)
        rows.append({**row, 'attempt_id': 'peer-' + row['attempt_id'],
            'shape_key_json': json.dumps(shape), 'binding_json': json.dumps(binding),
            'usage_settle_json': json.dumps({'token_split': {'fresh_input': 1,
                'cache_read': 0, 'cache_write': 0, 'output': 0}})})
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.backend_id == 'peer'
    by_route = {(c['backend_id'], c['effort']): c['evidence']
                for c in result.pace_decision.candidates}
    assert by_route['codex', 'low']['cost_units'] == 35000
    assert by_route['codex', 'medium']['cost_units'] == 35
    assert by_route['peer', 'low']['cost_units'] == pytest.approx(0.0005)
    assert by_route['peer', 'medium']['cost_units'] == pytest.approx(0.0005)
    assert all(e['cost_observations'] == 5 for e in by_route.values())
    # A cross-pool raw-price comparison would wrongly exclude the whole codex pool.
    assert sum(r.backend_id == 'codex' and r.reason == 'evidence_worse'
               and 'cost' in r.detail for r in result.rejected_routes) == 1


def test_statistics_quality_fallback_replays_real_counts_and_unknown_attempt_identities():
    import json
    from maestro.workflows.routing import resolve

    dispatch, rows = canonical_evidence_dispatch()
    for row in rows:
        shape = cat.ShapeKey(**json.loads(row['shape_key_json']))
        dispatch.statistics.record(shape, success=(row['attempt_id'] == 'low-0'
                                                   or row['attempt_id'].startswith('medium-')))
    dispatch.pace_history_reader = tuple
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.backend_id == 'peer' and result.requested_effort == 'medium'
    for candidate in result.pace_decision.candidates:
        evidence = candidate['evidence']
        assert evidence['success_source'] == 'verified_outcome_statistics'
        assert evidence['success_observations'] == 5
        assert evidence['success_successes'] == (1 if candidate['effort'] == 'low' else 5)
        assert evidence['success_attempt_ids'] is None
        assert evidence['success_minimum_observations'] == 5
        assert evidence['success_measured'] is True
        assert evidence['success_shapes'] == [{
            'shape': cat.ShapeKey('codex', 'gpt-6.1-sol', candidate['effort'],
                                  '1.0.0', 'ordinary', '', '').to_dict(),
            'observations': 5, 'successes': evidence['success_successes']}]
        assert evidence['observations'] == 0 and evidence['cost_observations'] == 0
        assert evidence['success_per_cost'] is None


def test_insufficient_quality_replay_keeps_own_sample_identity_and_unmet_floor():
    from maestro.workflows.routing import resolve

    dispatch, rows = cost_evidence_dispatch()
    rows[:] = [row for row in rows if not row['attempt_id'].endswith('-4')]
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    for candidate in result.pace_decision.candidates:
        evidence = candidate['evidence']
        assert evidence['success_source'] == 'own_pool_shape'
        assert evidence['success_observations'] == 4 and evidence['success_successes'] == 4
        assert evidence['success_attempt_ids'] == tuple(f"{candidate['effort']}-{i}" for i in range(4))
        assert evidence['success_minimum_observations'] == 5
        assert evidence['success_measured'] is False and evidence['success_rate'] is None
        assert evidence['cost_observations'] == 4 and evidence['success_per_cost'] is None


def test_measured_success_per_recorded_cost_excludes_worse_pair_in_its_pool():
    from maestro.workflows.routing import resolve
    dispatch, _ = cost_evidence_dispatch()
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'medium'
    assert any(r.reason == 'evidence_worse' and 'cost' in r.detail for r in result.rejected_routes)
    inputs = next(c['evidence'] for c in result.pace_decision.candidates if c['effort'] == 'low')
    assert inputs['cost_units'] == 35000
    assert inputs['successes'] == 5 and inputs['cost_observations'] == 5


@pytest.mark.parametrize('damage', [
    'incomplete', 'zero_tokens', 'negative_tokens', 'boolean_tokens', 'overflow_tokens',
    'zero_price', 'overflow_product', 'overflow_total', 'nonfinite_ratio',
])
def test_unusable_recorded_cost_keeps_quality_evidence_without_a_cost_preference(damage):
    import json
    from maestro.workflows.routing import resolve

    dispatch, rows = cost_evidence_dispatch()
    for row in rows:
        binding = json.loads(row['binding_json'])
        tokens = {'fresh_input': 1, 'cache_read': 0, 'cache_write': 0, 'output': 0}
        if damage == 'incomplete':
            del tokens['cache_write']
        elif damage == 'zero_tokens':
            tokens['fresh_input'] = 0
        elif damage == 'negative_tokens':
            tokens['output'] = -1
        elif damage == 'boolean_tokens':
            tokens['output'] = True
        elif damage == 'overflow_tokens':
            tokens['fresh_input'] = 10**400
        elif damage == 'zero_price':
            binding['relative_cost'] = 0
        elif damage == 'overflow_product':
            binding['relative_cost'] = 1e308
            tokens['fresh_input'] = 2
        elif damage == 'overflow_total':
            binding['relative_cost'] = 1e308
        elif damage == 'nonfinite_ratio':
            binding['relative_cost'] = 1e-320
        row['binding_json'] = json.dumps(binding)
        row['usage_settle_json'] = json.dumps({'token_split': tokens})
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'low'
    for candidate in result.pace_decision.candidates:
        evidence = candidate['evidence']
        assert evidence['success_rate'] == 1
        assert evidence['success_per_cost'] is None
        assert evidence['fallback']
        assert evidence['cost_observations'] == (
            5 if damage in ('overflow_total', 'nonfinite_ratio') else 0)


@pytest.mark.parametrize('damage', ['zero', 'negative', 'missing', 'nonfinite',
                                     'failed', 'unfinished', 'future', 'insufficient'])
def test_ineligible_runtime_history_retains_explicit_initial_estimate_and_coverage(damage):
    from maestro.workflows.routing import resolve

    dispatch, rows = history_dispatch()
    if damage == 'insufficient':
        rows.pop()
    else:
        for row in rows:
            if damage in ('zero', 'negative', 'missing', 'nonfinite'):
                row['wall_time_sec'] = {'zero': 0, 'negative': -1,
                                        'missing': None, 'nonfinite': float('inf')}[damage]
            elif damage == 'failed':
                row['status'] = 'failed'
            elif damage == 'unfinished':
                row['status'] = 'running'
            elif damage == 'future':
                row['started_at'] = datetime.fromtimestamp(NOW+1, timezone.utc).isoformat()
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    learned_shape = rows[0]['shape_key_json']
    import json
    shape = json.loads(learned_shape)
    estimate = next(c['estimate'] for c in result.pace_decision.candidates
                    if c['model_id'] == shape['model_id'] and c['effort'] == shape['effort'])
    assert estimate['runtime_sec'] == 1800
    assert estimate['runtime_source'] == 'configured_initial_estimate'
    assert estimate['runtime_total'] == len(rows)
    assert estimate['runtime_observations'] == (4 if damage == 'insufficient' else 0)
    assert estimate['runtime_fallback'] == 'insufficient_eligible_history'


def test_cost_tradeoff_keeps_the_measurably_better_quality_route():
    from maestro.workflows.routing import resolve

    dispatch, rows = cost_evidence_dispatch()
    # Low has perfect success at higher cost. Medium's cheap successes cannot
    # disqualify low after medium loses on meaningful measured quality.
    for row in rows:
        if row['attempt_id'].startswith('medium-'):
            row['verified_success'] = row['attempt_id'] == 'medium-0'
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'low'
    assert any(r.reason == 'evidence_worse' and 'success' in r.detail
               for r in result.rejected_routes)


@pytest.mark.parametrize('mode', ['on', 'shadow'])
def test_enabled_runaway_without_real_planner_band_leaves_evidence_unmeasured(mode):
    from maestro.workflows.routing import resolve
    dispatch, _ = cost_evidence_dispatch()
    dispatch.runaway_mode = mode
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'low'
    for candidate in result.pace_decision.candidates:
        assert candidate['evidence']['success_per_cost'] is None
        assert candidate['evidence']['fallback'] == 'planner_complexity_unavailable'
    assert result.pace_decision.demand['runaway_mode'] == mode
    assert result.pace_decision.demand['planner_complexity'] is None


@pytest.mark.parametrize('mode', ['on', 'shadow'])
def test_planner_band_evidence_uses_only_valid_matching_forecasts(mode):
    import json
    from maestro.workflows.routing import resolve

    dispatch, rows = cost_evidence_dispatch()
    dispatch.runaway_mode = mode
    dispatch.planner_complexity_reader = lambda _: 3
    for row in list(rows):
        binding = json.loads(row['binding_json'])
        binding['pace_decision'] = {'demand': {'planner_complexity': 3}}
        row['binding_json'] = json.dumps(binding)
        # None of these are a validated integer forecast of band 3. Even 3.0
        # must not borrow a band merely because Python's numeric equality agrees.
        for index, band in enumerate((3.0, '3', True, None, 0, 6, 2)):
            other = {**row, 'attempt_id': f"{row['attempt_id']}-other-{index}"}
            other['binding_json'] = json.dumps({**binding,
                'pace_decision': {'demand': {'planner_complexity': band}}})
            rows.append(other)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'medium'
    assert result.pace_decision.demand['planner_complexity'] == 3
    for candidate in result.pace_decision.candidates:
        assert candidate['evidence']['observations'] == 5
        assert candidate['evidence']['cost_observations'] == 5


@pytest.mark.parametrize('band', [True, 3.0, '3', None, 0, 6, {}, []])
def test_invalid_current_planner_band_keeps_routes_eligible(band):
    from maestro.workflows.routing import resolve

    dispatch, _ = cost_evidence_dispatch()
    dispatch.runaway_mode = 'on'
    dispatch.planner_complexity_reader = lambda _: band
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'low'
    assert result.pace_decision.demand['planner_complexity'] is None
    assert all(c['evidence']['fallback'] == 'planner_complexity_unavailable'
               for c in result.pace_decision.candidates)


def test_off_mode_uses_class_evidence_without_reading_a_forecast():
    from maestro.workflows.routing import resolve

    dispatch, _ = cost_evidence_dispatch()
    def unavailable(_):
        raise AssertionError('off must not ask for a planner forecast')
    dispatch.planner_complexity_reader = unavailable
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and result.requested_effort == 'medium'
    assert result.pace_decision.demand['planner_complexity'] is None


def test_refresh_keeps_opus_scope_separate_and_blocks_only_its_models():
    from maestro.backends.base import WindowUsage
    from maestro.usage_sources import PoolUsage
    from maestro.workflows.routing import DispatchRouting, resolve

    def read(binding):
        return PoolUsage(
            pool_id=binding.usage_pool_id, source='fixture',
            updated_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            windows={300: WindowUsage(10, NOW + 1800),
                     10080: WindowUsage(10, NOW + 3600)},
            scoped_windows={'opus': {10080: WindowUsage(100, NOW + 3600)}})

    dispatch = DispatchRouting(
        catalog=pair_catalog('claude'), policy=default_policy().with_objective('pace'),
        queries=None, task_spec=lambda _: None, usage_reader=read)
    dispatch.refresh_pace(NOW)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)

    assert result.is_bound and 'opus' not in result.model_id
    assert any(r.reason == 'quota_exhausted' and 'opus' in r.detail
               for r in result.rejected_routes)
    inputs = result.to_dict()['pace_decision']['pools']['claude-subscription']
    assert inputs['observation']['windows']['10080']['used_pct'] == 10
    assert inputs['observation']['scoped_windows']['opus']['10080']['used_pct'] == 100


def test_missing_applicable_opus_scope_waits_instead_of_inventing_headroom():
    from dataclasses import replace

    resources = cat.ResourcePoolState()
    observe(resources, 'claude-subscription')
    base = pair_catalog('claude').entry('claude')
    entry = replace(base, available_models=('claude-opus-5-5',),
                    model_profiles={'claude-opus-5-5': base.profile('claude-opus-5-5')},
                    pair_profiles={key: pair for key, pair in base.pair_profiles.items()
                                   if key[0] == 'claude-opus-5-5'})
    result = rt.resolve_agent(
        agent_node(agent_definition_ref='reviewer'), None, resources,
        cat.CapabilityCatalog.of([entry]), default_policy().with_objective('pace'), now_epoch=NOW)
    assert not result.is_bound and result.disposition == 'wait'
    assert result.reason == 'quota_unknown' and 'opus' in result.detail


@pytest.mark.parametrize('reset', [NOW - 1, NOW + 10080 * 60 + 1])
def test_unusable_applicable_scope_reset_waits(reset):
    from dataclasses import replace

    resources = cat.ResourcePoolState()
    observe(resources, 'claude-subscription')
    pool = resources.pool('claude-subscription')
    pool.observation = replace(pool.observation, scoped_windows={
        'opus': {10080: cat.QuotaWindow(10080, 10, reset)}})
    result = rt.resolve_agent(
        agent_node(), None, resources, pair_catalog('claude'),
        default_policy().with_objective('pace'), now_epoch=NOW,
        exclude=frozenset({('claude', 'claude-sonnet-5-5'), ('claude', 'claude-haiku-5-5')}))
    assert not result.is_bound and result.reason == 'quota_unknown'
    assert 'opus' in result.detail


@pytest.mark.parametrize('used', [None, float('nan'), -1, 101, True])
def test_unknown_or_invalid_applicable_scope_usage_waits(used):
    from dataclasses import replace

    resources = cat.ResourcePoolState()
    observe(resources, 'claude-subscription')
    pool = resources.pool('claude-subscription')
    pool.observation = replace(pool.observation, scoped_windows={
        'opus': {10080: cat.QuotaWindow(10080, used, NOW + 3600)}})
    result = rt.resolve_agent(
        agent_node(), None, resources, pair_catalog('claude'),
        default_policy().with_objective('pace'), now_epoch=NOW,
        exclude=frozenset({('claude', 'claude-sonnet-5-5'), ('claude', 'claude-haiku-5-5')}))
    assert not result.is_bound and result.reason == 'quota_unknown'
    assert 'opus' in result.detail


@pytest.mark.parametrize('bad_scope', [None, {'bad-duration': None}])
def test_malformed_optional_scope_keeps_account_and_valid_sibling_scope(bad_scope):
    from maestro.backends.base import WindowUsage
    from maestro.usage_sources import PoolUsage
    from maestro.workflows.routing import DispatchRouting, resolve

    sample = PoolUsage(
        pool_id='claude-subscription', source='fixture',
        updated_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        windows={300: WindowUsage(10, NOW + 1800)},
        scoped_windows={'opus': bad_scope, 'other': {10080: WindowUsage(20, NOW + 3600)}})
    dispatch = DispatchRouting(
        catalog=pair_catalog('claude'), policy=default_policy().with_objective('pace'),
        queries=None, task_spec=lambda _: None, usage_reader=lambda _: sample)
    dispatch.refresh_pace(NOW)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and 'opus' not in result.model_id
    observation = dispatch.resources.pool('claude-subscription').observation
    assert observation.five_hour.used_pct == 10
    assert observation.scoped_windows['other'][10080].used_pct == 20
    assert 'opus' not in observation.scoped_windows


@pytest.mark.parametrize('reset', ['unknown', float('nan'), True])
def test_refresh_explains_malformed_scoped_reset_and_keeps_replay_json_valid(reset):
    import json
    from maestro.backends.base import WindowUsage
    from maestro.usage_sources import PoolUsage
    from maestro.workflows.routing import DispatchRouting, resolve

    sample = PoolUsage(
        pool_id='claude-subscription', source='fixture',
        updated_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        windows={300: WindowUsage(10, NOW + 1800)},
        scoped_windows={'opus': {10080: WindowUsage(10, reset)}})
    dispatch = DispatchRouting(
        catalog=pair_catalog('claude'), policy=default_policy().with_objective('pace'),
        queries=None, task_spec=lambda _: None, usage_reader=lambda _: sample)
    dispatch.refresh_pace(NOW)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound and 'opus' not in result.model_id
    decision = result.to_dict()['pace_decision']
    json.dumps(decision, allow_nan=False)
    scoped = [c for c in decision['candidates'] if c['applicable_scopes']]
    assert scoped and all(c['five_hour'] == 'quota_unknown' for c in scoped)
    assert all('reset' in c['five_hour_detail'] for c in scoped)


@pytest.mark.parametrize('objective,admitted', [('pace', False), ('paid_efficiency', True)])
def test_pace_wait_keeps_quality_floor_even_with_explicit_step_down_fallback(objective, admitted):
    from maestro.workflows.routing import DispatchRouting, resolve
    from tests.backends.test_router import catalog_of

    resources = cat.ResourcePoolState()
    observe(resources, 'strong-pool', used=100)
    observe(resources, 'cheap-pool')
    dispatch = DispatchRouting(
        catalog=catalog_of(), policy=default_policy().with_objective(objective),
        queries=None, task_spec=lambda _: None, resources=resources,
        role_strengths={'implementer': 'strong'}, step_down_tasks={'T1'})
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW, task_id='T1')
    assert result.is_bound is admitted
    if not admitted:
        assert result.disposition == 'wait' and result.reason == 'quota_exhausted'
    else:
        assert result.backend_id == 'cheap_cli' and result.stepped_down


def test_production_graph_uses_configured_pool_burn_for_pace_admission(tmp_path, monkeypatch):
    from maestro import config, orchestrator
    from maestro.workflows.routing import resolve
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'project.yaml').write_text(
        _provider_tables(tmp_path) + 'routing:\n  pace:\n    pools:\n'
        '      codex-subscription:\n        initial_burn_pct_per_cost: 1000\n')
    dispatch = orchestrator._graph_routing()
    dispatch.policy = dispatch.policy.with_objective('pace')
    dispatch.catalog = pair_catalog('codex')
    observe(dispatch.resources, 'codex-subscription')
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert not result.is_bound and result.reason == 'quota_exhausted'
    estimates = result.to_dict()['pace_decision']['candidates']
    assert estimates and all(c['estimate']['burn_pct_per_cost'] == 1000 for c in estimates)


def test_configured_initial_runtime_accounts_only_for_work_before_reset(tmp_path, monkeypatch):
    from maestro import config, orchestrator
    from maestro.workflows.routing import resolve
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'project.yaml').write_text(
        _provider_tables(tmp_path) + 'routing:\n  pace:\n    initial_runtime_sec: 3600\n')
    dispatch = orchestrator._graph_routing()
    dispatch.policy = dispatch.policy.with_objective('pace')
    dispatch.catalog = pair_catalog('codex')
    observe(dispatch.resources, 'codex-subscription', used=91.6)
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    estimates = result.to_dict()['pace_decision']['candidates']
    assert estimates and all(c['estimate']['runtime_sec'] == 3600 for c in estimates)
    assert all(c['estimate']['runtime_source'] == 'configured_initial_estimate' for c in estimates)


@pytest.mark.parametrize('factory', ['direct', 'copy'])
def test_task_pace_override_is_applied_without_mutating_shared_pool_defaults(factory, tmp_path, monkeypatch):
    from maestro import config, orchestrator
    from maestro.workflows.routing import resolve
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/ROADMAP.md').write_text(
        '```yaml\nid: T1\nrouting:\n  pace:\n    pools:\n'
        '      codex-subscription:\n        initial_burn_pct_per_cost: 1\n```\n')
    (tmp_path / 'project.yaml').write_text(
        _provider_tables(tmp_path) + 'routing:\n  pace:\n    initial_runtime_sec: 3600\n'
        '    pools:\n      codex-subscription:\n        initial_burn_pct_per_cost: 1000\n')
    shared = orchestrator._graph_routing()
    shared.policy = shared.policy.with_objective('pace')
    shared.catalog = pair_catalog('codex')
    observe(shared.resources, 'codex-subscription')
    assert not resolve(shared, agent_node(), None, now_epoch=NOW).is_bound

    dispatch = (orchestrator._graph_routing(task_id='T1') if factory == 'direct'
                else orchestrator._graph_task_routing(shared, 'T1'))
    dispatch.policy = dispatch.policy.with_objective('pace')
    dispatch.catalog = pair_catalog('codex')
    observe(dispatch.resources, 'codex-subscription')
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    estimates = result.to_dict()['pace_decision']['candidates']
    assert all(c['estimate']['burn_pct_per_cost'] == 1 and c['estimate']['runtime_sec'] == 3600
               for c in estimates)
    assert not resolve(shared, agent_node(), None, now_epoch=NOW).is_bound


@pytest.mark.parametrize('factory', ['direct', 'copy'])
@pytest.mark.parametrize('project_mode,task_mode,expected', [
    (None, None, 'on'), ('shadow', None, 'shadow'), ('shadow', 'off', 'off'),
])
def test_graph_factories_apply_effective_runaway_mode_to_real_evidence(
        factory, project_mode, task_mode, expected, tmp_path, monkeypatch):
    from maestro import config, orchestrator
    from maestro.workflows.routing import resolve
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    monkeypatch.setattr(config, 'load_global_config', lambda: {'runaway': {'mode': 'on'}})
    task = {'id': 'T1'}
    if task_mode is not None:
        task['runaway'] = {'mode': task_mode}
    import yaml
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/ROADMAP.md').write_text('```yaml\n' + yaml.safe_dump(task) + '```\n')
    (tmp_path / 'project.yaml').write_text(_provider_tables(tmp_path) + (
        f'runaway:\n  mode: "{project_mode}"\n' if project_mode else ''))
    shared = orchestrator._graph_routing()
    dispatch = (orchestrator._graph_routing(task_id='T1') if factory == 'direct'
                else orchestrator._graph_task_routing(shared, 'T1'))
    fixture, rows = cost_evidence_dispatch()
    dispatch.catalog, dispatch.policy = fixture.catalog, fixture.policy
    dispatch.resources = fixture.resources
    dispatch.pace_history_reader = lambda: rows
    result = resolve(dispatch, agent_node(), None, now_epoch=NOW)
    assert result.is_bound
    assert result.requested_effort == ('medium' if expected == 'off' else 'low')
    assert result.pace_decision.demand['runaway_mode'] == expected
    assert shared.runaway_mode == (project_mode or 'on')


@pytest.mark.parametrize('factory', ['direct', 'copy'])
@pytest.mark.parametrize('source', ['missing', 'other-task', 'malformed-yaml', 'current'])
def test_graph_task_overrides_belong_to_current_project(factory, source, tmp_path, monkeypatch):
    from maestro import config, orchestrator
    from maestro.docs import roadmap
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'project.yaml').write_text(
        _provider_tables(tmp_path) + 'routing:\n  pace:\n    initial_runtime_sec: 3600\n')
    foreign = tmp_path / 'foreign.md'
    foreign.write_text('```yaml\nid: T1\nrunaway: {mode: "on"}\n'
                       'routing: {pace: {initial_runtime_sec: 900}}\n```\n')
    monkeypatch.setattr(roadmap, 'ROADMAP_FILE', foreign)
    local = tmp_path / 'docs/ROADMAP.md'
    local.parent.mkdir()
    if source != 'missing':
        local.write_text({
            'other-task': '```yaml\nid: T2\n```\n',
            'malformed-yaml': '```yaml\nid: [broken\n```\n',
            'current': '```yaml\nid: T1\nrunaway: {mode: "shadow"}\n'
                       'routing: {pace: {initial_runtime_sec: 7200}}\n```\n',
        }[source])
    shared = orchestrator._graph_routing()
    dispatch = (orchestrator._graph_routing(task_id='T1') if factory == 'direct'
                else orchestrator._graph_task_routing(shared, 'T1'))
    assert dispatch.pace_estimates['*'].runtime_sec == (7200 if source == 'current' else 3600)
    assert dispatch.runaway_mode == ('shadow' if source == 'current' else 'off')
    assert shared.pace_estimates['*'].runtime_sec == 3600 and shared.runaway_mode == 'off'
    if source == 'missing':
        # Optional overrides cannot make model-only recovery depend on a ROADMAP.
        # Actual task-spec use retains its required-source error.
        monkeypatch.setattr(roadmap, 'ROADMAP_FILE', local)
        with pytest.raises(FileNotFoundError):
            dispatch.task_spec('T1')


@pytest.mark.parametrize('factory', ['direct', 'copy'])
@pytest.mark.parametrize('failure', ['permission', 'invalid-pace', 'invalid-runaway'])
def test_graph_task_override_failures_are_not_silenced(factory, failure, tmp_path, monkeypatch):
    from pathlib import Path
    from maestro import config, orchestrator
    from maestro.docs import roadmap
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'project.yaml').write_text(_provider_tables(tmp_path))
    local = tmp_path / 'docs/ROADMAP.md'
    local.parent.mkdir()
    local.write_text('```yaml\nid: T1\n' + {
        'permission': '', 'invalid-pace': 'routing: {pace: {initial_runtime_sec: 0}}\n',
        'invalid-runaway': 'runaway: {mode: invalid}\n',
    }[failure] + '```\n')
    monkeypatch.setattr(roadmap, 'ROADMAP_FILE', local)
    shared = orchestrator._graph_routing()
    if failure == 'permission':
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == local:
                raise PermissionError('task source denied')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(PermissionError if failure == 'permission' else config.ConfigError):
        (orchestrator._graph_routing(task_id='T1') if factory == 'direct'
         else orchestrator._graph_task_routing(shared, 'T1'))


@pytest.mark.parametrize('factory', ['direct', 'copy'])
def test_graph_task_overrides_use_one_current_source_read(factory, tmp_path, monkeypatch):
    from pathlib import Path
    from maestro import config, orchestrator
    from maestro.docs import roadmap
    from tests.test_model_graph_routing import _provider_tables

    monkeypatch.setattr(orchestrator, 'REPO', tmp_path)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    (tmp_path / 'project.yaml').write_text(_provider_tables(tmp_path))
    local = tmp_path / 'docs/ROADMAP.md'
    monkeypatch.setattr(roadmap, 'ROADMAP_FILE', local)
    shared = orchestrator._graph_routing()
    original, reads = Path.read_text, []
    def read(path, *args, **kwargs):
        if path == local:
            reads.append(path)
            mode = 'shadow' if len(reads) == 1 else 'on'
            return '```yaml\nid: T1\nrouting: {pace: {initial_runtime_sec: 7200}}\n' + (
                f'runaway: {{mode: "{mode}"}}\n```\n')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    dispatch = (orchestrator._graph_routing(task_id='T1') if factory == 'direct'
                else orchestrator._graph_task_routing(shared, 'T1'))
    assert dispatch.pace_estimates['*'].runtime_sec == 7200
    assert dispatch.runaway_mode == 'shadow'
    assert reads == [local]


@pytest.mark.parametrize('pace', [
    {'initial_runtime_sec': 0}, {'initial_burn_pct_per_cost': float('nan')},
    False, {'pools': False}, {'pools': {'codex-subscription': []}},
    {'unexpected': 1}, {'pools': {1: {}}},
])
def test_doctor_reports_invalid_pace_estimate_configuration(pace):
    from maestro.model_policy import role_shape_issues

    assert any('routing.pace' in issue for issue in role_shape_issues({'routing': {'pace': pace}}))
