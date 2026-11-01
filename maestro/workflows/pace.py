"""W5's replayable quota estimates and the five-hour gate.

Costs stay in their own account pool. Estimates are explicitly labelled, never
written into W2's measured consumption columns.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from typing import Literal, Mapping

from maestro.backends.catalog import Admission, QuotaObservation


def positive_finite(value: float) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and value > 0)
    except OverflowError:
        return False


@dataclass(frozen=True)
class PaceEstimate:
    """Estimated percentage burn per cost unit and a positive runtime shape."""

    burn_pct_per_cost: float = 1.0
    runtime_sec: float = 1800.0
    burn_source: str = 'configured_initial_estimate'
    runtime_source: str = 'configured_initial_estimate'
    burn_observations: int = 0
    runtime_observations: int = 0
    runtime_total: int = 0
    runtime_attempt_ids: tuple[str, ...] = ()
    runtime_fallback: str | None = 'history_unavailable'
    burn_total: int = 0
    burn_fallback: str | None = 'history_unavailable'
    burn_inputs: tuple[Mapping, ...] = ()
    burn_exclusions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not positive_finite(self.burn_pct_per_cost) or not positive_finite(self.runtime_sec):
            raise ValueError('pace burn and expected runtime must be positive and finite')

    def charge_pct(self, relative_cost: float, reset_at: float, now_epoch: float) -> float:
        if not positive_finite(relative_cost):
            raise ValueError('relative_cost must be positive and finite')
        if not math.isfinite(reset_at) or not math.isfinite(now_epoch):
            raise ValueError('reset and clock must be finite')
        overlap = min(max(reset_at - now_epoch, 0.0) / self.runtime_sec, 1.0)
        return relative_cost * self.burn_pct_per_cost * overlap

    def to_dict(self) -> dict:
        return asdict(self)


def _document(value) -> dict:
    try:
        result = json.loads(value or 'null')
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _epoch(value) -> float | None:
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def estimate_from_history(rows, *, shape, pool_id: str, initial: PaceEstimate,
                          now_epoch: float, minimum_observations: int = 5) -> PaceEstimate:
    """Learn completed runtimes from the exact A07 shape, without repricing history."""
    expected = shape.to_dict()
    matching = []
    measured = {}
    for row in rows:
        recorded = _document(row.get('shape_key_json'))
        fragments = recorded.get('active_fragment_set', ())
        if (not isinstance(fragments, (list, tuple))
                or any(not isinstance(item, str) for item in fragments)):
            continue
        recorded['active_fragment_set'] = sorted(set(fragments))
        if recorded != expected or _document(row.get('binding_json')).get('usage_pool_id') != pool_id:
            continue
        matching.append(row)
        runtime = row.get('wall_time_sec')
        started = _epoch(row.get('started_at'))
        attempt_id = row.get('attempt_id')
        if (row.get('status') == 'succeeded' and positive_finite(runtime)
                and started is not None and started + runtime <= now_epoch
                and isinstance(attempt_id, str) and attempt_id):
            measured[attempt_id] = runtime
    learned = len(measured) >= minimum_observations
    estimate = replace(initial,
        runtime_sec=math.fsum(value / len(measured) for value in measured.values())
                    if learned else initial.runtime_sec,
        runtime_source='historical_shape_runtime' if learned else initial.runtime_source,
        runtime_observations=len(measured), runtime_total=len(matching),
        runtime_attempt_ids=tuple(sorted(measured)),
        runtime_fallback=None if learned else ('insufficient_eligible_history' if matching
                                                else 'history_unavailable'))
    return _pool_burn(rows, pool_id=pool_id, initial=estimate, now_epoch=now_epoch,
                      minimum_observations=minimum_observations)


def _burn_sample(sample, pool_id, now_epoch):
    """Validate a historical sample at its recorded read time, never at today's clock."""
    observed = _epoch(sample.get('observed_at') or sample.get('updated_at'))
    read = _epoch(sample.get('read_at'))
    bound = sample.get('staleness_bound')
    if (observed is None or read is None or read > now_epoch or not positive_finite(bound)
            or not 0 <= read-observed <= bound or sample.get('error')
            or sample.get('pool_id') != pool_id
            or not isinstance(sample.get('source'), str) or not sample['source'].strip()):
        return None
    pools = sample.get('pools')
    windows = pools.get(pool_id) if isinstance(pools, dict) else sample.get('windows')
    window = windows.get('300') if isinstance(windows, dict) else None
    if not isinstance(window, dict):
        return None
    used, reset = window.get('used_pct'), window.get('resets_at')
    if (isinstance(used, bool) or not isinstance(used, (int, float))
            or not 0 <= used <= 100 or not math.isfinite(used)
            or not positive_finite(reset) or not 0 < reset-observed <= 18000):
        return None
    return dict(observed=observed, read=read, used=used, reset=reset,
                bound=bound, source=sample['source'])


