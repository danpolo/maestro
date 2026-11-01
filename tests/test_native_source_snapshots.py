"""Source races use synthetic content only; native credentials are never opened."""
import os
from pathlib import Path

import pytest

from maestro.backends import agy_customizations, agy_runtime, codex_runtime, control_files


def snapshot(tmp_path):
    control = tmp_path / 'control'
    control.mkdir()
    return agy_customizations.Snapshot(control, home=tmp_path, cwd=tmp_path)


def test_instruction_leaf_open_cannot_follow_a_replaced_ancestor(tmp_path, monkeypatch):
    source, hostile = tmp_path / 'source', tmp_path / 'hostile'
    for directory, text in ((source, 'approved'), (hostile, 'substituted')):
        directory.mkdir()
        (directory / 'SKILL.md').write_text(text)
    target = tmp_path / 'frozen'
    copier = snapshot(tmp_path)
    original_open = os.open
    swapped = False

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == 'SKILL.md':
            swapped = True
            source.rename(tmp_path / 'saved')
            source.symlink_to(hostile, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', race)
    try:
        copier.content(source / 'SKILL.md', target)
    except (ValueError, OSError):
        pass
    assert swapped
    assert not target.exists() or target.read_text() == 'approved'


@pytest.mark.parametrize('kind', ['agy-controller', 'agy-helper', 'agy-launcher',
                                 'codex-launcher', 'security-script'])
def test_executable_copy_cannot_follow_a_substituted_source(tmp_path, monkeypatch, kind):
    import builtins
    from dataclasses import replace
    import json
    import shutil

    home, tree, workspace, installed = (tmp_path / name for name in
                                        ('home', 'tree', 'workspace', 'installed'))
    for directory in (home, tree, workspace, installed / 'backends',
                      home / '.gemini/config/scripts', home / '.gemini/antigravity-cli/bin',
                      home / '.codex'):
        directory.mkdir(parents=True, exist_ok=True)
    (home / '.codex/auth.json').write_text('synthetic')
    helper = home / '.gemini/antigravity-cli/bin/agentapi'
    helper.write_text('approved')
    for name in ('permissions.py', 'tool_hooks.py', 'tool_policy.py',
                 'backends/agy_worker.py', 'backends/control_files.py',
                 'backends/git_staging.py', 'backends/native_ownership.py',
                 'backends/codex_app_server.py', 'backends/codex_worker.py'):
        (installed / name).write_text('approved')
    script = home / '.gemini/config/scripts/skillspector_gate.py'
    script.write_text('approved')
    (home / '.gemini/config/hooks.json').write_text(json.dumps({
        'skillspector-security-gate': {'PreToolUse': [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': str(script)}]}]}}))
    victim = {'agy-controller': installed / 'permissions.py', 'agy-helper': helper,
              'agy-launcher': installed / 'backends/agy_worker.py',
              'codex-launcher': installed / 'backends/codex_worker.py',
              'security-script': script}[kind]
    hostile = tmp_path / 'hostile'
    shutil.copytree(victim.parent, hostile)
    (hostile / victim.name).write_text('substituted')
    inode = victim.parent.stat().st_ino
    real_open, real_os_open = builtins.open, os.open
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            victim.parent.rename(tmp_path / 'saved')
            victim.parent.symlink_to(hostile, target_is_directory=True)

    def open_race(path, mode='r', *args, **kwargs):
        if not isinstance(path, int) and Path(path) == victim and mode.startswith('r'):
            swap()
        return real_open(path, mode, *args, **kwargs)

    def os_open_race(path, flags, *args, **kwargs):
        if (Path(path).name == victim.name and flags & os.O_ACCMODE == os.O_RDONLY
                and (Path(path) == victim or 'dir_fd' in kwargs
                     and os.fstat(kwargs['dir_fd']).st_ino == inode)):
            swap()
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', open_race)
    monkeypatch.setattr(os, 'open', os_open_race)
    run = None
    try:
        if kind == 'security-script':
            agy_runtime._security_profile(home / '.gemini/config', workspace)
            frozen = workspace / 'skillspector_gate.py'
        elif kind == 'codex-launcher':
            run = codex_runtime.prepare(cwd=tree, workspace=workspace, host_home=home,
                model='test', effort='low', write_roots=(tree,))
            run = replace(run, trusted_source=installed)
            run.launcher(binary='codex', prompt='test')
            frozen = run.control / 'maestro/backends/codex_worker.py'
        else:
            if kind == 'agy-controller':
                monkeypatch.setattr(agy_runtime, '__file__', str(installed / 'backends/agy_runtime.py'))
            run = agy_runtime.prepare(cwd=tree, workspace=workspace, home=home, write_roots=(tree,))
            if kind == 'agy-launcher':
                run = replace(run, trusted_source=installed)
                run.launcher(['agy'], log_file=workspace / 'impl.log', mode='w')
                frozen = run.control / 'maestro/backends/agy_worker.py'
            elif kind == 'agy-helper':
                frozen = run.control.parent / 'bin/agentapi'
            else:
                frozen = run.control / 'maestro/permissions.py'
        assert frozen.read_text() == 'approved'
    except (ValueError, OSError):
        pass
    finally:
        if run is not None:
            run.cleanup()
    assert swapped


def test_instruction_recursion_never_reopens_a_replaced_directory(tmp_path, monkeypatch):
    source, hostile = tmp_path / 'source', tmp_path / 'hostile'
    for directory, text in ((source, 'approved'), (hostile, 'substituted')):
        directory.mkdir()
        (directory / 'SKILL.md').write_text(text)
    target = tmp_path / 'frozen'
    copier = snapshot(tmp_path)
    original_iterdir, original_listdir = Path.iterdir, os.listdir
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(tmp_path / 'saved')
            hostile.rename(source)

    def iterdir(path):
        if path == source:
            swap()
        return original_iterdir(path)

    def listdir(path):
        if isinstance(path, int) and os.fstat(path).st_ino == (tmp_path / 'saved' if swapped else source).stat().st_ino:
            swap()
        return original_listdir(path)

    monkeypatch.setattr(Path, 'iterdir', iterdir)
    monkeypatch.setattr(os, 'listdir', listdir)
    try:
        copier.content(source, target)
    except (ValueError, OSError):
        pass
    assert swapped
    leaf = target / 'SKILL.md'
    assert not leaf.exists() or leaf.read_text() == 'approved'


@pytest.mark.parametrize('kind', ['manifest', 'hooks', 'model-config', 'model-catalog'])
def test_configuration_read_refuses_ancestor_substitution(tmp_path, monkeypatch, kind):
    import json
    source, hostile, control = (tmp_path / name for name in ('source', 'hostile', 'control'))
    for directory in (source, hostile, control):
        directory.mkdir()
    name = {'manifest': 'skills.json', 'hooks': 'hooks.json',
            'model-config': 'config.toml', 'model-catalog': 'model_catalog.json'}[kind]
    if kind == 'model-config':
        good, bad = 'model_context_window = 100\n', 'model_context_window = 999\n'
    elif kind == 'model-catalog':
        good = json.dumps({'models': [{'slug': 'test', 'context_window': 100}]})
        bad = good.replace('100', '999')
    elif kind == 'hooks':
        good, bad = '{}', '{"substituted": {}}'
    else:
        good, bad = '{}', '{"exclude": ["substituted"]}'
    (source / name).write_text(good)
    (hostile / name).write_text(bad)
    original_file = control_files.file
    swapped = False

    def race(path):
        nonlocal swapped
        result = original_file(path)
        if not swapped and path == source / name:
            swapped = True
            source.rename(tmp_path / 'saved')
            source.symlink_to(hostile, target_is_directory=True)
        return result

    monkeypatch.setattr(control_files, 'file', race)
    try:
        if kind == 'manifest':
            agy_customizations.Snapshot(control, home=tmp_path, cwd=tmp_path).manifest(
                source / name, control / 'result', base=tmp_path)
            observed = (control / 'result').read_text()
        elif kind == 'hooks':
            _, observed = agy_runtime._security_profile(source, control)
            observed = json.dumps(observed)
        else:
            observed = codex_runtime._model_metadata(source, control, 'test')
            if kind == 'model-catalog':
                observed = (control / 'model_catalog.json').read_text()
            observed = str(observed)
        assert 'substituted' not in observed and '999' not in observed
    except (ValueError, OSError):
        pass
    assert swapped


def test_helper_snapshot_preserves_executable_and_directory_modes_above_16mib(tmp_path):
    import hashlib
    import stat
    source, target = tmp_path / 'bin', tmp_path / 'copy'
    source.mkdir(mode=0o700)
    helper = source / 'webm_encoder'
    with helper.open('wb') as stream:
        stream.write(b'approved helper')
        stream.truncate(16818467)
    helper.chmod(0o751)
    control_files.SourceCopy().copy(source, target)
    assert hashlib.sha256((target / helper.name).read_bytes()).digest() == hashlib.sha256(helper.read_bytes()).digest()
    assert stat.S_IMODE((target / helper.name).stat().st_mode) == 0o751
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.parametrize('moment', ['during-copy', 'after-eof'])
def test_instruction_snapshot_refuses_equal_metadata_content_change(tmp_path, monkeypatch, moment):
    source, target = tmp_path / 'SKILL.md', tmp_path / 'frozen'
    source.write_bytes(b'a' * 65536 + b'b' * 65536)
    before = source.stat()
    read, fstat, stat = os.read, os.fstat, os.stat
    mutated = False

    def same_tick(info):
        return before if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino) else info

    def racing_read(fd, size):
        nonlocal mutated
        content = read(fd, size)
        if (not mutated and fstat(fd).st_ino == before.st_ino
                and (bool(content) if moment == 'during-copy' else not content)):
            mutated = True
            source.write_bytes(b'c' * before.st_size)
        return content

    monkeypatch.setattr(os, 'read', racing_read)
    monkeypatch.setattr(os, 'fstat', lambda *args, **kwargs: same_tick(fstat(*args, **kwargs)))
    monkeypatch.setattr(os, 'stat', lambda *args, **kwargs: same_tick(stat(*args, **kwargs)))
    with pytest.raises(ValueError, match='changed'):
        snapshot(tmp_path).content(source, target)
    assert mutated
