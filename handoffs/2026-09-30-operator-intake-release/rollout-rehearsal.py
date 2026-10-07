import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile

path=Path('/home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release/adopt-at-boundary.py')
spec=importlib.util.spec_from_file_location('intake_adopter',path)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
repo=Path(tempfile.mkdtemp(prefix='maestro-intake-rollout-rehearsal-'))
(repo/'.orchestrator').mkdir()
(repo/'.orchestrator/HALT').write_text('preserved')
c=sqlite3.connect(':memory:')
c.executescript('''CREATE TABLE task_runs(run_id,task_id,status);
CREATE TABLE attempts(status);
CREATE TABLE tasks(task_id,status);
CREATE TABLE acceptance_decisions(run_id,decision);
CREATE TABLE writer_leases(writer_pid);''')
c.execute('INSERT INTO task_runs VALUES (?,?,?)',(m.RUN,m.TASK,'succeeded'))
c.execute('INSERT INTO tasks VALUES (?,?)',(m.TASK,'accepted'))
c.execute('INSERT INTO acceptance_decisions VALUES (?,?)',(m.RUN,'accepted'))
m.check_boundary(repo,c)
refusals=[]
for name,setup,cleanup in [
 ('active run',lambda:c.execute("UPDATE task_runs SET status='running'"),lambda:c.execute("UPDATE task_runs SET status='succeeded'")),
 ('pending attempt',lambda:c.execute("INSERT INTO attempts VALUES ('pending')"),lambda:c.execute('DELETE FROM attempts')),
 ('live writer',lambda:c.execute('INSERT INTO writer_leases VALUES (?)',(os.getpid(),)),lambda:c.execute('DELETE FROM writer_leases')),
 ('missing HALT',lambda:(repo/'.orchestrator/HALT').unlink(),lambda:(repo/'.orchestrator/HALT').write_text('preserved'))]:
 setup()
 try:
  m.check_boundary(repo,c)
 except RuntimeError:
  refusals.append(name)
 else:
  raise AssertionError(f'{name} was not refused')
 cleanup()
assert refusals==['active run','pending attempt','live writer','missing HALT']
print('Idle accepted fixture allowed; active run, pending attempt, live writer and missing HALT refused. No adoption executed.')
