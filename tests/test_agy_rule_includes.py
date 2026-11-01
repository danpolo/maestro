"""Retained native rule includes must not escape the prepared snapshot."""
import subprocess
import sys

import pytest

from tests.test_agy_runtime import installation
from tests.hang_bound import HANG_SECONDS


def test_retained_rule_include_reads_prepared_content_and_preserves_task_file(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    rules = tree / '.agents/rules'
    rules.mkdir(parents=True)
    included = tree / 'task-instruction.md'
    included.write_text('original included rule')
    (rules / 'fixture.md').write_text(f'Rule before\n@[fixture]({included})\nRule after\n')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    included.write_text('changed task file')
    program = r'''
from pathlib import Path
import re, sys
rule, task = map(Path, sys.argv[1:])
text = rule.read_text()
match = re.search(r'@\[([^\]]*)\]\(([^)]+)\)', text)
assert match, 'native include syntax was lost'
assert match.group(1) == 'fixture'
assert text.startswith('Rule before\n') and text.endswith('\nRule after\n')
frozen = Path(match.group(2))
assert frozen.read_text() == 'original included rule', 'native include followed live task content'
try:
    frozen.write_text('tampered snapshot')
except OSError:
    pass
else:
    raise AssertionError('native include snapshot is writable')
assert task.read_text() == 'changed task file', 'task content was masked by the instruction snapshot'
task.write_text('legitimate task change')
'''
    result = subprocess.run(run.wrap([sys.executable, '-I', '-c', program,
        str(rules / 'fixture.md'), str(included)]), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    assert included.read_text() == 'legitimate task change'


def test_relative_nested_rule_includes_keep_the_declaring_file_base(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    rule = tree / '.agents/rules/fixture.md'
    rule.parent.mkdir(parents=True)
    library = tree / 'library'
    library.mkdir()
    first, second = library / 'first.txt', library / 'second.txt'
    first.write_text('first\n@[nested](second.txt)\n')
    second.write_text('original nested instruction')
    rule.write_text('@[fixture](../../library/first.txt)\n')
    # This file must not win through project-root or cwd fallback.
    (tree / 'second.txt').write_text('wrong relative base')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    first.write_text('changed first')
    second.write_text('changed second')
    program = r'''
from pathlib import Path
import re, sys
pattern = re.compile(r'@\[([^\]]*)\]\(([^)]+)\)')
first = Path(pattern.search(Path(sys.argv[1]).read_text()).group(2))
assert first.read_text().startswith('first\n')
second = Path(pattern.search(first.read_text()).group(2))
assert second.read_text() == 'original nested instruction'
'''
    result = subprocess.run(run.wrap([sys.executable, '-I', '-c', program, str(rule)]),
                            capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('invalid', ['missing', 'cycle', 'directory', 'fifo'])
def test_unsafe_rule_includes_refuse_preparation(installation, invalid):
    import os
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    rule = tree / '.agents/rules/fixture.md'
    rule.parent.mkdir(parents=True)
    included = tree / 'include.txt'
    if invalid == 'cycle':
        included.write_text(f'@[cycle]({rule})')
    elif invalid == 'directory':
        included.mkdir()
    elif invalid == 'fifo':
        os.mkfifo(included)
    rule.write_text(f'@[fixture]({included})')
    with pytest.raises((ValueError, OSError), match='include'):
        prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)


@pytest.mark.parametrize('native_path', [
    '.codex/auth.json',
    '.codex/sessions/history.jsonl',
    '.gemini/antigravity-cli/antigravity-oauth-token',
    '.gemini/antigravity-cli/jetski_state.pbtxt',
])
def test_rule_include_never_copies_native_credentials_or_conversation_state(installation, native_path):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    secret = home / native_path
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('synthetic native private material')
    rule = tree / '.agents/rules/fixture.md'
    rule.parent.mkdir(parents=True)
    rule.write_text(f'@[private]({secret})')
    with pytest.raises(ValueError, match='private native'):
        prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)


@pytest.mark.parametrize('surface', ['rule-alias', 'skill-alias', 'manifest-inherits'])
def test_customization_aliases_cannot_import_private_native_state(installation, surface, monkeypatch):
    import json
    import os
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    private = home / '.codex/auth.json'
    private.parent.mkdir()
    private.write_text(json.dumps({'entries': []}))
    alias = tree / '.agents'
    alias.mkdir()
    if surface == 'manifest-inherits':
        (alias / 'rules.json').write_text(json.dumps({'inherits': [{'path': str(private)}]}))
    else:
        directory = alias / ('rules' if surface == 'rule-alias' else 'skills')
        directory.mkdir()
        (directory / 'fixture.md').symlink_to(private)
    # Refusal must precede any credential read, not merely discard its bytes.
    real_open = os.open
    def refuse_private_open(path, *args, **kwargs):
        if str(path) in (str(private), private.name):
            raise AssertionError('customization opened synthetic private native state')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', refuse_private_open)
    with pytest.raises(ValueError, match='private native'):
        prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)


def test_rule_include_cannot_bypass_native_private_path_through_a_symlinked_login(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    private = home / '.codex/auth.json'
    private.parent.mkdir()
    actual = home / 'native-login.json'
    actual.write_text('synthetic native private material')
    private.symlink_to(actual)
    rule = tree / '.agents/rules/fixture.md'
    rule.parent.mkdir(parents=True)
    rule.write_text(f'@[private]({private})')
    with pytest.raises(ValueError, match='private native'):
        prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)


@pytest.mark.parametrize('state_name', ['state_5.sqlite', 'logs_2.sqlite'])
@pytest.mark.parametrize('alias', ['leaf', 'directory'])
def test_retained_rules_refuse_resolved_native_database_aliases(tmp_path, monkeypatch, state_name, alias):
    import os
    from maestro.backends.agy_customizations import Snapshot
    home = tmp_path / 'home'
    home.mkdir()
    codex = home / '.codex'
    if alias == 'directory':
        actual_directory = home / 'native-state'
        actual_directory.mkdir()
        codex.symlink_to(actual_directory, target_is_directory=True)
        actual = actual_directory / state_name
    else:
        codex.mkdir()
        actual = home / 'native-state.sqlite'
        (codex / state_name).symlink_to(actual)
    actual.write_text('synthetic native conversation database')
    source = tmp_path / 'rule.md'
    # The retained instruction may name the resolved path directly too.
    source.write_text(f'@[state]({actual})')
    control = tmp_path / 'control'
    control.mkdir()
    snapshot = Snapshot(control, home=home, cwd=tmp_path)
    real_open = os.open
    def refuse_private_open(path, *args, **kwargs):
        if str(path) in (str(actual), actual.name):
            raise AssertionError('customization opened synthetic private native database')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', refuse_private_open)
    with pytest.raises(ValueError, match='private native'):
        snapshot.content(source, control / 'rule.md', rules=True)


@pytest.mark.parametrize('runtime', ['.agy-runtime', '.codex-runtime'])
def test_rule_include_cannot_read_prior_private_runtime_conversations(installation, runtime):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    prior = workspace / runtime / 'state/sessions/prior.jsonl'
    prior.parent.mkdir(parents=True)
    prior.write_text('synthetic prior conversation')
    rule = tree / '.agents/rules/fixture.md'
    rule.parent.mkdir(parents=True)
    rule.write_text(f'@[prior]({prior})')
    with pytest.raises(ValueError, match='private native'):
        prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)


