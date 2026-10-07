"""A bounded capture-only listener for the agreed Telegram intake smoke test.

Run only after pin-only adoption is approved. Never starts the watchdog/controller,
prepares a proposal, publishes a task, or clears HALT.
"""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

REPO = Path('/home/dan/projects/duetflow')
SHA = 'f46ffd403785bcd9040e892961731c2009fc6220'
TARGET = Path('/home/dan/.maestro/versions') / SHA
SUPPORT = Path('/home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release')


def main():
    if Path((REPO / '.orchestrator/current').read_text().strip()) != TARGET:
        raise RuntimeError('Install the verified intake candidate first')
    if not (REPO / '.orchestrator/HALT').is_file():
        raise RuntimeError('HALT must remain set')
    with (REPO / '.orchestrator/orchestrator.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect((REPO / '.orchestrator/control.sqlite3').as_uri() + '?mode=ro', uri=True) as c:
            if c.execute("SELECT 1 FROM task_runs WHERE status IN ('running','paused')").fetchone():
                raise RuntimeError('Active runs prevent the smoke listener')
            if c.execute("SELECT 1 FROM attempts WHERE status IN ('pending','claimed','running')").fetchone():
                raise RuntimeError('Active attempts prevent the smoke listener')
        os.environ['MAESTRO_REPO'] = str(REPO)
        os.environ['MAESTRO_BOOTSTRAPPED'] = '1'
        os.environ['MAESTRO_TELEGRAM_LISTENER'] = '0'
        sys.path.insert(0, str(TARGET))
        from maestro.cli import _load_project_env
        _load_project_env(REPO)
        from maestro.hitl import commands
        from maestro import intake
        from maestro.hitl.telegram import notify_intake
        assert Path(intake.__file__).resolve().parents[1] == TARGET
        deadline = time.monotonic() + 600
        offset = 0
        chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
        print('Ready: @MaestroGenericTestingBot; capture only; HALT retained; 10 minute limit', flush=True)
        while time.monotonic() < deadline:
            if not (REPO / '.orchestrator/HALT').is_file():
                raise RuntimeError('HALT changed; listener stopped without launching work')
            for update in commands._fetch_updates(offset, limit=100):
                message = update.get('message') or {}
                text = (message.get('text') or '').strip()
                if str(message.get('chat', {}).get('id', '')) == str(chat_id) and text:
                    if not intake.handle(REPO, text, update['update_id'], notify_intake):
                        notify_intake('Intake smoke listener only: /report, /reports and /intake. '
                                      'DuetFlow remains HALTed; no controller is running.')
                offset = int(update['update_id']) + 1
            # Retry only durable notifications; never prepare or publish here.
            intake.process(REPO, None, notify_intake, prepare=False)
            time.sleep(1)
        (SUPPORT / 'listener-terminal.json').write_text(json.dumps({'status': 'completed',
            'halt_retained': (REPO / '.orchestrator/HALT').is_file(), 'worker_started': False}) + '\n')
        print('Listener finished; HALT retained; no controller or worker started', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Telegram transport exceptions can contain a credential-bearing URL.
        print(f'Listener stopped ({type(exc).__name__}); inspect boundary before retry', file=sys.stderr)
        raise SystemExit(1)
