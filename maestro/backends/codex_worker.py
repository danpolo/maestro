"""Frozen launcher-side controller for a bounded stock Codex app-server turn."""
import json
from pathlib import Path
import time

from maestro.backends.codex_app_server import DeadThread, run_turn, verify_worker_config
from maestro.backends import codex_auth, control_files, git_staging, native_ownership
from maestro.tool_policy import ToolPolicy


def main(job_path: str) -> None:
    job = json.loads(control_files.read(Path(job_path)))
    transaction = None
    reservation = native_ownership.Reservation.load(job['reservation']) if job.get('reservation') else None
    deadline = time.monotonic() + job['timeout']
    lifetime = None
    try:
        if reservation is not None:
            reservation.start()
        if job.get('git_transaction'):
            transaction = git_staging.Transaction.load(job['git_transaction'])
        lifetime = codex_auth.Lifetime(job.get('auth_identity'), deadline=deadline)
        lifetime.__enter__()
        if transaction is not None:
            transaction.start()
        result = _run(job, lifetime=lifetime)
        lifetime.verify()
        lifetime.remaining()
        if transaction is not None:
            if result is None or not result.text:
                raise ValueError('native Git completion has no successful final response')
            def before_promotion():
                lifetime.verify()
                lifetime.remaining()
            transaction.finish(success=True, verify=before_promotion)
    finally:
        try:
            control_files.cleanup_resources(
                ('Git', lambda: transaction.cleanup() if transaction is not None else None),
                ('ownership', lambda: native_ownership.cleanup(reservation)),
                ('mountpoint', lambda: control_files.release_mountpoints(job.get('created_mountpoints', ()))))
        finally:
            if lifetime is not None:
                lifetime.__exit__(None, None, None)


def _run(job, *, lifetime) -> None:
    workspace = control_files.workspace(Path(job['workspace']))
    policy = ToolPolicy(allow=tuple(job['allow']), deny=tuple(job['deny']),
                        write_roots=tuple(map(Path, job['write_roots'])))
    with control_files.open_file(workspace / 'impl.log', job['mode']) as log:
        def emit(event):
            log.write(json.dumps(event) + '\n')
            log.flush()
            if event.get('type') == 'thread.started':
                control_files.write(workspace / 'codex_thread_id.txt', event['thread_id'])

        def turn(prompt, resume_id):
            remaining = lifetime.remaining()
            return run_turn(argv=control_files.MountArgv(job['argv'], job.get('mounts', {})), cwd=Path(job['cwd']),
                model=job['model'], effort=job['effort'], prompt=prompt,
                resume_id=resume_id, timeout=remaining, env=job['env'],
                policy=policy, emit=emit, verify_config=lambda response:
                    verify_worker_config(response, job['expected_config'], job['effort']),
                health=lifetime.verify, lifetime=lifetime)

        try:
            try:
                return turn(job['prompt'], job['resume_id'])
            except DeadThread:
                if job['fallback_prompt'] is None:
                    raise
                emit({'type': 'maestro.resume_missing', 'message': 'relaunching with a fresh brief'})
                return turn(job['fallback_prompt'], '')
        except Exception as exc:
            emit({'type': 'maestro.error', 'message': str(exc)})
            raise SystemExit(124 if isinstance(exc, TimeoutError) else 1)
