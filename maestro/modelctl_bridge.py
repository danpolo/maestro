"""Bounded subprocess adapter; modelctl retains discovery and table ownership.

No scanner, notification, daemon restart or inference is called here; registration only
through the owner's own `modelctl add` command, for a Telegram model ask Dan completed.
An owner without the durable outbox contract cannot silently use last_scan instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from maestro.model_policy import PolicyError


_SCRIPT = r'''
import dataclasses, hashlib, json, pathlib, sys
sys.path.insert(0, sys.argv[1])
request = json.loads(sys.stdin.read())
try:
    from modelctl.paths import Env
    from modelctl.core import table
    from modelctl.core.names import fold
    from modelctl.core.scan import latest_per_family
    from modelctl.providers.claude import ClaudeProvider
    from modelctl.providers.codex import CodexProvider
    from modelctl.providers.antigravity import AntigravityProvider, class_key
    env = Env.default()
    if request.get('home'):
        env = Env(home=pathlib.Path(request['home']), maestro_repo=env.maestro_repo)
    operation = request['operation']
    if operation == 'classes':
        from modelctl.core.transitions import configured_models, CONTRACT_VERSION
        if CONTRACT_VERSION != 1: raise ValueError('incompatible class contract')
        result = configured_models(env, request['provider'], identities=request.get('identities', []))
    elif operation in ('pending', 'ack'):
        from modelctl.core import outbox
        if outbox.CONTRACT_VERSION != 1:
            raise ValueError('incompatible outbox')
        result = outbox.pending(env) if operation == 'pending' else outbox.acknowledge(env, request['event_id'])
    elif operation == 'access':
        # The owner's last inference check per tracked model, read like `retired`.
        data = json.loads(env.state_file.read_text(encoding='utf-8'))
        models = data['providers'][request['provider']]['models']
        if not isinstance(models, dict) or not all(isinstance(m, dict) for m in models.values()):
            raise ValueError('unexpected state shape')
        result = {mid: m['access'] for mid, m in models.items()
                  if m.get('access') in ('VERIFIED', 'INCONCLUSIVE', 'FAILED')}
    elif operation == 'stopped':
        # Models whose automatic retests stopped and that Dan has not removed, read like `access`.
        data = json.loads(env.state_file.read_text(encoding='utf-8'))
        models = data['providers'][request['provider']]['models']
        if not isinstance(models, dict) or not all(isinstance(m, dict) for m in models.values()):
            raise ValueError('unexpected state shape')
        result = {mid: dict(reason=m['retest_stopped']['reason'], at=m['retest_stopped']['at'], access=m.get('access'))
                  for mid, m in models.items()
                  if isinstance(m.get('retest_stopped'), dict) and not m.get('removed_by_dan')}
    elif operation == 'drop_from_tests':
        from modelctl.core.scan import drop_from_tests
        result = drop_from_tests(env, request['provider'], request['model'])
    elif operation == 'retired':
        # Read the owner's state file directly: State() would move a corrupt file aside.
        data = json.loads(env.state_file.read_text(encoding='utf-8'))
        models = data['providers'][request['provider']]['models']
        if not isinstance(models, dict) or not all(isinstance(m, dict) for m in models.values()):
            raise ValueError('unexpected state shape')
        removed = sorted(mid for mid, m in models.items() if m.get('removed') is True)
        result = dict(provider=request['provider'], removed=removed,
                      state_hash=hashlib.sha256(json.dumps(removed).encode()).hexdigest())
    else:
        provider = request['provider']
        p = {'claude': ClaudeProvider, 'codex': CodexProvider, 'antigravity': AntigravityProvider}[provider](env)
        path = getattr(env, provider + '_limits')
        content = path.read_bytes()
        parsed = table.read_lifecycles(content.decode())
        def lifecycle(item):
            name, life = item
            if life is None:
                return None
            return dict(display=name, prepare=list(life.prepare), fresh=list(life.fresh), ceiling=life.ceiling)
        rows = {key: lifecycle(value) for key, value in parsed.items() if key != fold(table.UNKNOWN_ROW)}
        result = dict(provider=provider, rows=rows,
                      unknown=lifecycle(parsed[fold(table.UNKNOWN_ROW)]) if fold(table.UNKNOWN_ROW) in parsed else None,
                      table_hash=hashlib.sha256(content).hexdigest())
        if operation == 'discover':
            candidates = list(latest_per_family(p.discover()).values())
            row_key = class_key if provider == 'antigravity' else fold
            class_rows = {row_key(value['display']): value for value in rows.values()}
            result['candidates'] = [dict(backend_id=provider, model_id=c.id, display=c.display,
                family=c.family, version=list(c.version), fingerprint=c.fingerprint,
                registered=row_key(c.id) in class_rows, lifecycle=class_rows.get(row_key(c.id)),
                access='metadata_only') for c in candidates]
    print(json.dumps(dict(ok=True, result=result), allow_nan=False))
except Exception as exc:
    # Do not copy exception messages/credential-bearing provider output across the boundary.
    print(json.dumps(dict(ok=False, error=type(exc).__name__)))
'''


class ModelctlBridge:
    def __init__(self, source: Path | None = None, *, home: Path | None = None, timeout=60):
        raw = source or os.environ.get('MODELCTL_SOURCE')
        self.source = Path(raw).resolve() if raw else None
        self.home = home
        self.timeout = timeout

    def _call(self, operation, provider=None, **extra):
        if self.source is None or not (self.source / 'modelctl/paths.py').is_file():
            raise PolicyError('modelctl source missing; set MODELCTL_SOURCE to its compatible checkout')
        request = dict(operation=operation, provider=provider,
                       home=str(self.home) if self.home else None, **extra)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        # Isolated interpreter: neither PYTHONPATH nor the caller's imports affect modelctl.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run([sys.executable, '-I', '-c', _SCRIPT, str(self.source)],
                                        input=json.dumps(request).encode(), stdout=output, stderr=errors,
                                        timeout=self.timeout, cwd=self.source, env=environment)
                if result.returncode or output.tell() > 2_000_000:
                    raise PolicyError('modelctl adapter failed or exceeded output bound')
                output.seek(0)
                response = json.loads(output.read())
                if response.get('ok') is not True:
                    contract = 'durable outbox' if operation in ('pending', 'ack') else operation
                    raise PolicyError(f'modelctl {contract} unavailable or incompatible ({response.get("error", "unknown")})')
                return response['result']
            except PolicyError:
                raise
            except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as exc:
                raise PolicyError('modelctl adapter timed out or returned invalid data') from exc

    def _provider(self, backend):
        from maestro.backends import registry
        try:
            driver = registry.driver_name_for_backend_id(backend)
            provider = registry.driver_ref(driver).modelctl_provider
        except registry.UnknownBackend:
            provider = None
        if provider is None:
            raise PolicyError(f'{backend}: modelctl discovery/registration unsupported')
        return provider

    def limits(self, backend):
        return self._call('limits', self._provider(backend))

    def discover(self, backend):
        result = self._call('discover', self._provider(backend))
        result['candidates'] = [dict(row, backend_id=backend) for row in result['candidates']]
        return result

    def retired(self, backend):
        """Owner's explicit removed-model signal. Any read failure is unknown availability."""
        try:
            return self._call('retired', self._provider(backend))
        except PolicyError as exc:
            raise PolicyError(f'{backend}: modelctl removal state unavailable; not treated as retirement') from exc

    def access(self, backend):
        """model id -> modelctl's last access check (VERIFIED, INCONCLUSIVE or FAILED)."""
        result = self._call('access', self._provider(backend))
        if not isinstance(result, dict):
            raise PolicyError('modelctl access state invalid')
        return result

    def stopped(self, backend):
        """model id -> {reason, at, access} for models whose automatic retests are stopped."""
        result = self._call('stopped', self._provider(backend))
        if not isinstance(result, dict) or not all(isinstance(stop, dict) for stop in result.values()):
            raise PolicyError('modelctl stopped state invalid')
        return result

    def drop_from_tests(self, backend, model) -> None:
        """Dan's Remove model: the owner never tests it again until a re-add."""
        try:
            self._call('drop_from_tests', self._provider(backend), model=model)
        except PolicyError as exc:
            if 'LockBusy' in str(exc):
                raise PolicyError('modelctl is busy with a test or scan, tap again in a few minutes') from exc
            raise

    def pending_events(self):
        from maestro.backends import registry
        events = self._call('pending')
        if not isinstance(events, list):
            raise PolicyError('invalid modelctl outbox')
        result = []
        for event in events:
            if not isinstance(event, dict):
                raise PolicyError('invalid modelctl outbox event')
            try:
                backend = registry.backend_id_for_modelctl_provider(event.get('provider'))
            except registry.UnknownBackend as exc:
                raise PolicyError('unregistered modelctl outbox provider') from exc
            # Journal/ack identity stays exactly the owner's event_id.
            result.append(dict(event, provider=backend))
        return result

    def acknowledge(self, event_id: str):
        return self._call('ack', event_id=event_id)


    def _command(self, argv, timeout) -> subprocess.CompletedProcess:
        """Run one owner command in an isolated interpreter, with its `~/.local/bin` tools on PATH."""
        if self.source is None or not (self.source / 'modelctl/cli.py').is_file():
            raise PolicyError('modelctl source missing; set MODELCTL_SOURCE to its compatible checkout')
        script = ('import sys; sys.path.insert(0,sys.argv[1]); from modelctl.cli import main; '
                  'raise SystemExit(main(sys.argv[2:]))')
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        if self.home:
            environment['MODELCTL_HOME'] = str(self.home)
        local_bin = str((self.home or Path.home()) / '.local' / 'bin')
        current_path = os.environ.get('PATH', '')
        if local_bin not in current_path.split(':'):
            environment['PATH'] = f"{local_bin}:{current_path}" if current_path else local_bin
        return subprocess.run([sys.executable, '-I', '-c', script, str(self.source), *argv], cwd=self.source,
                              env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)

    def add(self, backend, argv, *, timeout=900) -> tuple[bool, str]:
        """Run the owner's transactional `modelctl add` (Telegram model asks); never edits tables here.

        modelctl validates, writes, smoke-tests inside its rollback boundary and hands
        MODEL_SET to Maestro itself. Returns (succeeded, combined output) for the ask to report.
        """
        try:
            result = self._command(['add', self._provider(backend), *argv], timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f'modelctl add did not finish ({type(exc).__name__})'
        return result.returncode == 0, (result.stdout + result.stderr)[-20_000:]

    def retest(self, backend, model, *, timeout=420) -> dict:
        """Test one model now, whatever its stop says: Dan's Retry button. The test takes up to
        180 seconds and modelctl's handover to `maestro models consume` up to 90."""
        argv = ['retest', '--provider', self._provider(backend), '--model', model, '--json']
        try:
            result = self._command(argv, timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise PolicyError(f'modelctl retest did not finish ({type(exc).__name__})') from exc
        try:
            answer = json.loads(result.stdout)
        except ValueError:
            answer = None
        if isinstance(answer, dict) and answer.get('error'):
            raise PolicyError(str(answer['error']))
        if (result.returncode or not isinstance(answer, dict)
                or answer.get('outcome') not in ('passed', 'rate_limited', 'stopped')):
            raise PolicyError(f'modelctl retest gave no usable result (exit {result.returncode})')
        return answer

    def configured_models(self, backend, *, identities=()):
        from maestro.backends.catalog import SHIPPED_CATALOG_DEFAULTS
        requested = set(identities)
        requested.update(model for base in SHIPPED_CATALOG_DEFAULTS.values()
                             if base['backend_id'] == backend
                             for model in base.get('model_profiles', {}))
        from maestro.model_policy import shipped_policy
        driver = self._provider(backend)
        policy = shipped_policy()
        requested.update(row['model_id'] for row in policy['routes'] if row['backend_id'] == backend)
        requested.update(entry['models'][driver] for entry in policy['roles'].values()
                          if driver in entry.get('models', {}))
        result = self._call('classes', self._provider(backend), identities=sorted(requested))
        result['models'] = [dict(row, backend_id=backend) for row in result['models']]
        return result