def _pool_burn(rows, *, pool_id, initial, now_epoch, minimum_observations):
    pool_rows = [row for row in rows
                 if _document(row.get('binding_json')).get('usage_pool_id') == pool_id]
    inputs, excluded = [], {}
    intervals = []
    for row in rows:
        binding = _document(row.get('binding_json'))
        if binding.get('usage_pool_id') not in (None, pool_id):
            continue
        started = _epoch(row.get('started_at'))
        runtime = row.get('wall_time_sec')
        finish = (started + runtime if started is not None and positive_finite(runtime)
                  else now_epoch if row.get('status') in ('claimed', 'running', 'uncertain') else None)
        intervals.append((row.get('attempt_id'), started, finish))
    for row in pool_rows:
        aid = row.get('attempt_id')
        price = _document(row.get('binding_json')).get('relative_cost')
        start = _burn_sample(_document(row.get('usage_start_json')), pool_id, now_epoch)
        end = _burn_sample(_document(row.get('usage_settle_json')), pool_id, now_epoch)
        started = _epoch(row.get('started_at'))
        runtime = row.get('wall_time_sec')
        reason = None
        if not positive_finite(price):
            reason = 'recorded_price_unavailable'
        elif row.get('status') != 'succeeded' or not start or not end:
            reason = 'samples_unavailable'
        elif (row.get('reset_crossing') or abs(start['reset']-end['reset']) > 600
              or end['read'] >= min(start['reset'], end['reset'])):
            reason = 'reset_crossing'
        elif start['source'] != end['source']:
            reason = 'source_changed'
        elif not start['observed'] < end['observed'] or not start['read'] < end['read']:
            reason = 'invalid_observation_interval'
        elif (started is None or not positive_finite(runtime)
              or abs(start['read']-started) > start['bound']
              or abs(end['read']-(started+runtime)) > end['bound']):
            reason = 'attempt_interval_mismatch'
        elif end['used'] <= start['used']:
            reason = 'nonpositive_counter_change'
        else:
            begin, finish = min(start['observed'], started), max(end['read'], started+runtime)
            if any(other != aid and (b is None or f is None or b < finish and begin < f)
                   for other, b, f in intervals):
                reason = 'overlapping_or_unknown_pool_activity'
        if reason:
            excluded[str(aid)] = reason
            continue
        ratio = (end['used'] - start['used']) / price
        if not positive_finite(ratio):
            excluded[str(aid)] = 'nonfinite_burn'
            continue
        inputs.append(dict(attempt_id=aid, pool_id=pool_id, recorded_relative_cost=price,
            observed_account_change_pct=end['used']-start['used'], source=start['source'],
            observed_start=start['observed'], observed_end=end['observed'],
            reset_at=start['reset'], estimated_burn_pct_per_cost=ratio,
            attributable_to_attempt=False))
    learned = len(inputs) >= minimum_observations
    return replace(initial,
        burn_pct_per_cost=math.fsum(item['estimated_burn_pct_per_cost']/len(inputs)
                                   for item in inputs) if learned else initial.burn_pct_per_cost,
        burn_source='estimated_nonoverlapping_pool_burn' if learned else initial.burn_source,
        burn_observations=len(inputs), burn_total=len(pool_rows), burn_inputs=tuple(inputs),
        burn_exclusions=excluded,
        burn_fallback=None if learned else ('insufficient_eligible_history' if pool_rows
                                            else 'history_unavailable'))


