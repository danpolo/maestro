"""One-time drain of the existing pinned pilot; never open another task."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path

REPO = Path('/home/dan/projects/duetflow')
RUN = 'run_qG3zSNhcUbdHKyDm'
TASK = '07-reconciliation'
PIN = Path('/home/dan/.maestro/versions/5c94c9fead187efc05157272da4eec9b36b19fff')
TERMINAL = Path('/tmp/duetflow-07-install-drain-terminal.json')


def guard_no_new_work(controller, repo, run_id, terminal):
    """Use the ordinary graph loop for its existing run, with no queue intake."""
    controller.parse_runnable_tasks = lambda: []
    controller.parse_prep_tasks = lambda: []
    controller._self_update_maybe = lambda: None
    controller.AUTO_PROPOSE = False
    controller._notify_proposal_test_due = lambda: None
    original = controller._graph_publish_live

    def publish(live):
        if any(row['run_id'] != run_id for row in live):
            controller.HALT_FILE.write_text('Installation drain encountered another run\n')
            raise RuntimeError('Installation drain refuses a successor or unrelated run')
        changed = original(live)
        if not live:
            store = controller._state_module.control_store()
            row = store.conn.execute('SELECT task_id,status FROM task_runs WHERE run_id=?',
                                     (run_id,)).fetchone()
            if row is None or row['status'] not in ('succeeded', 'failed', 'canceled'):
                raise RuntimeError('Expected pilot run has not settled')
            controller.HALT_FILE.write_text('Stopped at pilot task boundary for installation\n')
            terminal.write_text(json.dumps(dict(row), indent=2)+'\n')
            controller.append_journal('installation_task_boundary',
                                      f"{row['task_id']} run={run_id} status={row['status']}")
        return changed

    controller._graph_publish_live = publish


def main():
    # The shell's flock and this same lock file serialize against the existing launcher.
    lock_path = REPO / '.orchestrator/orchestrator.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        halt = REPO / '.orchestrator/HALT'
        if not halt.exists():
            raise RuntimeError('Stop the original controller with HALT before draining')
        if Path((REPO / '.orchestrator/current').read_text().strip()) != PIN:
            raise RuntimeError('Pilot pin changed; review before draining')
        with sqlite3.connect(f"{(REPO / '.orchestrator/control.sqlite3').as_uri()}?mode=ro", uri=True) as conn:
            runs = conn.execute("SELECT run_id,task_id FROM task_runs WHERE status IN ('running','paused')").fetchall()
            if runs != [(RUN, TASK)]:
                raise RuntimeError(f'Expected only the existing pilot, got {runs!r}')
        request = REPO / '.orchestrator/questions/verify-07-reconciliation-2f3bf5abeb3b.json'
        record = json.loads(request.read_text())
        if record.get('run_id') != RUN or record.get('snapshot') != '2f3bf5abeb3b92b34f3a0cc736acbfa3b29ea37a':
            raise RuntimeError('Pending verification identity changed')
        os.environ['MAESTRO_REPO'] = str(REPO)
        os.environ['MAESTRO_BOOTSTRAPPED'] = '1'
        from maestro import orchestrator
        from maestro import state
        if Path(orchestrator.__file__).resolve().parents[1] != PIN:
            raise RuntimeError('Drain imported a different Maestro version')
        guard_no_new_work(orchestrator, REPO, RUN, TERMINAL)
        print(f'Draining only {TASK} {RUN} at unchanged pin {PIN.name}', flush=True)
        halt.unlink()
        try:
            return orchestrator.main()
        finally:
            halt.write_text('Installation drain stopped; keep HALT until verified adoption\n')
            state._reset_control_store()


if __name__ == '__main__':
    raise SystemExit(main())
