"""W4 authorization at the stock app-server protocol seam (no model calls)."""
from pathlib import Path
import json
import shlex
import sys

import pytest

from maestro.backends.codex_app_server import ApprovalGate
from maestro.backends import codex_app_server
from maestro.tool_policy import ToolPolicy


@pytest.fixture
def gate(tmp_path):
    return ApprovalGate(ToolPolicy(allow=('Bash(echo *)', 'Bash(cat *)'),
                                  write_roots=(tmp_path,)), 'thread', 'turn')


def request(gate, tmp_path, command='echo OK > result.txt', *, shell='/bin/bash', **extra):
    params = dict(threadId='thread', turnId='turn', itemId='item',
                  cwd=str(tmp_path), command=shlex.quote(shell) + ' -c ' + shlex.quote(command))
    params.update(extra)
    gate.observe({'method': 'item/started', 'params': {
        'threadId': 'thread', 'turnId': 'turn', 'item': {
            'type': 'commandExecution', 'id': 'item',
            'command': params['command'], 'cwd': str(tmp_path)}}})
    return dict(id=0, method='item/commandExecution/requestApproval', params=params)


def accepted(reply):
    return reply.get('result', {}).get('decision') == 'accept'


def test_explicit_single_action_approval_uses_real_policy(gate, tmp_path):
    allowed = request(gate, tmp_path)
    allowed['params']['proposedExecpolicyAmendment'] = ['echo']
    assert gate.answer(allowed) == {'id': 0, 'result': {'decision': 'accept'}}
    assert not accepted(gate.answer(request(gate, tmp_path, 'uname -s')))
    assert not accepted(gate.answer(request(gate, tmp_path, 'cat .env')))
    assert not accepted(gate.answer(request(gate, tmp_path, 'echo OK; uname -s')))


@pytest.mark.parametrize('extra', [
    {'kind': 'stdin'}, {'reason': 'run outside sandbox'},
    {'additionalPermissions': {'network': True}}, {'grantRoot': '/'},
    {'networkApprovalContext': {'host': 'example.com'}},
    {'proposedNetworkPolicyAmendments': [{'allow': 'example.com'}]},
    {'sandbox_permissions': 'require_escalated'}, {'unknownFuturePermission': None},
    {'threadId': 'other'}, {'turnId': 'other'}, {'environmentId': 'remote'},
])
def test_widening_unknown_and_wrong_turn_requests_are_never_accepted(gate, tmp_path, extra):
    assert not accepted(gate.answer(request(gate, tmp_path, **extra)))


def test_mismatched_or_unobserved_item_cannot_be_authorized(gate, tmp_path):
    message = request(gate, tmp_path)
    message['params']['command'] = 'echo different'
    assert not accepted(gate.answer(message))
    message['params']['itemId'] = 'unseen'
    assert not accepted(gate.answer(message))


@pytest.mark.parametrize('shell', ['bash', './bash', '/task/bash', '/task/sh',
                                  '/task/dash', '/task/zsh'])
def test_a_shell_basename_does_not_authorize_an_untrusted_executable(gate, tmp_path, shell):
    assert not accepted(gate.answer(request(gate, tmp_path, 'echo OK', shell=shell)))


