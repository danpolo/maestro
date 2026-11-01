"""Parent/worker ownership of native workspace state, before any control write.

The marker is sealed in the native namespace. A queued launcher must claim its
token before opening logs; parent cleanup cannot release a different live owner.
"""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from maestro.backends import control_files, git_staging

_current = ContextVar('native_owner', default=None)


def _token(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def _marker(directory):
    try:
        data = json.loads(git_staging._read(directory / 'active.json'))
        if (not isinstance(data, dict) or not {'token', 'state', 'owner'} <= data.keys()
                or data.keys() - {'token', 'state', 'owner', 'deadline'}
                or not _token(data['token'])
                or data['state'] not in ('prepared', 'queued', 'running', 'released')):
            raise ValueError
        owner = data['owner']
        if (not isinstance(owner, dict) or set(owner) != {'pid', 'start'}
                or type(owner['pid']) is not int or owner['pid'] <= 0
                or not isinstance(owner['start'], str) or not owner['start'].isdigit()):
            raise ValueError
        if data['state'] == 'queued' or 'deadline' in data:
            deadline = data.get('deadline')
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('invalid native ownership marker') from exc


def reserve_tree(path):
    """Extend the driver's fence after read-only resume parameter recovery."""
    reservation = _current.get()
    if reservation is None:
        raise ValueError('native resume has no driver reservation')
    reservation.tree(path)


def current_job():
    reservation = _current.get()
    return reservation.job if reservation is not None else None


def active(job):
    """A retained Git lease follows its invocation, not a long-lived parent PID."""
    reservation = Reservation.load(job)
    for directory in reservation.directories:
        data = _marker(directory)
        if (data['token'] != reservation.token or data['state'] == 'released'
                or (data['state'] == 'queued' and data['deadline'] < time.monotonic())
                or not git_staging._alive(data['owner'])):
            return False
    return bool(reservation.directories)


@dataclass
class Reservation:
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    directories: list[Path] = field(default_factory=list)

    @contextmanager
    def guards(self):
        with ExitStack() as stack:
            for path in sorted(self.directories):
                stack.enter_context(git_staging._guard(path))
            yield

    def add(self, path):
        path = control_files.directory(path)
        if path in self.directories:
            return
        with git_staging._guard(path):
            marker = path / 'active.json'
            if marker.exists():
                old = _marker(path)
                expired = old['state'] == 'queued' and old['deadline'] < time.monotonic()
                if old['state'] != 'released' and not expired and git_staging._alive(old['owner']):
                    raise ValueError('native workspace/worktree already reserved')
            # Atomic replacement may succeed before its directory fsync fails.
            # Cleanup must know this potential acquisition even if _dump raises.
            self.directories.append(path)
            git_staging._dump(marker, {'token': self.token, 'state': 'prepared',
                                      'owner': git_staging._identity()})

    def workspace(self, path):
        path = control_files.directory(path)
        control_files.mkdir(path, parents=True, exist_ok=True)
        self.add(path / '.native-owner')

    def tree(self, path):
        path = control_files.directory(path)
        if not (path / '.git').exists():
            return
        result = git_staging._git(path, 'rev-parse', '--path-format=absolute',
                                 '--absolute-git-dir', '--git-common-dir')
        own, common = map(Path, result.stdout.splitlines())
        if own == common:
            return  # independent bounded readers of the controller are allowed
        ref = git_staging._git(path, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
        for identity in (str(own), 'ref:' + ref):
            digest = hashlib.sha256(identity.encode()).hexdigest()
            self.add(common / 'maestro-native' / ('owner-' + digest))

    @property
    def job(self):
        return {'token': self.token, 'directories': list(map(str, self.directories))}

    @classmethod
    def load(cls, job):
        if (not isinstance(job, dict) or set(job) != {'token', 'directories'}
                or not _token(job['token']) or not isinstance(job['directories'], list)
                or not 0 < len(job['directories']) <= 32
                or any(not isinstance(p, str) or not Path(p).is_absolute() or '..' in Path(p).parts
                       for p in job['directories'])):
            raise ValueError('invalid native ownership job')
        directories = list(map(control_files.directory, map(Path, job['directories'])))
        if len(set(directories)) != len(directories):
            raise ValueError('invalid native ownership duplicate directories')
        return cls(job['token'], directories)

    def _data(self, directory):
        data = _marker(directory)
        if data['token'] != self.token:
            raise ValueError('native reservation was superseded')
        return data

    def queue(self):
        with self.guards():
            for path in self.directories:
                data = self._data(path)
                if data['state'] == 'prepared' and data['owner'] == git_staging._identity():
                    data.update(state='queued', deadline=time.monotonic() + 60)
                    git_staging._dump(path / 'active.json', data)

    def start(self):
        with self.guards():
            records = [(path, self._data(path)) for path in self.directories]
            for _, data in records:
                if (data['state'] not in ('prepared', 'queued') or not git_staging._alive(data['owner']) or
                        (data['state'] == 'queued' and data['deadline'] < time.monotonic())):
                    raise ValueError('native worker has no fresh reservation')
            for path, data in records:
                data.update(state='running', owner=git_staging._identity())
                git_staging._dump(path / 'active.json', data)

    def cleanup(self):
        # Bounded calls without a log have already retired their private temp
        # workspace. Never recreate a retired directory just to release a marker.
        error = None
        for path in self.directories:
            try:
                if not path.exists():
                    continue
                with git_staging._guard(path):
                    try:
                        data = _marker(path)
                    except FileNotFoundError:
                        continue  # publication failed before marker replacement
                    if data['token'] == self.token and data['owner'] == git_staging._identity():
                        data['state'] = 'released'
                        git_staging._dump(path / 'active.json', data)
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def mounts(self):
        return [arg for path in self.directories for arg in ('--ro-bind', str(path), str(path))]


def cleanup(reservation):
    # A driver scope owns its last control/error write as well as preparation.
    if reservation is not None and _current.get() is not reservation:
        reservation.cleanup()


def prepare(function):
    """Direct preparations and driver preparations share one reservation."""
    @wraps(function)
    def guarded(**kwargs):
        inherited = _current.get()
        reservation = inherited or Reservation()
        scope = _current.set(reservation)
        try:
            reservation.workspace(kwargs['workspace'])
            reservation.tree(kwargs['cwd'])
            run = function(**kwargs)
            return replace(run, reservation=reservation)
        except BaseException:
            if inherited is None:
                reservation.cleanup()
            raise
        finally:
            _current.reset(scope)
    return guarded


def driver(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        argument = 'handle' if function.__name__ == 'resume' else 'spec'
        spec_or_handle = args[0] if args else kwargs[argument]
        reservation = Reservation()
        scope = _current.set(reservation)
        retained = False
        cleanup_failed = False
        try:
            if function.__name__ == 'complete':
                if spec_or_handle.log_file:
                    reservation.workspace(Path(spec_or_handle.log_file).absolute().parent)
                reservation.tree(spec_or_handle.cwd if spec_or_handle.cwd is not None else Path.cwd())
            else:
                reservation.workspace(spec_or_handle.workspace)
                spec = kwargs.get('spec') if function.__name__ == 'resume' else spec_or_handle
                if spec is not None:
                    if (function.__name__ == 'resume' and
                            Path(spec.workspace).absolute() != Path(spec_or_handle.workspace).absolute()):
                        raise ValueError('native resume spec workspace differs from handle workspace')
                    reservation.tree(spec.worktree)
            result = function(self, *args, **kwargs)
            if function.__name__ != 'complete':
                reservation.queue()
                retained = True
        except Exception:
            if function.__name__ == 'complete':
                from maestro.backends.base import Completion
                result = Completion(text='', returncode=1)
            else:
                raise
        finally:
            _current.reset(scope)
            if not retained:
                try:
                    reservation.cleanup()
                except Exception:
                    if function.__name__ != 'complete':
                        raise
                    cleanup_failed = True
        if cleanup_failed:
            from maestro.backends.base import Completion
            return Completion(text='', returncode=1)
        return result
    return guarded
