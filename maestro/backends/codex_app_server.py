"""Maestro-owned, fail-closed stock Codex app-server approval handling."""
from pathlib import Path
from dataclasses import dataclass
import json
import os
import select
import shlex
import signal
import subprocess
import time
from typing import Callable, Mapping, Sequence

from maestro.tool_policy import Decision, ToolPolicy

COMMAND_APPROVAL = 'item/commandExecution/requestApproval'
FILE_APPROVAL = 'item/fileChange/requestApproval'
OUTPUT_LIMIT = 16 * 1024 * 1024
STDERR_SAMPLE_LIMIT = 131072
_COMMON = {'threadId', 'turnId', 'itemId', 'startedAtMs', 'reason'}
_COMMAND_FIELDS = _COMMON | {
    'kind', 'approvalId', 'command', 'cwd', 'commandActions', 'environmentId',
    'additionalPermissions', 'networkApprovalContext', 'proposedExecpolicyAmendment',
    'proposedNetworkPolicyAmendments', 'availableDecisions'}
_FILE_FIELDS = _COMMON | {'grantRoot'}


def verify_worker_config(response: dict, expected_config: str, effort: str | None) -> None:
    """Check effective native facts against the immutable per-call profile."""
    def normalized(value):
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [normalized(v) for v in value]
        return value

    config = response.get('config')
    expected = json.loads(expected_config)
    if not isinstance(config, dict):
        raise RuntimeError('Codex effective worker configuration does not match its sealed profile')
    actual = normalized(config)
    mismatch = any(actual.get(key) != value for key, value in expected.items()
                   if key != 'features')
    if (mismatch
            or any(actual.get('features', {}).get(k) is not value
                   for k, value in expected['features'].items())
            or any(actual.get(k) for k in ('mcp_servers', 'plugins', 'hooks', 'tools',
                'notify', 'instructions', 'developer_instructions', 'model_providers',
                'openai_base_url', 'model_instructions_file', 'profile', 'default_permissions'))
            or actual.get('shell_environment_policy')
            or actual.get('approval_policy') is not None
            or actual.get('model_reasoning_effort') != effort):
        raise RuntimeError('Codex effective worker configuration does not match its sealed profile')


def _script(command: str) -> str:
    """Unwrap only the native literal shell invocation, never arbitrary wrappers."""
    args = shlex.split(command)
    shells = {'sh', 'bash', 'dash', 'zsh'}
    if len(args) == 3 and Path(args[0]).name in shells and args[1] in ('-c', '-lc'):
        # A task can create its own executable named bash. Never authorize its
        # arbitrary program by checking only the script argument. Native shells
        # must use a literal system path on the read-only host filesystem.
        if args[0] not in {f'{directory}/{name}' for directory in ('/bin', '/usr/bin')
                           for name in shells}:
            raise ValueError('untrusted shell executable')
        return args[2]
    if args and Path(args[0]).name in shells:
        raise ValueError('unsupported shell invocation')
    return command


