"""Run the prepared listener against disposable updates with forbidden paths trapped."""
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT=Path('/home/dan/projects/maestro')
spec=importlib.util.spec_from_file_location('smoke_listener', ROOT/'handoffs/2026-09-30-operator-intake-release/listen-halted.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
repo=Path(tempfile.mkdtemp(prefix='maestro-intake-listener-rehearsal-'))
orch=repo/'.orchestrator'
orch.mkdir()
(orch/'HALT').write_text('preserved')
(orch/'current').write_text(str(ROOT)+'\n')
from maestro.control.store import ControlStore
ControlStore.open_controller(orch/'control.sqlite3').close()
from maestro import intake
from maestro.hitl import commands,telegram
from maestro.workflows.runner import WorkflowRunner
m.REPO=repo
m.TARGET=ROOT
m.SUPPORT=repo
messages=['/help','/status','/report feature disposable intake check','/reports','/intake I-3',
          '/intake reject I-3 smoke complete']
updates=[{'update_id':i,'message':{'chat':{'id':4242},'text':text}} for i,text in enumerate(messages,1)]
updates.append({'update_id':7,'message':{'chat':{'id':999},'text':'/report bug foreign'}})
sent=[]
def send(text):
    sent.append(text)
    return True

def forbidden(*args,**kwargs):
    raise AssertionError('A forbidden controller/model/publication/dispatch path was reached')
with patch.object(commands,'_fetch_updates',return_value=updates), \
     patch.object(telegram,'notify_intake',side_effect=send), \
     patch.object(ControlStore,'open_controller',side_effect=forbidden), \
     patch.object(intake,'_generate',side_effect=forbidden), \
     patch.object(intake,'_publish',side_effect=forbidden), \
     patch.object(WorkflowRunner,'launch_worker',side_effect=forbidden), \
     patch.object(m.time,'monotonic',side_effect=[0,1,700]), \
     patch.object(m.time,'sleep',return_value=None), \
     patch.dict(m.os.environ,{'TELEGRAM_ALERT_CHAT_ID':'4242'}):
    m.main()
assert intake.load(repo,'I-3')['status']=='rejected'
assert not (orch/'intake/I-7.json').exists()
assert (orch/'HALT').read_text()=='preserved'
assert len(list((orch/'intake').glob('I-*.json')))==1
assert any('Saved I-3' in text for text in sent)
with ControlStore.open_client(orch/'control.sqlite3') as store:
    assert store.conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    assert store.conn.execute('SELECT count(*) FROM events').fetchone()[0]==0
    assert store.conn.execute('SELECT count(*) FROM writer_leases').fetchone()[0]==0
print('Listener rehearsal passed: report receipt/read/list/rejection, foreign-chat refusal, HALT retained; no model/controller/publication/dispatch reached; zero tasks/events/leases.')
