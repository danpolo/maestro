"""P13 W3: quota sources must preserve pressure, provenance and pool identity."""
from datetime import datetime, timezone
import importlib
import importlib.util
import copy

import pytest

from maestro.backends.base import Usage, WindowUsage

NOW = 1_800_000_000
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
RESET = datetime.fromtimestamp(NOW + 302400, timezone.utc).isoformat()


def sources():
    assert importlib.util.find_spec('maestro.usage_sources'), 'W3 UsageSource is missing'
    return importlib.import_module('maestro.usage_sources')


def provider(five=20, weekly=30):
    return {'observed_at': STAMP, 'five_hour': {'pct': five, 'resets_at': RESET},
            'weekly': {'pct': weekly, 'resets_at': RESET}}


def test_bridge_first_preserves_observation_and_pace_without_native_read():
    native_calls = []
    module = sources()
    source = module.UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'claude': provider()},
        native_reader=lambda driver: native_calls.append(driver),
        clock=lambda: NOW, journal=lambda *args: None,
    )
    sample = source.read('claude-subscription')
    assert sample.source == 'quotapulse_bridge'
    assert sample.windows[300].used_pct == 20
    assert sample.windows[10080].used_pct == 30
    assert sample.observed_at == sample.updated_at == STAMP
    assert sample.staleness_bound == 300
    assert sample.observation().pace_delta(NOW) == 20
    assert native_calls == []


@pytest.mark.parametrize('broken', [
    None, {}, {'fetched_at': STAMP},
    {'fetched_at': 'unknown', 'claude': provider()},
    {'fetched_at': datetime.fromtimestamp(NOW - 301, timezone.utc).isoformat(),
     'claude': provider()},
    {'fetched_at': datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat(),
     'claude': provider()},
    {'fetched_at': STAMP, 'claude': provider(), 'stale': {'claude': 'bad'}},
    {'fetched_at': STAMP, 'claude': provider(), 'errors': {'claude': 'bad'}},
    {'fetched_at': STAMP, 'claude': {**provider(), 'stale': True}},
    {'fetched_at': STAMP, 'claude': {'five_hour': [], 'weekly': {}}},
    {'fetched_at': STAMP, 'claude': provider(five=True)},
    {'fetched_at': STAMP, 'claude': provider(five=float('nan'))},
    {'fetched_at': STAMP, 'claude': provider(five=101)},
    {'fetched_at': STAMP, 'claude': provider(five=-1)},
])
def test_missing_malformed_or_stale_bridge_falls_back_to_native(broken):
    module = sources()
    calls = []
    native = Usage(windows={300: WindowUsage(44, NOW + 3600)},
                   updated_at=STAMP, source='claude_oauth_usage_api')
    source = module.UsageSource(
        bridge_reader=lambda: broken,
        native_reader=lambda driver: (calls.append(driver) or native),
        clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read('claude-subscription')
    assert sample.source == 'claude_oauth_usage_api'
    assert sample.windows[300].used_pct == 44
    assert sample.observed_at == STAMP
    assert sample.staleness_bound == 300
    assert calls == ['claude']


def test_bridge_failure_switches_and_recovery_switches_are_journalled_once():
    module = sources()
    state = {'down': False}
    records = []
    def bridge():
        if state['down']:
            raise OSError('a sensitive upstream error must not be journalled')
        return {'fetched_at': STAMP, 'claude': provider()}
    source = module.UsageSource(
        bridge_reader=bridge,
        native_reader=lambda _: Usage(windows={300: WindowUsage(50, NOW + 30)},
                                      updated_at=STAMP, source='claude_oauth_usage_api'),
        clock=lambda: NOW, journal=lambda *args: records.append(args))
    assert source.read('claude-subscription').source == 'quotapulse_bridge'
    assert source.read('claude-subscription').source == 'quotapulse_bridge'
    state['down'] = True
    assert source.read('claude-subscription').source == 'claude_oauth_usage_api'
    assert source.read('claude-subscription').source == 'claude_oauth_usage_api'
    state['down'] = False
    assert source.read('claude-subscription').source == 'quotapulse_bridge'
    assert len(records) == 3
    assert all(record[0] == 'usage_source_changed' for record in records)
    assert 'native' in records[1][1] and 'bridge' in records[2][1]
    assert 'sensitive' not in str(records)


@pytest.mark.parametrize('native', [None, Usage(),
    Usage(updated_at=STAMP, windows={300: WindowUsage(True)}),
    Usage(updated_at=STAMP, error='failed', windows={300: WindowUsage(0)}),
    Usage(updated_at=datetime.fromtimestamp(NOW - 301, timezone.utc).isoformat(),
          windows={300: WindowUsage(0)}),
])
def test_no_fresh_native_sample_means_unknown_not_zero(native):
    source = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: native, clock=lambda: NOW, journal=lambda *args: None)
    assert source.read('codex-subscription') is None


