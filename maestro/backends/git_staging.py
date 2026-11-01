"""Private native refs with trusted, expected-old promotion of one task branch.

The journal survives launcher/controller death. A failed call retains its staged
refs and index evidence; the existing task retry/restore owns dirty-tree recovery.
No interrupted invocation is converted into a successful completion here.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from maestro.backends import control_files


class GitStagingError(ValueError):
    """A refused native commit boundary; never a successful canonical commit."""


def _git(tree, *args, check=True, object_directory=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    # Discovery and exec share actual directory handles. Passing validated path
    # strings would let a renamed ancestor redirect a trusted Git subprocess.
    tree = control_files.directory(tree)
    with ExitStack() as stack:
        tree_fd = stack.enter_context(control_files.directory_handle(tree))
        marker = os.stat('.git', dir_fd=tree_fd, follow_symlinks=False)
        if stat.S_ISDIR(marker.st_mode):
            own = tree / '.git'
            own_fd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=tree_fd)
            stack.callback(os.close, own_fd)
        else:
            pointer = control_files.read_at(tree_fd, '.git').decode('utf-8').strip()
            if not pointer.startswith('gitdir: ') or '\n' in pointer:
                raise GitStagingError('invalid worktree Git pointer')
            own = control_files.directory(Path(os.path.abspath(tree / pointer.removeprefix('gitdir: '))))
            own_fd = stack.enter_context(control_files.directory_handle(own))
        common, common_fd = own, own_fd
        try:
            relative = control_files.read_at(own_fd, 'commondir').decode('utf-8').strip()
        except FileNotFoundError:
            pass
        else:
            if not relative or '\n' in relative:
                raise GitStagingError('invalid shared Git pointer')
            common = control_files.directory(Path(os.path.abspath(own / relative)))
            common_fd = stack.enter_context(control_files.directory_handle(common))
        handles = {tree: tree_fd, own: own_fd, common: common_fd}
        if object_directory is not None:
            for path in (object_directory, common / 'objects'):
                handles[path] = stack.enter_context(control_files.directory_handle(path))
            env.update(GIT_OBJECT_DIRECTORY=f'/proc/self/fd/{handles[object_directory]}',
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=f'/proc/self/fd/{handles[common / "objects"]}',
                       GIT_NO_REPLACE_OBJECTS='1')
        def verify_paths():
            for path, fd in handles.items():
                with control_files.directory_handle(path) as current:
                    a, b = os.fstat(fd), os.fstat(current)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise GitStagingError('Git directory identity changed')
        verify_paths()
        env.update(GIT_DIR=f'/proc/self/fd/{own_fd}', GIT_COMMON_DIR=f'/proc/self/fd/{common_fd}',
                   GIT_WORK_TREE=f'/proc/self/fd/{tree_fd}')
        command = ['git', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0', '-C', f'/proc/self/fd/{tree_fd}', *args]
        if object_directory is not None:
            # Audit traversals reach untrusted private objects before fsck. The
            # same fresh-child limits must protect these reads too.
            from maestro.backends.git_objects import _LIMITED_EXEC
            command = [sys.executable, '-I', '-c', _LIMITED_EXEC, '/usr/bin/git', *command[1:]]
        result = subprocess.run(command,
            env=env, pass_fds=tuple(set(handles.values())), capture_output=True, text=True, timeout=20)
        verify_paths()
    if check and result.returncode:
        raise GitStagingError(f'Git staging refused {args[0]}: {result.stderr.strip()}')
    return result


def _read(path):
    """Read bounded regular metadata through pinned nonsymlink ancestors."""
    return control_files.read(path)


def _pointer(path):
    with control_files.open_file(path, 'rb') as stream:
        info = os.fstat(stream.fileno())
        content = stream.read(4097)
        if len(content) > 4096:
            raise GitStagingError('oversized worktree Git pointer')
        return content, (info.st_dev, info.st_ino)


def _inventory(root):
    return _metadata_snapshot(root, contents=True)[0]


def _directories(root):
    return _metadata_snapshot(root, contents=False)[1]


def _metadata_snapshot(root, *, contents):
    """Traverse the pinned tree; path replacement cannot redirect enumeration."""
    files, directories = {}, set()
    total = 0
    entries = 0
    if not root.exists():
        return files, directories
    def signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def visit(fd, relative, depth):
        nonlocal total, entries
        if depth > 64:
            raise GitStagingError('Git metadata directory depth exceeded')
        before = os.fstat(fd)
        names = sorted(os.listdir(fd))
        for name in names:
            entries += 1
            if entries > 10000:
                raise GitStagingError('Git metadata snapshot entry budget exceeded')
            path = relative / name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
                try:
                    if signature(info) != signature(os.fstat(child)):
                        raise GitStagingError('Git metadata directory identity changed')
                    directories.add(str(path))
                    visit(child, path, depth + 1)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise GitStagingError('Git metadata must be single-link regular content')
            elif contents:
                content = control_files.read_at(fd, name)
                total += len(content)
                if total > 32 * 1024 * 1024:
                    raise GitStagingError('Git metadata snapshot byte budget exceeded')
                files[str(path)] = content
            if signature(info) != signature(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                raise GitStagingError('Git metadata entry changed during snapshot')
        if names != sorted(os.listdir(fd)) or signature(before) != signature(os.fstat(fd)):
            raise GitStagingError('Git metadata directory changed during snapshot')
    with control_files.directory_handle(root) as fd:
        visit(fd, Path('.'), 0)
        with control_files.directory_handle(root) as current:
            if signature(os.fstat(fd)) != signature(os.fstat(current)):
                raise GitStagingError('Git metadata root identity changed')
    return files, directories


def _dump(path, value):
    # Journals and lease markers are parent-owned, never a native write grant.
    with control_files.atomic_file(path) as stream:
        json.dump(value, stream, sort_keys=True)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sync_objects(common):
    """Harden existing objects too: update-ref does not write their contents.

    Native Git may have used loose objects or packed them. Sync the bounded local
    store, children before directories, through pinned descriptors. External
    alternates cannot be certified by this local publication boundary.
    """
    root = common / 'objects'
    try:
        if _read(root / 'info/alternates').strip():
            raise GitStagingError('external Git object alternates cannot be published durably')
    except FileNotFoundError:
        pass
    remaining = 100000

    def visit(fd, depth):
        nonlocal remaining
        if depth > 64:
            raise GitStagingError('Git object persistence depth exceeded')
        before = _signature(os.fstat(fd))
        names = sorted(os.listdir(fd))
        for name in names:
            remaining -= 1
            if remaining < 0:
                raise GitStagingError('Git object persistence entry budget exceeded')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            is_directory = stat.S_ISDIR(info.st_mode)
            if not is_directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise GitStagingError('Git persistence needs nonsymlink regular objects')
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if is_directory:
                flags |= os.O_DIRECTORY
            child = os.open(name, flags, dir_fd=fd)
            try:
                if _signature(info) != _signature(os.fstat(child)):
                    raise GitStagingError('Git object changed before persistence')
                if is_directory:
                    visit(child, depth + 1)
                else:
                    os.fsync(child)
                if (_signature(info) != _signature(os.fstat(child))
                        or _signature(info) != _signature(os.stat(name, dir_fd=fd, follow_symlinks=False))):
                    raise GitStagingError('Git object changed during persistence')
            finally:
                os.close(child)
        os.fsync(fd)
        if names != sorted(os.listdir(fd)) or before != _signature(os.fstat(fd)):
            raise GitStagingError('Git object directory changed during persistence')

    with control_files.directory_handle(root) as fd:
        visit(fd, 0)
        with control_files.directory_handle(root) as current:
            if _signature(os.fstat(fd)) != _signature(os.fstat(current)):
                raise GitStagingError('Git object store changed during persistence')


def _sync_ref(common, ref):
    """Persist the exact promoted ref/reflog and every new directory link."""
    records = {}
    for relative in (ref, 'logs/' + ref):
        path = common / relative
        try:
            stream = control_files.open_file(path, 'rb')
        except FileNotFoundError:
            if relative != ref:
                # Reflogs may be disabled. A no-op update can also leave a
                # packed ref packed, without creating a loose leaf.
                continue
            path = common / 'packed-refs'
            stream = control_files.open_file(path, 'rb')
        with stream:
            before = _signature(os.fstat(stream.fileno()))
            os.fsync(stream.fileno())
            with control_files.open_file(path, 'rb') as current:
                if before != _signature(os.fstat(current.fileno())):
                    raise GitStagingError('promoted Git ref/reflog changed during persistence')
        for directory in path.parents:
            if not directory.is_relative_to(common):
                break
            with control_files.directory_handle(directory) as fd:
                record = _signature(os.fstat(fd))
                if directory in records and records[directory] != record:
                    raise GitStagingError('promoted Git ref directory changed during persistence')
                os.fsync(fd)
                records[directory] = record
    for directory, expected in records.items():
        with control_files.directory_handle(directory) as fd:
            if _signature(os.fstat(fd)) != expected:
                raise GitStagingError('promoted Git ref directory changed during persistence')


@contextmanager
def _guard(directory):
    control_files.directory(directory)
    with control_files.directory_syncs():
        control_files.mkdir(directory, mode=0o700, parents=True, exist_ok=True, durable=True)
        with control_files.open_file(directory / 'guard', 'a') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield


def _identity():
    return {'pid': os.getpid(), 'start': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]}


def _alive(owner):
    try:
        fields = Path(f'/proc/{int(owner["pid"])}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[0] not in ('Z', 'X') and fields[19] == owner['start']
    except (OSError, KeyError, ValueError):
        return False


def write_roots(tree, roots):
    """Grant task admin only; shared objects and refs are private projections."""
    from maestro.confinement import is_namespaced
    roots = tuple(roots)
    if tree not in roots or not (tree / '.git').exists():
        return roots
    result = _git(tree, 'rev-parse', '--path-format=absolute', '--absolute-git-dir',
                  '--git-common-dir')
    own, common = map(Path, result.stdout.splitlines())
    ref = _git(tree, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    if own == common or not is_namespaced(ref):
        if any(root.is_relative_to(common) for root in roots):
            raise GitStagingError('controller Git metadata is never native-writable')
        return roots
    allowed = (own, common / 'objects')
    if any(root.is_relative_to(common) and root not in allowed for root in roots):
        raise GitStagingError('host Git namespace is never native-writable')
    return tuple(dict.fromkeys((*(p for p in roots if p != common / 'objects'), own)))


def private_roots(tree, roots):
    """Native inner-sandbox grants for paths projected onto PRIVATE staging.

    These paths must never be passed as outer host write grants. The final outer
    mounts replace their backing files before the native sandbox starts.
    """
    if tree not in roots or not (tree / '.git').is_file():
        return ()
    common = Path(_git(tree, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    return (common / 'objects', common / 'refs', common / 'logs')


def publication_timeout(workspace, *, reason=None):
    """Read/record a controller outcome outside all native writable mounts.

    This separate durable record survives worker journal promotion and controller
    restart. It belongs to the exact current call, never a subsequent retry.
    Invalid metadata raises, so it cannot authorize success or tree replacement.
    """
    current = Path(workspace) / '.native-git/current.json'
    try:
        job = Path(json.loads(_read(current))['journal'])
    except FileNotFoundError:
        if not current.exists() and not current.is_symlink():
            return None
        raise
    if (job.name != 'transaction.json' or not job.parent.name.startswith('call-')
            or job.parent.parent != current.parent):
        raise GitStagingError('invalid native timeout journal')
    transaction = Transaction.load(job)
    path = transaction.directory / 'controller-timeout.json'
    try:
        data = json.loads(_read(path))
    except FileNotFoundError:
        if reason is None:
            return None
        if not isinstance(reason, str) or not reason.startswith('timeout after '):
            raise GitStagingError('invalid controller timeout reason')
        data = dict(journal=str(job), reason=reason)
        _dump(path, data)
    if (set(data) != {'journal', 'reason'} or data['journal'] != str(job)
            or not isinstance(data['reason'], str) or not data['reason'].startswith('timeout after ')):
        raise GitStagingError('invalid controller timeout record')
    return data['reason']


def publication_active(workspace):
    """A missing tmux window does not prove its transaction owner has exited."""
    from maestro.backends import native_ownership

    current = Path(workspace) / '.native-git/current.json'
    try:
        job = json.loads(_read(current))['journal']
        transaction = Transaction.load(job)
        # A crash can split the journal/lease state writes. Check both owners
        # independently so torn ready publication still permits dead-owner recovery.
        records = [json.loads(_read(Path(job))),
                   json.loads(_read(transaction.lease / 'active.json'))]
        for data in records:
            reservation = data.get('reservation')
            if reservation is not None:
                if native_ownership.active(reservation):
                    return True
            else:
                owner = data['owner']
                if (not isinstance(owner, dict) or set(owner) != {'pid', 'start'}
                        or type(owner['pid']) is not int or owner['pid'] <= 0
                        or not isinstance(owner['start'], str) or not owner['start'].isdigit()):
                    raise ValueError('invalid native publication owner')
                if _alive(owner):
                    return True
        return False
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable owner record is not evidence of a stopped worker.
        return True


def publication_state(workspace):
    """Gate legacy success sentinels on the trusted journal, including restart."""
    current = Path(workspace) / '.native-git/current.json'
    if not current.exists() and not current.is_symlink():
        return None
    try:
        job = json.loads(_read(current))['journal']
        job_path = Path(job)
        if (job_path.name != 'transaction.json' or not job_path.parent.name.startswith('call-')
                or job_path.parent.parent != current.parent):
            return 'failed'
        data = json.loads(_read(Path(job)))
        if data['state'] == 'promoted':
            transaction = Transaction.load(job)
            transaction._state()
            if _git(transaction.tree, 'rev-parse', transaction.ref).stdout.strip() != data['staged_tip']:
                return 'failed'
            return 'promoted'
        return 'pending' if data['state'] in ('prepared', 'running', 'ready') else 'failed'
    except (OSError, ValueError, KeyError, TypeError):
        return 'failed'


@dataclass
class Transaction:
    tree: Path
    own: Path
    common: Path
    directory: Path
    lease: Path
    ref: str
    start_tip: str
    pointer_identity: tuple[int, int]

    @property
    def write_roots(self):
        return (self.own,)

    @property
    def job(self):
        return str(self.directory / 'transaction.json')

    @classmethod
    def prepare(cls, tree: Path, workspace: Path):
        from maestro.confinement import is_namespaced
        from maestro.backends import native_ownership
        tree = control_files.directory(tree)
        marker = tree / '.git'
        if not marker.exists():
            return None
        marker_content, marker_identity = _pointer(marker) if marker.is_file() else (None, None)
        result = _git(tree, 'rev-parse', '--path-format=absolute', '--absolute-git-dir',
                      '--git-common-dir', check=False)
        if result.returncode:
            raise GitStagingError('unreadable native Git worktree')
        own, common = (control_files.directory(Path(line)) for line in result.stdout.splitlines())
        ref = _git(tree, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
        if own == common or not is_namespaced(ref):
            # General bounded readers/calls may run in a controller checkout, but
            # receive no host Git metadata grants there.
            return None
        start = _git(tree, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.strip()
        lease = common / 'maestro-native' / hashlib.sha256(str(own).encode()).hexdigest()
        with _guard(lease):
            active = lease / 'active.json'
            if active.exists():
                previous = json.loads(_read(active))
                if previous['state'] not in ('promoted', 'aborted', 'interrupted'):
                    retained_owner = previous.get('reservation')
                    occupied = (native_ownership.active(retained_owner) if retained_owner is not None
                                else _alive(previous.get('owner', {})))
                    if occupied:
                        raise GitStagingError('native worktree already reserved or running')
                    # A dead launcher is evidence, never proof of success. Permit
                    # retry only after the existing restore has made the tree clean.
                    if _git(tree, 'status', '--porcelain').stdout:
                        raise GitStagingError('interrupted native Git transaction needs task restore')
                    previous['state'] = 'interrupted'
                    _dump(Path(previous['journal']), previous)
                    _dump(active, previous)
                    from maestro.backends.git_objects import release_keep
                    with _guard(common / 'maestro-native/object-publication'):
                        release_keep(cls.load(previous['journal']))
            directory = control_files.directory(workspace / '.native-git')
            control_files.mkdir(directory, mode=0o700, exist_ok=True, durable=True)
            directory = control_files.temporary_directory(directory, durable=True)
            transaction = cls(tree, own, common, directory, lease, ref, start, marker_identity)
            if marker_content is None or _pointer(marker) != (marker_content, marker_identity):
                raise GitStagingError('worktree Git pointer changed during preparation')
            control_files.write_bytes(directory / 'baseline-git', marker_content, durable=True)
            for name in ('objects', 'objects/info', 'objects/pack', 'baseline-objects'):
                control_files.mkdir(directory / name, durable=True)
            # The only native alternate is an outer read-only mount of the
            # original shared store. Trusted validation removes this authority.
            control_files.write(directory / 'objects/info/alternates',
                str(directory / 'baseline-objects') + '\n', durable=True)
            for name in ('refs', 'logs'):
                original = common / name
                control_files.mkdir(directory / name, durable=True)
                control_files.mkdir(directory / ('baseline-' + name), durable=True)
                for relative in _directories(original):
                    for base in (directory / name, directory / ('baseline-' + name)):
                        control_files.mkdir(base / relative, parents=True, exist_ok=True, durable=True)
                for relative, content in _inventory(original).items():
                    for target in (directory / name / relative,
                                   directory / ('baseline-' + name) / relative):
                        control_files.mkdir(target.parent, parents=True, exist_ok=True, durable=True)
                        control_files.write_bytes(target, content, durable=True)
            # Materialize a packed own ref privately, without unpacking host refs.
            own_ref = directory / ref
            control_files.mkdir(own_ref.parent, parents=True, exist_ok=True, durable=True)
            control_files.write(own_ref, start + '\n', durable=True)
            control_files.mkdir(directory / 'baseline-own', durable=True)
            for relative in _directories(own):
                control_files.mkdir(directory / 'baseline-own' / relative, parents=True, exist_ok=True, durable=True)
            for relative, content in _inventory(own).items():
                target = directory / 'baseline-own' / relative
                control_files.mkdir(target.parent, parents=True, exist_ok=True, durable=True)
                control_files.write_bytes(target, content, durable=True)
            data = {'version': 3, 'tree': str(tree), 'own': str(own), 'common': str(common),
                    'directory': str(directory), 'lease': str(lease), 'ref': ref,
                    'start_tip': start, 'state': 'prepared', 'owner': _identity(),
                    'reservation': native_ownership.current_job(), 'pointer_identity': list(marker_identity),
                    'journal': transaction.job}
            try:
                _dump(directory / 'transaction.json', data)
                _dump(active, data)
                _dump(directory.parent / 'current.json', {'journal': transaction.job})
            except BaseException as error:
                # Publication spans three records; a failed fsync can follow a
                # successful replace. Settle this attempt while still holding
                # its lease guard, without releasing an older/different owner.
                targets = [directory / 'transaction.json']
                try:
                    if json.loads(_read(active)) == data:
                        targets.append(active)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    error.add_note(f'native Git failed-publication lease check: {cleanup_error!r}')
                aborted = dict(data, state='aborted', error=str(error))
                for target in targets:
                    try:
                        _dump(target, aborted)
                    except Exception as cleanup_error:
                        error.add_note(f'native Git failed-publication cleanup {target}: {cleanup_error!r}')
                raise
            return transaction

    @classmethod
    def load(cls, job):
        try:
            job = control_files.file(Path(job))
            data = json.loads(_read(job))
            paths = [control_files.directory(Path(data[k])) for k in
                     ('tree', 'own', 'common', 'directory', 'lease')]
            tree, own, common, directory, lease = paths
            ref, tip = data['ref'], data['start_tip']
            pointer_identity = data['pointer_identity']
            if (data['version'] != 3 or not isinstance(pointer_identity, list)
                    or len(pointer_identity) != 2
                    or any(type(value) is not int or value < 0 for value in pointer_identity)
                    or job != directory / 'transaction.json'
                    or directory.parent.name != '.native-git' or not directory.name.startswith('call-')
                    or data['journal'] != str(job)
                    or not re.fullmatch(r'refs/heads/(impl|selffix|redo)/[^\s]+', ref)
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', tip)
                    or lease != common / 'maestro-native' / hashlib.sha256(str(own).encode()).hexdigest()
                    or _read(directory / 'baseline-own/HEAD') != ('ref: ' + ref + '\n').encode()):
                raise GitStagingError('invalid native Git transaction journal')
            actual = _git(tree, 'rev-parse', '--path-format=absolute', '--absolute-git-dir', '--git-common-dir')
            if actual.stdout.splitlines() != [str(own), str(common)] or own == common:
                raise GitStagingError('redirected native Git transaction metadata')
            transaction = cls(*paths, ref, tip, tuple(pointer_identity))
            transaction._check_pointer()
            return transaction
        except (OSError, KeyError, TypeError) as exc:
            raise GitStagingError('invalid native Git transaction journal') from exc

    def _state(self):
        data = json.loads(_read(self.lease / 'active.json'))
        if data['journal'] != self.job:
            raise GitStagingError('native Git reservation was superseded')
        if data != json.loads(_read(Path(self.job))):
            raise GitStagingError('native Git journal and lease disagree')
        return data

    def _save(self, data, state):
        data['state'] = state
        _dump(self.directory / 'transaction.json', data)
        _dump(self.lease / 'active.json', data)

    def start(self):
        with _guard(self.lease):
            data = self._state()
            if data['state'] != 'prepared':
                raise GitStagingError('native Git launcher is not a fresh reservation')
            self._check_pointer()
            if _git(self.tree, 'rev-parse', 'HEAD').stdout.strip() != self.start_tip:
                raise GitStagingError('native task ref moved before launch')
            data['owner'] = _identity()
            self._save(data, 'running')

    def exclude_generated(self, paths):
        """Hide only absent generated leaves from ordinary native Git staging.

        This is a private discovery aid, not a publication exemption. Explicit
        forced adds or repository ignore overrides still face the unchanged
        clean-tip audit. Existing user files are never excluded by this method.
        """
        if self._state()['state'] != 'prepared':
            raise GitStagingError('generated exclusions require a prepared transaction')
        names = []
        for path in paths:
            path = control_files.file(Path(path))
            if not path.is_relative_to(self.tree):
                continue
            relative = path.relative_to(self.tree).as_posix()
            if '\n' in relative or '\r' in relative:
                raise GitStagingError('unsupported generated exclusion path')
            if path.exists() or path.is_symlink():
                raise GitStagingError('generated exclusion names a real user file')
            if _git(self.tree, 'ls-files', '--', relative).stdout:
                raise GitStagingError('generated exclusion names a tracked user file')
            # Git ignore syntax, escaped literally and anchored at this tree.
            names.append('/'+''.join('\\'+c if c in '\\*?[] ' else c for c in relative))
        if not names:
            return
        source = control_files.directory(self.common / 'info')
        if not source.is_dir():
            raise GitStagingError('native Git discovery needs existing info metadata')
        target = self.directory / 'discovery-info'
        control_files.SourceCopy().copy(source, target)
        original = _read(target / 'exclude') if (target / 'exclude').exists() else b''
        control_files.write_bytes(target / 'exclude', original+b'\n'+
            ('\n'.join(sorted(set(names)))+'\n').encode(), durable=True)

    def mounts(self):
        # Re-seal the original common path AFTER all ordinary writable-root
        # rebinds. Its private refs/logs cannot expose original shared aliases.
        self._check_pointer()
        args = ['--ro-bind', str(self.directory / 'baseline-git'), str(self.tree / '.git'),
                '--ro-bind', str(self.common), str(self.common),
                '--ro-bind', str(self.directory.parent), str(self.directory.parent),
                '--ro-bind', str(self.common / 'objects'), str(self.directory / 'baseline-objects'),
                '--bind', str(self.directory / 'objects'), str(self.common / 'objects'),
                '--ro-bind', str(self.directory / 'objects/info/alternates'),
                str(self.common / 'objects/info/alternates'),
                '--bind', str(self.own), str(self.own)]
        for name in ('refs', 'logs'):
            args += ['--bind', str(self.directory / name), str(self.common / name)]
        discovery = self.directory / 'discovery-info'
        if discovery.exists():
            args += ['--ro-bind', str(discovery), str(self.common / 'info')]
        # Existing siblings are read-only too. New private foreign entries are
        # never published and the audit refuses them.
        for name in ('refs', 'logs'):
            own_path = self.ref if name == 'refs' else 'logs/' + self.ref
            baseline = _inventory(self.directory / name)
            for relative in baseline:
                target = f'{name}/{relative}'
                if target != own_path:
                    args += ['--ro-bind', str(self.directory / target), str(self.common / target)]
        for name in ('HEAD', 'commondir', 'gitdir'):
            source = self.directory / 'baseline-own' / name
            if source.exists():
                args += ['--ro-bind', str(source), str(self.own / name)]
        return args

    def _check_pointer(self):
        content, identity = _pointer(self.tree / '.git')
        if identity != self.pointer_identity or content != _read(self.directory / 'baseline-git'):
            raise GitStagingError('worktree Git pointer changed')

    def _audit(self):
        self._check_pointer()
        if _git(self.tree, 'symbolic-ref', '-q', 'HEAD').stdout.strip() != self.ref:
            raise GitStagingError('native symbolic HEAD changed')
        for name in ('refs', 'logs'):
            before = _inventory(self.directory / ('baseline-' + name))
            after = _inventory(self.directory / name)
            allowed = self.ref.removeprefix('refs/') if name == 'refs' else self.ref
            allowed_dirs = {str(p) for p in Path(allowed).parents if str(p) != '.'}
            if (_directories(self.directory / name)
                    - _directories(self.directory / ('baseline-' + name)) - allowed_dirs):
                raise GitStagingError('foreign staged Git directory or lock leftover')
            changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
            if changed - {allowed}:
                raise GitStagingError('foreign staged Git ref/reflog or lock leftover')
        before = _inventory(self.directory / 'baseline-own')
        after = _inventory(self.own)
        if ((_directories(self.own) ^ _directories(self.directory / 'baseline-own')) - {'logs'}
                or any(path.endswith('.lock') for path in after)):
            raise GitStagingError('native Git admin directory or lock leftover changed')
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        if changed - {'index', 'COMMIT_EDITMSG', 'logs/HEAD'}:
            raise GitStagingError('native Git admin metadata or lock leftover changed')
        tip = _read(self.directory / self.ref).decode('ascii').strip()
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', tip):
            raise GitStagingError('invalid staged task tip')
        objects = self._object_snapshot
        _git(self.tree, 'cat-file', '-e', tip + '^{commit}', object_directory=objects)
        _git(self.tree, 'merge-base', '--is-ancestor', self.start_tip, tip, object_directory=objects)
        if (_git(self.tree, 'diff', '--quiet', tip, '--', check=False, object_directory=objects).returncode
                or _git(self.tree, 'diff', '--cached', '--quiet', tip, '--', check=False,
                        object_directory=objects).returncode
                or _git(self.tree, 'ls-files', '--others', '--exclude-standard').stdout):
            raise GitStagingError('native Git worktree is not clean at staged tip')
        return tip

    def finish(self, *, success, verify=None):
        with _guard(self.lease):
            data = self._state()
            if data['owner'] != _identity():
                raise GitStagingError('native Git transaction belongs to another worker')
            if data['state'] not in ('prepared', 'running'):
                raise GitStagingError('native Git transaction already finished')
            if success and data['state'] != 'running':
                raise GitStagingError('native Git transaction is not running')
            if not success:
                self._save(data, 'aborted')
                return None
            try:
                if verify is not None:
                    verify()
                from maestro.backends.git_objects import ObjectValidation, release_keep
                objects = ObjectValidation(self)
                self._object_snapshot = objects.objects
                tip = self._audit()
                objects.validate(tip)
                # A short shared guard serializes GC keep ownership/import/CAS
                # across Maestro publishers. Native work remains concurrent.
                with _guard(self.common / 'maestro-native/object-publication'):
                    try:
                        objects.publish()
                        _sync_objects(self.common)
                        data['staged_tip'] = tip
                        self._save(data, 'ready')
                        if verify is not None:
                            verify()
                        _git(self.tree, 'update-ref', '--no-deref', '-m', 'Maestro native task promotion',
                             self.ref, tip, self.start_tip)
                        _sync_ref(self.common, self.ref)
                        if _git(self.tree, 'rev-parse', '--verify', self.ref).stdout.strip() != tip:
                            raise GitStagingError('promoted Git task ref changed before publication')
                        self._save(data, 'promoted')
                        return tip
                    finally:
                        release_keep(self)
            except Exception as exc:
                data['error'] = str(exc)
                self._save(data, 'aborted')
                raise

    def cleanup(self):
        with _guard(self.lease):
            data = self._state()
            if data['state'] in ('prepared', 'running') and data['owner'] == _identity():
                self._save(data, 'aborted')