class ApprovalGate:
    def __init__(self, policy: ToolPolicy, thread_id: str, turn_id: str):
        self.policy = policy
        self.thread_id, self.turn_id = thread_id, turn_id
        self.items: dict[str, dict] = {}
        self.last_reason = ''

    def observe(self, message: dict) -> None:
        params = message.get('params', {})
        if not isinstance(params, dict) or not self._matches(params):
            return
        item = params.get('item')
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            return
        if message.get('method') == 'item/started':
            self.items[item['id']] = item
        elif message.get('method') == 'item/completed':
            self.items.pop(item['id'], None)

    def _matches(self, params: dict) -> bool:
        return params.get('threadId') == self.thread_id and params.get('turnId') == self.turn_id

    def answer(self, message: dict) -> dict:
        method = message.get('method')
        if method not in (COMMAND_APPROVAL, FILE_APPROVAL):
            self.last_reason = 'unsupported server request'
            return {'id': message['id'], 'error': {'code': -32601,
                                                  'message': self.last_reason}}
        try:
            decision = self._decide(method, message.get('params'))
        except Exception as exc:
            decision = Decision(False, f'approval guard failure: {type(exc).__name__}')
        self.last_reason = decision.reason
        return {'id': message['id'], 'result': {
            'decision': 'accept' if decision.allowed else 'decline'}}

    def _decide(self, method: str, params: dict) -> Decision:
        fields = _COMMAND_FIELDS if method == COMMAND_APPROVAL else _FILE_FIELDS
        if not isinstance(params, dict) or set(params) - fields or not self._matches(params):
            return Decision(False, 'unknown fields or mismatched active turn')
        # The installed protocol has no exhaustive escalation-kind field. A reason
        # may indicate an escape from the sandbox; conservatively refuse it. Never
        # accept session/rule amendments, grants, network requests or terminal input.
        if any(params.get(k) for k in ('reason', 'additionalPermissions',
               'networkApprovalContext', 'proposedNetworkPolicyAmendments', 'grantRoot')):
            return Decision(False, 'permission widening is refused')
        item = self.items.get(params.get('itemId'))
        if not item:
            return Decision(False, 'no matching tool item observed')
        if method == COMMAND_APPROVAL:
            if params.get('kind', 'command') != 'command':
                return Decision(False, 'terminal input is refused')
            if params.get('environmentId') not in (None, 'local'):
                return Decision(False, 'remote environment is refused')
            command, cwd = params.get('command'), params.get('cwd')
            if (item.get('type') != 'commandExecution' or not isinstance(command, str)
                    or not isinstance(cwd, str) or not Path(cwd).is_absolute()
                    or command != item.get('command') or cwd != item.get('cwd')):
                return Decision(False, 'command does not match the observed item')
            decisions = params.get('availableDecisions')
            if decisions is not None and (not isinstance(decisions, list) or 'accept' not in decisions):
                return Decision(False, 'single-action acceptance is unavailable')
            return self.policy.command(_script(command), cwd=Path(cwd))
        if item.get('type') != 'fileChange':
            return Decision(False, 'not a file-change item')
        changes = item.get('changes')
        if not isinstance(changes, list) or not changes:
            return Decision(False, 'file change needs literal paths')
        for change in changes:
            if not isinstance(change, dict) or not isinstance(change.get('path'), str):
                return Decision(False, 'file change lacks a path')
            kind = change.get('kind')
            if not isinstance(kind, dict) or kind.get('type') not in ('add', 'delete', 'update'):
                return Decision(False, 'unknown file-change operation')
            if set(kind) - {'type', 'move_path'}:
                return Decision(False, 'unknown file-change fields')
            targets = [change['path']]
            if kind.get('move_path') is not None:
                targets.append(kind['move_path'])
            for target in targets:
                if not isinstance(target, str):
                    return Decision(False, 'file change needs a literal target')
                decision = self.policy.file('Edit', Path(target))
                if not decision.allowed:
                    return decision
        return Decision(True, 'every changed path is permitted')


class DeadThread(RuntimeError):
    """Only a native missing-rollout response permits a fresh-launch fallback."""


@dataclass(frozen=True)
class TurnResult:
    text: str
    thread_id: str


