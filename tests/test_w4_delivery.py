"""Exact pair delivery survives native driver restarts and switch-back resumes."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from maestro.backends import registry
from maestro.backends.base import LaunchSpec

pytestmark = pytest.mark.usefixtures('isolated_native_host_home')


@pytest.fixture
def delivery_provider_limits(tmp_path):
    """The switch exercises real lifecycle eligibility without operator tables."""
    header = ('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n'
              '|---|---|---|---|\n')
    rows = {
        'codex': ('| GPT-6 Astra | 160K-180K | 220K-240K | 320K |\n'
                  '| GPT-6.1 Sol | 160K-180K | 220K-240K | 320K |\n'
                  '| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n'),
        'claude': ('| Claude Opus 5.5 | 160K-180K | 220K-240K | 320K |\n'
                   '| Claude Sonnet 5.5 | 160K-180K | 220K-240K | 320K |\n'
                   '| Claude Haiku 4.5 | 70K-80K | 110K-120K | 150K |\n'),
    }
    paths = {'antigravity': str(
        Path(__file__).parent / 'fixtures/model_limits/antigravity.md')}
    for backend, table in rows.items():
        path = tmp_path / (backend + '-limits.md')
        path.write_text(header + table)
        paths[backend] = str(path)
    return paths


@pytest.fixture(autouse=True)
def isolated_native_runtime(tmp_path, monkeypatch):
    from maestro.backends import agy_runtime
    home = tmp_path / 'native-home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    original = agy_runtime.prepare
    def prepare(**kwargs):
        kwargs['home'] = home
        return original(**kwargs)
    monkeypatch.setattr(agy_runtime, 'prepare', prepare)


@pytest.mark.parametrize('target,model,effort_args', [
    ('antigravity', 'gemini-3.8-flash-high', ()),
    ('claude', 'claude-opus-5-5', ('--effort', 'high')),
])
def test_fresh_and_resume_keep_pair_after_driver_restart(target, model, effort_args,
                                                        tmp_path, monkeypatch):
    from maestro.backends import antigravity, claude
    calls = tmp_path / 'calls.jsonl'
    fake = tmp_path / 'fake-agent'
    fake.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
                    f'with Path({str(calls)!r}).open("a") as out: '
                    'out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                    'print(json.dumps({"event":"result","status":"SUCCESS","response":"OK"}))\n')
    fake.chmod(0o755)
    monkeypatch.setattr(antigravity if target == 'antigravity' else claude, 'BINARY', str(fake))
    cls = registry.launch_driver_class(target)
    monkeypatch.setattr(cls, '_open_window', lambda *a: None)
    if target == 'claude':
        monkeypatch.setattr(cls, 'mode', lambda self: 'headless')
    spec = LaunchSpec('T1', 's1', model, 'go', tmp_path, tmp_path,
                      effort_args=effort_args)
    handle = cls().launch(spec)
    fresh = subprocess.run([sys.executable, str(tmp_path / 'launch.py')],
                           cwd=tmp_path, capture_output=True, text=True)
    assert fresh.returncode == 0, fresh.stderr
    cls().resume(replace(handle, native_id='recorded-native-id'), 'continue', worktree=tmp_path)
    resumed = subprocess.run([sys.executable, str(tmp_path / 'resume.py')],
                             cwd=tmp_path, capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stderr
    launch, resume = [json.loads(line) for line in calls.read_text().splitlines()]
    assert 'recorded-native-id' in resume
    for argv in (launch, resume):
        assert '--model' in argv and argv[argv.index('--model') + 1] == model
        if effort_args:
            assert argv[argv.index('--effort') + 1] == 'high'
        else:
            assert '--effort' not in argv


@pytest.mark.parametrize('target', ['claude', 'codex', 'antigravity'])
def test_switch_back_resume_uses_and_retains_the_incoming_pair(
    target, tmp_path, monkeypatch, delivery_provider_limits,
):
    from maestro import config, model_policy, switch
    from tests.test_w4_owner_routes import flash_event, flash_owner

    policy = model_policy.shipped_policy()
    if target == 'antigravity':
        from maestro import model_detection
        policy = model_detection.policy_for_set(policy, flash_event(), None, owner=flash_owner())
    project = {'model_limits': delivery_provider_limits}
    monkeypatch.setattr(config, 'load_project_yaml', lambda: project)
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    monkeypatch.setattr(model_policy, 'adopted_policy', lambda repo: (None, policy))
    monkeypatch.setattr(switch, 'WORKSPACES', tmp_path / 'workspaces')
    monkeypatch.setattr(switch, 'handoff_brief', lambda *a, **k: 'continue')
    cls = registry.launch_driver_class(target)
    if target == 'codex':
        native = tmp_path / 'native'
        native.mkdir()
        (native / 'auth.json').write_text('synthetic')
        monkeypatch.setenv('CODEX_HOME', str(native))
        monkeypatch.setattr(cls, 'binary_path', lambda self: 'codex')
        monkeypatch.setattr(cls, 'binary_warning', lambda self: None)
        monkeypatch.setattr(cls, '_new_window', lambda *a: None)
    else:
        monkeypatch.setattr(cls, '_open_window', lambda *a: None)
        if target == 'claude':
            monkeypatch.setattr(cls, 'mode', lambda self: 'headless')
    old = tmp_path / 'old'
    old.mkdir()
    model = 'gemini-3.8-flash-low' if target == 'antigravity' else (
        'claude-opus-5-5' if target == 'claude' else 'gpt-6.1-sol')
    handle = cls().launch(LaunchSpec('T1', 'old', model, 'go', old, tmp_path))
    if target != 'claude':
        from tests.backends.test_codex_driver import _settle_mocked_worker
        _settle_mocked_worker(old)
    handle = replace(handle, native_id='old-native-thread')
    deps = switch.SwitchDeps(window_exists=lambda window: False, journal=lambda *a: None,
        notify=lambda *a: None, load_state=lambda: {}, save_state=lambda state: None,
        session_id_factory=lambda task: 'incoming')
    outcome = switch.switch_task('T1', reason=switch.REASON_QUOTA,
        entry={'backend': 'codex' if target != 'codex' else 'claude', 'worktree': str(tmp_path)},
        to_backend=target, handle=handle, deps=deps)
    assert outcome.switched and outcome.resumed, outcome.note
    selected = model_policy.derived_roles(project, policy)['roles']['implementer']
    expected = selected['models'][target]
    source = (old / ('launch.py' if target == 'codex' else 'resume.py')).read_text()
    if target == 'antigravity':
        from tests.backends.test_antigravity import _job as agy_job
        source = repr(agy_job(old, 'resume.py')['argv'])
    if target == 'codex':
        from tests.backends.test_codex_driver import _job
        job = _job(old)
        assert job['model'] == expected
    else:
        assert expected in source

    args, settings = selected['effort_controls'][target]
    for arg in args:
        assert repr(arg) in source
    for key, value in settings:
        if target == 'codex':
            assert json.loads(job['expected_config'])[key] == value
        else:
            assert f'{key}={value}' in source
    assert outcome.entry['model'] == expected
    # A second new driver must retain the route chosen during the switch-back.
    if target != 'claude':
        _settle_mocked_worker(old, 'launch.py' if target == 'codex' else 'resume.py')
    cls().resume(handle, 'again')
    source = (old / ('launch.py' if target == 'codex' else 'resume.py')).read_text()
    if target == 'antigravity':
        source = repr(agy_job(old, 'resume.py')['argv'])
    if target == 'codex':
        assert _job(old)['model'] == expected
        assert _job(old)['effort'] == job['effort']
        from tests.native_test_home import assert_codex_discovery_isolated
        assert_codex_discovery_isolated(old, tmp_path / 'host-home')
    else:
        assert expected in source


def test_legacy_switched_tree_proof_review_excludes_both_actual_writer_families(
    tmp_path, monkeypatch, caplog, delivery_provider_limits,
):
    """Actual switch + proof gate + router; only native model execution is fake."""
    from maestro import agentcall, config, gates, model_detection, model_policy, switch
    from maestro.backends.base import Capabilities, Completion, Handle
    from tests.test_w4_owner_routes import flash_event, flash_owner

    tree, workspaces = tmp_path / 'tree', tmp_path / 'sessions'
    tree.mkdir()
    subprocess.run(['git', 'init', '-q', str(tree)], check=True)
    subprocess.run(['git', '-C', str(tree), '-c', 'user.name=W4',
                    '-c', 'user.email=w4@example.invalid', 'commit',
                    '--allow-empty', '-qm', 'initial'], check=True)
    inherited = tree / 'claude.txt'
    inherited.write_text('outgoing writer left this uncommitted')
    policy = model_detection.policy_for_set(model_policy.shipped_policy(), flash_event(),
                                           None, owner=flash_owner())
    # A fully reconciled W4 policy includes the native providers' owner facts too.
    # An old policy without them intentionally reports unknown independence.
    policy['source']['class_metadata'].extend([
        dict(backend_id='claude', model_id='claude-opus-5-5', family='opus',
             version=[5, 5], aliases=[], set=True, enforced_window=1000000,
             canonical_model_id='claude-opus-5-5', model_family='claude'),
        dict(backend_id='codex', model_id='gpt-6.1-sol', family='sol',
             version=[6, 1], aliases=[], set=True, enforced_window=400000,
             canonical_model_id='gpt-6-1-sol', model_family='gpt'),
    ])
    project = {'roles': {'reviewer': {'independent_of': ['implementer']}},
               'model_limits': delivery_provider_limits}
    monkeypatch.setattr(config, 'PROJECT_YAML', tmp_path / 'project.yaml')
    monkeypatch.setattr(config, 'load_project_yaml', lambda: project)
    monkeypatch.setattr(model_policy, 'adopted_policy', lambda repo: (None, policy))
    monkeypatch.setattr(switch, 'WORKSPACES', workspaces)
    monkeypatch.setattr(agentcall, 'operator_backend', lambda: 'claude')
    monkeypatch.setattr(agentcall.quota, 'exhausted_backends', lambda: ())
    launched, reviewed = [], []

    class Writer:
        def capabilities(self):
            return Capabilities()

        def launch(self, spec):
            assert inherited.read_text() == 'outgoing writer left this uncommitted'
            assert spec.worktree == tree
            (tree / 'gemini.txt').write_text('incoming writer kept the first edit')
            launched.append(spec)
            return Handle('antigravity', spec.session_id, None, spec.workspace, 'impl-T1')

    class Reviewer:
        def complete(self, spec):
            reviewed.append(spec)
            return Completion('{"V1":{"pass":true}}')

    chosen = []
    monkeypatch.setattr(agentcall.registry, 'driver_class', lambda name:
                        chosen.append(name) or Reviewer)
    deps = switch.SwitchDeps(driver_factory=lambda name: Writer(),
        window_exists=lambda window: False, journal=lambda *a: None,
        notify=lambda *a: None, load_state=lambda: {}, save_state=lambda state: None,
        session_id_factory=lambda task: 'incoming')
    outcome = switch.switch_task('T1', reason=switch.REASON_QUOTA,
        entry={'backend': 'claude', 'model': 'claude-opus-5-5',
               'session_id': 'outgoing', 'worktree': str(tree)},
        to_backend='antigravity', deps=deps)
    assert outcome.switched, outcome.note
    assert launched[0].model == 'gemini-3.8-flash-high'
    authors = model_policy.legacy_writer_routes(outcome.entry)
    assert authors == [['claude', 'claude-opus-5-5'],
                       ['antigravity', 'gemini-3.8-flash-high']]
    caplog.set_level('INFO', logger='maestro.agentcall')
    passed, feedback = gates.sonnet_review_proofs('T1',
        [{'id': 'V1', 'kind': 'manual', 'check': 'both edits are present'}],
        {'V1': {'proof': 'Both writer files remain present after the actual switch.'}},
        writer_routes=authors)
    assert passed, feedback
    assert chosen == ['codex']
    assert reviewed[0].model.startswith('gpt-')
    assert not reviewed[0].writable
    assert 'independence_relaxed' not in caplog.text
    assert inherited.read_text() == 'outgoing writer left this uncommitted'
    assert (tree / 'gemini.txt').read_text() == 'incoming writer kept the first edit'