def test_reader_and_journal_failures_never_stop_routing():
    def broken(*args):
        raise OSError('unavailable')
    source = sources().UsageSource(bridge_reader=broken, native_reader=broken,
                                  clock=lambda: NOW, journal=broken)
    assert source.read('codex-subscription') is None


def test_unknown_window_percent_stays_unknown_while_zero_is_a_measurement():
    source = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'codex': provider(five=0, weekly=None)},
        native_reader=lambda _: None, clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read('codex-subscription')
    assert sample.windows[300].used_pct == 0
    assert sample.windows[10080].used_pct is None
    assert sample.observation().pace_delta(NOW) is None


def test_opus_weekly_constraint_stays_separate_from_account_weekly():
    raw = provider()
    raw['weekly_model'] = {'pct': 89, 'resets_at': RESET, 'label': 'Opus'}
    raw['model_pools'] = {'scoped': {'label': 'Opus', 'weekly': raw['weekly_model']}}
    source = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'claude': raw},
        native_reader=lambda _: None, clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read('claude-subscription')
    assert sample.windows[10080].used_pct == 30
    assert sample.scoped_windows['opus'][10080].used_pct == 89
    assert sample.scoped_windows['opus'][10080].resets_at == NOW + 302400


@pytest.mark.parametrize('pool,used', [('antigravity-gemini', 10), ('antigravity-3p', 99)])
def test_agy_bridge_selects_the_bound_pool(pool, used):
    raw = {**provider(10, 20), 'model_pools': {
        'gemini': provider(10, 20), '3p': provider(99, 100)}}
    source = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'antigravity': raw},
        native_reader=lambda _: None, clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read(pool)
    assert sample.windows[300].used_pct == used
    assert sample.pools[pool] == sample.windows


def test_missing_agy_3p_bridge_pool_falls_back_without_copying_gemini():
    source = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'antigravity': provider(3, 4)},
        native_reader=lambda _: Usage(windows={300: WindowUsage(3)}, updated_at=STAMP,
            pools={'antigravity-gemini': {300: WindowUsage(3)},
                   'antigravity-3p': {300: WindowUsage(88)}}),
        clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read('antigravity-3p')
    assert sample.source == 'native_agy'
    assert sample.windows[300].used_pct == 88


def test_native_agy_missing_3p_is_unknown():
    source = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: Usage(windows={300: WindowUsage(3)}, updated_at=STAMP),
        clock=lambda: NOW, journal=lambda *args: None)
    assert source.read('antigravity-3p') is None


def test_native_claude_retains_opus_from_the_current_usage_api(tmp_path):
    from maestro.backends import claude_subscription
    creds = tmp_path / 'synthetic-credentials.json'
    import json
    creds.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fake',
                                                  'expiresAt': (NOW + 600) * 1000}}))
    usage = claude_subscription.read_subscription_usage(
        cache_path=tmp_path / 'usage.json', credentials_path=creds, now=NOW,
        fetch=lambda *_: {'five_hour': {'utilization': 20, 'resets_at': RESET},
                         'seven_day': {'utilization': 30, 'resets_at': RESET},
                         'limits': [{'kind': 'weekly_scoped', 'percent': 89,
                                     'resets_at': RESET,
                                     'scope': {'model': {'display_name': 'Opus'}}}]})
    source = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: usage, clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read('claude-subscription')
    assert sample.windows[10080].used_pct == 30
    assert sample.scoped_windows['opus'][10080].used_pct == 89
    cached = claude_subscription.read_subscription_usage(
        cache_path=tmp_path / 'usage.json', credentials_path=creds, now=NOW + 30,
        fetch=lambda *_: pytest.fail('throttled native sample must be reused'))
    assert cached.scoped_windows == usage.scoped_windows
    assert cached.updated_at == STAMP.replace('+00:00', 'Z')


def test_configured_bridge_reads_bounded_http_and_preserves_cached_source_time(monkeypatch):
    module = sources()
    from maestro import source_http
    monkeypatch.setenv('MAESTRO_QUOTAPULSE_URL', 'http://127.0.0.1:8787')
    monkeypatch.setenv('USAGE_BRIDGE_TOKEN', 'synthetic')
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return {'fetched_at': STAMP, 'codex': provider()}
    monkeypatch.setattr(source_http, 'get_json', get)
    current = [NOW]
    source = module.UsageSource(native_reader=lambda _: pytest.fail('bridge is valid'),
        clock=lambda: current[0], journal=lambda *args: None)
    first = source.read('codex-subscription')
    current[0] += 30
    assert source.read('codex-subscription').observed_at == first.observed_at == STAMP
    assert len(calls) == 1
    assert calls[0][0] == 'http://127.0.0.1:8787/usage'
    assert calls[0][1]['bearer_token'] == 'synthetic'
    assert calls[0][1]['timeout'] == 2
    current[0] += 31
    source.read('codex-subscription')
    assert len(calls) == 2


