"""Approved live direct-rule bytes and edit grants, with frozen alias/global views.

These are filesystem and delivery checks. Native directory discovery evidence is
retained separately; this synthetic child does not pretend to prove native scope.
"""
import json
import subprocess
import sys

import pytest

from maestro.backends import agy_runtime, agy_worker
from maestro.backends.antigravity import AntigravityBackend
from maestro.backends.base import CompletionSpec
from tests.test_agy_runtime import installation
from tests.hang_bound import HANG_SECONDS


@pytest.mark.parametrize('writer', [False, True])
def test_direct_rules_are_live_with_only_normal_task_write_grants(installation, writer):
    home, tree, report = installation
    direct = [tree/'AGENTS.md',tree/'nested/GEMINI.md']
    alias = tree/'.agents/GEMINI.md'
    global_rule = home/'.gemini/GEMINI.md'
    direct[1].parent.mkdir()
    alias.parent.mkdir()
    for path in (*direct,alias,global_rule): path.write_text('before')
    run = agy_runtime.prepare(cwd=tree,workspace=report,home=home,
        write_roots=(tree,) if writer else ())
    try:
        for path in (*direct,alias,global_rule): path.write_text('after')
        program = '''
import json,sys
from pathlib import Path
direct, frozen, writer = json.loads(sys.argv[1])
for name in direct:
    p=Path(name)
    assert p.read_text()=='after'
    try: p.write_text('task edit')
    except OSError: assert not writer
    else: assert writer
for name in frozen:
    p=Path(name)
    assert p.read_text()=='before'
    try: p.write_text('forbidden')
    except OSError: pass
    else: raise AssertionError('frozen instruction became writable')
'''
        result = agy_worker.run(run.wrap([sys.executable,'-I','-c',program,
            json.dumps([[str(p) for p in direct],[str(alias),str(global_rule)],writer])]),
            cwd=str(tree),env=run.environment(),capture_output=True,timeout=HANG_SECONDS)
        assert result.returncode==0,result.stderr
        assert all(p.read_text()==('task edit' if writer else 'after') for p in direct)
        assert alias.read_text()==global_rule.read_text()=='after'
    finally: run.cleanup()


@pytest.mark.parametrize('writer', [False, True])
def test_nongit_calls_explain_live_task_rules_too(installation, writer):
    home,tree,report=installation
    calls=[]
    def native(argv,**kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,json.dumps({'status':'SUCCESS','response':'fixture'}),'')
    result=AntigravityBackend(gemini_home=home/'.gemini',runner=native).complete(
        CompletionSpec(prompt='fixture',cwd=tree,writable=writer,log_file=report/'impl.log'))
    assert result.returncode==0
    assert len(calls)==1
    assert 'Direct AGENTS.md and GEMINI.md are LIVE' in calls[0][-1]
