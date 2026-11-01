"""Directory-input substitution, using metadata and synthetic content only."""
import os
from pathlib import Path
import shutil

import pytest


def test_removed_codex_discovery_directory_reports_its_absolute_path(tmp_path, monkeypatch):
    from maestro.backends import codex_runtime, control_files
    tree, reports, native = (tmp_path / name for name in ('tree', 'reports', 'native'))
    for path in (tree, reports, native):
        path.mkdir()
    (native / 'auth.json').write_text('synthetic login')
    discovery = tmp_path / '.codex'
    discovery.mkdir()
    verify = control_files.verify_directories
    removed = []
    def remove_before_verification(records):
        if not removed and any(spelling == str(discovery) for spelling, _ in records):
            discovery.rmdir()
            removed.append(str(discovery))
        return verify(records)
    monkeypatch.setattr(control_files, 'verify_directories', remove_before_verification)
    with pytest.raises(FileNotFoundError) as failure:
        codex_runtime.prepare(cwd=tree, workspace=reports, native_home=native,
            host_home=tmp_path, model='gpt-6-luna', effort=None, write_roots=())
    assert removed == [str(discovery)]
    assert failure.value.filename == '.codex'
    assert any(str(discovery) in note for note in getattr(failure.value, '__notes__', ()))

from maestro.backends import agy_runtime, codex_runtime, control_files
from tests.test_agy_runtime import installation


def test_codex_omission_inventory_ignores_disappearing_unrelated_cache(tmp_path, monkeypatch):
    root = tmp_path / 'native'
    root.mkdir()
    (root / 'AGENTS.md').write_text('masked instructions')
    cache = root / 'memories_1.sqlite-shm'
    cache.touch()
    original = os.listdir
    visited = []
    def listdir(fd):
        names = original(fd)
        if isinstance(fd, int) and os.fstat(fd).st_ino == root.stat().st_ino and cache.exists():
            cache.unlink()
            visited.append(True)
        return names
    monkeypatch.setattr(os, 'listdir', listdir)
    inventory = codex_runtime._omission_inventory(root)
    assert visited
    assert inventory['omitted_paths'] == ['AGENTS.md']


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_omission_inventory_traverses_the_pinned_plugin_directory(tmp_path, monkeypatch, replacement):
    root, hostile = tmp_path / 'config', tmp_path / 'hostile'
    (root / 'plugins/approved').mkdir(parents=True)
    (hostile / 'substituted').mkdir(parents=True)
    inode = (root / 'plugins').stat().st_ino
    original_rglob, original_listdir = Path.rglob, os.listdir
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            (root / 'plugins').rename(root / 'saved-plugins')
            if replacement == 'directory':
                hostile.rename(root / 'plugins')
            else:
                (root / 'plugins').symlink_to(hostile, target_is_directory=True)
    def rglob(path, *args, **kwargs):
        if path == root / 'plugins':
            swap()
        return original_rglob(path, *args, **kwargs)
    def listdir(path):
        if isinstance(path, int) and os.fstat(path).st_ino == inode:
            swap()
        return original_listdir(path)
    monkeypatch.setattr(Path, 'rglob', rglob)
    monkeypatch.setattr(os, 'listdir', listdir)
    try:
        result = agy_runtime._omission_inventory(root)
    except (ValueError, OSError):
        pass
    else:
        assert 'plugins/substituted' not in result['omitted_paths']
        assert 'plugins/approved' in result['omitted_paths']
    assert swapped


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_omission_inventory_json_uses_its_pinned_root(tmp_path, monkeypatch, replacement):
    root, hostile = tmp_path / 'config', tmp_path / 'hostile'
    for path, name in ((root, 'approved'), (hostile, 'substituted')):
        path.mkdir()
        (path / 'mcp_config.json').write_text('{"mcpServers":{"' + name + '":{}}}')
    original_file, original_open = control_files.file, os.open
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(tmp_path / 'saved-config')
            if replacement == 'directory':
                hostile.rename(root)
            else:
                root.symlink_to(hostile, target_is_directory=True)
    def file(path):
        result = original_file(path)
        if path == root / 'mcp_config.json':
            swap()
        return result
    def open_at(path, flags, *args, **kwargs):
        if Path(path).name == 'mcp_config.json' and 'dir_fd' in kwargs:
            swap()
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(control_files, 'file', file)
    monkeypatch.setattr(os, 'open', open_at)
    try:
        result = agy_runtime._omission_inventory(root)
    except (ValueError, OSError):
        pass
    else:
        assert result['omitted_mcp_servers'] in (['approved'], [])
    assert swapped


