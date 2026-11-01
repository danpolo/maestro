"""Names-only omissions at the actual preparation seam, with fake auth only."""
import json
from pathlib import Path

from maestro.backends import codex_runtime, control_files
from tests.test_codex_runtime import prepare


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
