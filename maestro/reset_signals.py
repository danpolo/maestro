"""Read-only codex-reset-monitor state. Forecast odds never become announcements."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import math
import os
import time
from typing import Mapping

from maestro.usage_sources import epoch


@dataclass(frozen=True)
class ResetForecast:
    p24: float | None = None
    p48: float | None = None
    confidence: str | None = None
    is_announcement: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ResetSignals:
    observed_at: str
    staleness_bound: float
    active_signal: Mapping | None = None
    banked_signal: Mapping | None = None
    forecast: ResetForecast | None = None
    events: tuple[Mapping, ...] = ()


class ResetSignalSource:
    def __init__(self, *, reader=None, clock=time.time, staleness_bound=300.0):
        self.reader = reader or self._get
        self.clock = clock
        self.staleness_bound = staleness_bound
        if (isinstance(staleness_bound, bool) or not isinstance(staleness_bound, (int, float))
                or not math.isfinite(staleness_bound) or staleness_bound <= 0):
            raise ValueError('staleness_bound must be finite and positive')

    @staticmethod
    def _get(path):
        from maestro.source_http import get_json
        base = os.environ.get('MAESTRO_RESET_MONITOR_URL', 'http://127.0.0.1:8080').rstrip('/')
        return get_json(base + path, timeout=2)

    def read(self):
        try:
            state = self.reader('/status')
            if not isinstance(state, Mapping) or state.get('is_healthy') is not True:
                return None
            observed = epoch(state.get('last_poll_at_utc'))
            if observed is None or not 0 <= self.clock() - observed <= self.staleness_bound:
                return None
            active, banked = state.get('active_signal'), state.get('banked_signal')
            if any(value is not None and not isinstance(value, Mapping) for value in (active, banked)):
                return None
            raw = state.get('forecast')
            forecast = None
            if raw is not None:
                if not isinstance(raw, Mapping):
                    return None
                for key in ('p24', 'p48'):
                    value = raw.get(key)
                    if value is not None and (isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(value) or not 0 <= value <= 100):
                        return None
                confidence = raw.get('confidence')
                if confidence is not None and not isinstance(confidence, str):
                    return None
                forecast = ResetForecast(raw.get('p24'), raw.get('p48'), confidence)
            events = self.reader('/events')
            if not isinstance(events, list) or any(not isinstance(event, Mapping) for event in events):
                return None
            history = []
            for event in events:
                # Historical events never override current state. Keep lifecycle/timing
                # evidence, including expiry fields the monitor carries in raw_data.
                cleaned = {k: copy.deepcopy(v) for k, v in event.items() if k != 'raw_data'}
                raw_event = event.get('raw_data')
                if isinstance(raw_event, Mapping):
                    for key in ('expires_at', 'expires_at_utc', 'expiry', 'effective_at', 'official_window'):
                        if key in raw_event and key not in cleaned:
                            cleaned[key] = copy.deepcopy(raw_event[key])
                history.append(cleaned)
            return ResetSignals(state['last_poll_at_utc'], self.staleness_bound,
                                copy.deepcopy(active), copy.deepcopy(banked),
                                forecast, tuple(history))
        except Exception:
            return None  # missing/broken monitor never fails dispatch
