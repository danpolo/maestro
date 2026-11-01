"""Bound private validation before shared writes, using bounded fixture generation."""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import zlib

import pytest

from maestro.backends import git_objects, git_staging
from tests.test_git_staging import linked, git
from tests.test_git_object_validation import staged
from tests.test_private_git_objects import hashes


@pytest.mark.parametrize('output', ['limits', 'stdout', 'stderr', 'file'])
def test_validation_child_enforces_resource_and_output_limits(linked, tmp_path, monkeypatch, output):
    transaction = staged(linked)
    view = git_objects.ObjectValidation(transaction)
    original = subprocess.Popen
    children = []
    scripts = {
        'limits': "import json,resource;print(json.dumps([resource.getrlimit(v) for v in (resource.RLIMIT_AS,resource.RLIMIT_FSIZE,resource.RLIMIT_CPU)]))",
        'stdout': "import sys;sys.stdout.write('x'*65)",
        'stderr': "import sys;sys.stderr.write('x'*(1024*1024+1))",
        'file': "import os,sys;f=open(sys.argv[1],'wb');f.truncate(129*1024*1024)",
    }
    def launch(argv, **kwargs):
        assert argv[4] == '/usr/bin/git'
        # Keep the ACTUAL production limiter and transport/reap code; substitute
        # just its Git executable at this seam to measure each child contract.
        argv = [*argv[:4], sys.executable, '-I', '-c', scripts[output], str(tmp_path/'bounded-output')]
        child = original(argv, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', launch)
    if output == 'stdout':
        monkeypatch.setattr(git_objects, 'MAX_BYTES', 64)
    try:
        if output == 'limits':
            value = json.loads(view.run('--version', objects=view.objects))
            assert value == [[512*1024*1024]*2, [128*1024*1024]*2, [20,21]]
        else:
            with pytest.raises(ValueError, match='limit|refused'):
                view.run('--version', objects=view.objects)
        assert children and all(child.poll() is not None for child in children)
        if output == 'file':
            assert (tmp_path/'bounded-output').stat().st_size <= 128*1024*1024
    finally:
        transaction.cleanup()


@pytest.mark.parametrize('limit', ['bytes', 'entries', 'depth'])
def test_snapshot_limits_refuse_before_shared_publication(linked, monkeypatch, limit):
    repo, tree, _, start = linked
    transaction = staged(linked)
    objects = transaction.directory/'objects'
    if limit == 'bytes':
        monkeypatch.setattr(git_objects, 'MAX_BYTES', 32)
    elif limit == 'entries':
        monkeypatch.setattr(git_objects, 'MAX_FILES', 2)
    else:
        directory = objects/'info'
        for _ in range(65):
            directory = directory/'deep'
        directory.mkdir(parents=True)
    before = hashes(repo/'.git/objects')
    try:
        with pytest.raises(ValueError, match='limit'):
            transaction.finish(success=True)
        assert hashes(repo/'.git/objects') == before
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    finally:
        transaction.cleanup()


def test_strict_fsck_ignores_external_relaxation_of_malformed_commit(linked, monkeypatch, tmp_path):
    repo, tree, _, start = linked
    transaction = staged(linked)
    # Hash-valid but semantically invalid commit. Trusted Git must refuse it
    # even when the inherited/global/shared policy says to ignore missingEmail.
    root = git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    content = (f'tree {root}\nauthor Invalid 1 +0000\ncommitter Invalid 1 +0000\n\nuntrusted\n').encode()
    encoded = b'commit '+str(len(content)).encode()+b'\0'+content
    oid = hashlib.sha1(encoded).hexdigest()
    path = transaction.directory/'objects'/oid[:2]/oid[2:]
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(zlib.compress(encoded))
    global_config = tmp_path/'foreign-config'
    global_config.write_text('[fsck]\nmissingEmail = ignore\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_config))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'fsck.missingEmail')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'ignore')
    git(repo, 'config', 'fsck.missingEmail', 'ignore')
    before = hashes(repo/'.git/objects')
    try:
        with pytest.raises(ValueError, match='missingEmail|badEmail|invalid author'):
            transaction.finish(success=True)
        assert hashes(repo/'.git/objects') == before
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
    finally:
        transaction.cleanup()


def test_compressed_oversized_tree_is_refused_under_production_child_memory_limit(linked):
    repo, tree, _, start = linked
    transaction = staged(linked)
    # Generate 600 MiB of highly compressible input in 64 KiB chunks. The host
    # never holds its expansion. Only capped production Git sees the object.
    size = 600*1024*1024
    header = f'tree {size}\0'.encode()
    compressor = zlib.compressobj()
    digest = hashlib.sha1(header)
    compressed = bytearray(compressor.compress(header))
    chunk = b'\0'*65536
    for _ in range(size//len(chunk)):
        digest.update(chunk)
        compressed.extend(compressor.compress(chunk))
    compressed.extend(compressor.flush())
    oid = digest.hexdigest()
    path = transaction.directory/'objects'/oid[:2]/oid[2:]
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(compressed)
    assert len(compressed) < 1024*1024
    before = hashes(repo/'.git/objects')
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match='memory|alloc|mmap|large|inflate|refused') as error:
            transaction.finish(success=True)
        print('bounded decompression refusal:', str(error.value), 'seconds:', time.monotonic()-started)
        assert hashes(repo/'.git/objects') == before
        assert git(tree, 'rev-parse', 'HEAD').stdout.strip() == start
        assert json.loads(Path(transaction.job).read_text())['state'] == 'aborted'
    finally:
        transaction.cleanup()


def test_worktree_audit_private_object_reads_use_production_child_limits(linked,monkeypatch):
    transaction=staged(linked)
    view=git_objects.ObjectValidation(transaction)
    original=subprocess.Popen
    children=[]
    def launch(argv,**kwargs):
        assert argv[:4]==[sys.executable,'-I','-c',git_objects._LIMITED_EXEC], \
            'worktree audit reads private objects without the production resource limiter'
        script='import json,resource;print(json.dumps([resource.getrlimit(v) for v in (resource.RLIMIT_AS,resource.RLIMIT_FSIZE,resource.RLIMIT_CPU)]))'
        child=original([*argv[:4],sys.executable,'-I','-c',script],**kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess,'Popen',launch)
    try:
        result=git_staging._git(transaction.tree,'--version',object_directory=view.objects)
        assert json.loads(result.stdout)==[[512*1024*1024]*2,[128*1024*1024]*2,[20,21]]
        assert children and all(child.poll() is not None for child in children)
    finally:transaction.cleanup()
