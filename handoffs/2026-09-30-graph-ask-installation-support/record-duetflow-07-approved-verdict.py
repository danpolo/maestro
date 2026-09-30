from pathlib import Path
import functools
import json
import os
import sys

repo=Path('/home/dan/projects/duetflow')
pin=Path('/home/dan/.maestro/versions/5c94c9fead187efc05157272da4eec9b36b19fff')
os.environ['MAESTRO_REPO']=str(repo)
os.environ['MAESTRO_BOOTSTRAPPED']='1'
sys.path.insert(0,str(pin))
from maestro.hitl import verify,telegram
from maestro import state

projection=json.loads((repo/'.orchestrator/state.json').read_text())
entry=projection.get('awaiting_dan_verification',{}).get('07-reconciliation')
if entry is None:
    raise SystemExit('No pending 07 verification; inspect current result before recording')
assert entry['run_id']=='run_qG3zSNhcUbdHKyDm'
assert entry['snapshot']=='2f3bf5abeb3b92b34f3a0cc736acbfa3b29ea37a'
assert entry['request_id']=='verify-07-reconciliation-2f3bf5abeb3b'
# The controller owns SQLite. External answer submission journals through the existing
# documented text-only seam, while the controller later records the durable verdict.
telegram.append_journal=functools.partial(state.append_journal,shadow=False)
print(verify.handle_verify_command('07-reconciliation approve',state=projection))
