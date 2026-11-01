"""Nonsymlink, single-link parent controls for isolated native workers."""
import os
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from pathlib import Path
import stat
import sys
import uuid

PARENT_CONTROLS = ('impl.log', 'codex_thread_id.txt', 'codex_launch.json', 'launch.py',
                   'brief.txt', 'resume_prompt.txt')
_preparing = ContextVar('native_preparation_directories', default=None)
_synced_directories = ContextVar('native_synced_directories', default=None)


def cleanup_resources(*actions):
    """Attempt every release; retain the operation error and every cleanup error.

    SystemExit alone has no traceback, so a release failure must raise rather
    than disappear behind a worker's already selected exit status.
    """
    primary = sys.exception()
    if isinstance(primary, SystemExit):
        primary = None
    errors = []
    for name, action in actions:
        try:
            action()
        except BaseException as error:
            errors.append((name, error))
    if not errors:
        return
    target = primary if primary is not None else errors[0][1]
    for name, error in errors:
        target.add_note(f'native {name} cleanup failed: {error!r}')
    if primary is None:
        raise target


@contextmanager
def directory_syncs():
    """Reuse successful syncs of unchanged directories within one operation.

    No evidence survives this scope: a retry must persist even existing links.
    Stat versions make a changed directory eligible again, including changes
    made outside these helpers. Failed fsyncs never enter the cache.
    """
    token = _synced_directories.set({})
    try:
        yield
    finally:
        _synced_directories.reset(token)


def _sync_directory(fd):
    synced = _synced_directories.get()
    if synced is None:
        os.fsync(fd)
        return
    info = os.fstat(fd)
    key = (info.st_dev, info.st_ino)
    version = (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_nlink)
    if synced.get(key) != version:
        os.fsync(fd)
        # Keep the pre-sync version. A concurrent change during fsync must
        # require another sync when next observed, rather than being blessed.
        synced[key] = version


def _observe_directory(path, info):
    current = _preparing.get()
    if current is not None:
        value = identity(info)
        if path in current and current[path] != value:
            raise ValueError(f'native preparation directory changed: {path}')
        current[path] = value


def verify_directories(records):
    for spelling, expected in records:
        try:
            with directory_handle(Path(spelling)) as fd:
                if identity(os.fstat(fd)) != expected:
                    raise ValueError(f'native preparation directory changed: {spelling}')
        except OSError as error:
            # openat reports only the failing component. Retain that exception
            # and the exact validated spelling so a transient path is traceable.
            error.add_note(f'native preparation directory unavailable: {spelling}')
            raise


def preparation(function):
    """Keep validated directory identities across an entire preparation call.

    Persist metadata only, so a later launcher/mount capture can reject a new
    ordinary directory as well as a symlink. No credential contents are opened.
    """
    @wraps(function)
    def guarded(**kwargs):
        directories = {}
        scope = _preparing.set(directories)
        run = None
        try:
            run = function(**kwargs)
            records = tuple((str(path), value) for path, value in directories.items())
            verify_directories(records)
            if hasattr(run, 'verify_auth'):
                run.verify_auth()
            return replace(run, prepared_directories=records)
        except BaseException as error:
            if run is not None:
                try:
                    run.cleanup()
                except Exception as cleanup_error:
                    error.add_note(f'native preparation cleanup failed: {cleanup_error!r}')
            raise
        finally:
            _preparing.reset(scope)
    return guarded


