import math
import subprocess

import pytest

from maestro import config


def test_project_priority_uses_project_then_global_default_then_normal():
    read = getattr(config, 'project_priority', None)
    assert callable(read), 'project priority has no runtime reader'
    assert read({}) == 1
    assert read({'defaults': {'priority': 2}}) == 2
    assert read({'priority': 3, 'defaults': {'priority': 2}}) == 3
    for invalid in (True, 0, -1, 'high', math.inf, math.nan):
        with pytest.raises(config.ConfigError):
            read({'priority': invalid})


def test_priority_commit_preserves_comments_and_unrelated_index(tmp_path):
    update = getattr(config, 'set_project_priority', None)
    assert callable(update), 'no persisted priority command'
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    path = tmp_path / 'project.yaml'
    path.write_text('# keep this\nproject: {name: itv}\npriority: 1 # normal\n')
    other = tmp_path / 'other.txt'
    other.write_text('before\n')
    git('add', '.')
    git('commit', '-qm', 'initial')
    other.write_text('after\n')
    git('add', 'other.txt')
    update(tmp_path, 2.5)
    assert path.read_text() == '# keep this\nproject: {name: itv}\npriority: 2.5 # normal\n'
    assert git('show', '--format=', '--name-only', 'HEAD').strip() == 'project.yaml'
    assert git('diff', '--cached', '--name-only').strip() == 'other.txt'
    path.write_text(path.read_text() + '# operator edit\n')
    with pytest.raises(config.ConfigError, match='dirty'):
        update(tmp_path, 3)
    assert '# operator edit' in path.read_text()


def test_doctor_validates_priority_and_inherited_default(tmp_path, monkeypatch):
    from maestro import cli
    shared = tmp_path / 'global.yaml'
    shared.write_text('defaults: {priority: 2}\n')
    monkeypatch.setenv('MAESTRO_GLOBAL_CONFIG', str(shared))
    (tmp_path / 'project.yaml').write_text('project: {name: test}\n')
    check = getattr(cli, '_check_priority', None)
    assert callable(check), 'doctor does not validate project priority'
    result = check(tmp_path)
    assert result.ok and result.required and '2' in result.detail
    (tmp_path / 'project.yaml').write_text('priority: false\n')
    assert not check(tmp_path).ok
    (tmp_path / 'project.yaml').write_text('project: {name: test}\n')
    shared.write_text('defaults: {priority: 0}\n')
    assert not check(tmp_path).ok


def test_failed_priority_commit_restores_config_and_preserves_unrelated_index(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    path = tmp_path / 'project.yaml'
    original = '# keep\nproject: {name: itv}\npriority: 1 # normal\n'
    path.write_text(original)
    other = tmp_path / 'other.txt'
    other.write_text('before\n')
    git('add', '.')
    git('commit', '-qm', 'initial')
    previous = git('rev-parse', 'HEAD')
    other.write_text('after\n')
    git('add', 'other.txt')
    hook = tmp_path / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o755)
    with pytest.raises(config.ConfigError, match='not committed'):
        config.set_project_priority(tmp_path, 3)
    assert path.read_text() == original
    assert git('rev-parse', 'HEAD') == previous
    assert git('diff', '--cached', '--name-only').strip() == 'other.txt'
    assert not git('status', '--porcelain', '--', 'project.yaml').strip()


@pytest.mark.parametrize('original', [
    'defaults: {priority: &normal 1}\npriority: *normal\n',
    'priority: &normal 1\nother_setting: *normal\n',
])
def test_priority_update_refuses_dangling_yaml_aliases_without_writing_or_committing(tmp_path, original):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    path = tmp_path / 'project.yaml'
    path.write_text(original)
    git('add', 'project.yaml')
    git('commit', '-qm', 'initial')
    previous = git('rev-parse', 'HEAD')
    with pytest.raises(config.ConfigError, match='literal numeric priority'):
        config.set_project_priority(tmp_path, 2)
    assert path.read_text() == original
    assert git('rev-parse', 'HEAD') == previous
    assert not git('status', '--porcelain', '--', 'project.yaml').strip()
    assert not git('diff', '--cached', '--name-only').strip()