def class_evidence(rows, *, shape, pool_id, minimum_observations=5,
                   runaway_mode='off', planner_complexity=None, equivalent_shapes=()) -> dict:
    """Verified outcomes and frozen W2 token cost, compared only within this pool."""
    matching = {}
    prior = {}
    expected = shape.to_dict()
    equivalents = [key.to_dict() for key in equivalent_shapes] or [expected]
    band_required = runaway_mode != 'off'
    band_available = (not isinstance(planner_complexity, bool)
                      and isinstance(planner_complexity, int) and 1 <= planner_complexity <= 5)
    for row in rows:
        binding = _document(row.get('binding_json'))
        decision = binding.get('pace_decision')
        demand = decision.get('demand', {}) if isinstance(decision, dict) else {}
        prior_band = demand.get('planner_complexity') if isinstance(demand, dict) else None
        if band_required and (not band_available or prior_band != planner_complexity
                              or isinstance(prior_band, bool) or not isinstance(prior_band, int)):
            continue
        if (not isinstance(row.get('verified_success'), bool)
                or not isinstance(row.get('attempt_id'), str) or not row['attempt_id']):
            continue
        recorded = _document(row.get('shape_key_json'))
        if recorded in equivalents:
            prior[row['attempt_id']] = row
        if recorded == expected and binding.get('usage_pool_id') == pool_id:
            matching[row['attempt_id']] = row
    costs, cost_successes = [], 0
    for row in matching.values():
        price = _document(row.get('binding_json')).get('relative_cost')
        tokens = _document(row.get('usage_settle_json')).get('token_split')
        if not positive_finite(price) or not isinstance(tokens, dict):
            continue
        fields = [tokens.get(name) for name in ('fresh_input', 'cache_read', 'cache_write', 'output')]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in fields):
            continue
        count = sum(fields)
        if not positive_finite(count) or not positive_finite(price*count):
            continue
        costs.append(price*count)
        cost_successes += int(row['verified_success'])
    # Avoid a finite-input overflow without claiming a partial cost sum as complete.
    average = math.fsum(value/len(costs) for value in costs) if costs else None
    total = average*len(costs) if average is not None else None
    total = total if positive_finite(total) else None
    ratio = (cost_successes/total if total is not None
             and len(costs) >= minimum_observations else None)
    nonfinite_ratio = ratio is not None and not math.isfinite(ratio)
    if nonfinite_ratio:
        ratio = None
    successes = sum(int(row['verified_success']) for row in matching.values())
    quality = matching if len(matching) >= minimum_observations else prior
    quality_successes = sum(int(row['verified_success']) for row in quality.values())
    quality_shapes = {}
    for row in quality.values():
        recorded = _document(row.get('shape_key_json'))
        key = json.dumps(recorded, sort_keys=True)
        item = quality_shapes.setdefault(key, dict(shape=recorded, observations=0, successes=0))
        item['observations'] += 1
        item['successes'] += int(row['verified_success'])
    return dict(observations=len(matching), successes=successes,
        success_rate=quality_successes/len(quality) if len(quality) >= minimum_observations else None,
        success_observations=len(quality), success_attempt_ids=tuple(sorted(quality)),
        success_successes=quality_successes,
        success_minimum_observations=minimum_observations,
        success_measured=len(quality) >= minimum_observations,
        success_shapes=[item for _, item in sorted(quality_shapes.items())],
        success_source=('unavailable' if not quality else 'own_pool_shape'
                        if quality.keys() == matching.keys() else 'canonical_equivalent_shapes'),
        cost_observations=len(costs), cost_successes=cost_successes, cost_units=total,
        success_per_cost=ratio,
        pool_id=pool_id, attempt_ids=tuple(sorted(matching)),
        cost_source='recorded_relative_cost_times_complete_tokens',
        fallback=('planner_complexity_unavailable' if band_required and not band_available
                  else 'nonfinite_success_per_cost' if nonfinite_ratio
                  else None if len(costs) >= minimum_observations and total is not None
                  else 'insufficient_recorded_cost_evidence'))