def test_unconfigured_bridge_does_not_read_credentials_or_contact_a_service(monkeypatch):
    from maestro import source_http
    monkeypatch.delenv('USAGE_BRIDGE_TOKEN', raising=False)
    monkeypatch.delenv('MAESTRO_QUOTAPULSE_TOKEN_FILE', raising=False)
    monkeypatch.setattr(source_http, 'get_json', lambda *a, **k: pytest.fail('not configured'))
    source = sources().UsageSource(native_reader=lambda _: Usage(
        windows={300: WindowUsage(7)}, updated_at=STAMP), clock=lambda: NOW,
        journal=lambda *args: None)
    assert source.read('codex-subscription').windows[300].used_pct == 7


@pytest.mark.parametrize('pool,provider_name', [
    ('codex-subscription', 'codex'),
    ('antigravity-gemini', 'antigravity'),
    ('antigravity-3p', 'antigravity'),
])
@pytest.mark.parametrize('stamp', [None, 'malformed',
    datetime.fromtimestamp(NOW - 1200, timezone.utc).isoformat(),
    datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat(),
])
def test_bridge_provider_without_proven_freshness_uses_native(pool, provider_name, stamp):
    # Current bridge lockout/dump branches omit provider measurement time even
    # when aggregate fetched_at is new and the cached measurement is 1200s old.
    raw = {**provider(), 'is_limited': True,
           'model_pools': {'gemini': provider(), '3p': provider()}}
    raw.pop('observed_at', None)
    if stamp is not None:
        raw['observed_at'] = stamp
    native_windows = {300: WindowUsage(44, NOW + 3600)}
    source = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, provider_name: raw},
        native_reader=lambda _: Usage(windows=native_windows, pools={pool: native_windows},
                                      updated_at=STAMP, source='native_fixture'),
        clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read(pool)
    assert sample.source == 'native_fixture'
    assert sample.windows[300].used_pct == 44


@pytest.mark.parametrize('pool,provider_name', [
    ('codex-subscription', 'codex'),
    ('antigravity-gemini', 'antigravity'),
    ('antigravity-3p', 'antigravity'),
])
def test_bridge_provider_timestamp_is_preserved_through_cache_and_pace(pool, provider_name):
    old = datetime.fromtimestamp(NOW - 300, timezone.utc).isoformat()
    raw = {**provider(), 'observed_at': old,
           'model_pools': {'gemini': provider(), '3p': provider()}}
    current = [NOW]
    source = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, provider_name: raw},
        native_reader=lambda _: None, clock=lambda: current[0], journal=lambda *args: None)
    sample = source.read(pool)
    assert sample.source == 'quotapulse_bridge'
    assert sample.observed_at == sample.updated_at == old
    assert sample.observation().age_seconds(datetime.fromtimestamp(NOW, timezone.utc)) == 300
    current[0] += 1
    assert source.read(pool) is None, 'a new aggregate timestamp cannot renew provider freshness'


def test_current_claude_bridge_contract_can_use_aggregate_fetch_time():
    raw = provider()
    raw.pop('observed_at', None)
    sample = sources().UsageSource(
        bridge_reader=lambda: {'fetched_at': STAMP, 'claude': raw},
        native_reader=lambda _: None, clock=lambda: NOW,
        journal=lambda *args: None).read('claude-subscription')
    assert sample.source == 'quotapulse_bridge'
    assert sample.observed_at == STAMP


@pytest.mark.parametrize('scoped', [
    {'opus': {10080: WindowUsage(float('nan'))}},
    {'opus': {10080: WindowUsage(float('inf'))}},
    {'opus': {10080: WindowUsage(101)}},
    {'opus': {10080: WindowUsage(-1)}},
    {'opus': {10080: WindowUsage(True)}},
    {'opus': {0: WindowUsage(10)}},
    {'opus': {True: WindowUsage(10)}},
    {'opus': {1.5: WindowUsage(10)}},
    {'opus': {10080: WindowUsage(10, float('nan'))}},
    {'opus': {10080: WindowUsage(10, float('inf'))}},
    {'opus': {10080: WindowUsage(10, -1)}},
    {'opus': {10080: WindowUsage(10, True)}},
    {'opus': {10080: WindowUsage(10, 'tomorrow')}},
    {'opus': {10080: {}}},
    {'opus': []},
    ['malformed'],
])
def test_malformed_native_scope_preserves_account_and_valid_other_scope(scoped):
    import json
    from maestro.backends.claude_subscription import SubscriptionUsage
    from maestro.workflows.routing import usage_to_dict

    if isinstance(scoped, dict):
        scoped = {**scoped, 'other': {10080: WindowUsage(25, NOW + 60)}}
    native = SubscriptionUsage(windows={300: WindowUsage(10, NOW + 60)},
                              scoped_windows=scoped, updated_at=STAMP)
    sample = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: native, clock=lambda: NOW,
        journal=lambda *args: None).read('claude-subscription')
    assert sample is not None
    assert sample.windows[300].used_pct == 10
    assert 'opus' not in sample.scoped_windows
    if isinstance(scoped, dict):
        assert sample.scoped_windows['other'][10080].used_pct == 25
    json.dumps(usage_to_dict(sample), allow_nan=False)


