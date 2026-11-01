"""P13 W3 account quota adapters. Source time is never the reader's current time."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import math
import json
import os
from pathlib import Path
from typing import Callable, Mapping

from maestro.backends.base import Usage, WindowUsage
from maestro.backends.catalog import QuotaObservation

# provider, driver, subpool, requires split, scoped weekly, requires provider time
POOL_SPECS = {
    'claude-subscription': ('claude', 'claude', None, False, True, False),
    'codex-subscription': ('codex', 'codex', None, False, False, True),
    'antigravity-gemini': ('antigravity', 'agy', 'gemini', False, False, True),
    'antigravity-3p': ('antigravity', 'agy', '3p', True, False, True),
}


def _finite_number(value):
    """JSON integers can be too large for math's float conversion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class PoolUsage(Usage):
    """A selected pool, with independent model constraints alongside account windows."""
    pool_id: str = ''
    observed_at: str = ''
    staleness_bound: float = 300.0
    scoped_windows: Mapping[str, Mapping[int, WindowUsage]] = field(default_factory=dict)

    def observation(self, *, written_at=None):
        return QuotaObservation.from_usage(
            self, pool_id=self.pool_id, acquisition='pull',
            staleness_bound=self.staleness_bound, written_at=written_at)


class UsageSource:
    def __init__(self, *, bridge_reader: Callable | None = None,
                 native_reader: Callable | None = None,
                 clock: Callable = time.time, journal: Callable | None = None,
                 staleness_bound: float = 300.0):
        self.bridge_reader = bridge_reader or self._configured_bridge
        self.native_reader = native_reader or native_account_usage
        self.clock = clock
        self.journal = journal or _journal
        self.staleness_bound = staleness_bound
        if (isinstance(staleness_bound, bool) or not isinstance(staleness_bound, (int, float))
                or not math.isfinite(staleness_bound) or staleness_bound <= 0):
            raise ValueError('staleness_bound must be finite and positive')
        self._sources = {}
        self._bridge_cache = None
        self._bridge_cached_at = None
        self._bridge_config = None

    def _configured_bridge(self):
        from maestro.source_http import get_json

        url = os.environ.get('MAESTRO_QUOTAPULSE_URL', 'http://127.0.0.1:8787').rstrip('/')
        token = os.environ.get('USAGE_BRIDGE_TOKEN', '')
        token_file = os.environ.get('MAESTRO_QUOTAPULSE_TOKEN_FILE', '')
        if not token and token_file:
            # Explicit runtime configuration only. Never discover/read CLI credentials.
            with Path(token_file).open(encoding='utf-8') as stream:
                token = stream.read(4096).strip()
        if not token:
            return None
        config = (url, token)
        at = self.clock()
        if (self._bridge_config == config and self._bridge_cached_at is not None
                and 0 <= at - self._bridge_cached_at < 60):
            return self._bridge_cache
        body = get_json(url + '/usage', bearer_token=token, timeout=2)
        self._bridge_config, self._bridge_cache, self._bridge_cached_at = config, body, at
        return body

    def _fresh(self, stamp):
        observed = epoch(stamp)
        return observed is not None and 0 <= self.clock() - observed <= self.staleness_bound

    def _transition(self, pool_id, current):
        previous = self._sources.get(pool_id)
        if previous == current:
            return
        self._sources[pool_id] = current
        try:
            self.journal('usage_source_changed', json.dumps({
                'pool_id': pool_id, 'previous': previous, 'current': current}, sort_keys=True))
        except Exception:
            pass  # quota telemetry cannot stop dispatch when the journal is unavailable

    @staticmethod
    def _number(value):
        return _finite_number(value) and 0 <= value <= 100

    def _bridge_windows(self, raw):
        if not isinstance(raw, Mapping):
            return None
        windows = {}
        for name, minutes in (('five_hour', 300), ('weekly', 10080)):
            window = raw.get(name)
            if window is None:
                continue
            if not isinstance(window, Mapping):
                return None
            used = window.get('pct')
            if used is not None and not self._number(used):
                return None
            duration = window.get('window_minutes', minutes)
            if duration is None:
                duration = minutes
            if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
                return None
            reset = window.get('resets_at')
            parsed = epoch(reset) if reset is not None else None
            if reset is not None and parsed is None:
                return None
            if duration in windows:
                return None
            windows[duration] = WindowUsage(used, int(parsed) if parsed is not None else None)
        return windows if any(w.used_pct is not None for w in windows.values()) else None

    def _bridge(self, body, pool_id):
        spec = POOL_SPECS.get(pool_id)
        if spec is None or not isinstance(body, Mapping) or not self._fresh(body.get('fetched_at')):
            return None
        provider, _, subpool, requires_split, has_scoped_weekly, requires_provider_time = spec
        for flags in ('stale', 'errors'):
            flagged = body.get(flags) or {}
            if not isinstance(flagged, Mapping) or provider in flagged:
                return None
        raw = body.get(provider)
        if not isinstance(raw, Mapping) or raw.get('stale'):
            return None
        # Aggregate refreshes can re-serve Codex lockout caches or agy dumps.
        # Their measurement time must be explicit; a new envelope is not proof.
        observed_at = raw.get('observed_at')
        if observed_at is None and not requires_provider_time:
            observed_at = body['fetched_at']
        if not self._fresh(observed_at):
            return None
        selected = raw
        if subpool is not None:
            split = raw.get('model_pools') or {}
            if not isinstance(split, Mapping):
                return None
            selected = split.get(subpool)
            # Old Gemini-only bridges can still measure Gemini; never infer 3P from it.
            if selected is None and not requires_split:
                selected = raw
        windows = self._bridge_windows(selected)
        if windows is None:
            return None
        scoped = {}
        if has_scoped_weekly:
            model = raw.get('weekly_model')
            if model is None:
                split = raw.get('model_pools') or {}
                if isinstance(split, Mapping) and isinstance(split.get('scoped'), Mapping):
                    model = split['scoped'].get('weekly')
            if model is not None:
                if not isinstance(model, Mapping):
                    return None
                label = str(model.get('label') or '')
                if 'opus' in label.lower():
                    constraint = self._bridge_windows({'weekly': model})
                    if constraint is None:
                        return None
                    scoped['opus'] = constraint
        return PoolUsage(windows=windows, pools={pool_id: windows}, pool_id=pool_id,
                         updated_at=observed_at, observed_at=observed_at,
                         staleness_bound=self.staleness_bound, source='quotapulse_bridge',
                         scoped_windows=scoped)

    def _native_windows(self, windows):
        """Account and scoped constraints share the same measurement contract."""
        if not isinstance(windows, Mapping) or not windows:
            return None
        for minutes, window in windows.items():
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
                return None
            if not isinstance(window, WindowUsage):
                return None
            if window.used_pct is not None and not self._number(window.used_pct):
                return None
            if window.resets_at is not None and (
                    not _finite_number(window.resets_at) or window.resets_at < 0):
                return None
        if not any(w.used_pct is not None for w in windows.values()):
            return None
        return dict(windows)

    def _native(self, pool_id, driver_name):
        usage = self.native_reader(driver_name)
        if usage is None or getattr(usage, 'error', None) or not self._fresh(usage.updated_at):
            return None
        windows = usage.windows
        if getattr(usage, 'pools', None):
            windows = usage.pools.get(pool_id)
        elif POOL_SPECS.get(pool_id, (None, None, None, False, False))[3]:
            return None
        windows = self._native_windows(windows)
        if windows is None:
            return None
        scoped = {}
        raw_scoped = getattr(usage, 'scoped_windows', {})
        if isinstance(raw_scoped, Mapping):
            for scope, raw_windows in raw_scoped.items():
                if not isinstance(scope, str) or not scope:
                    continue
                constraint = self._native_windows(raw_windows)
                if constraint is not None:
                    scoped[scope] = constraint
        return PoolUsage(windows=dict(windows), pools={pool_id: dict(windows)}, pool_id=pool_id,
                         updated_at=usage.updated_at, observed_at=usage.updated_at,
                         staleness_bound=self.staleness_bound,
                         source=usage.source or 'native_' + str(driver_name),
                         scoped_windows=scoped)

    def read(self, pool_id: str, *, driver_name=None):
        driver_name = driver_name or POOL_SPECS.get(pool_id, (None, None))[1]
        try:
            sample = self._bridge(self.bridge_reader(), pool_id)
        except Exception:
            sample = None
        selected = 'bridge'
        if sample is None:
            selected = 'native'
            try:
                sample = self._native(pool_id, driver_name) if driver_name else None
            except Exception:
                sample = None
        self._transition(pool_id, selected if sample is not None else 'unavailable')
        return sample


def native_account_usage(driver_name):
    """Existing account reader, called without a handle; used directly for recovery too."""
    from maestro.backends import registry

    driver = registry.get_backend(driver_name)
    reader = getattr(driver, 'subscription_usage', None) or getattr(driver, 'usage', None)
    return None if reader is None else reader()


def _journal(event, detail):
    from maestro.state import append_journal
    append_journal(event, detail)


_production_source = UsageSource()


def get_usage_source():
    """One source-selection ledger/cache per controller process; no read at import."""
    return _production_source
