"""Stage the integrated modelctl D-A–D-E release plus the unchanged document test inputs; never adopt."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
ROOT=Path('/home/dan/projects/maestro')
SUPPORT=ROOT/'handoffs/2026-10-08-modelctl-release'
SHA='0c7eb92be656b3ca7d2d0c7d783bac9998c38fce'
BASE=Path('/home/dan/.maestro/versions/f13b2c7eb8498edf39c1e72e439131e0635b024a')
os.environ.pop('MAESTRO_HOME',None)
sys.path.insert(0,str(ROOT))
from maestro import selfupdate
target=selfupdate.materialize_worktree(ROOT,SHA)
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=target,text=True).strip()==SHA
inputs=json.loads((SUPPORT/'candidate-doc-inputs.json').read_text())
for relative,digest in inputs.items():
    source=BASE/'docs/graph-engineering'/relative
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    dest=target/'docs/graph-engineering'/relative
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,dest)
clean=not subprocess.check_output(['git','status','--porcelain'],cwd=target,text=True).strip()
assert clean
(SUPPORT/'candidate-integrity.json').write_text(json.dumps({'sha':SHA,'worktree':str(target),'clean':clean,'document_hashes_verified':len(inputs)},indent=2)+'\n')
print(f'Staged exact {SHA} with {len(inputs)} verified unchanged document inputs; no runtime pin changed')
