"""Durable use-it-or-lose-it controls. Only the controller writes state."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import uuid
from typing import Mapping

from maestro.backends.catalog import RESET_JITTER_SEC
from maestro.usage_sources import epoch


CONTROL_FIELDS = ('skipped_weeks', 'stopped_weeks', 'skip_pending',
                  'skip_pending_pools', 'stopped_pools', 'last_control')


def apply_control(document, prepared, update_id):
    """Recover a claimed Telegram control with its original weekly identity.

    Later global controls supersede older global controls. A newer pool button
    protects that pool while this older global command still applies elsewhere.
    Unrelated updates and status reads do not participate in this ordering.
    """
    result = copy.deepcopy(document)
    receipts = result.setdefault('control_receipts', {})
    scope = '|'.join(prepared['scope'])
    if receipts.get(scope, -1) >= update_id:
        return result
    protected = {pool for pool, receipt in result.get('pool_control_receipts', {}).get(scope, {}).items()
                 if receipt > update_id}
    prior = copy.deepcopy(result)
    for key in CONTROL_FIELDS:
        result.pop(key, None)
    result.update(copy.deepcopy(prepared['fields']))
    for key in ('skipped_weeks', 'stopped_weeks'):
        for pool in protected:
            result.get(key, {}).pop(pool, None)
            if pool in prior.get(key, {}):
                result.setdefault(key, {})[pool] = prior[key][pool]
    for key in ('skip_pending_pools', 'stopped_pools'):
        pools = set(result.get(key, [])) - protected
        pools.update(set(prior.get(key, [])) & protected)
        if pools or key in result:
            result[key] = sorted(pools)
    receipts[scope] = update_id
    action = result['last_control']['action']
    if action in ('off', 'stop'):
        for pool, decision in result.get('decisions', {}).items():
            if ((decision.get('week') is not None and (
                    decision.get('week') == result.get('skipped_weeks', {}).get(pool)
                    or decision.get('week') == result.get('stopped_weeks', {}).get(pool)))
                    or pool in result.get('skip_pending_pools', [])
                    or pool in result.get('stopped_pools', [])):
                decision.update(active=False, reason='operator_' + action)
    return result


def _number(value) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


@dataclass(frozen=True)
class SprintConfig:
    enabled: bool = True
    window_hours: float = 24.0
    behind_margin_pct: float = 5.0
    forecast_p24: float = 70.0
    forecast_confidence: str = 'high'

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError('sprint.enabled must be a boolean')
        if not _number(self.window_hours) or not 0 < self.window_hours <= 168:
            raise ValueError('sprint.window_hours must be in (0, 168]')
        for key in ('behind_margin_pct', 'forecast_p24'):
            if not _number(getattr(self, key)) or not 0 <= getattr(self, key) <= 100:
                raise ValueError(f'sprint.{key} must be in [0, 100]')
        if self.forecast_confidence not in ('high', 'medium', 'low'):
            raise ValueError('sprint.forecast_confidence must be high, medium or low')


@dataclass(frozen=True)
class SprintDecision:
    pool_id: str
    active: bool
    reason: str
    week: float | None
    deadline: float | None
    target_pct: float | None
    inputs: Mapping
    activate_credit: bool = False

    def to_dict(self):
        return asdict(self)


def _weekly(observation, now_epoch):
    if observation is None:
        return None
    moment = datetime.fromtimestamp(now_epoch, timezone.utc)
    age = observation.age_seconds(moment)
    window = observation.weekly
    if (age is None or age < 0 or not observation.fresh_enough(moment) or window is None
            or not _number(window.used_pct) or not 0 <= window.used_pct <= 100
            or not _number(window.resets_at) or not 0 < window.resets_at-now_epoch <= 604800):
        return None
    return dict(used_pct=window.used_pct, resets_at=window.resets_at,
        observed_at=observation.observed_at, staleness_bound=observation.staleness_bound,
        pace_delta=observation.pace_delta(now_epoch))


def _signals(signals, now_epoch):
    """Consume only current validated fields. Monitor history is never a trigger."""
    if signals is None:
        return None
    observed = epoch(signals.observed_at)
    if (observed is None or not _number(signals.staleness_bound)
            or signals.staleness_bound <= 0
            or not 0 <= now_epoch-observed <= signals.staleness_bound):
        return None

    def deadline(block, keys):
        if not isinstance(block, Mapping):
            return None
        return next((value for key in keys if (value := epoch(block.get(key))) is not None), None)

    banked = signals.banked_signal
    expiry = deadline(banked, ('expires_at', 'expires_at_utc', 'expiry'))
    if (not isinstance(banked, Mapping) or banked.get('banked_state') != 'available'
            or ('credits' in banked and (not _number(banked['credits']) or banked['credits'] <= 0))):
        expiry = None
    forecast = signals.forecast
    valid_forecast = (forecast is not None and _number(forecast.p24)
                      and 0 <= forecast.p24 <= 100
                      and forecast.confidence in ('low', 'medium', 'high'))
    return dict(observed_at=signals.observed_at, staleness_bound=signals.staleness_bound,
        announced_deadline=deadline(signals.active_signal,
            ('deadline', 'effective_at', 'effective_at_utc')),
        banked_expiry=expiry,
        forecast={'p24': forecast.p24, 'confidence': forecast.confidence} if valid_forecast else None,
        forecast_is_announcement=False)


def weekly_pace(observation, *, now_epoch):
    """Unknown weekly evidence stays neutral in the pace objective."""
    weekly = _weekly(observation, now_epoch)
    return weekly['pace_delta'] if weekly else None


def pace_target(pool_id, observation, decision, *, now_epoch):
    """Read a current active weekly target without modifying quota evidence."""
    if (not isinstance(decision, Mapping) or decision.get('active') is not True
            or decision.get('pool_id') != pool_id
            or not _number(decision.get('target_pct'))
            or not 0 <= decision['target_pct'] <= 100):
        return None
    weekly = _weekly(observation, now_epoch)
    inputs = decision.get('inputs')
    if (weekly is None or not isinstance(inputs, Mapping)
            or not _number(inputs.get('now_epoch')) or inputs['now_epoch'] > now_epoch):
        return None
    week = decision.get('week')
    if week is not None and (not _number(week)
                            or abs(weekly['resets_at']-week) > RESET_JITTER_SEC):
        return None
    if decision.get('reason') != 'weekly_reset':
        signals = inputs.get('signals')
        if not isinstance(signals, Mapping) or pool_id != 'codex-subscription':
            return None
        observed = epoch(signals.get('observed_at'))
        bound = signals.get('staleness_bound')
        if (observed is None or not _number(bound) or bound <= 0
                or not 0 <= now_epoch-observed <= bound):
            return None
    if decision.get('reason') != 'forecast_reset':
        deadline = decision.get('deadline')
        if not _number(deadline) or deadline <= now_epoch:
            return None
    return float(decision['target_pct'])


def evaluate(pool_id, observation, *, config: SprintConfig, now_epoch: float,
             week: float | None = None, blocked: str | None = None,
             signals=None) -> SprintDecision:
    """A target decision only; no quota, hold or dispatch authority is created."""
    weekly = _weekly(observation, now_epoch)
    current_signals = _signals(signals, now_epoch) if pool_id == 'codex-subscription' else None
    inputs = dict(now_epoch=now_epoch, weekly=weekly, config=asdict(config), signals=current_signals)
    reason = ('config_disabled' if not config.enabled else blocked or
              ('weekly_unknown' if weekly is None else 'outside_reset_window'))
    active = False
    deadline = weekly['resets_at'] if weekly else None
    if config.enabled and blocked is None and weekly:
        if deadline-now_epoch <= config.window_hours*3600:
            active = weekly['pace_delta'] >= config.behind_margin_pct
            reason = 'weekly_reset' if active else 'not_behind_pace'
    if config.enabled and blocked is None and current_signals:
        announced = current_signals['announced_deadline']
        banked = current_signals['banked_expiry']
        forecast = current_signals['forecast']
        confidence = {'low': 0, 'medium': 1, 'high': 2}
        if announced is not None and 0 < announced-now_epoch <= config.window_hours*3600:
            active, reason, deadline = True, 'announced_reset', announced
        elif banked is not None and 0 < banked-now_epoch <= config.window_hours*3600:
            active, reason, deadline = True, 'banked_credit', banked
        elif (forecast and forecast['p24'] >= config.forecast_p24
              and confidence[forecast['confidence']] >= confidence[config.forecast_confidence]):
            # A probability never supplies a reset deadline or quota observation.
            active, reason, deadline = True, 'forecast_reset', None
    return SprintDecision(pool_id, active, reason, week, deadline,
                          100.0 if active else None, inputs, reason == 'banked_credit' and active)


def refresh(resources, *, config: SprintConfig, now_epoch: float, signals=None) -> dict:
    """Evaluate on the existing controller and persist via its atomic state writer."""
    from maestro import state
    document = state.read_state()
    sprint = copy.deepcopy(document.get('sprint', {}))
    weeks = sprint.setdefault('weeks', {})
    pending = set(sprint.get('skip_pending_pools', []))
    if sprint.get('skip_pending') and resources.pools:
        pending.update(resources.pools)
        sprint.pop('skip_pending', None)
    stopped = set(sprint.get('stopped_pools', []))
    decisions = {}
    for pool, resource in sorted(resources.pools.items()):
        weekly = _weekly(resource.observation, now_epoch)
        previous = weeks.get(pool)
        if weekly:
            current = weekly['resets_at']
            if previous is None or current-previous > RESET_JITTER_SEC:
                weeks[pool] = current
        week = weeks.get(pool)
        if pool in pending and weekly:
            sprint.setdefault('skipped_weeks', {})[pool] = week
            pending.remove(pool)
        if pool in stopped and weekly:
            sprint.setdefault('stopped_weeks', {})[pool] = week
            stopped.remove(pool)
        blocked = next((reason for field, reason in (
            ('skipped_weeks', 'skipped_this_week'), ('stopped_weeks', 'stopped_this_week'))
            if week is not None and sprint.get(field, {}).get(pool) == week), None)
        if pool in pending:
            blocked = 'skipping_next_week'
        if pool in stopped:
            blocked = 'stopped_this_week'
        decisions[pool] = evaluate(pool, resource.observation, config=config,
            now_epoch=now_epoch, week=week, blocked=blocked, signals=signals).to_dict()
        decision = decisions[pool]
        # Keep actual forecast episodes for the later pilot. Absence is unknown,
        # never a fabricated negative reset outcome.
        records = sprint.setdefault('forecast_sprints', [])
        for record in records:
            if (record['pool_id'] == pool and record['real_reset_observed'] is None
                    and weekly and record['weekly_reset_at'] is not None
                    and weekly['resets_at']-record['weekly_reset_at'] > RESET_JITTER_SEC
                    and record['started_at'] <= epoch(weekly['observed_at']) <= record['horizon_end']):
                record.update(real_reset_observed=True, reset_observed_at=weekly['observed_at'])
        prior = sprint.get('decisions', {}).get(pool, {})
        if (decision['active'] and decision['reason'] == 'forecast_reset'
                and (not prior.get('active') or prior.get('reason') != 'forecast_reset'
                     or prior.get('week') != week)):
            records.append(dict(pool_id=pool, started_at=now_epoch,
                horizon_end=now_epoch+86400, weekly_reset_at=weekly['resets_at'] if weekly else None,
                inputs=copy.deepcopy(decision['inputs']['signals']), real_reset_observed=None))
    sprint['skip_pending_pools'] = sorted(pending)
    sprint['stopped_pools'] = sorted(stopped)
    previous = document.get('sprint', {}).get('decisions', {})
    if all(_same_outcome(previous.get(pool), decision) for pool, decision in decisions.items()):
        # Keep the stored evaluation (its now_epoch is earlier, so still usable)
        # instead of appending a full state event on every poll.
        decisions = {pool: previous[pool] for pool in decisions}
    sprint['decisions'] = decisions
    if document.get('sprint') != sprint:
        document['sprint'] = sprint
        state.write_state(document)
    return sprint


def _same_outcome(stored, fresh) -> bool:
    """Equal apart from the evaluation time itself."""
    if not isinstance(stored, Mapping):
        return False
    def strip(decision):
        decision = copy.deepcopy(dict(decision))
        if isinstance(decision.get('inputs'), Mapping):
            decision['inputs'] = {k: v for k, v in decision['inputs'].items() if k != 'now_epoch'}
        return decision
    return strip(stored) == strip(fresh)


def control(document: Mapping, action: str, *, now_epoch: float) -> dict:
    """Apply a reversible operator choice to the observed week, never to config."""
    if action not in ('on', 'off', 'stop', 'status'):
        raise ValueError('Usage: /sprint on|off|stop|status')
    result = copy.deepcopy(dict(document))
    if action == 'status':
        return result
    weeks = {pool: end for pool, end in result.get('weeks', {}).items() if end > now_epoch}
    if action == 'on':
        for key in ('skipped_weeks', 'stopped_weeks', 'skip_pending',
                    'skip_pending_pools', 'stopped_pools'):
            result.pop(key, None)
    elif action == 'off':
        result['skipped_weeks'] = weeks
        result['skip_pending'] = not bool(weeks)
        result['skip_pending_pools'] = sorted(set(result.get('decisions', {})) - weeks.keys())
    else:
        # Merge: a pool stopped earlier this week is inactive now and must stay stopped.
        result['stopped_weeks'] = {**result.get('stopped_weeks', {}), **{
            pool: end for pool, end in weeks.items()
            if result.get('decisions', {}).get(pool, {}).get('active')}}
        result['stopped_pools'] = sorted(set(result.get('stopped_pools', [])) | {
            pool for pool, decision in result.get('decisions', {}).items()
            if decision.get('active') and pool not in weeks})
    if action in ('off', 'stop'):
        for decision in result.get('decisions', {}).values():
            if decision.get('active'):
                decision.update(active=False, reason='operator_' + action)
    result['last_control'] = {'action': action, 'at': now_epoch}
    return result


def status_text(document: Mapping) -> str:
    lines = ['Sprint: ' + ('skipping the next observed week' if document.get('skip_pending')
                           or document.get('skip_pending_pools')
                           else 'enabled')]
    for pool in sorted(set(document.get('weeks', {})) | set(document.get('decisions', {}))):
        end = document.get('weeks', {}).get(pool)
        decision = document.get('decisions', {}).get(pool, {})
        reason = ('skipping_next_week' if pool in document.get('skip_pending_pools', [])
                  else 'stopped_this_week' if pool in document.get('stopped_pools', [])
                  else 'skipped_this_week' if end is not None and document.get('skipped_weeks', {}).get(pool) == end
                  else 'stopped_this_week' if end is not None and document.get('stopped_weeks', {}).get(pool) == end
                  else decision.get('reason', 'waiting_for_evaluation'))
        lines.append(f'{pool}: {reason}')
    return '\n'.join(lines)


def _notice_week(decision, now_epoch):
    week = decision.get('week')
    return week if _number(week) and week > now_epoch else None


def sync_notices(repo, *, now_epoch):
    """Controller-only notice state; addressing reads the exported projection.

    Intent is durable before sending. A lost send receipt may duplicate delivery,
    but only the exact subsequently bound message can control the episode.
    """
    from maestro import state
    from maestro.hitl import shared_bot, telegram
    document = state.read_state()
    sprint = document.get('sprint', {})
    notices = sprint.setdefault('notices', {})
    active = sprint.setdefault('active_notices', {})
    bot_token, chat = shared_bot.credentials()
    if not bot_token or not chat:
        return
    for pool, decision in sprint.get('decisions', {}).items():
        previous = notices.get(active.get(pool), {})
        if not decision['active']:
            active.pop(pool, None)
            continue
        week = _notice_week(decision, now_epoch)
        identity = (week, decision['reason'])
        if (identity != (previous.get('week'), previous.get('reason'))
                or (previous.get('project_key'), previous.get('chat_id'), previous.get('bot_id'))
                    != (shared_bot.project_key(repo), str(chat), bot_token.partition(':')[0])
                or previous.get('expires_at', 0) <= now_epoch):
            token = uuid.uuid4().hex[:24]
            deadlines = [value for value in (week, decision['deadline'])
                         if value is not None and value > now_epoch]
            record = dict(pool_id=pool, week=week, reason=decision['reason'],
                deadline=decision['deadline'], started_at=now_epoch,
                expires_at=min(deadlines) if deadlines else now_epoch+86400,
                project_key=shared_bot.project_key(repo), chat_id=str(chat),
                bot_id=bot_token.partition(':')[0], message_id=None, skipped=False)
            notices[token] = record
            active[pool] = token
        token = active[pool]
        record = notices[token]
        if record['message_id'] is not None:
            continue
        document['sprint'] = sprint
        state.write_state(document)
        explanation = {
            'weekly_reset': 'Unused capacity expires at the weekly reset.',
            'announced_reset': 'An announced forced reset will wipe unused capacity.',
            'forecast_reset': 'Forecast-based sprint: a forced reset is likely (not confirmed).',
            'banked_credit': 'Banked credit expires soon. Please activate it; Maestro cannot activate credit.',
        }[record['reason']]
        message = f"Sprint started for {pool}. {explanation} Five-hour and admission limits still apply."
        mid = telegram.notify_sprint(message, token, repo=repo)
        if mid is not None:
            record['message_id'] = mid
            state.write_state(document)
    if state.read_state().get('sprint') != sprint:
        document['sprint'] = sprint
        state.write_state(document)


def skip_notice(document, token, *, now_epoch):
    """Skip exactly the delivered pool's active episode, never a later week."""
    result = copy.deepcopy(document)
    record = result.get('notices', {}).get(token)
    if not record:
        return result, 'Sprint notice not found'
    if record.get('skipped'):
        return result, ''
    pool = record['pool_id']
    decision = result.get('decisions', {}).get(pool, {})
    if (record['expires_at'] <= now_epoch or not decision.get('active')
            or result.get('active_notices', {}).get(pool) != token
            or _notice_week(decision, now_epoch) != record['week']):
        return result, 'This sprint notice is no longer current'
    if record['week'] is None:
        result['skip_pending_pools'] = sorted(set(result.get('skip_pending_pools', [])) | {pool})
    else:
        result.setdefault('skipped_weeks', {})[pool] = record['week']
    record['skipped'] = True
    decision.update(active=False, reason='skipped_this_week')
    result['active_notices'].pop(pool, None)
    return result, ''