@pytest.mark.parametrize('location', ['home', 'state'])
@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_native_directory_enumeration_cannot_adopt_replacement_before_mount_capture(installation, monkeypatch, location, replacement):
    home, tree, report = installation
    run = agy_runtime.prepare(cwd=tree, workspace=report, home=home, write_roots=(tree,))
    source = home / '.gemini' if location == 'home' else home / '.gemini/antigravity-cli'
    credential = home / '.gemini/antigravity-cli/antigravity-oauth-token'
    approved = credential.stat()
    hostile = tree.parent / 'hostile'
    shutil.copytree(source, hostile)
    inode = source.stat().st_ino
    original_iterdir, original_listdir = Path.iterdir, os.listdir
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(source.with_name('saved-' + source.name))
            if replacement == 'directory':
                hostile.rename(source)
            else:
                source.symlink_to(hostile, target_is_directory=True)
    def iterdir(path):
        if path == source:
            swap()
        return original_iterdir(path)
    def listdir(path):
        if isinstance(path, int) and os.fstat(path).st_ino == inode:
            swap()
        return original_listdir(path)
    monkeypatch.setattr(Path, 'iterdir', iterdir)
    monkeypatch.setattr(os, 'listdir', listdir)
    try:
        try:
            argv = run.wrap(['/bin/true'])
        except (ValueError, OSError):
            pass
        else:
            indexes = [str(i) for i, value in enumerate(argv) if value == '--ro-bind' and argv[i+1] == str(credential)]
            assert indexes
            assert all(argv.identities[i][:2] == [approved.st_dev, approved.st_ino] for i in indexes)
        assert swapped
    finally:
        run.cleanup()


@pytest.mark.parametrize('backend', ['agy', 'codex'])
@pytest.mark.parametrize('stage', ['native', 'state'])
def test_preparation_refuses_plain_directory_replacement_after_validation(
        installation, monkeypatch, backend, stage):
    home, tree, report = installation
    native = home / ('.gemini/antigravity-cli' if backend == 'agy' else '.codex')
    if backend == 'codex':
        native.mkdir()
        (native / 'auth.json').write_text('synthetic-approved')
    state = report / ('.agy-runtime' if backend == 'agy' else '.codex-runtime') / 'state'
    # Persistent private state already exists on a later preparation.
    state.mkdir(parents=True)
    (state / 'approved-state').write_text('retained')
    victim = native if stage == 'native' else state
    hostile = tree.parent / 'replacement'
    shutil.copytree(victim, hostile)
    (hostile / 'substituted').write_text('untrusted')
    real_directory = control_files.directory
    swapped = False
    def directory(path):
        nonlocal swapped
        result = real_directory(path)
        if Path(path) == victim and not swapped:
            swapped = True
            victim.rename(victim.with_name('saved-' + victim.name))
            hostile.rename(victim)
        return result
    monkeypatch.setattr(control_files, 'directory', directory)
    run = None
    try:
        with pytest.raises((ValueError, OSError)):
            if backend == 'agy':
                run = agy_runtime.prepare(cwd=tree, workspace=report, home=home, write_roots=(tree,))
            else:
                run = codex_runtime.prepare(cwd=tree, workspace=report, native_home=native,
                    host_home=home, write_roots=(tree,), model='test', effort='low')
            run.wrap(['/bin/true'])
    finally:
        if run is not None:
            run.cleanup()
    assert swapped


def test_inventory_refuses_leaf_substitution_even_when_restored_after_open(tmp_path, monkeypatch):
    root = tmp_path / 'config'
    root.mkdir()
    leaf = root / 'mcp_config.json'
    leaf.write_text('{"mcpServers":{"approved":{}}}')
    hostile = root / 'hostile.json'
    hostile.write_text('{"mcpServers":{"substituted":{}}}')
    original_open = os.open
    swapped = False
    def open_at(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path).name == leaf.name and 'dir_fd' in kwargs and not swapped:
            swapped = True
            leaf.rename(root / 'saved')
            hostile.rename(leaf)
            fd = original_open(path, flags, *args, **kwargs)
            leaf.rename(hostile)
            (root / 'saved').rename(leaf)
            return fd
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', open_at)
    try:
        result = agy_runtime._omission_inventory(root)
    except (ValueError, OSError):
        pass
    else:
        assert result['omitted_mcp_servers'] == ['approved']
    assert swapped


@pytest.mark.parametrize('backend', ['agy', 'codex'])
@pytest.mark.parametrize('location', ['native', 'state', 'control'])
def test_prepared_run_refuses_pre_mount_directory_replacement(installation, backend, location):
    home, tree, report = installation
    if backend == 'agy':
        run = agy_runtime.prepare(cwd=tree, workspace=report, home=home, write_roots=(tree,))
        native = home / '.gemini/antigravity-cli'
    else:
        native = home / '.codex'
        native.mkdir()
        (native / 'auth.json').write_text('synthetic')
        run = codex_runtime.prepare(cwd=tree, workspace=report, native_home=native,
            host_home=home, write_roots=(tree,), model='test', effort='low')
    victim = {'native': native, 'state': run.state, 'control': run.control}[location]
    hostile = tree.parent / 'replacement'
    shutil.copytree(victim, hostile)
    victim.rename(victim.with_name('saved-' + victim.name))
    hostile.rename(victim)
    try:
        with pytest.raises((ValueError, OSError)):
            run.wrap(['/bin/true'])
    finally:
        run.cleanup()
