"""Bounded private-object validation and immutable import before task-ref CAS.

Native alternates, indexes, configuration and refs are never validation authority.
Only a new strictly checked reachable pack and its rebuilt index may be imported.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from maestro.backends import control_files

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 20000
_LIMITED_EXEC = '''import os,resource,sys
resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (128*1024*1024, 128*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU, (20, 21))
os.execv(sys.argv[1], sys.argv[1:])
'''


def _environment():
    return {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/nonexistent',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1',
            'GIT_TERMINAL_PROMPT': '0'}


class ObjectValidation:
    def __init__(self, transaction):
        self.transaction = transaction
        self.shared = transaction.common / 'objects'
        self.directory = control_files.temporary_directory(transaction.directory)
        self.database = self.directory / 'git'
        for path in (self.database, self.database / 'objects', self.database / 'refs'):
            control_files.mkdir(path)
        config = '[core]\nrepositoryformatversion = 0\nbare = true\n'
        if len(transaction.start_tip) == 64:
            config = config.replace('version = 0', 'version = 1')
            config += '[extensions]\nobjectFormat = sha256\n'
        control_files.write(self.database / 'config', config)
        control_files.write(self.database / 'HEAD', 'ref: refs/heads/unused\n')
        self.objects = self.directory / 'objects'
        # Snapshot ALL entries before discarding native authority. Special files,
        # links, mutation, depth, entry and aggregate byte excess fail closed.
        control_files.SourceCopy(max_bytes=MAX_BYTES, max_files=MAX_FILES).copy(
            transaction.directory / 'objects', self.objects)
        info = self.objects / 'info'
        if info.exists():
            shutil.rmtree(info)
        control_files.mkdir(info)
        pack = self.objects / 'pack'
        control_files.mkdir(pack, exist_ok=True)
        width = len(transaction.start_tip)
        for path in self.objects.iterdir():
            if path.name in ('pack', 'info'):
                if not path.is_dir():
                    raise ValueError('private Git object metadata must be directories')
            elif not re.fullmatch('[0-9a-f]{2}', path.name) or not path.is_dir():
                raise ValueError('unsupported private Git object entry')
            else:
                for leaf in path.iterdir():
                    if not leaf.is_file() or not re.fullmatch('[0-9a-f]{' + str(width-2) + '}', leaf.name):
                        raise ValueError('unsupported loose Git object entry')
        packs = []
        for path in pack.iterdir():
            if not path.is_file() or not re.fullmatch('pack-[0-9a-f]{' + str(width) + r'}\.(pack|idx|rev|bitmap|keep|mtimes|promisor)', path.name):
                raise ValueError('unsupported private Git pack entry')
            if path.suffix == '.pack':
                packs.append(path)
            else:
                path.unlink()  # Rebuild indexes; ignore all native pack accelerators.
        # No inherited external alternates are allowed at this durable boundary.
        for name in ('alternates', 'http-alternates'):
            try:
                content = control_files.read(self.shared / 'info' / name)
            except FileNotFoundError:
                continue
            if content.strip():
                raise ValueError('external Git object alternates cannot be published durably')
        for path in sorted(packs):
            # Rebuild from pack bytes before graph traversal. Full strict graph
            # validation follows once every pack can resolve cross-pack links.
            self.run('index-pack', '--index-version=2', str(path), objects=self.objects)
        self.quarantine = self.directory / 'quarantine'
        for path in (self.quarantine, self.quarantine / 'pack', self.quarantine / 'info'):
            control_files.mkdir(path)

    def run(self, *args, objects, input=b''):
        """Isolated bare Git, pinned inputs, bounded I/O/deadline and child reap."""
        with ExitStack() as stack:
            paths = (self.database, objects, self.shared)
            fds = [stack.enter_context(control_files.directory_handle(p)) for p in paths]
            env = _environment()
            env.update(GIT_OBJECT_DIRECTORY=f'/proc/self/fd/{fds[1]}',
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=f'/proc/self/fd/{fds[2]}')
            stream = stack.enter_context(tempfile.TemporaryFile())
            stream.write(input)
            stream.seek(0)
            argv = ['/usr/bin/git', '--no-replace-objects', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'gc.auto=0', '-c', 'pack.threads=1',
                    '--git-dir=' + f'/proc/self/fd/{fds[0]}', *args]
            # Apply limits in a fresh interpreter before Git exec, avoiding
            # Python preexec callbacks in a possibly multithreaded controller.
            process = subprocess.Popen([sys.executable, '-I', '-c', _LIMITED_EXEC, *argv], env=env, stdin=stream,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=tuple(fds),
                start_new_session=True)
            buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
            deadline = time.monotonic() + 20
            try:
                with selectors.DefaultSelector() as selector:
                    for pipe in buffers:
                        selector.register(pipe, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Git object validation deadline exceeded')
                        for key, _ in selector.select(min(.1, remaining)):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            buffer = buffers[key.fileobj]
                            buffer.extend(chunk)
                            if len(buffer) > (MAX_BYTES if key.fileobj is process.stdout else 1024*1024):
                                raise ValueError('Git object validation output limit exceeded')
                process.wait(timeout=max(.001, deadline-time.monotonic()))
                if process.returncode:
                    raise ValueError('Git object validation refused ' + args[0] + ': ' +
                                     buffers[process.stderr].decode('utf-8', errors='replace')[:4096])
                # The handles stayed authoritative throughout Git execution.
                for path, fd in zip(paths, fds):
                    with control_files.directory_handle(path) as current:
                        if control_files.identity(os.fstat(fd)) != control_files.identity(os.fstat(current)):
                            raise ValueError('Git object validation directory changed')
                return bytes(buffers[process.stdout])
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for pipe in buffers:
                    pipe.close()
                process.wait(timeout=5)

    def validate(self, tip):
        self.run('fsck', '--strict', '--no-reflogs', '--no-dangling', tip, objects=self.objects)
        packed = self.run('pack-objects', '--stdout', '--revs', '--no-reuse-object', '--no-reuse-delta',
            objects=self.objects, input=(tip+'\n^'+self.transaction.start_tip+'\n').encode())
        reply = self.run('index-pack', '--strict', '--stdin', objects=self.quarantine, input=packed)
        width = len(tip)
        match = re.fullmatch(rb'pack\t([0-9a-f]{' + str(width).encode() + rb'})\n', reply)
        if match is None:
            raise ValueError('Git strict quarantine returned an invalid pack identity')
        self.pack_name = 'pack-' + match[1].decode()
        self.validated = {self.pack_name + extension: hashlib.sha256(control_files.read(
            self.quarantine / 'pack' / (self.pack_name + extension), limit=MAX_BYTES)).digest()
            for extension in ('.pack', '.idx')}
        if self.validated[self.pack_name + '.pack'] != hashlib.sha256(packed).digest():
            raise ValueError('strict quarantine pack differs from generated object bytes')
        self.run('fsck', '--strict', '--no-reflogs', '--no-dangling', tip, objects=self.quarantine)

    def publish(self):
        """Caller holds the shared publication guard through expected-old CAS."""
        if not hasattr(self, 'pack_name'):
            raise ValueError('Git objects were not strictly validated')
        # Read BOTH artifacts through pinned nonsymlink paths and match their
        # strictly validated bytes before making any shared-store write. Import
        # uses these immutable buffers, never a later reread of quarantine.
        contents = {name: control_files.read(self.quarantine / 'pack' / name, limit=MAX_BYTES)
                    for name in self.validated}
        if any(hashlib.sha256(content).digest() != self.validated[name]
               for name, content in contents.items()):
            raise ValueError('strictly validated Git quarantine content changed')
        transaction = self.transaction
        # Durable intent lets an interrupted transaction release ONLY its keep.
        receipt = {'pack': self.pack_name, 'journal': transaction.job, 'temporary': [],
                   'keep': None, 'keep_linked': False}
        def save_intent():
            control_files.write(transaction.directory / 'object-import.json', json.dumps(receipt), durable=True)
        save_intent()
        destination = self.shared / 'pack'
        control_files.mkdir(destination, exist_ok=True, durable=True)
        with control_files.directory_handle(destination) as fd:
            keep = self.pack_name + '.keep'
            def publish_file(name, content, *, protection=False):
                temporary = '.maestro-import-' + uuid.uuid4().hex
                opened = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o444, dir_fd=fd)
                try:
                    with os.fdopen(opened, 'wb') as stream:
                        identity = control_files.identity(os.fstat(stream.fileno()))
                        receipt['temporary'].append({'name': temporary,
                            'identity': identity})
                        if protection:
                            receipt['keep'] = identity
                        # Record ownership before a link can raise its count to
                        # two. Recovery may unlink ONLY this exact inode/name.
                        save_intent()
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                    except FileExistsError:
                        if protection:
                            # The link never became ours. A crash before this
                            # update still cannot grant deletion of that inode.
                            receipt['keep'] = None
                            save_intent()
                            control_files.read_at(fd, name)
                        elif control_files.read_at(fd, name, limit=MAX_BYTES) != content:
                            raise ValueError('immutable Git object publication collision')
                    else:
                        if protection:
                            receipt['keep_linked'] = True
                            save_intent()
                finally:
                    os.unlink(temporary, dir_fd=fd)
                os.fsync(fd)
            try:
                control_files.read_at(fd, keep)
            except FileNotFoundError:
                publish_file(keep, (transaction.job+'\n').encode(), protection=True)
            for extension in ('.pack', '.idx'):
                name = self.pack_name + extension
                publish_file(name, contents[name])
        with control_files.directory_handle(self.shared) as fd:
            os.fsync(fd)


def release_keep(transaction):
    """Shared guard required; never remove another invocation's GC protection."""
    try:
        receipt = json.loads(control_files.read(transaction.directory / 'object-import.json'))
    except FileNotFoundError:
        return
    if not isinstance(receipt, dict):
        raise ValueError('invalid Git object import receipt')
    pack = receipt.get('pack')
    temporary = receipt.get('temporary')
    keep_identity = receipt.get('keep')
    linked = receipt.get('keep_linked', False)
    def valid_identity(value):
        return (isinstance(value, list) and len(value) == 3
                and all(type(n) is int and n >= 0 for n in value)
                and value[2] == stat.S_IFREG)
    if (receipt.get('journal') != transaction.job or not isinstance(pack, str)
            or not isinstance(temporary, list) or len(temporary) > 3
            or type(linked) is not bool or (linked and keep_identity is None)
            or (keep_identity is not None and not valid_identity(keep_identity))
            or not re.fullmatch(r'pack-([0-9a-f]{40}|[0-9a-f]{64})', pack)):
        raise ValueError('invalid Git object import receipt')
    names = set()
    for entry in temporary:
        if (not isinstance(entry, dict) or set(entry) != {'name', 'identity'}
                or not isinstance(entry['name'], str)
                or not re.fullmatch(r'\.maestro-import-[0-9a-f]{32}', entry['name'])
                or entry['name'] in names or not valid_identity(entry['identity'])):
            raise ValueError('invalid Git object import temporary receipt')
        names.add(entry['name'])
    destination = transaction.common / 'objects/pack'
    # Intent is durable before the destination exists. Absence cannot confer
    # unlink rights, and must not prevent settling/retrying that early crash.
    with control_files.directory_handle(destination.parent) as parent:
        try:
            opened = os.open('pack', os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return
    with ExitStack() as stack:
        stack.callback(os.close, opened)
        fd = opened
        retained = []
        for entry in temporary:
            name = entry['name']
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if control_files.identity(info) != entry['identity']:
                raise ValueError('Git object import temporary identity changed')
            retained.append(name)
        owned_keep = False
        if keep_identity is not None:
            try:
                current = os.stat(pack+'.keep', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                owned_keep = control_files.identity(current) == keep_identity
                if linked and not owned_keep:
                    raise ValueError('Git object import keep identity changed')
        for name in retained:
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
        if not owned_keep:
            return
        try:
            content = control_files.read_at(fd, pack+'.keep')
        except FileNotFoundError:
            return
        if content != (transaction.job+'\n').encode():
            raise ValueError('Git object import keep content changed')
        os.unlink(pack+'.keep', dir_fd=fd)
        os.fsync(fd)