def test_set_global_leaf_runs_its_check_under_the_lock_and_a_refusal_writes_nothing(
        tmp_path, monkeypatch):
    """GDT-12 review M3/L2: the caller's `check` sees the text read under the lock (an
    approval re-checks the leaf there); raising refuses the edit. `global_config_lock` is
    that same lock, for the rollback's byte restore."""
    import fcntl

    path = tmp_path / 'home' / 'config.yaml'
    path.parent.mkdir()
    monkeypatch.setenv(config.GLOBAL_CONFIG_ENV, str(path))
    text = 'roles:\n  reviewer:\n    strength: strong\n'
    path.write_text(text)
    lock = path.parent / f'.{path.name}.lock'

    def locked() -> bool:
        with lock.open('a') as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(other, fcntl.LOCK_UN)
            return False

    seen = []

    def check(original):
        seen.append((original, locked()))
        raise config.ConfigError('superseded')

    with pytest.raises(config.ConfigError, match='superseded'):
        config.set_global_leaf(('roles', 'reviewer', 'strength'), 'balanced', check=check)
    assert seen == [(text, True)] and path.read_text() == text
    passed = []
    assert config.set_global_leaf(('roles', 'reviewer', 'strength'), 'strong',
                                  check=passed.append)[1] == text
    assert passed == [text]
    with config.global_config_lock() as target:
        assert target == path and locked()
    assert not locked()


def test_set_global_leaf_edits_one_leaf_inserts_a_role_and_refuses_flow_style(
        tmp_path, monkeypatch):
    """GDT-12 S4 (§7.1): an approved improvement edits one leaf of the global config and
    keeps every other byte, comments included."""
    import yaml

    path = tmp_path / 'home' / 'config.yaml'
    path.parent.mkdir()
    monkeypatch.setenv(config.GLOBAL_CONFIG_ENV, str(path))
    leaf = ('roles', 'reviewer', 'strength')
    text = ('# mine\nroles:\n  reviewer:\n    strength: balanced # keep\n    effort: high\n'
            'thresholds: {a: 1}\n')
    path.write_text(text)
    assert config.set_global_leaf(leaf, 'strong') == (text, text.replace('balanced', 'strong'))
    assert path.read_text() == text.replace('balanced', 'strong')
    assert config.set_global_leaf(leaf, 'strong')[1] == path.read_text()   # no change: same

    # A role the file does not name is inserted under the block-style `roles:`.
    before, after = config.set_global_leaf(('roles', 'planner', 'strength'), 'strong')
    assert after == before.replace('    effort: high\n',
                                   '    effort: high\n  planner:\n    strength: strong\n')
    assert yaml.safe_load(after)['roles'] == {
        'reviewer': {'strength': 'strong', 'effort': 'high'}, 'planner': {'strength': 'strong'}}
    # A role without the leaf gets it under its own block.
    path.write_text('roles:\n  reviewer:\n    effort: high  # why\nother: 1\n')
    assert config.set_global_leaf(leaf, 'strong')[1] == (
        'roles:\n  reviewer:\n    effort: high  # why\n    strength: strong\nother: 1\n')
    # No `roles:` block: one is appended.
    path.write_text('# only a comment\nthresholds:\n  x: 1')
    assert config.set_global_leaf(leaf, 'strong')[1] == (
        '# only a comment\nthresholds:\n  x: 1\nroles:\n  reviewer:\n    strength: strong\n')
    # No file at all: it is written.
    path.unlink()
    before, after = config.set_global_leaf(leaf, 'strong')
    assert before == '' and yaml.safe_load(path.read_text()) == {
        'roles': {'reviewer': {'strength': 'strong'}}}
    # Flow style and aliases are refused, and the file is left as it was.
    for bad in ('roles: {reviewer: {strength: balanced}}\n',
                'roles:\n  reviewer: {strength: balanced}\n',
                'base: &b balanced\nroles:\n  reviewer:\n    strength: *b\n',
                '[1, 2]\n'):
        path.write_text(bad)
        with pytest.raises(config.ConfigError):
            config.set_global_leaf(leaf, 'strong')
        assert path.read_text() == bad
