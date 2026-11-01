"""Disposable switches publish once, adopt independently, and reject stale previews."""
import json
from pathlib import Path

import pytest

from maestro.model_policy import PolicyStore, PolicyError, shipped_policy_v1 as shipped_policy
from maestro import cli
from maestro.control.store import ControlStore


class Metadata:
    """External metadata-only boundary; never stands in for policy/adoption code."""
    def __init__(self):
        self.table_hash = 'table-1'
        self.registered = True
        self.available = True

    def discover(self, backend):
        model = 'gpt-6-luna' if backend == 'codex' else 'claude-opus-5-5'
        return dict(provider=backend, table_hash=self.table_hash, unknown={
            'display': 'Model unknown', 'prepare': [70000, 70000],
            'fresh': [90000, 100000], 'ceiling': 150000}, candidates=[dict(
                backend_id=backend, model_id=model, family='luna' if backend == 'codex' else 'opus',
                display=model, version=[6] if backend == 'codex' else [5, 5], fingerprint='fp1',
                access='metadata_only', registered=self.registered,
                lifecycle={'prepare': [100000, 110000], 'fresh': [150000, 160000], 'ceiling': 200000},
            )] if self.available else [])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Start on the historical schema 1 bootstrap; pair replacements have their own suite.
    from maestro import model_policy
    monkeypatch.setattr(model_policy, 'shipped_policy', model_policy.shipped_policy_v1)
    from maestro.model_inventory import register_project
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    store = PolicyStore()
    projects = []
    for name, override, active in [('inheritor', False, False), ('explicit', True, False), ('active', False, True)]:
        repo = tmp_path / name
        (repo / '.orchestrator').mkdir(parents=True)
        cfg = {'project': {'name': name, 'repo_path': str(repo)}}
        if override:
            cfg['roles'] = {'implementer': {'backend': 'claude', 'models': {'claude': 'claude-sonnet-5-5'}}}
        import yaml
        (repo / 'project.yaml').write_text(yaml.safe_dump(cfg))
        (repo / '.orchestrator/state.json').write_text(json.dumps({'in_flight': [{'task_id': '01'}] if active else []}))
        (repo / '.orchestrator/current').write_text(str(Path(__file__).resolve().parents[1]))
        ControlStore.open_controller(repo / '.orchestrator/control.sqlite3').close()
        register_project(repo, store)
        projects.append(repo)
    return store, projects, Metadata()


def test_registered_cross_harness_switch_reports_real_adoption_and_override(setup):
    from maestro.model_replacement import preview_replace, apply_preview
    from maestro.model_policy import adopted_policy, effective_roles
    store, projects, metadata = setup
    before = [p.joinpath('project.yaml').read_bytes() for p in projects]
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    outcome = apply_preview(preview, store, metadata, confirmed=True)
    assert [r['status'] for r in outcome['projects']] == ['updated', 'updated', 'deferred']
    assert outcome['revision'] == store.current()
    inherited = adopted_policy(projects[0], store)[1]
    assert inherited['roles']['implementer']['backend'] == 'codex'
    import yaml
    explicit, origins = effective_roles(yaml.safe_load(projects[1].joinpath('project.yaml').read_text()), inherited)
    assert explicit['roles']['implementer']['models']['claude'] == 'claude-sonnet-5-5'
    assert origins['implementer']['claude'] == 'project'
    assert [p.joinpath('project.yaml').read_bytes() for p in projects] == before


@pytest.mark.parametrize('mutation', ['config', 'table', 'policy', 'availability'])
def test_preview_staleness_refuses_before_publication(setup, mutation):
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, metadata = setup
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    if mutation == 'config':
        with projects[0].joinpath('project.yaml').open('a') as stream:
            stream.write('\n# changed\n')
    elif mutation == 'table':
        metadata.table_hash = 'table-2'
    elif mutation == 'availability':
        metadata.available = False
    else:
        store.publish(shipped_policy(), expected=None)
    current = store.current()
    with pytest.raises(PolicyError):
        apply_preview(preview, store, metadata, confirmed=True)
    assert store.current() == current


def test_unknown_row_shows_actual_suggestion_and_keeps_registration_disabled(setup):
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, metadata = setup
    metadata.registered = False
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert preview['status'] == 'registration_required'
    assert preview['suggested_limits']['prepare'] == [70000, 70000]
    assert 'custom' in preview['notice']
    with pytest.raises(PolicyError, match='owner'):
        apply_preview(preview, store, metadata, confirmed=True)
    assert store.current() is None


def test_unconfirmed_switch_never_publishes(setup):
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, metadata = setup
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    with pytest.raises(PolicyError, match='confirmation'):
        apply_preview(preview, store, metadata, confirmed=False)
    assert store.current() is None


def test_interactive_public_picker_applies_registered_replacement(setup, monkeypatch, capsys):
    from maestro import model_commands
    store, projects, metadata = setup
    monkeypatch.setattr(model_commands, 'ModelctlBridge', lambda: metadata)
    # Select Sonnet from qualified routes, Codex, Luna, then explicitly confirm.
    inputs = iter(['claude:claude-sonnet-5-5', 'codex', '1', 'yes'])
    monkeypatch.setattr('builtins.input', lambda _: next(inputs))
    assert cli.main(['models']) == 0
    assert store.current() is not None
    assert 'deferred' in capsys.readouterr().out


def test_missing_row_preview_directs_registration_to_owner_without_stale_disabled_claim(setup):
    from maestro.model_replacement import preview_replace
    store, _, metadata = setup
    metadata.registered = False
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert preview['registered'] is False and preview['selected'] is False
    assert preview['registration_command'] == 'modelctl add codex --model gpt-6-luna'
    assert 'automatic' in preview['notice']
    assert 'unavailable until' not in preview['notice']
    assert store.current() is None


def test_existing_class_missing_row_uses_predecessor_even_without_unknown_fallback(setup):
    from maestro.model_replacement import preview_replace
    store, _, metadata = setup
    metadata.registered = False
    discover = metadata.discover
    def without_unknown(backend):
        result = discover(backend); result['unknown'] = None; return result
    metadata.discover = without_unknown
    metadata.configured_models = lambda backend: dict(contract=1, models=[dict(
        backend_id='codex', model_id='gpt-5.6-luna', family='luna', version=[5, 6],
        set=True, enforced_window=240000, lifecycle=dict(
            display='GPT-5.6 Luna', prepare=[100000, 110000], fresh=[150000, 160000], ceiling=200000))])
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert preview['suggested_limits']['prepare'] == [100000, 110000]
    assert preview['registration_kind'] == 'existing_class'
    assert preview['suggested_window'] == 240000
    assert 'inherit' in preview['notice']
    assert store.current() is None


def test_old_owner_class_capability_failure_keeps_readonly_missing_row_guidance(setup):
    from maestro.model_replacement import preview_replace
    store, _, metadata = setup
    metadata.registered = False
    def unavailable(_):
        raise PolicyError('modelctl classes unavailable or incompatible (ModuleNotFoundError)')
    metadata.configured_models = unavailable
    preview = preview_replace(store, metadata, ('claude', 'claude-sonnet-5-5'), ('codex', 'gpt-6-luna'))
    assert preview['status'] == 'registration_required'
    assert preview['registration_command'] == 'modelctl add codex --model gpt-6-luna'
    assert preview['suggested_limits']['prepare'] == [70000, 70000]
    assert store.current() is None
