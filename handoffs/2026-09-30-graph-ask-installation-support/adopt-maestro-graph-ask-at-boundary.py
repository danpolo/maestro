"""Adopt only this tested candidate after this pilot has accepted and stopped."""
from pathlib import Path
import fcntl
import json
import os
import sqlite3
import subprocess
import sys

REPO=Path('/home/dan/projects/duetflow')
SHA='ead3819ce9110debec25f12dc1fee66174b1367b'
TARGET=Path('/home/dan/.maestro/versions')/SHA
RUN='run_qG3zSNhcUbdHKyDm'
TASK='07-reconciliation'
REPORT=Path('/tmp/maestro-graph-ask-installation.json')


def check_boundary(repo, conn):
    if not (repo/'.orchestrator/HALT').is_file():
        raise RuntimeError('HALT must remain set during installation')
    run=conn.execute('SELECT task_id,status FROM task_runs WHERE run_id=?',(RUN,)).fetchone()
    if run != (TASK,'succeeded'):
        raise RuntimeError('Pilot has not succeeded; no mid-task adoption')
    if conn.execute("SELECT 1 FROM task_runs WHERE status IN ('running','paused') LIMIT 1").fetchone():
        raise RuntimeError('An active graph run prevents adoption')
    if conn.execute("SELECT 1 FROM attempts WHERE status IN ('claimed','running') LIMIT 1").fetchone():
        raise RuntimeError('An active worker prevents adoption')
    if conn.execute("SELECT status FROM tasks WHERE task_id=?",(TASK,)).fetchone() != ('accepted',):
        raise RuntimeError('Pilot has no accepted task record')
    if not conn.execute("SELECT 1 FROM acceptance_decisions WHERE run_id=? AND decision='accepted'",(RUN,)).fetchone():
        raise RuntimeError('Pilot has no durable acceptance decision')
    for (pid,) in conn.execute('SELECT writer_pid FROM writer_leases'):
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            continue
        raise RuntimeError(f'A live writer lease owned by PID {pid} prevents adoption')


def main():
    with (REPO/'.orchestrator/orchestrator.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Pilot controller is still running; no mid-task adoption') from None
        with sqlite3.connect(f"{(REPO/'.orchestrator/control.sqlite3').as_uri()}?mode=ro",uri=True) as conn:
            check_boundary(REPO,conn)
        tested=json.loads(Path('/tmp/maestro-graph-ask-candidate-selftest.json').read_text())
        if not tested.get('passed') or tested.get('sha')!=SHA or tested.get('worktree')!=str(TARGET):
            raise RuntimeError('Exact candidate self-test is missing or failed')
        if subprocess.check_output(['git','rev-parse','HEAD'],cwd=TARGET,text=True).strip()!=SHA:
            raise RuntimeError('Candidate revision changed')
        if subprocess.check_output(['git','status','--porcelain'],cwd=TARGET,text=True).strip():
            raise RuntimeError('Candidate has uncommitted changes')
        os.environ['MAESTRO_REPO']=str(REPO)
        os.environ['MAESTRO_BOOTSTRAPPED']='1'
        os.environ.pop('MAESTRO_HOME',None)
        sys.path.insert(0,str(TARGET))
        from maestro import selfupdate,state
        if Path(selfupdate.__file__).resolve().parents[1]!=TARGET:
            raise RuntimeError('Installer imported another candidate')
        pointer=REPO/'.orchestrator/current'
        previous=pointer.read_bytes()
        previous_state=selfupdate.read_state()
        global_pointer=Path('/home/dan/.maestro/current')
        global_before=global_pointer.read_bytes() if global_pointer.exists() else None
        backup=Path('/tmp/maestro-graph-ask-install-before.json')
        backup.write_text(json.dumps({'project_pin':previous.decode().strip(),
                                     'maestro_version':previous_state.get('maestro_version')},indent=2)+'\n')
        try:
            selfupdate.adopt(SHA,project_repo=REPO)
            state._reset_control_store()
            env={**os.environ,'PYTHONPATH':str(TARGET)}
            doctor=subprocess.run([sys.executable,'-m','maestro.cli','doctor','--repo',str(REPO)],
                                  cwd='/tmp',env=env,text=True,capture_output=True,timeout=180)
            Path('/tmp/maestro-graph-ask-pinned-doctor.log').write_text(doctor.stdout+doctor.stderr)
            if doctor.returncode:
                raise RuntimeError('Pinned doctor failed; inspect /tmp/maestro-graph-ask-pinned-doctor.log')
            if Path(pointer.read_text().strip())!=TARGET:
                raise RuntimeError('Project pin changed during installation')
            global_after=global_pointer.read_bytes() if global_pointer.exists() else None
            if global_before!=global_after:
                raise RuntimeError('Global pointer changed unexpectedly')
            REPORT.write_text(json.dumps({'sha':SHA,'project':str(REPO),'pin':str(TARGET),
                'doctor_exit':doctor.returncode,'self_test':tested['summary'],'halt_retained':True},indent=2)+'\n')
            print(f'Installed {SHA}; pinned doctor exit 0; HALT retained')
        except Exception:
            pointer.write_bytes(previous)
            rollback=selfupdate.read_state()
            rollback['maestro_version']=previous_state.get('maestro_version')
            selfupdate.write_state(rollback)
            selfupdate.append_journal('self_update_rolled_back',f'candidate={SHA} pin={previous.decode().strip()}')
            raise
        finally:
            state._reset_control_store()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'Installation refused: {exc}',file=sys.stderr)
        raise SystemExit(1)
