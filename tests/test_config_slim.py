"""Idle migration through real Git/controller ownership; no live projects or transport."""
import json
import subprocess

import pytest
import yaml

from maestro import config, selfupdate, state
from maestro.control.models import EventType
from maestro.control.store import ControlStore
from tests.hang_bound import HANG_SECONDS


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.com')
    original = ('# Keep this header\nproject: {name: demo}\nthresholds:\n'
                '  five_h_pause_pct: 92 # inherited\n'
                '  concurrency_cap: 5 # real override\n'
                '# Keep this explanation\nswitch: {manual: true}\n'
                'risky_set: [auth.py]\n')
    (repo / 'project.yaml').write_text(original)
    (repo / 'other.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'baseline')
    global_path = tmp_path / 'global.yaml'
    global_path.write_text('thresholds: {five_h_pause_pct: 92, concurrency_cap: 3}\n'
                           'switch: {manual: true}\nrisky_set: [auth.py]\n')
    monkeypatch.setenv(config.GLOBAL_CONFIG_ENV, str(global_path))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    db = repo / '.orchestrator/control.sqlite3'
    db.parent.mkdir()
    state._reset_control_store()
    monkeypatch.setattr(state, 'CONTROL_DB', db)
    monkeypatch.setattr(state, 'STATE_JSON', db.parent / 'state.json')
    monkeypatch.setattr(state, 'JOURNAL', db.parent / 'journal.ndjson')
    ControlStore.open_controller(db).close()
    state.write_state({'maestro_version': 'released'})
    state._reset_control_store()
    from maestro.hitl import telegram
    notices = []
    monkeypatch.setattr(telegram, 'notify_intake',
                        lambda text, **kw: notices.append((text, kw)) or True)
    yield repo, global_path, notices
    state._reset_control_store()


def test_idle_update_slims_commits_only_config_and_notifies_once(project, monkeypatch):
    repo, _, notices = project
    before = config.effective_config(repo)[0]
    (repo / 'other.txt').write_text('staged work\n')
    git(repo, 'add', 'other.txt')
    monkeypatch.setattr(selfupdate, 'read_latest', lambda: 'released')
    assert selfupdate.maybe_self_update(project_repo=repo) == 'up_to_date'
    after = config.effective_config(repo)[0]
    assert after == before
    text = (repo / 'project.yaml').read_text()
    assert 'five_h_pause_pct' not in text
    assert 'switch:' not in text
    assert '# Keep this header' in text and '# Keep this explanation' in text
    assert 'concurrency_cap: 5 # real override' in text and 'risky_set: [auth.py]' in text
    assert git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD') == 'project.yaml'
    assert git(repo, 'diff', '--cached', '--name-only') == 'other.txt'
    assert len(notices) == 1 and notices[0][1]['repo'] == repo
    head = git(repo, 'rev-parse', 'HEAD')
    assert selfupdate.maybe_self_update(project_repo=repo) == 'up_to_date'
    assert git(repo, 'rev-parse', 'HEAD') == head and len(notices) == 1


def test_busy_authoritative_state_beats_idle_projection(project, monkeypatch):
    from maestro import config_slim
    repo, _, notices = project
    original = (repo / 'project.yaml').read_bytes()
    (repo / '.orchestrator/state.json').write_text('{}')
    with ControlStore.open_controller(repo / '.orchestrator/control.sqlite3') as controller:
        controller.append_event(EventType.LEGACY_STATE_WRITTEN, 'legacy_state',
                                payload={'document': {'in_flight': [{'task_id': 'T1'}]}})
        result = config_slim.slim_project_config(repo, controller=controller)
    assert result['status'] == 'deferred' and 'in_flight' in result['reason']
    assert (repo / 'project.yaml').read_bytes() == original and not notices


def test_live_controller_refuses_an_external_slim(project):
    from maestro import config_slim
    repo, _, notices = project
    import sys
    command = ('from pathlib import Path; from maestro.control.store import ControlStore; '
               'import sys; c=ControlStore.open_controller(Path(sys.argv[1])); '
               'print("ready", flush=True); sys.stdin.readline(); c.close()')
    with subprocess.Popen([sys.executable, '-c', command,
                           str(repo / '.orchestrator/control.sqlite3')],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline().strip() == 'ready'
            result = config_slim.slim_project_config(repo)
        finally:
            child.communicate('\n', timeout=HANG_SECONDS)
    assert result['status'] == 'deferred' and 'controller pid' in result['reason']
    assert not notices


@pytest.mark.parametrize('bad', [
    'thresholds: [unclosed\n', 'thresholds: {x: 1, x: 2}\n',
    'thresholds: &shared {five_h_pause_pct: 92}\nswitch: *shared\n', '- list\n',
])
def test_ambiguous_yaml_is_never_rewritten(project, bad):
    from maestro import config_slim
    repo, _, notices = project
    (repo / 'project.yaml').write_text(bad)
    git(repo, 'add', 'project.yaml')
    git(repo, 'commit', '-qm', 'operator config')
    head = git(repo, 'rev-parse', 'HEAD')
    result = config_slim.slim_project_config(repo)
    assert result['status'] == 'deferred'
    assert (repo / 'project.yaml').read_text() == bad
    assert git(repo, 'rev-parse', 'HEAD') == head and not notices


def test_commit_failure_restores_bytes_mode_and_staged_work(project, monkeypatch):
    from maestro import config_slim
    repo, _, notices = project
    original = (repo / 'project.yaml').read_bytes()
    (repo / 'project.yaml').chmod(0o640)
    (repo / 'other.txt').write_text('staged\n')
    git(repo, 'add', 'other.txt')
    real = subprocess.run
    def fail_commit(argv, **kw):
        if 'commit' in argv:
            return subprocess.CompletedProcess(argv, 1, '', 'commit failed')
        return real(argv, **kw)
    monkeypatch.setattr(subprocess, 'run', fail_commit)
    result = config_slim.slim_project_config(repo)
    assert result['status'] == 'deferred' and 'commit' in result['reason']
    assert (repo / 'project.yaml').read_bytes() == original
    assert (repo / 'project.yaml').stat().st_mode & 0o777 == 0o640
    assert git(repo, 'diff', '--cached', '--name-only') == 'other.txt'
    assert not notices


def test_failed_notice_retries_at_next_idle_without_another_commit(project, monkeypatch):
    from maestro import config_slim
    from maestro.hitl import telegram
    repo, _, notices = project
    monkeypatch.setattr(telegram, 'notify_intake', lambda *a, **kw: False)
    first = config_slim.slim_project_config(repo)
    assert first['status'] == 'slimmed' and first['notice_sent'] is False
    head = git(repo, 'rev-parse', 'HEAD')
    monkeypatch.setattr(telegram, 'notify_intake',
                        lambda text, **kw: notices.append(text) or True)
    second = config_slim.slim_project_config(repo)
    assert second['status'] == 'unchanged' and second['notice_sent'] is True
    assert git(repo, 'rev-parse', 'HEAD') == head and len(notices) == 1


def test_only_strict_equal_global_paths_are_removed(project):
    from maestro import config_slim
    repo, global_path, _ = project
    global_path.write_text('thresholds: {five_h_pause_pct: true, concurrency_cap: 3}\n')
    # bool == 1 and int == float are Python equality, not effective-setting equivalence.
    (repo / 'project.yaml').write_text('thresholds: {five_h_pause_pct: 1}\npriority: 1\n')
    git(repo, 'add', 'project.yaml')
    git(repo, 'commit', '-qm', 'typed config')
    before = (repo / 'project.yaml').read_bytes()
    result = config_slim.slim_project_config(repo)
    assert result['status'] == 'unchanged'
    assert (repo / 'project.yaml').read_bytes() == before


def test_absent_global_config_is_a_noop(project):
    from maestro import config_slim
    repo, global_path, notices = project
    global_path.unlink()
    before = git(repo, 'rev-parse', 'HEAD')
    assert config_slim.slim_project_config(repo)['status'] == 'unchanged'
    assert git(repo, 'rev-parse', 'HEAD') == before and not notices


@pytest.mark.parametrize('staged', [False, True])
def test_existing_config_edits_are_preserved(project, staged):
    from maestro import config_slim
    repo, _, notices = project
    text = (repo / 'project.yaml').read_text() + 'priority: 2\n'
    (repo / 'project.yaml').write_text(text)
    if staged:
        git(repo, 'add', 'project.yaml')
    index = git(repo, 'diff', '--cached')
    result = config_slim.slim_project_config(repo)
    assert result['status'] == 'deferred' and 'dirty' in result['reason']
    assert (repo / 'project.yaml').read_text() == text
    assert git(repo, 'diff', '--cached') == index and not notices


def test_unmigrated_project_has_no_database_or_config_writes(project):
    from maestro import config_slim
    repo, _, notices = project
    database = repo / '.orchestrator/control.sqlite3'
    database.unlink()
    before = (repo / 'project.yaml').read_bytes()
    assert config_slim.slim_project_config(repo)['status'] == 'deferred'
    assert not database.exists() and (repo / 'project.yaml').read_bytes() == before
    assert not notices


def test_invalid_global_never_drops_overrides(project):
    from maestro import config_slim
    repo, global_path, notices = project
    global_path.write_text('thresholds: [broken\n')
    before = (repo / 'project.yaml').read_bytes()
    result = config_slim.slim_project_config(repo)
    assert result['status'] == 'deferred'
    assert (repo / 'project.yaml').read_bytes() == before and not notices


def test_nested_flow_overrides_are_slimmed_without_losing_other_comments():
    from maestro import config_slim
    original = '# header\nthresholds: {one: 1, two: 5} # my override\n# footer\n'
    changed, removed = config_slim.slim_config_text(original, 'thresholds: {one: 1, two: 3}\n')
    assert removed == ['thresholds.one']
    assert yaml.safe_load(changed) == {'thresholds': {'two': 5}}
    assert '# header' in changed and '# my override' in changed and '# footer' in changed


def test_whole_equal_mapping_does_not_eat_following_comments():
    from maestro import config_slim
    original = 'thresholds:\n  one: 1\n# project explanation\nproject: {name: demo}\n'
    changed, removed = config_slim.slim_config_text(original, 'thresholds: {one: 1}\n')
    assert removed == ['thresholds.one']
    assert changed == '# project explanation\nproject: {name: demo}\n'


def test_notice_failure_survives_a_later_operator_config_commit(project, monkeypatch):
    from maestro import config_slim
    from maestro.hitl import telegram
    repo, _, notices = project
    monkeypatch.setattr(telegram, 'notify_intake', lambda *a, **kw: False)
    assert config_slim.slim_project_config(repo)['notice_sent'] is False
    with (repo / 'project.yaml').open('a') as stream:
        stream.write('priority: 2\n')
    git(repo, 'add', 'project.yaml')
    git(repo, 'commit', '-qm', 'operator priority')
    monkeypatch.setattr(telegram, 'notify_intake',
                        lambda text, **kw: notices.append(text) or True)
    assert config_slim.slim_project_config(repo)['notice_sent'] is True
    assert len(notices) == 1


def test_all_redundant_config_remains_valid_and_failed_notice_recovers(project, monkeypatch):
    from maestro import config_slim
    from maestro.hitl import telegram
    repo, global_path, notices = project
    original = '# My project defaults\nthresholds:\n  concurrency_cap: 3\n'
    (repo / 'project.yaml').write_text(original)
    git(repo, 'add', 'project.yaml')
    git(repo, 'commit', '-qm', 'defaults only')
    global_path.write_text('thresholds: {concurrency_cap: 3}\n')
    monkeypatch.setattr(telegram, 'notify_intake', lambda *a, **kw: False)
    first = config_slim.slim_project_config(repo)
    assert first['status'] == 'slimmed' and first['notice_sent'] is False
    assert yaml.safe_load((repo / 'project.yaml').read_text()) == {}
    head = git(repo, 'rev-parse', 'HEAD')
    monkeypatch.setattr(telegram, 'notify_intake',
                        lambda text, **kw: notices.append(text) or True)
    second = config_slim.slim_project_config(repo)
    assert second['status'] == 'unchanged' and second['notice_sent'] is True
    assert git(repo, 'rev-parse', 'HEAD') == head and len(notices) == 1


def test_partial_flow_mapping_with_internal_comments_is_safely_deferred(project):
    from maestro import config_slim
    repo, global_path, notices = project
    original = ('# header\nthresholds: {\n  one: 1, # inherited value\n'
                '  two: 5 # Explain this retained override\n}\n# footer\n')
    (repo / 'project.yaml').write_text(original)
    git(repo, 'add', 'project.yaml')
    git(repo, 'commit', '-qm', 'commented flow config')
    global_path.write_text('thresholds: {one: 1, two: 3}\n')
    head = git(repo, 'rev-parse', 'HEAD')
    result = config_slim.slim_project_config(repo)
    assert result['status'] == 'deferred' and 'comment' in result['reason']
    assert (repo / 'project.yaml').read_text() == original
    assert git(repo, 'rev-parse', 'HEAD') == head and not notices
