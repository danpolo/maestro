"""Names-only omissions at the actual preparation seam, with fake auth only."""
import json
from pathlib import Path

from maestro.backends import codex_runtime, control_files
from tests.test_codex_runtime import prepare


def test_a_deep_surface_is_recorded_by_name_and_marked_truncated(tmp_path):
    deep = tmp_path / 'plugins/cache/vendor/plugin/1.0/src'
    deep.mkdir(parents=True)
    (deep / 'tool.py').write_text('x')
    inventory = codex_runtime._omission_inventory(tmp_path)
    assert inventory['truncated'] is True
    assert 'plugins/cache/vendor/plugin' in inventory['omitted_paths']
    assert not any(path.startswith('plugins/cache/vendor/plugin/') for path in inventory['omitted_paths'])


def test_a_small_surface_is_complete_and_not_marked(tmp_path):
    (tmp_path / 'skills/one').mkdir(parents=True)
    (tmp_path / 'skills/one/SKILL.md').write_text('x')
    inventory = codex_runtime._omission_inventory(tmp_path)
    assert 'truncated' not in inventory
    assert inventory['omitted_paths'] == ['skills', 'skills/one', 'skills/one/SKILL.md']


def test_too_many_entries_truncate_the_record_instead_of_failing_the_prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runtime, '_INVENTORY_ENTRIES', 3, raising=False)
    (tmp_path / 'skills').mkdir()
    for index in range(10):
        (tmp_path / f'skills/{index}').write_text('x')
    inventory = codex_runtime._omission_inventory(tmp_path)
    assert inventory['truncated'] is True and len(inventory['omitted_paths']) == 3


def test_a_directory_changing_during_the_walk_cannot_fail_the_prepare(tmp_path, monkeypatch):
    (tmp_path / 'skills/one').mkdir(parents=True)
    (tmp_path / 'AGENTS.md').write_text('x')
    original = control_files.DirectoryView.entries
    def entries(self, *, only=None):
        if self.path == tmp_path / 'skills':
            raise ValueError('directory changed')
        return original(self, only=only)
    monkeypatch.setattr(control_files.DirectoryView, 'entries', entries)
    inventory = codex_runtime._omission_inventory(tmp_path)
    assert inventory['truncated'] is True
    assert inventory['omitted_paths'] == ['AGENTS.md', 'skills']


def test_codex_preparation_records_omissions_without_opening_auth(tmp_path, monkeypatch):
    initial = prepare(tmp_path)
    initial.cleanup()
    native = initial.native_home
    (native / 'config.toml').write_text(
        'notify = ["SYNTHETIC_SECRET_COMMAND"]\n'
        '[mcp_servers.omitted-server]\ncommand = "SYNTHETIC_MCP_COMMAND"\n'
        'env = {PRIVATE_ENV = "SYNTHETIC_ENV_VALUE"}\n'
        '[plugins.omitted-plugin]\nenabled = true\n')
    for root in (native, initial.cwd / '.agents', tmp_path / '.codex'):
        (root / 'skills/private-skill').mkdir(parents=True)
        (root / 'skills/private-skill/SKILL.md').write_text('SYNTHETIC_SKILL_CONTENT')
        (root / 'agents').mkdir()
        (root / 'agents/reviewer.toml').write_text('SYNTHETIC_AGENT_CONTENT')
    original_read = control_files.read
    def read(path, *args, **kwargs):
        assert Path(path) != native / 'auth.json', 'parent opened original auth'
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(control_files, 'read', read)
    run = codex_runtime.prepare(cwd=initial.cwd, workspace=initial.workspace,
        native_home=native, host_home=tmp_path, model=initial.model,
        effort='low', write_roots=initial.write_roots, allow=())
    try:
        text = (run.control / 'customizations.json').read_text()
        entries = json.loads(text)
        for root in (native, initial.cwd / '.agents', tmp_path / '.codex'):
            entry = next(row for row in entries if row['path'] == str(root))
            assert 'skills/private-skill/SKILL.md' in entry['omitted_paths']
            assert 'agents/reviewer.toml' in entry['omitted_paths']
        entry = next(row for row in entries if row['path'] == str(native))
        assert entry['omitted_config_keys'] == ['mcp_servers', 'notify', 'plugins']
        assert entry['omitted_mcp_servers'] == ['omitted-server']
        assert entry['omitted_plugins'] == ['omitted-plugin']
        for secret in ('SYNTHETIC_SECRET_COMMAND', 'SYNTHETIC_MCP_COMMAND',
                       'SYNTHETIC_ENV_VALUE', 'PRIVATE_ENV', 'SYNTHETIC_SKILL_CONTENT',
                       'SYNTHETIC_AGENT_CONTENT', 'synthetic-original'):
            assert secret not in text
    finally:
        run.cleanup()