def directory(path: Path) -> Path:
    """Validate the original spelling before resolving or creating anything."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError(f'parent traversal in Codex directory: {path}')
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'Codex directory must have nonsymlink ancestors: {parent}')
        _observe_directory(parent, info)
    return path


def file(path: Path) -> Path:
    path = Path(path).absolute()
    directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f'Codex parent control must be a single-link regular file: {path}')
    return path


def workspace(path: Path, extra=()) -> Path:
    path = directory(path)
    for target in (*(path / name for name in PARENT_CONTROLS), *extra):
        file(target)
    runtime = directory(path / '.codex-runtime')
    state = directory(runtime / 'state')
    for name in ('sessions', 'archived_sessions', 'rules', 'skills', 'plugins'):
        directory(state / name)
    for name in ('auth.json', 'config.toml', 'hooks.json', 'AGENTS.md', 'AGENTS.override.md'):
        file(state / name)
    return path


@contextmanager
def parent(path: Path):
    # Pin every ancestor with openat/O_NOFOLLOW. Validating its spelling and
    # opening an absolute path later would leave a symlink substitution window.
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(path.anchor, directory_flags)
    try:
        current = Path(path.anchor)
        _observe_directory(current, os.fstat(parent_fd))
        for component in path.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
            current /= component
            _observe_directory(current, os.fstat(parent_fd))
        yield parent_fd
    finally:
        os.close(parent_fd)


@contextmanager
def directory_handle(path: Path):
    """Hold a nonsymlink directory identity across traversal or subprocess exec."""
    path = directory(path)
    with parent(path / '.entry') as fd:
        yield fd


def mkdir(path: Path, mode=0o777, *, parents=False, exist_ok=False, durable=False):
    """Create directories relative to pinned parents, never through aliases."""
    path = directory(path)
    if parents:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for index, name in enumerate(path.parts[1:]):
                final = index == len(path.parts) - 2
                try:
                    os.mkdir(name, mode if final else 0o777, dir_fd=fd)
                except FileExistsError:
                    if final and not exist_ok:
                        raise
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
                try:
                    if durable:
                        # Existing entries may be leftovers of an earlier
                        # failed fsync; existence is not persistence evidence.
                        _sync_directory(child)
                        _sync_directory(fd)
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
                _observe_directory(Path(path.anchor).joinpath(*path.parts[1:index + 2]), os.fstat(fd))
        finally:
            os.close(fd)
    else:
        with parent(path) as fd:
            try:
                os.mkdir(path.name, mode, dir_fd=fd)
            except FileExistsError:
                if not exist_ok:
                    raise
            child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            try:
                _observe_directory(path, os.fstat(child))
                if durable:
                    _sync_directory(child)
                    _sync_directory(fd)
            finally:
                os.close(child)


def temporary_directory(path: Path, prefix='call-', *, durable=False) -> Path:
    target = directory(path) / (prefix + uuid.uuid4().hex)
    mkdir(target, mode=0o700, durable=durable)
    return target


def touch(path: Path):
    with open_file(path, 'a'):
        pass


def chmod(path: Path, mode):
    with open_file(path, 'rb') as stream:
        os.fchmod(stream.fileno(), mode)


def read_at(directory_fd: int, name: str, limit=16 * 1024 * 1024, *, expected=None) -> bytes:
    """Read and compare one bounded leaf through an already pinned directory."""
    if not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('metadata read needs one leaf name')
    def version(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    before, result = None, None
    for _ in range(2):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if expected is not None and identity(info) != expected:
                raise ValueError('metadata leaf identity changed')
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValueError('metadata must be a bounded single-link regular file')
            if before is not None and before != version(info):
                raise ValueError('metadata leaf changed')
            content = stream.read(limit + 1)
            if len(content) > limit:
                raise ValueError('metadata read exceeded limit')
            if version(info) != version(os.fstat(stream.fileno())):
                raise ValueError('metadata content changed')
            if before is not None and result != content:
                raise ValueError('metadata content changed')
            before, result = version(info), content
    if before != version(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)):
        raise ValueError('metadata leaf changed')
    return result


def identity(info):
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]


class DirectoryView:
    """A bounded directory listing and leaf reads tied to one opened root.

    The caller holds this context through its final publication/mount capture.
    Replacing a parent or an enumerated entry fails closed. Symlink entries can
    be inventoried by name but are never traversed or read.
    """
    def __init__(self, path: Path):
        self.path = directory(path)
        self.stack = ExitStack()
        self.expected = {}

    def __enter__(self):
        self.fd = self.stack.enter_context(directory_handle(self.path))
        self.info = os.fstat(self.fd)
        return self

    def entries(self, *, only=None):
        """Pin selected entries; leave unrelated live cache leaves unobserved."""
        names = sorted(os.listdir(self.fd))
        if len(names) > 20000:
            raise ValueError('directory entry limit exceeded')
        if only is not None:
            names = [name for name in names if name in only]
        result = {}
        for name in names:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            result[name] = info
            key = self.path / name
            current = identity(info)
            if key in self.expected and self.expected[key] != current:
                raise ValueError('directory entry identity changed')
            self.expected[key] = current
        return result

    def read(self, name, limit=16 * 1024 * 1024):
        before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        expected = self.expected.get(self.path / name, identity(before))
        if identity(before) != expected:
            raise ValueError('directory document identity changed')
        content = read_at(self.fd, name, limit, expected=expected)
        after = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if (identity(before), before.st_mtime_ns, before.st_ctime_ns) != (
                identity(after), after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('directory document changed')
        self.expected[self.path / name] = identity(before)
        return content

    def verify(self):
        with directory_handle(self.path) as current:
            if identity(os.fstat(current)) != identity(self.info):
                raise ValueError('directory root identity changed')
        for path, expected in self.expected.items():
            if identity(os.stat(path.name, dir_fd=self.fd, follow_symlinks=False)) != expected:
                raise ValueError('directory entry identity changed')

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.verify()
        finally:
            self.stack.close()


def open_file(path: Path, mode='w'):
    """Never truncate a link or block opening a substituted special file."""
    path = file(path)
    if mode not in ('w', 'wb', 'a', 'r', 'rb'):
        raise ValueError('unsupported parent control mode')
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    flags |= os.O_RDONLY if mode in ('r', 'rb') else os.O_WRONLY | os.O_CREAT
    if mode == 'a':
        flags |= os.O_APPEND
    with parent(path) as parent_fd:
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f'Codex parent control must be a single-link regular file: {path}')
        if mode in ('w', 'wb'):
            os.ftruncate(fd, 0)
        return os.fdopen(fd, mode, encoding=None if 'b' in mode else 'utf-8')
    except BaseException:
        os.close(fd)
        raise


def write(path: Path, text: str, *, durable=False) -> None:
    with (atomic_file(path) if durable else open_file(path)) as stream:
        stream.write(text)


def write_bytes(path: Path, content: bytes, *, durable=False) -> None:
    with (atomic_file(path, binary=True) if durable else open_file(path, 'wb')) as stream:
        stream.write(content)


def read(path: Path, limit=16 * 1024 * 1024) -> bytes:
    """Bounded, version-checked read through pinned, nonsymlink ancestors."""
    def version(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with open_file(path, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit:
            raise ValueError(f'oversized parent control: {path}')
        data = stream.read(limit + 1)
        if version(before) != version(os.fstat(stream.fileno())):
            raise ValueError(f'parent control content changed: {path}')
        with open_file(path, 'rb') as current:
            if version(before) != version(os.fstat(current.fileno())):
                raise ValueError(f'parent control leaf changed: {path}')
            # Same-size writes can share identical filesystem timestamps. A
            # separately opened bounded read must agree too; metadata alone
            # cannot establish that an ordinary control leaf stayed unchanged.
            if data != current.read(limit + 1) or version(before) != version(os.fstat(current.fileno())):
                raise ValueError(f'parent control content changed: {path}')
    if len(data) > limit:
        raise ValueError(f'oversized parent control: {path}')
    return data


class MountArgv(list):
    """An argv plus the mount identities frozen by native preparation."""
    def __init__(self, argv, identities):
        super().__init__(argv)
        self.identities = identities


def _mount_rows(argv):
    if not argv or Path(argv[0]).name != 'bwrap':
        return []
    arity = {'--die-with-parent': 0, '--new-session': 0, '--unshare-user': 0,
             '--unshare-pid': 0, '--unshare-ipc': 0, '--cap-drop': 1,
             '--proc': 1, '--dev': 1, '--tmpfs': 1, '--chdir': 1,
             '--setenv': 2, '--bind': 2, '--ro-bind': 2}
    rows, index = [], 1
    while index < len(argv) and argv[index] != '--':
        option = argv[index]
        if option not in arity or index + arity[option] >= len(argv):
            raise ValueError('unsupported native mount argument')
        if option in ('--bind', '--ro-bind'):
            rows.append((index, argv[index + 1]))
        index += arity[option] + 1
    if index >= len(argv) or not rows:
        raise ValueError('native mount argv lacks command or filesystem boundary')
    return rows


@contextmanager
def _mount_handles(rows):
    # O_PATH pins credentials without reading or hashing their contents. These
    # descriptors go only to bubblewrap, whose fd binds close them before exec.
    with ExitStack() as stack:
        handles = {}
        for index, spelling in rows:
            path = Path(spelling)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('native mount source must be an absolute path')
            if path == Path('/'):
                fd = os.open('/', os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
            else:
                with parent(path) as parent_fd:
                    fd = os.open(path.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=parent_fd)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                raise ValueError('native mount source must be a directory or single-link regular file')
            # bubblewrap consumes each bind-fd argument. Repeated source paths
            # need distinct descriptors even when they name the same inode.
            handles[index] = fd
        yield handles


def _mount_identities(handles):
    return {str(index): [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]
            for index, fd in handles.items() for info in (os.fstat(fd),)}


def freeze_mounts(argv, *, expected=None):
    """Retain metadata only; source paths must still match when launched later."""
    with _mount_handles(_mount_rows(argv)) as handles:
        identities = _mount_identities(handles)
        for index, spelling in _mount_rows(argv):
            if expected and Path(spelling) in expected and identities[str(index)] != expected[Path(spelling)]:
                raise ValueError('native mount source changed during directory enumeration')
        return MountArgv(argv, identities)


@contextmanager
def pinned_mounts(argv):
    """Use actual source fds throughout bubblewrap setup, closing parent copies."""
    rows = _mount_rows(argv)
    with _mount_handles(rows) as handles:
        if isinstance(argv, MountArgv) and argv.identities != _mount_identities(handles):
            raise ValueError('native mount source identity changed after preparation')
        result = list(argv)
        for index, _ in rows:
            result[index] += '-fd'
            result[index + 1] = str(handles[index])
        yield result, tuple(handles.values())


class SourceCopy:
    """Bounded source snapshot through pinned directory and file descriptors.

    Aliases are opt-in for instruction libraries. Each resolved target is opened
    without following links; recursive traversal uses its pinned parent, never
    an absolute pathname reconstructed from a directory listing.
    """
    def __init__(self, *, dereference=False, max_bytes=128 * 1024 * 1024,
                 max_files=20000, validate_source=None):
        self.dereference = dereference
        self.remaining_bytes = max_bytes
        self.remaining_files = max_files
        self.validate_source = validate_source

    def copy(self, source: Path, target: Path):
        source = Path(source).absolute()
        target = Path(target).absolute()
        if self.dereference:
            source = source.resolve(strict=True)
        directory(source.parent)
        with parent(source) as fd, parent(target) as target_fd:
            self._copy(source, target, fd, (), target_fd)

    def _copy(self, source, target, parent_fd, ancestors, target_parent_fd):
        self.remaining_files -= 1
        if self.remaining_files < 0 or len(ancestors) >= 64:
            raise ValueError('source snapshot file/depth limit exceeded')
        info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) and self.dereference:
            # Resolve only an intentionally retained library alias, and first
            # prove its parent still names the directory we actually traversed.
            with directory_handle(source.parent) as current:
                if not self._same(os.fstat(current), os.fstat(parent_fd)):
                    raise ValueError('source directory identity changed')
            resolved = source.resolve(strict=True)
            with parent(resolved) as resolved_fd:
                self._copy(resolved, target, resolved_fd, ancestors, target_parent_fd)
            if not self._same(info, os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)):
                raise ValueError('source alias identity changed')
            return
        is_directory = stat.S_ISDIR(info.st_mode)
        if not is_directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError(f'unsupported source snapshot content: {source}')
        if self.validate_source is not None:
            self.validate_source(source)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if is_directory:
            flags |= os.O_DIRECTORY
        fd = os.open(source.name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not self._same(info, opened):
                raise ValueError('source snapshot identity changed')
            identity = (opened.st_dev, opened.st_ino)
            if identity in ancestors:
                raise ValueError('cyclic source snapshot content')
            if is_directory:
                os.mkdir(target.name, 0o700, dir_fd=target_parent_fd)
                expected = os.stat(target.name, dir_fd=target_parent_fd, follow_symlinks=False)
                output_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=target_parent_fd)
                try:
                    if not self._same(expected, os.fstat(output_fd)):
                        raise ValueError('snapshot output directory identity changed')
                    names = sorted(os.listdir(fd))
                    for name in names:
                        self._copy(source / name, target / name, fd, (*ancestors, identity), output_fd)
                    if names != sorted(os.listdir(fd)):
                        raise ValueError('source directory entries changed')
                    os.fchmod(output_fd, stat.S_IMODE(opened.st_mode) & 0o777)
                finally:
                    os.close(output_fd)
            else:
                if opened.st_size > self.remaining_bytes:
                    raise ValueError('source snapshot byte limit exceeded')
                output_fd = os.open(target.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    0o600, dir_fd=target_parent_fd)
                with os.fdopen(output_fd, 'w+b') as out:
                    while chunk := os.read(fd, min(65536, self.remaining_bytes + 1)):
                        self.remaining_bytes -= len(chunk)
                        if self.remaining_bytes < 0:
                            raise ValueError('source snapshot byte limit exceeded')
                        out.write(chunk)
                    # Metadata can stay equal across same-size rewrites. Compare
                    # the bounded copy with a separately opened source, through
                    # the same pinned parent, before making it executable.
                    current_fd = os.open(source.name, flags, dir_fd=parent_fd)
                    with os.fdopen(current_fd, 'rb') as current:
                        checked = os.fstat(current.fileno())
                        if not self._same(opened, checked) or checked.st_nlink != 1:
                            raise ValueError('source snapshot identity changed')
                        out.flush()
                        out.seek(0)
                        while chunk := out.read(65536):
                            if chunk != current.read(len(chunk)):
                                raise ValueError('source snapshot content changed')
                        if current.read(1):
                            raise ValueError('source snapshot content changed')
                        final = os.fstat(current.fileno())
                        if (checked.st_size, checked.st_mtime_ns, checked.st_ctime_ns, checked.st_nlink) != (
                                final.st_size, final.st_mtime_ns, final.st_ctime_ns, final.st_nlink):
                            raise ValueError('source snapshot content changed')
                    os.fchmod(out.fileno(), stat.S_IMODE(opened.st_mode) & 0o777)
            final = os.fstat(fd)
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                    final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                raise ValueError('source snapshot content changed')
            if not self._same(opened, os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)):
                raise ValueError('source snapshot path identity changed')
        finally:
            os.close(fd)

    @staticmethod
    def _same(a, b):
        return (a.st_dev, a.st_ino, a.st_mode) == (b.st_dev, b.st_ino, b.st_mode)


@contextmanager
def atomic_file(path: Path, *, binary=False):
    """Durable replace: a failed/torn write leaves the previous record intact."""
    path = file(path)
    temporary = '.journal-' + uuid.uuid4().hex
    with parent(path) as parent_fd:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, 'wb' if binary else 'w', encoding=None if binary else 'utf-8') as stream:
                yield stream
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def release_mountpoints(records) -> None:
    """Retain empty discovery mountpoints until their owning tree is retired.

    Another invocation may already have mounted the same host directory. Even
    an inode-matched rmdir detaches its protected alias from path lookup in that
    invocation, allowing a replacement. Per-run cleanup must never unlink it.
    No configuration content is written to these host mountpoints, and Git
    ignores empty directories. The ordinary worktree/report lifecycle owns them.
    Records remain in the sealed job as mountpoint provenance, not unlink rights.
    """