def test_native_scope_unknown_and_boundary_values_remain_separate():
    from maestro.backends.claude_subscription import SubscriptionUsage
    scoped = {'opus': {300: WindowUsage(None, None), 10080: WindowUsage(100, 0)}}
    native = SubscriptionUsage(windows={10080: WindowUsage(0, NOW + 60)},
                              scoped_windows=scoped, updated_at=STAMP)
    sample = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: native, clock=lambda: NOW,
        journal=lambda *args: None).read('claude-subscription')
    assert sample.windows[10080].used_pct == 0
    assert sample.scoped_windows == scoped


@pytest.mark.parametrize('used,reset', [
    (float('nan'), NOW + 60), (float('inf'), NOW + 60),
    (101, NOW + 60), (-1, NOW + 60), (True, NOW + 60),
    (25, float('nan')), (25, -1), (25, True), (25, 'tomorrow'),
    pytest.param(10**400, NOW + 60, id='huge-percent'),
    pytest.param(-(10**400), NOW + 60, id='huge-negative-percent'),
    pytest.param(25, 10**400, id='huge-reset'),
])
def test_cached_native_scope_is_safe_for_strict_attempt_json(tmp_path, used, reset):
    import json
    from maestro.backends import claude_subscription
    from maestro.workflows.routing import usage_to_dict
    cache = tmp_path / 'usage.json'
    cache.write_text(json.dumps({'fetched_epoch': NOW, 'fetched_at': STAMP,
        'windows': {'300': {'used_pct': 10, 'resets_at': NOW + 60}},
        'scoped_windows': {'opus': {'10080': {'used_pct': used, 'resets_at': reset}},
                           'other': {'10080': {'used_pct': 20, 'resets_at': NOW + 60}}}}))
    source = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: claude_subscription.read_subscription_usage(
            cache_path=cache, credentials_path=tmp_path / 'absent.json', now=NOW,
            fetch=lambda *_: pytest.fail('cached read must not fetch')),
        clock=lambda: NOW, journal=lambda *args: None)
    sample = source.read('claude-subscription')
    assert sample.windows[300].used_pct == 10
    assert sample.scoped_windows['other'][10080].used_pct == 20
    json.dumps(usage_to_dict(sample), allow_nan=False)
    assert 'opus' not in sample.scoped_windows


@pytest.mark.parametrize('used', [
    pytest.param(10**400, id='huge-percent'),
    pytest.param(-(10**400), id='huge-negative-percent'),
])
@pytest.mark.parametrize('shape', ['seven_day_opus', 'limits'])
def test_fresh_native_scoped_overflow_preserves_account(tmp_path, used, shape):
    import json
    from maestro.backends import claude_subscription
    creds = tmp_path / 'synthetic-credentials.json'
    creds.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fake',
        'expiresAt': (NOW + 600) * 1000}}))
    raw = {'five_hour': {'utilization': 10, 'resets_at': RESET}}
    if shape == 'limits':
        raw['limits'] = [{'kind': 'weekly_scoped', 'percent': used, 'resets_at': RESET,
                         'scope': {'model': {'display_name': 'Opus'}}}]
    else:
        raw[shape] = {'utilization': used, 'resets_at': RESET}
    cache = tmp_path / 'usage.json'
    native = claude_subscription.read_subscription_usage(cache_path=cache,
        credentials_path=creds, now=NOW, fetch=lambda *_: raw)
    assert native.error is None
    assert native.windows[300].used_pct == 10
    source = sources().UsageSource(bridge_reader=lambda: None,
        native_reader=lambda _: native, clock=lambda: NOW, journal=lambda *a: None)
    assert source.read('claude-subscription').windows[300].used_pct == 10
    cached = claude_subscription.read_subscription_usage(cache_path=cache,
        credentials_path=creds, now=NOW + 10,
        fetch=lambda *_: pytest.fail('freshly cached account must be reused'))
    assert cached.error is None
    assert cached.windows == native.windows
