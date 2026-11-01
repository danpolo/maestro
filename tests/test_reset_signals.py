"""W3 consumes monitor state; forecasts and historical events never announce resets."""
import importlib
import importlib.util
from datetime import datetime, timezone

import pytest

NOW = 1_800_000_000
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


def source_class():
    assert importlib.util.find_spec('maestro.reset_signals'), 'W3 ResetSignalSource is missing'
    return importlib.import_module('maestro.reset_signals').ResetSignalSource


def state(**overrides):
    return {'is_healthy': True, 'last_poll_at_utc': STAMP,
            'source_used': 'codex-reset.com', 'active_signal': None,
            'banked_signal': None, 'forecast': None, **overrides}


def test_announced_banked_and_forecast_samples_stay_distinct():
    active = {'deadline': '2027-01-15T12:00:00Z', 'expected_window': 'tonight'}
    banked = {'banked_state': 'available', 'expires_at': '2027-01-16T12:00:00Z', 'credits': 1}
    forecast = {'p24': 95, 'p48': 99, 'confidence': 'high', 'is_announcement': False}
    calls = []
    def read(path):
        calls.append(path)
        return state(active_signal=active, banked_signal=banked, forecast=forecast) if path == '/status' else []
    sample = source_class()(reader=read, clock=lambda: NOW).read()
    assert sample.active_signal == active
    assert sample.banked_signal == banked
    assert sample.forecast.p24 == 95 and sample.forecast.p48 == 99
    assert sample.forecast.confidence == 'high'
    assert sample.forecast.is_announcement is False
    assert sample.observed_at == STAMP and sample.staleness_bound == 300
    assert calls == ['/status', '/events']


@pytest.mark.parametrize('bad', [None, [], {}, {'status': 'initializing'},
    state(is_healthy=False), state(is_healthy='true'), state(last_poll_at_utc=None),
    state(last_poll_at_utc='broken'),
    state(last_poll_at_utc=datetime.fromtimestamp(NOW - 301, timezone.utc).isoformat()),
    state(last_poll_at_utc=datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat()),
    state(active_signal='soon'), state(banked_signal=[]), state(forecast='high'),
    state(forecast={'p24': True}), state(forecast={'p24': float('nan')}),
    state(forecast={'p48': 101}), state(forecast={'confidence': 4}),
])
def test_absent_unhealthy_stale_or_malformed_monitor_returns_no_signal(bad):
    source = source_class()(reader=lambda path: bad if path == '/status' else [],
                            clock=lambda: NOW)
    assert source.read() is None


@pytest.mark.parametrize('endpoint', ['/status', '/events'])
def test_endpoint_failure_returns_no_signal(endpoint):
    def read(path):
        if path == endpoint:
            raise OSError('monitor unavailable')
        return state()
    assert source_class()(reader=read, clock=lambda: NOW).read() is None


def test_forecast_and_old_announcements_do_not_create_an_active_reset():
    events = [{'event_id': 'older', 'event_kind': 'announcement',
               'reset_type': 'automatic_global', 'effective_at_utc': STAMP},
              {'event_id': 'newer', 'event_kind': 'completion',
               'reset_type': 'automatic_global'}]
    source = source_class()(reader=lambda path: state(forecast={
        'p24': 99, 'p48': 100, 'confidence': 'high', 'is_announcement': True})
        if path == '/status' else events, clock=lambda: NOW)
    sample = source.read()
    assert sample.active_signal is None and sample.banked_signal is None
    assert sample.forecast.p24 == 99 and sample.forecast.is_announcement is False
    assert len(sample.events) == 2


def test_banked_lifecycle_and_expiry_remain_history_and_raw_payload_is_discarded():
    event = {'event_id': 'credit', 'event_kind': 'banked_grant', 'reset_type': 'banked',
             'banked_state': 'available', 'expected_window': 'tonight',
             'raw_data': {'expires_at': '2027-01-17T00:00:00Z', 'unrelated': 'omit'}}
    sample = source_class()(reader=lambda path: state() if path == '/status' else [event],
                            clock=lambda: NOW).read()
    assert sample.banked_signal is None
    assert sample.events[0]['banked_state'] == 'available'
    assert sample.events[0]['expires_at'] == '2027-01-17T00:00:00Z'
    assert 'raw_data' not in sample.events[0]
    assert 'unrelated' not in sample.events[0]


def test_status_freshness_boundary_is_explicit_and_not_restamped():
    old = datetime.fromtimestamp(NOW - 300, timezone.utc).isoformat()
    source = source_class()(reader=lambda path: state(last_poll_at_utc=old,
        forecast={'p24': 0, 'p48': None, 'confidence': None}) if path == '/status' else [],
        clock=lambda: NOW)
    sample = source.read()
    assert sample.observed_at == old
    assert sample.forecast.p24 == 0 and sample.forecast.p48 is None


@pytest.mark.parametrize('events', [None, {}, 'events', [4]])
def test_malformed_event_surface_is_not_a_signal(events):
    assert source_class()(reader=lambda path: state() if path == '/status' else events,
                          clock=lambda: NOW).read() is None


def test_default_reader_uses_only_local_status_and_events_gets(monkeypatch):
    from maestro import source_http
    monkeypatch.setenv('MAESTRO_RESET_MONITOR_URL', 'http://127.0.0.1:8080')
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return state(forecast={'p24': 80, 'confidence': 'high'}) if url.endswith('/status') else []
    monkeypatch.setattr(source_http, 'get_json', get)
    sample = source_class()(clock=lambda: NOW).read()
    assert sample.forecast.p24 == 80
    assert [url for url, _ in calls] == ['http://127.0.0.1:8080/status', 'http://127.0.0.1:8080/events']
    assert all(options['timeout'] == 2 for _, options in calls)
