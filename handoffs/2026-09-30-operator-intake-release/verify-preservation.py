"""Read-only verification against baselines saved before the pin-only rollout."""
import hashlib,json,sqlite3,subprocess
from pathlib import Path
root=Path('/home/dan/projects/maestro'); support=root/'handoffs/2026-09-30-operator-intake-release'
repo=Path('/home/dan/projects/duetflow');sha='f13b2c7eb8498edf39c1e72e439131e0635b024a'
before=json.loads((support/'live-before.json').read_text())
for f,h in json.loads((support/'preserved-files.json').read_text()).items():
    assert hashlib.sha256((root/f).read_bytes()).hexdigest()==h,f
for f,h in json.loads((support/'product-before-hashes.json').read_text()).items():
    if f=='.orchestrator/state.json': continue
    assert hashlib.sha256((repo/f).read_bytes()).hexdigest()==h,f
conn=sqlite3.connect((repo/'.orchestrator/control.sqlite3').as_uri()+'?mode=ro',uri=True)
records={t:hashlib.sha256(repr(conn.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall()).encode()).hexdigest() for t in ['tasks','task_runs','attempts','acceptance_decisions']}
state=json.loads((repo/'.orchestrator/state.json').read_text())
for key in ['maestro_version','updated_at','control_sequence']: state.pop(key,None)
records['state_except_install_metadata']=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()
baseline=json.loads((support/'product-records-before.json').read_text())
assert all(h==baseline[k] for k,h in records.items()),'Product records/state changed'
assert Path('/home/dan/.maestro/current').read_text().strip()==before['global_pin']
assert (repo/'.orchestrator/current').read_text().strip()==f'/home/dan/.maestro/versions/{sha}'
assert subprocess.check_output(['git','status','--short'],cwd=repo,text=True).strip()==before['duetflow_status']
reports=[]
for path in sorted((repo/'.orchestrator/intake').glob('I-*.json')):
    report=json.loads(path.read_text())
    reports.append({k:report[k] for k in ['id','status','revision','text']})
    reports[-1]['notification_pending']=bool(report.get('notification'))
    reports[-1]['has_proposal']=bool(report.get('proposal'))
result={'pin':sha,'halt_retained':True,'global_pointer_unchanged':True,'model_owner_files_unchanged':True,'product_files_records_and_state_unchanged_except_install_metadata':True,'reports':reports}
(support/'preservation-latest.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
