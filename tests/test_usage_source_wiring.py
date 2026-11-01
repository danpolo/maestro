"""W3 adapters through the production attempt and pace callbacks."""
from types import SimpleNamespace

from maestro.backends.base import Usage, WindowUsage
from maestro.workflows import routing
from maestro.usage_sources import PoolUsage, UsageSource
from maestro.workflows.policy import default_policy
from tests.backends.test_router import pair_catalog
from tests.test_routing import shipped_catalog, node, manifest
from tests.test_usage_sources import NOW, STAMP, provider


import pytest


@pytest.fixture(autouse=True)
def no_live_native_reader(monkeypatch):
    from maestro.backends import registry
    monkeypatch.setattr(registry, 'get_backend', lambda _: SimpleNamespace(usage=lambda: None))


def test_attempt_callback_prefers_bridge_and_records_freshness_and_scopes(monkeypatch):
    from maestro import usage_sources
    source = UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'claude': {
            **provider(), 'weekly_model': {'pct': 85, 'resets_at': STAMP, 'label': 'Opus'}}},
        native_reader=lambda _: None, clock=lambda: NOW, journal=lambda *args: None)
    monkeypatch.setattr(usage_sources, 'get_usage_source', lambda: source)
    binding = SimpleNamespace(backend_id='claude', usage_pool_id='claude-subscription')
    sample = routing.driver_usage(binding)
    assert sample is not None
    assert sample.source == 'quotapulse_bridge'
    document = routing.usage_to_dict(sample)
    assert document['observed_at'] == STAMP
    assert document['staleness_bound'] == 300
    assert document['pool_id'] == 'claude-subscription'
    assert document['scoped_windows']['opus']['10080']['used_pct'] == 85
    assert document['pools']['claude-subscription']['300']['used_pct'] == 20


def test_dead_bridge_still_routes_on_native_readers(monkeypatch):
    from maestro import usage_sources
    calls = []
    def killed():
        raise OSError('inert killed-bridge fixture')
    def native(driver):
        calls.append(driver)
        return Usage(windows={10080: WindowUsage(10 if driver == 'codex' else 40, NOW + 302400)},
                     updated_at=STAMP, source='native_' + driver)
    source = UsageSource(bridge_reader=killed, native_reader=native, clock=lambda: NOW,
                         journal=lambda *args: None)
    monkeypatch.setattr(usage_sources, 'get_usage_source', lambda: source)
    dispatch = routing.DispatchRouting(catalog=pair_catalog('claude', 'codex'),
        policy=default_policy(), queries=None, task_spec=lambda _: None,
        usage_reader=routing.driver_usage)
    dispatch.refresh_pace(NOW)
    chosen = routing.resolve(dispatch, node(), manifest(), now_epoch=NOW)
    assert chosen.is_bound and chosen.backend_id == 'codex'
    assert sorted(calls) == ['claude', 'codex']
    assert dispatch.resources.pool('codex-subscription').observation.observed_at == STAMP
    assert dispatch.resources.pool('codex-subscription').observation.staleness_bound == 300


def test_pace_refresh_preserves_both_agy_pools_without_activating_routes(shipped_catalog):
    calls = []
    def read(binding):
        calls.append(binding.usage_pool_id)
        windows = {10080: WindowUsage(90 if binding.usage_pool_id.endswith('3p') else 20, NOW + 302400)}
        return PoolUsage(pool_id=binding.usage_pool_id, windows=windows,
                         pools={binding.usage_pool_id: windows},
                         updated_at=STAMP, observed_at=STAMP, staleness_bound=120)
    dispatch = routing.DispatchRouting(catalog=shipped_catalog, policy=default_policy(),
        queries=None, task_spec=lambda _: None, usage_reader=read)
    dispatch.refresh_pace(NOW)
    gemini = dispatch.resources.pool('antigravity-gemini').observation
    third = dispatch.resources.pool('antigravity-3p').observation
    assert gemini is not None and third is not None
    assert gemini.pace_delta(NOW) == 30 and third.pace_delta(NOW) == -40
    assert gemini.staleness_bound == third.staleness_bound == 120
    dispatch.refresh_pace(NOW + 1)
    assert calls.count('antigravity-gemini') == calls.count('antigravity-3p') == 1


def test_recovery_reader_bypasses_bridge_and_keeps_the_fresh_native_proof(monkeypatch):
    from maestro import usage_sources
    from maestro.backends import registry
    native = Usage(updated_at=STAMP, source='claude_oauth_usage_api',
                   windows={300: WindowUsage(40, NOW + 500)})
    monkeypatch.setattr(registry, 'get_backend', lambda _: SimpleNamespace(subscription_usage=lambda: native))
    monkeypatch.setattr(usage_sources, 'get_usage_source', lambda: (_ for _ in ()).throw(
        AssertionError('bridge data cannot prove pause recovery')))
    assert routing._fresh_pause_usage('claude') is native


def test_bridge_settle_rollover_does_not_shorten_a_native_quota_pause(monkeypatch):
    import json
    from datetime import datetime, timezone
    from maestro import usage_sources
    from maestro.workflows.runner import _quota_reset_recovered
    from maestro.workflows.failures import FailureRecord, pause_for

    stamp = datetime.fromtimestamp(NOW, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    raw = {'fetched_at': stamp, 'claude': {
        'five_hour': {'pct': 0, 'resets_at': datetime.fromtimestamp(
            NOW + 18000, timezone.utc).isoformat()}}}
    source = UsageSource(bridge_reader=lambda: raw, native_reader=lambda _: None,
                         clock=lambda: NOW, journal=lambda *args: None)
    monkeypatch.setattr(usage_sources, 'get_usage_source', lambda: source)
    closing = routing.usage_to_dict(routing.driver_usage(SimpleNamespace(
        backend_id='claude', usage_pool_id='claude-subscription')))
    opening = {'windows': {'300': {'used_pct': 100, 'resets_at': NOW - 10}}}
    crossed = routing.reset_crossing(opening, closing)
    assert crossed == 1, 'bridge reset-crossing telemetry remains measurable'
    recovered = _quota_reset_recovered({
        'reset_crossing': crossed, 'usage_settle_json': json.dumps(closing)}, at=stamp)
    record = FailureRecord(kind='quota_exhausted', evidence='CLI quota refusal',
        reset_at=datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat())
    assert pause_for(record, now_epoch=NOW, prior_blocks=0, auth_pause_sec=60,
                     reset_recovered=recovered) == (NOW + 3600, True)


@pytest.mark.parametrize('source_name', ['claude_oauth_usage_api', 'codex_app_server'])
def test_fresh_native_settle_rollover_still_shortens_a_stale_cli_pause(source_name):
    import json
    from datetime import datetime, timezone
    from maestro.workflows.runner import _quota_reset_recovered
    stamp = datetime.fromtimestamp(NOW, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    closing = routing.usage_to_dict(Usage(updated_at=stamp, source=source_name,
                                         windows={300: WindowUsage(0, NOW + 18000)}))
    assert _quota_reset_recovered({'reset_crossing': 1,
        'usage_settle_json': json.dumps(closing)}, at=stamp)
