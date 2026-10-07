from dataclasses import asdict
from pathlib import Path
import json
import os
import sys
SHA='f46ffd403785bcd9040e892961731c2009fc6220'
TARGET=Path('/home/dan/.maestro/versions')/SHA
SUPPORT=Path('/home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release')
os.environ['MAESTRO_BOOTSTRAPPED']='1'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.path.insert(0,str(TARGET))
from maestro import selfupdate
assert Path(selfupdate.__file__).resolve().parents[1]==TARGET
result=selfupdate.self_test(TARGET)
(SUPPORT/'candidate-selftest.json').write_text(json.dumps({**asdict(result),'sha':SHA,'worktree':str(TARGET)},indent=2)+'\n')
print(result.summary)
raise SystemExit(0 if result.passed else 1)