def test_guard_failure_and_unknown_request_fail_closed(gate, tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError('failure')
    message = request(gate, tmp_path)
    monkeypatch.setattr(ToolPolicy, 'command', broken)
    assert not accepted(gate.answer(message))
    message['method'] = 'item/permissions/requestApproval'
    assert 'error' in gate.answer(message)


def patch(gate, changes):
    gate.observe({'method': 'item/started', 'params': {
        'threadId': 'thread', 'turnId': 'turn', 'item': {
            'type': 'fileChange', 'id': 'patch', 'changes': changes}}})
    return {'id': 1, 'method': 'item/fileChange/requestApproval', 'params': {
        'threadId': 'thread', 'turnId': 'turn', 'itemId': 'patch'}}


def test_patch_checks_every_source_and_move_target(gate, tmp_path):
    source = str(tmp_path / 'safe.txt')
    assert accepted(gate.answer(patch(gate, [{'path': source, 'kind': {'type': 'add'}}])))
    assert not accepted(gate.answer(patch(gate, [{'path': source, 'kind': {
        'type': 'update', 'move_path': str(tmp_path.parent / 'escape.txt')}}])))
    for target in (str(tmp_path / '.env'), '../relative.txt', str(tmp_path.parent / 'outside')):
        assert not accepted(gate.answer(patch(gate, [{'path': target, 'kind': {'type': 'add'}}])))
    assert not accepted(gate.answer(patch(gate, [])))


def test_completed_item_cannot_grant_later_approval(gate, tmp_path):
    message = request(gate, tmp_path)
    gate.observe({'method': 'item/completed', 'params': {
        'threadId': 'thread', 'turnId': 'turn', 'item': {'id': 'item'}}})
    assert not accepted(gate.answer(message))


@pytest.fixture
def server(tmp_path):
    """Real pipes/process lifetime; only the native server/model boundary is fake."""
    script = tmp_path / 'server.py'
    script.write_text('''
import json, os, sys, time, tomllib
from pathlib import Path
mode = sys.argv[1]
def send(m):
    print(json.dumps(m), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    method = m.get('method')
    if method == 'initialize':
        send({'id': m['id'], 'result': {'userAgent': 'codex/0.163.0'}})
    elif method == 'config/read':
        config = tomllib.loads((Path(os.environ['CODEX_HOME']) / 'config.toml').read_text())
        send({'id': m['id'], 'result': {'config': config}})
    elif method in ('thread/start', 'thread/resume'):
        if mode == 'missing' and method == 'thread/resume':
            send({'id': m['id'], 'error': {'code': -32600, 'message': 'no rollout found for thread id'}})
            continue
        result = {'thread': {'id': m['params'].get('threadId', 'thread')},
                  'model': m['params']['model'], 'approvalPolicy': m['params']['approvalPolicy']}
        if mode == 'substitute':
            result['model'] = 'different'
        send({'id': m['id'], 'result': result})
    elif method == 'turn/start':
        p = m['params']
        if mode == 'hang':
            time.sleep(60)
        if mode == 'malformed':
            print('not json', flush=True)
            continue
        if mode == 'eof':
            sys.exit(0)
        if mode == 'unicode-frames':
            frame = json.dumps({'method': 'test/progress', 'params': {'text': '\\u4e2d' * 1024}}, ensure_ascii=False) + '\\n'
            for _ in range(4000):
                sys.stdout.write(frame)
            sys.stdout.flush()
        if mode in ('many-frames', 'stderr-flood', 'combined-output', 'stderr-valid'):
            frame = json.dumps({'method': 'test/progress', 'params': {'text': 'x' * 1024}}) + '\\n'
            if mode == 'many-frames':
                for _ in range(17 * 1024):
                    sys.stdout.write(frame)
                sys.stdout.flush()
            elif mode == 'stderr-flood':
                for _ in range(17 * 1024):
                    os.write(2, b'x' * 1024)
            elif mode == 'stderr-valid':
                os.write(2, b'x' * 65536)
            else:
                for _ in range(9 * 1024):
                    sys.stdout.write(frame)
                sys.stdout.flush()
                for _ in range(9 * 1024):
                    os.write(2, b'x' * 1024)
        reply = {'id': m['id'], 'result': {'turn': {
            'id': '' if mode == 'empty-turn' else 'other' if mode == 'wrong-reply' else 'turn'}}}
        started = {'method': 'turn/started', 'params': {
            'threadId': 'other' if mode == 'wrong-thread' else 'thread',
            'turn': {'id': 'other' if mode == 'wrong-notification' else 'turn'}}}
        if mode in ('reply-first', 'wrong-notification', 'wrong-thread', 'empty-turn'):
            send(reply)
            send(started)
        else:
            send(started)
            send(reply)
        item = {'id': 'cmd', 'type': 'commandExecution', 'command': "/bin/bash -c 'echo OK'", 'cwd': p['cwd']}
        send({'method': 'item/started', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': item}})
        send({'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'itemId': 'cmd', 'command': item['command'], 'cwd': p['cwd']}})
    elif m.get('id') == 'approval':
        decision = m.get('result', {}).get('decision')
        send({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {
            'id': 'final', 'type': 'agentMessage',
            'phase': 'final_answer' if mode == 'native-final' else 'final', 'text': decision}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {
            'id': 'turn', 'status': 'failed' if mode == 'failed' else 'completed'}}})
''')
    return script


def run_server(server, tmp_path, mode='ok', **kwargs):
    run = getattr(codex_app_server, 'run_turn', None)
    assert callable(run), 'bounded app-server transport is missing'
    return run(argv=[sys.executable, str(server), mode], cwd=tmp_path,
               model='gpt-6-luna', prompt='test', effort='high',
               policy=ToolPolicy(allow=('Bash(echo *)',), write_roots=(tmp_path,)),
               timeout=kwargs.pop('timeout', 3), **kwargs)


@pytest.mark.parametrize('mode', ['ok', 'reply-first', 'native-final'])
def test_real_stdio_approval_round_trip_and_final_message(server, tmp_path, mode):
    result = run_server(server, tmp_path, mode)
    assert result.text == 'accept'
    assert result.thread_id == 'thread'


@pytest.mark.parametrize('mode', ['substitute', 'malformed', 'eof', 'failed',
                                 'wrong-reply', 'wrong-notification', 'wrong-thread', 'empty-turn'])
def test_protocol_and_native_failures_are_never_successful(server, tmp_path, mode):
    run = getattr(codex_app_server, 'run_turn', None)
    assert callable(run), 'bounded app-server transport is missing'
    with pytest.raises(RuntimeError):
        run_server(server, tmp_path, mode)


def test_timeout_reaps_native_process(server, tmp_path):
    run = getattr(codex_app_server, 'run_turn', None)
    assert callable(run), 'bounded app-server transport is missing'
    import os
    import subprocess
    processes = []
    def capture(*a, **kw):
        processes.append(subprocess.Popen(*a, **kw))
        return processes[-1]
    with pytest.raises(TimeoutError):
        run_server(server, tmp_path, 'hang', timeout=.2, popen=capture)
    assert processes[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


def test_resume_missing_thread_is_distinct_from_other_failures(server, tmp_path):
    run = getattr(codex_app_server, 'run_turn', None)
    assert callable(run), 'bounded app-server transport is missing'
    with pytest.raises(codex_app_server.DeadThread):
        run_server(server, tmp_path, 'missing', resume_id='missing')


def test_notification_alone_cannot_authorize_before_start_reply(tmp_path):
    replies = []
    client = codex_app_server._Client(None, deadline=0,
        policy=ToolPolicy(allow=('Bash(echo *)',), write_roots=(tmp_path,)),
        emit=lambda _: None)
    client.thread_id = 'thread'
    client.send = replies.append
    client.event({'method': 'turn/started', 'params': {
        'threadId': 'thread', 'turn': {'id': 'turn'}}})
    message = request(ApprovalGate(client.policy, 'thread', 'turn'), tmp_path)
    client.event(message)
    assert not accepted(replies[-1])
    with pytest.raises(RuntimeError, match='identity mismatch'):
        client.confirm_turn({'turn': {'id': 'different'}})


def test_deadline_applies_to_already_buffered_frames(tmp_path, monkeypatch):
    # A busy native server can fill a single OS read with many notifications.
    # Once the overall deadline expires, queued frames must not extend the turn.
    monkeypatch.setattr(codex_app_server.time, 'monotonic', lambda: 11)
    emitted = []
    client = codex_app_server._Client(None, deadline=10,
        policy=ToolPolicy(allow=(), write_roots=()), emit=emitted.append)
    client.buffer = b'{"method":"thread/status/changed","params":{}}\n' * 100
    with pytest.raises(TimeoutError, match='deadline exceeded'):
        client.receive()
    assert emitted == []


def test_send_deadline_when_native_server_does_not_read_stdin(tmp_path):
    import subprocess
    import time
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    client = codex_app_server._Client(proc, deadline=time.monotonic() + .1,
        policy=ToolPolicy(allow=(), write_roots=()), emit=lambda _: None)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='deadline exceeded'):
            client.send({'prompt': 'x' * 262144})
        assert time.monotonic() - started < .6
    finally:
        codex_app_server._stop(proc)


def test_send_rejects_expired_deadline_before_writing(tmp_path):
    import subprocess
    import time
    proc = subprocess.Popen([sys.executable, '-c',
        'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    client = codex_app_server._Client(proc, deadline=time.monotonic() - 1,
        policy=ToolPolicy(allow=(), write_roots=()), emit=lambda _: None)
    try:
        with pytest.raises(TimeoutError, match='deadline exceeded'):
            client.send({'method': 'must-not-send'})
    finally:
        codex_app_server._stop(proc)


@pytest.mark.parametrize('mode', ['many-frames', 'stderr-flood', 'combined-output', 'unicode-frames'])
def test_whole_turn_output_is_bounded_and_process_reaped(server, tmp_path, mode):
    import subprocess
    processes = []
    events = []
    def capture(*args, **kwargs):
        proc = subprocess.Popen(*args, **kwargs)
        processes.append(proc)
        return proc
    with pytest.raises(RuntimeError, match='output exceeds'):
        run_server(server, tmp_path, mode,
                   popen=capture, emit=events.append)
    assert processes and processes[0].poll() is not None
    assert sum(len(json.dumps(e).encode()) for e in events) <= 16 * 1024 * 1024 + 131072


def test_stderr_pipe_cannot_stall_an_otherwise_valid_turn(server, tmp_path):
    events = []
    result = run_server(server, tmp_path, 'stderr-valid', emit=events.append)
    assert result.text == 'accept'
    diagnostics = [e['message'] for e in events if e.get('type') == 'maestro.native_stderr']
    assert diagnostics == ['x' * 65536]