def _journal_snapshot(value):
    """Keep invalid numeric inputs explicit without emitting nonstandard JSON.

    This only encodes evidence. It never substitutes a usable quota measurement
    or changes the observation that admission actually consumed.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return {'unavailable_number': repr(value)}
    if isinstance(value, dict):
        return {key: _journal_snapshot(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return type(value)(_journal_snapshot(item) for item in value)
    return value


@dataclass(frozen=True)
class PaceStage:
    """One actually reached stage; pending budget is never proof of admission."""

    name: Literal['eligibility', 'soft_independence', 'five_hour', 'evidence', 'weekly_pace', 'budget']
    status: Literal['passed', 'wait', 'refused', 'blocked', 'skipped', 'pending']
    inputs: Mapping = field(default_factory=dict)
    result: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class PaceDecision:
    """Frozen inputs and stage results carried by bindings and waits."""

    now_epoch: float
    node_id: str
    demand: Mapping
    pools: Mapping
    candidates: tuple[Mapping, ...] = ()
    selected: Mapping | None = None
    stages: tuple[PaceStage, ...] = ()
    rejected_routes: tuple[Mapping, ...] = ()
    version: int = 2

    def with_budget(self, inputs: Mapping, result: Mapping) -> PaceDecision:
        stage = PaceStage('budget', 'passed' if result['admitted'] else 'refused', inputs, result)
        return replace(self, stages=(*self.stages[:-1], stage))

    def to_dict(self) -> dict:
        return _journal_snapshot(asdict(self))


def five_hour_gate(observation: QuotaObservation | None, *, now_epoch: float,
                   relative_cost: float, estimate: PaceEstimate,
                   hard_limit_pct: float = 92.0, scopes: tuple[str, ...] = ()) -> Admission:
    """Headroom must be a current measurement and fit projected pre-reset burn.

    Expired windows do not turn into empty windows, and a future observation is
    never evidence of headroom. Additional model-scoped windows remain separate
    constraints; their usage is never folded into the account's 300-minute cost.
    """
    if not positive_finite(hard_limit_pct) or hard_limit_pct > 100:
        raise ValueError('hard_limit_pct must be in (0, 100]')
    if observation is None:
        return Admission(False, 'quota_unknown', 'five-hour observation unavailable')
    moment = datetime.fromtimestamp(now_epoch, timezone.utc)
    age = observation.age_seconds(moment)
    window = observation.five_hour
    if (age is None or age < 0 or not observation.fresh_enough(moment)
            or window is None or window.used_pct is None
            or isinstance(window.used_pct, bool) or not math.isfinite(window.used_pct)
            or not 0 <= window.used_pct <= 100 or window.resets_at is None
            or not math.isfinite(window.resets_at)
            or not 0 < window.resets_at - now_epoch <= 5 * 3600):
        return Admission(False, 'quota_unknown',
                         'five-hour headroom unavailable, stale, expired or future')
    for minutes, constraint in observation.windows.items():
        if minutes == 300:
            continue
        if (constraint.used_pct is not None and math.isfinite(constraint.used_pct)
                and constraint.used_pct >= hard_limit_pct):
            return Admission(False, 'quota_exhausted', f'{minutes}-minute account window full',
                             max(0.0, (constraint.resets_at or now_epoch) - now_epoch))
    charge = estimate.charge_pct(relative_cost, window.resets_at, now_epoch)
    if window.used_pct >= hard_limit_pct or window.used_pct + charge > hard_limit_pct:
        return Admission(False, 'quota_exhausted',
                         f'five-hour used={window.used_pct:g} + estimated={charge:g} '
                         f'exceeds hard limit={hard_limit_pct:g}',
                         window.resets_at - now_epoch)
    for scope in scopes:
        constraints = observation.scoped_windows.get(scope)
        if not constraints:
            return Admission(False, 'quota_unknown', f'{scope} scoped quota unavailable')
        for minutes, constraint in constraints.items():
            if (constraint.resets_at is None
                    or not math.isfinite(constraint.resets_at)
                    or not 0 < constraint.resets_at - now_epoch <= minutes * 60):
                return Admission(False, 'quota_unknown', f'{scope} scoped quota reset unusable')
            if (isinstance(constraint.used_pct, bool)
                    or not isinstance(constraint.used_pct, (int, float))
                    or not math.isfinite(constraint.used_pct)
                    or not 0 <= constraint.used_pct <= 100):
                return Admission(False, 'quota_unknown', f'{scope} scoped quota usage unusable')
            if constraint.used_pct >= hard_limit_pct:
                return Admission(False, 'quota_exhausted',
                                 f'{scope} {minutes}-minute scoped window full',
                                 constraint.resets_at - now_epoch)
    return Admission(True)