class _Client:
    def __init__(self, proc, *, deadline, policy, emit, health=None):
        self.proc, self.deadline, self.policy, self.emit = proc, deadline, policy, emit
        self.health = health or (lambda: None)
        self.buffer = b''
        self.sequence = 0
        self.thread_id = ''
        self.turn_id = ''
        self.turn_reply_id = ''
        self.gate = None
        self.final = ''
        self.completed = None
        self.output_bytes = 0
        self.stderr_sample = bytearray()
        self.stderr_open = getattr(proc, 'stderr', None) is not None
        self.stdout_open = True

    def _account(self, chunk):
        self.output_bytes += len(chunk)
        if self.output_bytes > OUTPUT_LIMIT:
            raise RuntimeError('Codex app-server output exceeds 16 MiB')

    def drain_stderr(self):
        # Also poll while consuming already buffered protocol frames. Otherwise
        # an active stdout stream could starve stderr until the OS pipe fills.
        while self.stderr_open:
            ready, _, _ = select.select([self.proc.stderr], [], [], 0)
            if not ready:
                break
            chunk = os.read(self.proc.stderr.fileno(), 65536)
            if not chunk:
                self.stderr_open = False
                break
            self._account(chunk)
            self.stderr_sample.extend(chunk[:max(0, STDERR_SAMPLE_LIMIT - len(self.stderr_sample))])
            if time.monotonic() >= self.deadline:
                raise TimeoutError('Codex app-server deadline exceeded')

    def confirm_turn(self, result):
        turn = result.get('turn')
        tid = turn.get('id') if isinstance(turn, dict) else None
        if (not isinstance(tid, str) or not tid
                or (self.turn_id and tid != self.turn_id)):
            raise RuntimeError('Codex turn/start identity mismatch')
        self.turn_reply_id = tid
        # Both independently delivered identities must agree before approvals.
        if self.turn_id:
            self.gate = ApprovalGate(self.policy, self.thread_id, tid)

    def send(self, message):
        # BufferedIO.write/flush can block forever when the native server stops
        # reading. Use the pipe directly so every partial write shares the turn's
        # deadline, including approval replies and large prompts.
        pending = memoryview((json.dumps(message) + '\n').encode())
        if len(pending) > 4 * 1024 * 1024:
            raise RuntimeError('Codex app-server frame exceeds 4 MiB')
        fd = self.proc.stdin.fileno()
        os.set_blocking(fd, False)
        while pending:
            self.health()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex app-server deadline exceeded')
            _, ready, _ = select.select([], [fd], [], min(remaining, .1))
            if not ready:
                continue
            try:
                written = os.write(fd, pending[:65536])
            except BlockingIOError:
                continue
            if written == 0:
                raise RuntimeError('Codex app-server closed stdin before completion')
            pending = pending[written:]

    def receive(self):
        self.health()
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Codex app-server deadline exceeded')
        self.drain_stderr()
        while b'\n' not in self.buffer:
            self.health()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex app-server deadline exceeded')
            if not self.stdout_open:
                raise RuntimeError('Codex app-server closed stdout before completion')
            streams = [self.proc.stdout]
            if self.stderr_open:
                streams.append(self.proc.stderr)
            ready, _, _ = select.select(streams, [], [], min(remaining, .1))
            if not ready:
                continue
            self.drain_stderr()
            if self.proc.stdout in ready:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                self._account(chunk)
                self.stdout_open = bool(chunk)
                self.buffer += chunk
            if len(self.buffer) > 4 * 1024 * 1024:
                raise RuntimeError('Codex app-server frame exceeds 4 MiB')
        line, self.buffer = self.buffer.split(b'\n', 1)
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError('invalid Codex app-server JSON frame') from exc
        if not isinstance(message, dict):
            raise RuntimeError('invalid Codex app-server message')
        if 'method' in message:
            self.event(message)
        self.health()
        return message

    def event(self, message):
        self.health()
        method = message['method']
        params = message.get('params') or {}
        if not isinstance(params, dict):
            raise RuntimeError('invalid Codex notification parameters')
        if 'id' in message:
            reply = (self.gate.answer(message) if self.gate is not None else {
                'id': message['id'], 'error': {'code': -32601, 'message': 'no active turn'}})
            self.send(reply)
            self.emit({'type': 'maestro.approval', 'method': method,
                       'item_id': params.get('itemId'), 'reply': reply,
                       'reason': self.gate.last_reason if self.gate else 'no active turn'})
            return
        self.emit(message)
        if method == 'turn/started':
            turn = params.get('turn') or {}
            tid = turn.get('id')
            if params.get('threadId') != self.thread_id or not isinstance(tid, str) or not tid:
                raise RuntimeError('mismatched Codex turn identity')
            if ((self.turn_id and tid != self.turn_id)
                    or (self.turn_reply_id and tid != self.turn_reply_id)):
                raise RuntimeError('unexpected second Codex turn')
            self.turn_id = tid
            if self.turn_reply_id:
                self.gate = ApprovalGate(self.policy, self.thread_id, tid)
        if self.gate is not None:
            self.gate.observe(message)
        if params.get('threadId') != self.thread_id:
            return
        if method == 'item/completed' and params.get('turnId') == self.turn_id:
            item = params.get('item') or {}
            if item.get('type') == 'agentMessage' and item.get('phase') in (None, 'final', 'final_answer'):
                value = item.get('text')
                if isinstance(value, str):
                    self.final = value
        if method == 'turn/completed':
            turn = params.get('turn') or {}
            if not self.turn_id or turn.get('id') != self.turn_id:
                raise RuntimeError('mismatched Codex completion identity')
            self.completed = turn

    def rpc(self, method, params):
        self.sequence += 1
        rid = self.sequence
        self.send({'id': rid, 'method': method, 'params': params})
        while True:
            message = self.receive()
            if message.get('id') == rid and 'method' not in message:
                if 'error' in message:
                    error = message['error']
                    text = json.dumps(error)
                    if method == 'thread/resume' and 'no rollout found for thread id' in text.lower():
                        raise DeadThread(text)
                    raise RuntimeError(f'Codex {method}: {text}')
                result = message.get('result')
                if not isinstance(result, dict):
                    raise RuntimeError(f'Codex {method} returned no result')
                return result


