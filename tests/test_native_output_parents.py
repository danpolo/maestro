"""Synthetic output-parent substitution at actual filesystem operation seams."""
import os
from pathlib import Path
import stat

import pytest

from maestro.backends import control_files, launch_state
from maestro.backends.base import LaunchSpec
from tests.test_agy_runtime import installation
from tests.test_git_staging import linked


@pytest.mark.parametrize('kind', ['file', 'directory', 'mode'])
def test_source_copy_never_creates_or_chmods_through_substituted_output_parent(tmp_path, monkeypatch, kind):
    source, output, outside = (tmp_path / name for name in ('source', 'output', 'outside'))
    output.mkdir()
    outside.mkdir()
    target = output / 'frozen'
    if kind == 'directory':
        source.mkdir()
        (source / 'instruction').write_text('approved')
    else:
        source.write_text('approved')
        source.chmod(0o751)
    if kind == 'mode':
        (outside / 'frozen').write_text('unrelated')
        (outside / 'frozen').chmod(0o600)
    original_open, original_os_open = Path.open, os.open
    original_mkdir, original_chmod, original_fchmod = os.mkdir, Path.chmod, os.fchmod
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            output.rename(tmp_path / 'saved-output')
            output.symlink_to(outside, target_is_directory=True)
    def open_path(path, mode='r', *args, **kwargs):
        if kind == 'file' and path == target and 'x' in mode:
            swap()
        return original_open(path, mode, *args, **kwargs)
    def open_at(path, flags, *args, **kwargs):
        if kind == 'file' and Path(path).name == target.name and flags & os.O_CREAT:
            swap()
        return original_os_open(path, flags, *args, **kwargs)
    def mkdir(path, *args, **kwargs):
        if kind == 'directory' and Path(path).name == target.name:
            swap()
        return original_mkdir(path, *args, **kwargs)
    def chmod(path, *args, **kwargs):
        if kind == 'mode' and path == target:
            swap()
        return original_chmod(path, *args, **kwargs)
    def fchmod(fd, mode):
        if kind == 'mode':
            swap()
        return original_fchmod(fd, mode)
    monkeypatch.setattr(Path, 'open', open_path)
    monkeypatch.setattr(os, 'open', open_at)
    monkeypatch.setattr(os, 'mkdir', mkdir)
    monkeypatch.setattr(Path, 'chmod', chmod)
    monkeypatch.setattr(os, 'fchmod', fchmod)
    try:
        control_files.SourceCopy().copy(source, target)
    except (ValueError, OSError):
        pass
    assert swapped
    if kind == 'mode':
        assert (outside / 'frozen').read_text() == 'unrelated'
        assert stat.S_IMODE((outside / 'frozen').stat().st_mode) == 0o600
    else:
        assert list(outside.iterdir()) == []


@pytest.mark.parametrize('backend', ['codex', 'agy'])
@pytest.mark.parametrize('operation', ['state-directory', 'invocation-directory', 'configuration', 'job'])
def test_runtime_preparation_never_writes_through_substituted_output_parent(installation, monkeypatch, backend, operation):
    from maestro.backends import agy_runtime, codex_runtime
    home, tree, report = installation
    native = home / '.codex'
    native.mkdir()
    (native / 'auth.json').write_text('synthetic')
    runtime = codex_runtime if backend == 'codex' else agy_runtime
    outside = tree.parent / 'outside'
    outside.mkdir()
    original_mkdir, original_open, original_os_open = os.mkdir, Path.open, os.open
    substituted = None
    def swap(path):
        nonlocal substituted
        if substituted is None:
            substituted = path
            path.rename(path.with_name('saved-' + path.name))
            path.symlink_to(outside, target_is_directory=True)
    def absolute(path, kwargs):
        path = Path(path)
        return path if path.is_absolute() else Path(os.readlink(f'/proc/self/fd/{kwargs["dir_fd"]}')) / path
    def mkdir(path, *args, **kwargs):
        full = absolute(path, kwargs)
        if ((operation == 'state-directory' and full.name == 'state' and full.parent.name == runtime.RUNTIME_DIR)
                or (operation == 'invocation-directory' and full.name.startswith('call-'))):
            swap(full.parent)
        return original_mkdir(path, *args, **kwargs)
    leaf = 'config.toml' if backend == 'codex' else 'policy.json'
    if operation == 'job':
        leaf = 'job.json'
    def open_path(path, mode='r', *args, **kwargs):
        if operation in ('configuration', 'job') and path.name == leaf and 'w' in mode:
            swap(path.parent)
        return original_open(path, mode, *args, **kwargs)
    def open_at(path, flags, *args, **kwargs):
        if operation in ('configuration', 'job') and Path(path).name == leaf and flags & os.O_CREAT:
            swap(absolute(path, kwargs).parent)
        return original_os_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'mkdir', mkdir)
    monkeypatch.setattr(Path, 'open', open_path)
    monkeypatch.setattr(os, 'open', open_at)
    run = None
    try:
        kwargs = dict(cwd=tree, workspace=report, write_roots=(tree,))
        if backend == 'codex':
            run = runtime.prepare(**kwargs, host_home=home, model='test', effort='low')
            if operation == 'job':
                run.launcher(binary='codex', prompt='test')
        else:
            run = runtime.prepare(**kwargs, home=home)
            if operation == 'job':
                run.launcher(['agy'], log_file=report / 'impl.log', mode='w')
    except (ValueError, OSError):
        pass
    finally:
        if run is not None:
            run.cleanup()
    assert substituted is not None
    assert list(outside.iterdir()) == []


