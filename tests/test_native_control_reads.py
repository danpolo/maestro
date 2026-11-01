"""Ordinary control leaves must stay stable during preparation reads."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from maestro.backends import agy_customizations, agy_runtime, codex_runtime, control_files, git_staging


@pytest.mark.parametrize('kind', ['manifest', 'hooks', 'model-config', 'model-catalog'])
@pytest.mark.parametrize('mutation', ['in-place', 'replacement'])
def test_preparation_refuses_ordinary_leaf_mutation_during_read(tmp_path, monkeypatch, kind, mutation):
    source, control = tmp_path / 'source', tmp_path / 'control'
    source.mkdir()
    control.mkdir()
    name = {'manifest': 'skills.json', 'hooks': 'hooks.json',
            'model-config': 'config.toml', 'model-catalog': 'model_catalog.json'}[kind]
    if kind == 'model-config':
        good, bad = 'model_context_window = 100\n', 'model_context_window = 999\n'
    elif kind == 'model-catalog':
        good = json.dumps({'models': [{'slug': 'test', 'context_window': 100}]})
        bad = good.replace('100', '999')
    else:
        good, bad = '{}', '{"unexpected": {}}'
    leaf = source / name
    leaf.write_text(good)
    opened = control_files.open_file
    mutated = False

    class MutatingRead:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def read(self, *args):
            nonlocal mutated
            content = self.stream.read(*args)
            if mutated:
                return content
            mutated = True
            if mutation == 'replacement':
                other = source / 'replacement'
                other.write_text(bad)
                other.replace(leaf)
            else:
                leaf.write_text(bad)
            return content

    @contextmanager
    def racing_open(path, mode='w'):
        with opened(path, mode) as stream:
            yield MutatingRead(stream) if Path(path) == leaf and mode == 'rb' else stream

    monkeypatch.setattr(control_files, 'open_file', racing_open)
    with pytest.raises(ValueError) as caught:
        if kind == 'manifest':
            agy_customizations.Snapshot(control, home=tmp_path, cwd=tmp_path).manifest(
                leaf, control / 'result', base=tmp_path)
        elif kind == 'hooks':
            agy_runtime._security_profile(source, control)
        else:
            codex_runtime._model_metadata(source, control, 'test')
    error = caught.value
    messages = []
    while error is not None:
        messages.append(str(error))
        error = error.__cause__
    assert any('changed' in message for message in messages), messages
    assert mutated
    assert not (control / 'result').exists()
    assert not (control / 'model_catalog.json').exists()


@pytest.mark.parametrize('consumer', ['omission-inventory', 'git-metadata'])
def test_pinned_directory_read_refuses_equal_metadata_content_change(tmp_path, monkeypatch, consumer):
    root = tmp_path / 'source'
    root.mkdir()
    leaf = root / ('mcp_config.json' if consumer == 'omission-inventory' else 'config')
    good = b'{"mcpServers":{"approved":{}}}'
    bad = good.replace(b'approved', b'modified')
    leaf.write_bytes(good)
    before = leaf.stat()
    fdopen, fstat, stat = os.fdopen, os.fstat, os.stat
    mutated = False

    # The actual host produced equal metadata for same-size rewrites. Freeze
    # only this synthetic inode's metadata to reproduce that case deterministically.
    def same_tick(info):
        return before if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino) else info

    class MutatingRead:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, *args):
            nonlocal mutated
            content = self.stream.read(*args)
            if not mutated:
                mutated = True
                leaf.write_bytes(bad)
            return content

    def racing_fdopen(fd, *args, **kwargs):
        info = fstat(fd)
        stream = fdopen(fd, *args, **kwargs)
        return MutatingRead(stream) if info.st_ino == before.st_ino else stream

    monkeypatch.setattr(os, 'fdopen', racing_fdopen)
    monkeypatch.setattr(os, 'fstat', lambda *args, **kwargs: same_tick(fstat(*args, **kwargs)))
    monkeypatch.setattr(os, 'stat', lambda *args, **kwargs: same_tick(stat(*args, **kwargs)))
    with pytest.raises(ValueError, match='changed'):
        if consumer == 'omission-inventory':
            agy_runtime._omission_inventory(root)
        else:
            git_staging._inventory(root)
    assert mutated