def _stop(proc):
    """Reap the app-server and its child group on success, error or timeout."""
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
    proc.stdout.close()
    if getattr(proc, 'stderr', None) is not None:
        proc.stderr.close()


def run_turn(*, argv: Sequence[str], cwd: Path, model: str, prompt: str,
             effort: str | None, policy: ToolPolicy, timeout: float,
             resume_id: str = '', env: Mapping[str, str] | None = None,
             popen: Callable = subprocess.Popen, emit: Callable[[dict], None] | None = None,
             verify_config: Callable[[dict], None] | None = None,
             health: Callable[[], None] | None = None, lifetime=None) -> TurnResult:
    """One fresh/resumed native turn, with an overall deadline and no implicit approvals.

    Caller supplies the sealed runtime argv/environment. No inference begins until
    the effective configuration and returned thread model/policy are verified.
    The explicit popen seam is for deterministic process/protocol integration tests.
    """
    if timeout <= 0:
        raise ValueError('Codex turn needs a positive timeout')
    deadline = time.monotonic() + timeout
    callback = emit or (lambda message: None)
    emitted_bytes = 0

    def emit(event, *, required=True):
        # JSON logs can expand native UTF-8 and add approval records. Bound the
        # serialized stream independently of bytes received from native pipes.
        nonlocal emitted_bytes
        size = len((json.dumps(event) + '\n').encode())
        if emitted_bytes + size > OUTPUT_LIMIT:
            if required:
                raise RuntimeError('Codex app-server output exceeds 16 MiB serialized log')
            return
        emitted_bytes += size
        callback(event)

    from maestro.backends import control_files
    health = health or (lambda: None)
    health()
    with control_files.pinned_mounts(argv) as (pinned, fds):
        health()
        if lifetime is not None:
            pinned, fds = lifetime.child_arguments(pinned, fds)
        proc = popen(pinned, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                     **({'pass_fds': fds} if fds else {}))
    client = _Client(proc, deadline=deadline, policy=policy, emit=emit, health=health)
    try:
        health()
        client.rpc('initialize', {'clientInfo': {'name': 'maestro', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        client.send({'method': 'initialized'})
        if verify_config is not None:
            verify_config(client.rpc('config/read', {'cwd': str(cwd)}))
        params = {'cwd': str(cwd), 'model': model, 'modelProvider': 'openai',
                  'approvalPolicy': 'untrusted', 'approvalsReviewer': 'user',
                  'permissions': 'maestro'}
        if resume_id:
            params['threadId'] = resume_id
        response = client.rpc('thread/resume' if resume_id else 'thread/start', params)
        tid = (response.get('thread') or {}).get('id')
        if (not isinstance(tid, str) or not tid or (resume_id and tid != resume_id)
                or response.get('model') != model or response.get('approvalPolicy') != 'untrusted'):
            raise RuntimeError('Codex substituted the requested model, thread or approval policy')
        client.thread_id = tid
        emit({'type': 'thread.started', 'thread_id': tid})
        start = {'threadId': tid, 'cwd': str(cwd), 'approvalPolicy': 'untrusted',
                 'approvalsReviewer': 'user', 'model': model,
                 'input': [{'type': 'text', 'text': prompt}]}
        if effort is not None:
            start['effort'] = effort
        result = client.rpc('turn/start', start)
        client.confirm_turn(result)
        while client.completed is None:
            client.receive()
        if client.completed.get('status') != 'completed':
            raise RuntimeError('Codex turn failed: ' + json.dumps(client.completed))
        client.drain_stderr()
        health()
        return TurnResult(client.final.strip(), tid)
    finally:
        try:
            _stop(proc)
        finally:
            if lifetime is not None:
                lifetime.finish_child()
        diagnostic = client.stderr_sample.decode(errors='replace').strip()
        if diagnostic:
            emit({'type': 'maestro.native_stderr', 'message': diagnostic}, required=False)