@pytest.mark.parametrize('limit', ['depth', 'references', 'bytes'])
def test_rule_include_expansion_obeys_shared_snapshot_budgets(tmp_path, limit):
    from maestro.backends.agy_customizations import Snapshot
    control = tmp_path / 'control'
    control.mkdir()
    snapshot = Snapshot(control, home=tmp_path, cwd=tmp_path)
    source = tmp_path / 'rule.md'
    if limit == 'depth':
        for index in range(34):
            (tmp_path / f'part-{index}.txt').write_text(
                f'@[next](part-{index + 1}.txt)' if index < 33 else 'last')
        source.write_text('@[first](part-0.txt)')
        error = 'overdeep'
    elif limit == 'references':
        included = tmp_path / 'included.txt'
        included.write_text('retained')
        # Manifest entries and includes share the same counter. Do not give
        # each recursive file a fresh allowance.
        snapshot.count = 511
        source.write_text('@[one](included.txt) @[two](included.txt)')
        error = 'reference limit'
    else:
        included = tmp_path / 'included.txt'
        included.write_text('x' * 64)
        source.write_text('@[one](included.txt) @[two](included.txt)')
        snapshot.copier.remaining_bytes = len(source.read_bytes()) + 65
        error = 'byte limit'
    with pytest.raises(ValueError, match=error):
        snapshot.content(source, control / 'rule.md', rules=True)


def test_retained_instruction_inventory_names_actual_sources_without_body_text(installation):
    import json
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    rules = tree / '.agents/rules'
    rules.mkdir(parents=True)
    include = tree / 'included.txt'
    include.write_text('SYNTHETIC_PRIVATE_INSTRUCTION_BODY')
    rule = rules / 'fixture.md'
    rule.write_text(f'@[included]({include})')
    skills = tree / '.agents/skills'
    skills.mkdir()
    library = tree / 'library'
    library.mkdir()
    skill = library / 'SKILL.md'
    skill.write_text('SYNTHETIC_RETAINED_SKILL_BODY')
    (skills / 'shared').symlink_to(library, target_is_directory=True)
    manifest = tree / '.agents/rules.json'
    manifest.write_text(json.dumps({'entries': [{'path': str(rules)}]}))
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    text = (run.control / 'instruction-sources.json').read_text()
    inventory = json.loads(text)
    assert inventory['version'] == 1
    assert {str(rule), str(include), str(skill), str(manifest)} <= set(inventory['reads'])
    assert any(row['source'] == str(rules) and row['surface'] == 'rules'
               for row in inventory['mappings'])
    assert any(row['source'] == str(skills) and row['surface'] == 'skills'
               for row in inventory['mappings'])
    assert all(row['snapshot'].startswith(str(run.control) + '/') for row in inventory['mappings'])
    assert 'SYNTHETIC_PRIVATE_INSTRUCTION_BODY' not in text
    assert 'SYNTHETIC_RETAINED_SKILL_BODY' not in text
