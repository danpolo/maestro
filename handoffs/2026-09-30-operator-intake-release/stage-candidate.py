"""Stage final code plus the unchanged required document test inputs; never adopt."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
ROOT=Path('/home/dan/projects/maestro')
SUPPORT=ROOT/'handoffs/2026-09-30-operator-intake-release'
SHA='f46ffd403785bcd9040e892961731c2009fc6220'
BASE=Path('/home/dan/.maestro/versions/9b4585683eb21d6dda0a12f4206254a8826828cf')
os.environ.pop('MAESTRO_HOME',None)
sys.path.insert(0,str(ROOT))
from maestro import selfupdate
target=selfupdate.materialize_worktree(ROOT,SHA)
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=target,text=True).strip()==SHA
for relative,digest in json.loads((SUPPORT/'candidate-doc-inputs.json').read_text()).items():
    source=BASE/'docs/graph-engineering'/relative
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    dest=target/'docs/graph-engineering'/relative
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,dest)
assert not subprocess.check_output(['git','status','--porcelain'],cwd=target,text=True).strip()
print(f'Staged exact {SHA} with verified unchanged document inputs; no runtime pin changed')