def test_durable_launch_publication_uses_the_same_pinned_parent_as_its_write(tmp_path, monkeypatch):
    report, outside = tmp_path / 'report', tmp_path / 'outside'
    report.mkdir()
    outside.mkdir()
    (outside / 'launch_spec.tmp').write_text('unrelated temporary')
    (outside / launch_state.FILENAME).write_text('unrelated state')
    original_replace = os.replace
    swapped = False
    def replace(source, target, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            report.rename(tmp_path / 'saved-report')
            report.symlink_to(outside, target_is_directory=True)
        return original_replace(source, target, *args, **kwargs)
    monkeypatch.setattr(os, 'replace', replace)
    spec = LaunchSpec(task_id='test', session_id='test', model='test', brief='test',
                      workspace=report, worktree=tmp_path)
    try:
        launch_state.write(spec)
    except (ValueError, OSError):
        pass
    assert swapped
    assert (outside / launch_state.FILENAME).read_text() == 'unrelated state'
    assert (outside / 'launch_spec.tmp').read_text() == 'unrelated temporary'

@pytest.mark.parametrize('backend', ['codex', 'agy'])
def test_driver_launcher_mode_cannot_change_an_outside_file(installation, monkeypatch, backend):
    from maestro.backends import agy_runtime, codex_runtime
    from maestro.backends.antigravity import AntigravityBackend
    from maestro.backends.codex import CodexBackend
    home, tree, report = installation
    native = home / '.codex'
    native.mkdir()
    (native / 'auth.json').write_text('synthetic')
    driver = (CodexBackend(codex_home=native) if backend == 'codex'
              else AntigravityBackend(gemini_home=home / '.gemini'))
    monkeypatch.setattr(driver, '_new_window' if backend == 'codex' else '_open_window', lambda *a: None)
    outside = tree.parent / 'outside'
    outside.mkdir()
    (outside / 'launch.py').write_text('unrelated')
    (outside / 'launch.py').chmod(0o600)
    original_chmod, original_fchmod = Path.chmod, os.fchmod
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            report.rename(report.with_name('saved-report'))
            report.symlink_to(outside, target_is_directory=True)
    def chmod(path, mode, *args, **kwargs):
        if path == report / 'launch.py':
            swap()
        return original_chmod(path, mode, *args, **kwargs)
    def fchmod(fd, mode):
        if Path(os.readlink(f'/proc/self/fd/{fd}')).name == 'launch.py':
            swap()
        return original_fchmod(fd, mode)
    monkeypatch.setattr(Path, 'chmod', chmod)
    monkeypatch.setattr(os, 'fchmod', fchmod)
    try:
        driver.launch(LaunchSpec('test', 'test', 'gpt-6-luna', 'test', report, tree))
    except (ValueError, OSError):
        pass
    assert swapped
    assert stat.S_IMODE((outside / 'launch.py').stat().st_mode) == 0o600


def test_git_preparation_baseline_never_writes_through_replaced_output_parent(linked, monkeypatch):
    from maestro.backends import git_staging
    _, tree, report, _ = linked
    outside = report.parent / 'outside'
    outside.mkdir()
    original_open, original_os_open, original_replace = Path.open, os.open, os.replace
    swapped = False
    def swap(path):
        nonlocal swapped
        if not swapped:
            swapped = True
            path.rename(path.with_name('saved-output'))
            path.symlink_to(outside, target_is_directory=True)
    def open_path(path, mode='r', *args, **kwargs):
        if path.name == 'baseline-git' and 'w' in mode:
            swap(path.parent)
        return original_open(path, mode, *args, **kwargs)
    def open_at(path, flags, *args, **kwargs):
        if Path(path).name == 'baseline-git' and flags & os.O_CREAT:
            parent = Path(os.readlink(f'/proc/self/fd/{kwargs["dir_fd"]}'))
            swap(parent)
        return original_os_open(path, flags, *args, **kwargs)
    def replace(source, target, *args, **kwargs):
        if Path(target).name == 'baseline-git':
            parent = Path(os.readlink(f'/proc/self/fd/{kwargs["dst_dir_fd"]}'))
            swap(parent)
        return original_replace(source, target, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_path)
    monkeypatch.setattr(os, 'open', open_at)
    monkeypatch.setattr(os, 'replace', replace)
    try:
        git_staging.Transaction.prepare(tree, report)
    except (ValueError, OSError):
        pass
    assert swapped
    assert list(outside.iterdir()) == []
