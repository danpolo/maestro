"""Controller-created manual commands and their captured execution evidence.

Prep describes a command; it never supplies a success receipt. The controller creates
an attempt/subject-bound wrapper outside the agent's writable worktree. Dan runs it.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid


def _publish(path: Path, write, *, mode: int) -> None:
    """Only a fully flushed artifact becomes visible at its stable controller path."""
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x') as stream:
            os.fchmod(stream.fileno(), mode)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_external(*, run_id: str, attempt_id: str, tree: Path,
                     step_check_cmd: str, expected_result: str) -> dict:
    """A non-shell action owes an executable artifact/result check, not a shell receipt."""
    if not step_check_cmd.strip() or not expected_result.strip():
        raise ValueError('external manual action requires step_check_cmd and expected_result')
    return dict(kind='external', run_id=run_id, attempt_id=attempt_id, tree=str(tree),
                snapshot=subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip(),
                step_check_cmd=step_check_cmd.replace('{worktree}', str(tree)), expected_result=expected_result)


def _check(contract: dict, tree: Path, evidence: dict) -> tuple[str, dict]:
    check = subprocess.run(contract['step_check_cmd'], shell=True, cwd=tree,
                           capture_output=True, text=True, timeout=1800)
    evidence['step_check'] = dict(command=contract['step_check_cmd'], exit_status=check.returncode,
                                  stdout=check.stdout, stderr=check.stderr)
    if check.returncode:
        return f"manual step expected {contract['expected_result']!r}; check exited {check.returncode}: {check.stdout[-750:]}{check.stderr[-750:]}", evidence
    return '', evidence


def prepare(root: Path, *, run_id: str, attempt_id: str, tree: Path,
            command: str, step_check_cmd: str, expected_result: str) -> dict:
    """Create once; re-poll/restart must preserve the same wrapper and captured output."""
    directory = root / '.orchestrator/manual'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = _sha(json.dumps([run_id, attempt_id]).encode())[:24]
    with (directory / f'{identity}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _prepare_locked(root, run_id=run_id, attempt_id=attempt_id, tree=tree,
                               command=command, step_check_cmd=step_check_cmd,
                               expected_result=expected_result)


def _prepare_locked(root: Path, *, run_id: str, attempt_id: str, tree: Path,
                    command: str, step_check_cmd: str, expected_result: str) -> dict:
    if not command.strip() or not step_check_cmd.strip() or not expected_result.strip():
        raise ValueError('manual command requires step_check_cmd and expected_result')
    identity = _sha(json.dumps([run_id, attempt_id]).encode())[:24]
    directory = root / '.orchestrator/manual'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{identity}.json'
    snapshot = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
    binding = dict(run_id=run_id, attempt_id=attempt_id, tree=str(tree), snapshot=snapshot,
                   command=command.replace('{worktree}', str(tree)),
                   step_check_cmd=step_check_cmd.replace('{worktree}', str(tree)),
                   expected_result=expected_result)
    if path.exists():
        contract = json.loads(path.read_text())
        if any(contract.get(k) != v for k, v in binding.items()):
            raise ValueError('manual step changed during the same attempt')
        return contract
    script = directory / f'{identity}.sh'
    contract = dict(binding, nonce=uuid.uuid4().hex, created=time.time(),
                    log=str(directory/f'{identity}.log'), receipt=str(directory/f'{identity}.exit.json'),
                    script=str(script), descriptor=str(path))
    contract['command_hash'] = _sha(contract['command'].encode())
    # This program intentionally uses only the standard library: running the prepared
    # step must not depend on the target checkout's import path or installed release.
    program = '''import hashlib, json, os, pathlib, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
c = json.loads(p.read_text())
expected = json.loads(EXPECTED_CONTRACT)
if any(c.get(k) != v for k, v in expected.items()):
    sys.exit('Manual step descriptor changed; request Redo before running it.')
if hashlib.sha256(pathlib.Path(c['script']).read_bytes()).hexdigest() != c['script_hash']:
    sys.exit('Manual step script changed; request Redo before running it.')
subject = subprocess.check_output(['git', '-C', c['tree'], 'rev-parse', 'HEAD'], text=True).strip()
if subject != c['snapshot']:
    sys.exit('Manual step source changed; request Redo before running it.')
receipt = pathlib.Path(c['receipt'])
if receipt.exists():
    sys.exit('This step already ran; Maestro retains its receipt. Request Redo for a new step.')
log = pathlib.Path(c['log'])
with log.open('xb') as stream:
    proc = subprocess.Popen(['bash', '-c', c['command']], cwd=c['tree'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    while True:
        block = proc.stdout.read(8192)
        if not block:
            break
        stream.write(block)
        stream.flush()
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
    status = proc.wait()
    os.fsync(stream.fileno())
r = {k: c[k] for k in ('run_id', 'attempt_id', 'snapshot', 'command_hash', 'nonce')}
r.update(exit_status=status, log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(), finished=time.time())
temp = receipt.with_suffix('.tmp')
with temp.open('x') as stream:
    json.dump(r, stream)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temp, receipt)
fd = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
sys.exit(status)
'''
    program = program.replace('EXPECTED_CONTRACT', repr(json.dumps(contract, sort_keys=True)))
    source = ('#!/usr/bin/env bash\nset -euo pipefail\numask 077\n'
                      + shlex.quote(sys.executable) + ' - ' + shlex.quote(str(path))
                      + " <<'MAESTRO_MANUAL_CAPTURE'\n" + program + 'MAESTRO_MANUAL_CAPTURE\n')
    _publish(script, lambda stream: stream.write(source), mode=0o700)
    contract['script_hash'] = _sha(script.read_bytes())
    _publish(path, lambda stream: json.dump(contract, stream, sort_keys=True), mode=0o600)
    return contract


def verify(contract: dict, tree: Path) -> tuple[str, dict]:
    """Fail closed on missing, altered, stale or nonzero command evidence, then check."""
    try:
        current = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
        if current != contract['snapshot']:
            return 'manual step evidence names a stale source snapshot', {}
        if contract.get('kind') == 'external':
            return _check(contract, tree, {'contract': contract})
        if _sha(Path(contract['script']).read_bytes()) != contract['script_hash']:
            return 'manual step script changed after preparation', {}
        if json.loads(Path(contract['descriptor']).read_text()) != contract:
            return 'manual step descriptor changed after preparation', {}
        result = json.loads(Path(contract['receipt']).read_text())
        for key in ('run_id', 'attempt_id', 'snapshot', 'command_hash', 'nonce'):
            if result.get(key) != contract[key]:
                return f'manual step evidence has a stale or mismatched {key}', {}
        if not contract['created'] <= result['finished'] <= time.time() + 5:
            return 'manual step evidence has a stale completion time', {}
        log = Path(contract['log']).read_bytes()
        if _sha(log) != result['log_sha256']:
            return 'manual step output does not match its captured receipt', {}
        evidence = dict(contract=contract, receipt=result, output=log.decode(errors='replace'))
        if type(result['exit_status']) is not int or result['exit_status'] != 0:
            return f"manual step expected {contract['expected_result']!r}; exited {result['exit_status']}: {evidence['output'][-1500:]}", evidence
        return _check(contract, tree, evidence)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return f'manual step evidence missing or invalid: {type(exc).__name__}: {exc}', {}
