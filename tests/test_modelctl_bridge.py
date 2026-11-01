"""The external adapter reads provider-owned tables without scan or inference."""
from pathlib import Path

import pytest

from maestro.model_policy import PolicyError


def test_real_owner_limits_are_provider_specific_and_unknown_row_is_suggested_only(tmp_path):
    from maestro.modelctl_bridge import ModelctlBridge
    source = Path('/home/dan/projects/modelctl')
    if not (source / 'modelctl/paths.py').exists():
        pytest.skip('external owner checkout unavailable')
    for provider, ceiling in [('claude', '180K'), ('codex', '220K')]:
        directory = tmp_path / ('.' + provider)
        directory.mkdir()
        (directory / 'model_context_limits.md').write_text(
            '| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
            '|---|---|---|---|\n'
            f'| Same | 80K-90K | 100K-120K | {ceiling} |\n'
            '| Model unknown | 70K | 90K-100K | 150K |\n')
    bridge = ModelctlBridge(source, home=tmp_path)
    claude = bridge.limits('claude')
    codex = bridge.limits('codex')
    assert claude['rows']['same']['ceiling'] == 180000
    assert codex['rows']['same']['ceiling'] == 220000
    assert claude['unknown']['prepare'] == [70000, 70000]
    assert claude['unknown']['ceiling'] == 150000
    assert not (tmp_path / '.local/state/modelctl/state.json').exists()


def test_missing_or_incompatible_owner_fails_clearly(tmp_path):
    from maestro.modelctl_bridge import ModelctlBridge
    with pytest.raises(PolicyError, match='modelctl'):
        ModelctlBridge(tmp_path).limits('codex')


def test_antigravity_owner_bridge_preserves_class_identity_and_maps_catalog_backend(tmp_path):
    from maestro.modelctl_bridge import ModelctlBridge
    source = Path('/home/dan/projects/modelctl')
    if not (source / 'modelctl/paths.py').exists():
        pytest.skip('external owner checkout unavailable')
    directory = tmp_path / '.gemini'
    directory.mkdir()
    (directory / 'model_context_limits.md').write_text(
        '| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
        '|---|---|---|---|\n'
        '| agy/Gemini 3.8 Flash | 100K | 125K | 140K |\n'
        '\n| Model | Enforced window |\n|---|---|\n'
        '| agy/Gemini 3.8 Flash | 176K |\n')
    binary = tmp_path / '.local/bin/agy'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\n[ "$1" = models ] || exit 91\n'
                      "printf 'gemini-3.8-flash-low\\tGemini 3.8 Flash (Low)\\n'\n")
    binary.chmod(0o755)
    bridge = ModelctlBridge(source, home=tmp_path)
    limits = bridge.limits('antigravity_cli')
    assert limits['rows']['agy-gemini-3-8-flash']['ceiling'] == 140000
    discovery = bridge.discover('antigravity_cli')
    candidate, = discovery['candidates']
    assert (candidate['backend_id'], candidate['model_id'], candidate['registered']) == (
        'antigravity_cli', 'gemini-3-8-flash', True)
    row = next(m for m in bridge.configured_models('antigravity_cli')['models']
               if m['model_id'] == 'gemini-3-8-flash')
    assert row['backend_id'] == 'antigravity_cli'
    assert not (tmp_path / '.local/state/modelctl').exists()


def test_legacy_owner_cannot_be_mistaken_for_durable_detection_outbox(tmp_path):
    from maestro.modelctl_bridge import ModelctlBridge
    source = Path('/home/dan/projects/modelctl')
    if not (source / 'modelctl/paths.py').exists():
        pytest.skip('external owner checkout unavailable')
    import shutil
    legacy = tmp_path / 'legacy'
    shutil.copytree(source / 'modelctl', legacy / 'modelctl', ignore=shutil.ignore_patterns('__pycache__'))
    (legacy / 'modelctl/core/outbox.py').unlink(missing_ok=True)
    with pytest.raises(PolicyError, match='outbox'):
        ModelctlBridge(legacy, home=tmp_path).pending_events()


def test_real_owner_removed_flag_is_the_only_retirement_signal(tmp_path):
    from maestro.modelctl_bridge import ModelctlBridge
    import json
    source = Path('/home/dan/projects/modelctl')
    if not (source / 'modelctl/paths.py').exists():
        pytest.skip('external owner checkout unavailable')
    bridge = ModelctlBridge(source, home=tmp_path)
    with pytest.raises(PolicyError, match='not treated as retirement'):
        bridge.retired('codex')  # never scanned: unknown, not retired
    state = tmp_path / '.local/state/modelctl/state.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'version': 1, 'notified': {}, 'last_scan': {}, 'providers': {'codex': {
        'bootstrapped': True, 'failures': 0, 'models': {
            'gpt-5.6-luna': {'status': 'superseded', 'removed': True},
            'gpt-6-luna': {'status': 'configured', 'removed': False}}}}}))
    result = bridge.retired('codex')
    assert result['removed'] == ['gpt-5.6-luna']
    state.write_text('{corrupt')
    with pytest.raises(PolicyError, match='not treated as retirement'):
        bridge.retired('codex')
    assert state.read_text() == '{corrupt'  # owner's corrupt-state handling is not triggered
    assert [p.name for p in state.parent.iterdir()] == ['state.json']
