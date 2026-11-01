"""Assertions on real generated native controls, shared by caller tests."""
import json


def assert_codex_discovery_isolated(workspace, home):
    controls = list(workspace.glob('.codex-runtime/call-*/customizations.json'))
    assert controls, 'no real Codex preparation was exercised'
    for path in controls:
        inventories = json.loads(path.read_text())
        discovery = next((row for row in inventories
                          if row['path'] == str(home / '.claude')), None)
        assert discovery is not None, 'native caller inherited an external host home'
        assert discovery['omitted_paths'] == [
            'plugins', 'plugins/known_marketplaces.json.lock',
            'plugins/known_marketplaces.json.lock/fixture',
        ], 'real synthetic discovery traversal must remain covered'
