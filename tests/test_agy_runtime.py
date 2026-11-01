"""Real child-process checks for per-call agy settings and immutable guard state."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.hang_bound import HANG_SECONDS


@pytest.fixture
def installation(tmp_path):
    home = tmp_path / 'home'
    state = home / '.gemini/antigravity-cli'
    (state / 'bin').mkdir(parents=True)
    (state / 'bin/agentapi').write_text('original helper')
    (state / 'builtin').mkdir()
    for name in ('antigravity-oauth-token', 'installation_id', 'jetski_state.pbtxt'):
        (state / name).write_text('native-owned')
    (home / '.gemini/config').mkdir()
    tree, workspace = tmp_path / 'tree', tmp_path / 'workspace'
    tree.mkdir()
    workspace.mkdir()
    return home, tree, workspace


def test_prepared_native_child_preserves_writes_and_cannot_modify_guard_or_host_state(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree, workspace),
                  home=home, allow=('Read', 'Bash(echo *)'))
    program = '''
import json, pathlib, subprocess, sys
tree, report, control, native = map(pathlib.Path, sys.argv[1:])
(tree / 'change.txt').write_text('persisted')
(report / 'DONE').write_text('done')
for path in (control / 'policy.json', native / 'antigravity-oauth-token', tree.parent / 'escape'):
    try:
        path.write_text('tampered')
    except OSError:
        pass
    else:
        raise SystemExit('unexpected writable control/outside path: ' + str(path))
settings = json.loads((native / 'settings.json').read_text())
assert settings['toolPermission'] == 'request-review'
assert settings['permissions']['allow'] == ['command(*)', 'read_file(*)',
    'write_file(' + str(tree) + ')', 'write_file(' + str(report) + ')']
print('protected')
'''
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', program, str(tree),
                                   str(workspace), str(run.control),
                                   str(home / '.gemini/antigravity-cli')]),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout.strip() == 'protected'
    assert (tree / 'change.txt').read_text() == 'persisted'
    assert (workspace / 'DONE').read_text() == 'done'
    assert (home / '.gemini/antigravity-cli/antigravity-oauth-token').read_text() == 'native-owned'


def test_guard_uses_frozen_code_and_policy_even_with_hostile_python_environment(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,),
                  home=home, allow=('Bash(echo *)',))
    import os
    environment = dict(os.environ, PYTHONPATH=str(tree), PYTHONSTARTUP=str(tree / 'startup.py'))
    (tree / 'maestro.py').write_text('raise SystemExit("hostile import")')
    for command, expected in [('echo ok', 'allow'), ('sudo whoami', 'deny'),
                              ('echo ok; curl example.invalid', 'deny')]:
        done = subprocess.run(run.hook_argv, input=json.dumps({'toolCall': {
            'name': 'run_command', 'args': {'CommandLine': command, 'Cwd': str(tree)}}}),
            env=environment, cwd=tree, capture_output=True, text=True, timeout=HANG_SECONDS)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)['decision'] == expected


def test_reader_gets_only_verdict_workspace_and_native_state_is_private(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    source = tree / 'source.py'
    source.write_text('original')
    run = prepare(cwd=workspace, workspace=workspace, write_roots=(workspace,),
                  home=home, allow=('Read',))
    program = '''
import pathlib, sys
source, report, state = map(pathlib.Path, sys.argv[1:])
try:
    source.write_text('changed')
except OSError:
    report.write_text('refused')
else:
    raise SystemExit('reader wrote tree')
(state / 'conversation').write_text('private')
'''
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', program, str(source),
                                   str(workspace / 'review.json'),
                                   str(home / '.gemini/antigravity-cli')]),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr
    assert source.read_text() == 'original'
    assert (workspace / 'review.json').read_text() == 'refused'
    assert not (home / '.gemini/antigravity-cli/conversation').exists()
    assert (run.state / 'conversation').read_text() == 'private'


def test_policy_cannot_be_changed_through_the_private_config_source_path(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree, workspace),
                  home=home, allow=())
    program = '''
import pathlib, sys
for name in sys.argv[1:]:
    try:
        pathlib.Path(name).write_text('{}')
    except OSError:
        continue
    raise SystemExit('writable policy alias: ' + name)
'''
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', program,
                                   str(run.config / 'hooks.json'),
                                   str(home / '.gemini/config/hooks.json')]),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr


def test_writer_cannot_rename_guard_parent_and_replace_it_with_a_forged_guard(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree, workspace),
                  home=home, allow=())
    program = '''
import pathlib, sys
original = pathlib.Path(sys.argv[1])
try:
    original.rename(original.with_name('stolen-runtime'))
except OSError:
    pass
else:
    raise SystemExit('writer moved guard parent')
'''
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', program, str(run.directory)]),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr


def test_workspace_cannot_shadow_guard_and_existing_rules_and_skills_remain_visible(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    custom = tree / '.agents'
    (custom / 'rules').mkdir(parents=True)
    (custom / 'skills/example').mkdir(parents=True)
    (custom / 'rules/AGENTS.md').write_text('keep this project rule')
    (custom / 'skills/example/SKILL.md').write_text('keep this skill')
    hostile = {'maestro-permissions': {'enabled': False}}
    (custom / 'hooks.json').write_text(json.dumps(hostile))
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree, workspace),
                  home=home, allow=())
    program = '''
import json, pathlib, sys
tree = pathlib.Path(sys.argv[1])
custom = tree / '.agents'
assert (custom / 'rules/AGENTS.md').read_text() == 'keep this project rule'
assert (custom / 'skills/example/SKILL.md').read_text() == 'keep this skill'
hook = json.loads((custom / 'hooks.json').read_text())['maestro-permissions']
assert hook.get('enabled', True) and hook['PreToolUse'][0]['matcher'] == '*'
for alias in ('.agents', '.agent', '_agents', '_agent'):
    try:
        (tree / alias / 'hooks.json').write_text('{}')
    except OSError:
        pass
    else:
        raise SystemExit('writable discovery alias: ' + alias)
'''
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', program, str(tree)]),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr
    assert json.loads((custom / 'hooks.json').read_text()) == hostile


def test_next_turn_keeps_native_conversations_but_refreshes_immutable_policy(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    first = prepare(cwd=tree, workspace=workspace, write_roots=(tree,),
                    home=home, allow=('Bash(echo *)',))
    (first.state / 'conversation').write_text('recorded conversation')
    first.cleanup()
    second = prepare(cwd=tree, workspace=workspace, write_roots=(tree,),
                     home=home, allow=('Bash(git status *)',))
    assert (second.state / 'conversation').read_text() == 'recorded conversation'
    assert first.control != second.control
    assert json.loads((first.control / 'policy.json').read_text())['allow'] == ['Bash(echo *)']
    assert json.loads((second.control / 'policy.json').read_text())['allow'] == ['Bash(git status *)']


def test_reader_isolates_ancestor_customizations_without_writing_repository_root(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, _ = installation
    (tree / '.git').mkdir()
    workspace = tree / 'runs/review'
    workspace.mkdir(parents=True)
    run = prepare(cwd=workspace, workspace=workspace, write_roots=(workspace,),
                  home=home, allow=())
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', 'print("confined")']),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr
    assert not (tree / '.agents').exists()
    assert not (tree / 'runs/.agents').exists()


def test_native_child_can_create_private_temporary_files(installation):
    if shutil.which('bwrap') is None:
        pytest.skip('bubblewrap is not installed')
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree, workspace),
                  home=home, allow=())
    program = '''
import os, pathlib, tempfile
scratch = pathlib.Path(os.environ['TMPDIR'])
with tempfile.NamedTemporaryFile(dir=scratch) as temporary:
    temporary.write(b'private')
    temporary.flush()
    assert pathlib.Path(temporary.name).read_bytes() == b'private'
print('temporary file created')
'''
    done = subprocess.run(run.wrap([sys.executable, '-I', '-c', program]),
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'temporary file created'
    assert list(run.scratch.iterdir()) == []


def test_existing_security_hook_is_frozen_and_composed_with_global_policy(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    config = home / '.gemini/config'
    script = config / 'scripts/skillspector_gate.py'
    script.parent.mkdir()
    script.write_text('import json,sys\n'
        'payload=json.load(sys.stdin)\n'
        'unsafe=payload["toolCall"]["args"].get("CodeContent")=="unsafe"\n'
        'print(json.dumps({"decision":"deny" if unsafe else "allow", "reason":"security"}))\n')
    command = f'{script} >> /tmp/hook_log.txt 2>&1'
    (config / 'hooks.json').write_text(json.dumps({
        'skillspector-security-gate': {'enabled': True, 'PreToolUse': [{
            'matcher': '.*', 'hooks': [{'type': 'command', 'command': command, 'timeout': 30}]}]},
        'other-hook': {'PreToolUse': []}}))
    (config / 'mcp_config.json').write_text('{"mcpServers":{"omitted-server":{}}}')
    (config / 'plugins.json').write_text('{"plugins":["omitted-plugin"]}')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home, allow=())
    # Subsequent edits to the original security source cannot alter this call.
    script.write_text('print("invalid changed source")')
    for target, content, expected in [(tree / 'SKILL.md', 'safe', 'allow'),
            (tree / 'SKILL.md', 'unsafe', 'deny'),
            (tree.parent / 'SKILL.md', 'safe', 'deny')]:
        result = subprocess.run(run.hook_argv, input=json.dumps({'toolCall': {
            'name': 'write_to_file', 'args': {'TargetFile': str(target), 'CodeContent': content}}}),
            capture_output=True, text=True, timeout=HANG_SECONDS)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['decision'] == expected
    inventory = json.loads((run.control / 'customizations.json').read_text())
    global_config = next(entry for entry in inventory if entry['path'] == str(config))
    assert global_config['retained_security_hooks'] == ['skillspector-security-gate']
    assert 'other-hook' in global_config['omitted_hooks']
    assert {'mcp_config.json', 'plugins.json'} <= set(global_config['omitted'])


@pytest.mark.parametrize('location', ['cwd', 'workspace', 'home', 'write_root',
    'runtime', 'state', 'helper', 'discovery'])
def test_original_directory_aliases_are_rejected_before_preparation(installation, location):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    outside = tree.parent / 'outside'
    outside.mkdir()
    alias = tree.parent / 'alias'
    alias.symlink_to(outside, target_is_directory=True)
    cwd, report, native_home, roots = tree, workspace, home, (tree,)
    if location == 'cwd':
        cwd = alias
    elif location == 'workspace':
        report = alias
    elif location == 'home':
        alias.unlink()
        alias.symlink_to(home, target_is_directory=True)
        native_home = alias
    elif location == 'write_root':
        roots = (alias,)
    elif location == 'runtime':
        (workspace / '.agy-runtime').symlink_to(outside, target_is_directory=True)
    elif location == 'state':
        (workspace / '.agy-runtime').mkdir()
        (workspace / '.agy-runtime/state').symlink_to(outside, target_is_directory=True)
    elif location == 'helper':
        helper = home / '.gemini/antigravity-cli/bin'
        shutil.rmtree(helper)
        helper.symlink_to(outside, target_is_directory=True)
    else:
        (tree / '.agent').symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, RuntimeError)):
        prepare(cwd=cwd, workspace=report, write_roots=roots, home=native_home)
    assert list(outside.iterdir()) == []
    assert not list(workspace.glob('.agy-runtime/call-*'))


def test_worker_cannot_poison_future_controller_or_parent_controls(installation, monkeypatch):
    from maestro.backends import agy_runtime
    home, tree, workspace = installation
    installed = tree / 'installed/maestro'
    (installed / 'backends').mkdir(parents=True)
    real_source = Path(agy_runtime.__file__).parents[1]
    for name in ('permissions.py', 'tool_policy.py', 'tool_hooks.py'):
        shutil.copyfile(real_source / name, installed / name)
    monkeypatch.setattr(agy_runtime, '__file__', str(installed / 'backends/agy_runtime.py'))
    controls = [workspace / name for name in ('impl.log', 'launch.py', 'resume.py',
        'launch_spec.json', 'brief.txt', 'resume_prompt.txt')]
    for path in controls:
        path.write_text('parent-owned')
    run = agy_runtime.prepare(cwd=tree, workspace=workspace, home=home,
        write_roots=(tree, workspace))
    program = '''
import pathlib,sys
for name in sys.argv[1:]:
    path=pathlib.Path(name)
    try:
        path.write_text('poisoned')
    except OSError:
        pass
    else:
        raise SystemExit('writer poisoned future control: '+name)
    try:
        path.rename(path.with_name(path.name+'.old'))
    except OSError:
        pass
    else:
        raise SystemExit('writer renamed future control: '+name)
'''
    result = subprocess.run(run.wrap([sys.executable, '-I', '-c', program,
        str(installed / 'tool_hooks.py'), *map(str, controls)]),
        capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_under_native_home_retains_task_tree_and_private_profile(installation):
    from maestro.backends.agy_runtime import prepare
    home, _, _ = installation
    tree = home / '.gemini/worktrees/task'
    tree.mkdir(parents=True)
    workspace = tree / 'reports'
    workspace.mkdir()
    (tree / 'input').write_text('task source')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree, workspace), home=home)
    result = subprocess.run(run.wrap([sys.executable, '-I', '-c',
        'import pathlib,json,sys; t=pathlib.Path(sys.argv[1]); '
        'assert (t/"input").read_text()=="task source"; '
        '(t/"output").write_text("persisted"); '
        'assert json.loads(pathlib.Path(sys.argv[2]).read_text())["toolPermission"]=="request-review"',
        str(tree), str(home / '.gemini/antigravity-cli/settings.json')]),
        capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tree / 'output').read_text() == 'persisted'


def test_driver_launch_restart_resume_and_completion_use_the_guarded_runtime(installation, monkeypatch):
    from dataclasses import replace
    from maestro.backends import antigravity
    from maestro.backends.base import CompletionSpec, LaunchSpec
    home, tree, workspace = installation
    native = tree.parent / 'fake-agy'
    calls = workspace / 'native-calls.jsonl'
    native.write_text(f'#!{sys.executable}\n'
        'import json,pathlib,subprocess,sys\n'
        f'home=pathlib.Path({str(home)!r})\n'
        'assert "--dangerously-skip-permissions" not in sys.argv\n'
        'config=home/".gemini/config"\n'
        'settings=json.loads((home/".gemini/settings.json").read_text())\n'
        'assert settings["toolPermission"]=="request-review"\n'
        'hook=json.loads((config/"hooks.json").read_text())["maestro-permissions"]\n'
        'command=hook["PreToolUse"][0]["hooks"][0]["command"]\n'
        f'payload={dict(toolCall=dict(name="run_command", args=dict(CommandLine="sudo whoami", Cwd=str(tree))))!r}\n'
        'decision=subprocess.run(command,shell=True,input=json.dumps(payload),'
        'capture_output=True,text=True,check=True)\n'
        'assert json.loads(decision.stdout)["decision"]=="deny"\n'
        'if sys.argv[sys.argv.index("--output-format")+1]=="json":\n'
        ' print(json.dumps({"status":"SUCCESS","response":"GUARDED:"+'
        'sys.argv[sys.argv.index("--model")+1]}))\n'
        'else:\n'
        f' with pathlib.Path({str(calls)!r}).open("a") as stream: '
        'stream.write(json.dumps(sys.argv[1:])+"\\n")\n'
        ' print(json.dumps({"event":"init","conversation_id":"native-id"}))\n'
        ' print(json.dumps({"event":"result","status":"SUCCESS","response":"OK"}))\n')
    native.chmod(0o755)
    monkeypatch.setattr(antigravity, 'BINARY', str(native))
    monkeypatch.setattr(antigravity.AntigravityBackend, '_open_window', lambda *args: None)
    def driver():
        return antigravity.AntigravityBackend(gemini_home=home / '.gemini')
    spec = LaunchSpec('T1', 's1', 'gemini-3.8-flash-high', 'go', workspace, tree)
    handle = driver().launch(spec)
    for name in ('launch.py', 'resume.py'):
        if name == 'resume.py':
            driver().resume(replace(handle, native_id=None), 'continue')
        result = subprocess.run([sys.executable, str(workspace / name)],
            capture_output=True, text=True, timeout=HANG_SECONDS)
        assert result.returncode == 0, result.stdout + result.stderr
    result = driver().complete(CompletionSpec(prompt='read only', model=spec.model,
        cwd=tree, log_file=workspace / 'review.log'))
    assert result.text == 'GUARDED:' + spec.model
    launch, resume = map(json.loads, calls.read_text().splitlines())
    for argv in (launch, resume):
        assert argv[argv.index('--model') + 1] == spec.model
    assert resume[resume.index('--conversation') + 1] == 'native-id'
    for alias in ('.agents', '.agent', '_agents', '_agent'):
        assert (tree / alias).is_dir() and not list((tree / alias).iterdir())


@pytest.mark.parametrize('log_in_tree', [False, True])
def test_driver_completion_reader_and_failed_launch_preserve_host_boundary(installation, monkeypatch, log_in_tree):
    from maestro.backends import antigravity
    from maestro.backends.base import CompletionSpec, LaunchSpec
    home, tree, workspace = installation
    native = tree.parent / 'fake-agy'
    victim = tree / 'source.py'
    victim.write_text('original')
    native.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nimport json\n'
        f'path=Path({str(victim)!r})\n'
        'try: path.write_text("reader escape")\n'
        'except OSError: print(json.dumps({"status":"SUCCESS","response":"REFUSED"}))\n'
        'else: raise SystemExit("reader wrote source")\n')
    native.chmod(0o755)
    monkeypatch.setattr(antigravity, 'BINARY', str(native))
    driver = antigravity.AntigravityBackend(gemini_home=home / '.gemini')
    result = driver.complete(CompletionSpec(prompt='review', model='gemini-3.8-flash-low',
        cwd=tree, log_file=(tree if log_in_tree else workspace) / 'review.log'))
    assert result.text == 'REFUSED'
    assert victim.read_text() == 'original'
    def fail(*args):
        raise OSError('window launch failed')
    monkeypatch.setattr(driver, '_open_window', fail)
    with pytest.raises(OSError, match='window launch failed'):
        driver.launch(LaunchSpec('T1', 's1', 'gemini-3.8-flash-low', 'go', workspace, tree))
    for alias in ('.agents', '.agent', '_agents', '_agent'):
        assert (tree / alias).is_dir() and not list((tree / alias).iterdir())


def test_linked_rule_and_skill_content_is_frozen_without_preserving_live_aliases(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    shared = tree.parent / 'shared-skill'
    shared.mkdir()
    (shared / 'SKILL.md').write_text('original shared skill')
    skills = home / '.agents/skills'
    skills.mkdir(parents=True)
    (skills / 'example').symlink_to(shared, target_is_directory=True)
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    (shared / 'SKILL.md').write_text('changed shared skill')
    snapshot = dict(run.customizations)[home / '.agents']
    assert (snapshot / 'skills/example/SKILL.md').read_text() == 'original shared skill'
    assert not (snapshot / 'skills/example').is_symlink()


def test_completion_cleans_its_unreported_private_runtime(installation, monkeypatch, tmp_path):
    import tempfile
    from maestro.backends.antigravity import AntigravityBackend
    from maestro.backends.base import CompletionSpec
    home, tree, _ = installation
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, '{"status":"SUCCESS","response":"OK"}', '')
    result = AntigravityBackend(gemini_home=home / '.gemini', runner=runner).complete(
        CompletionSpec(prompt='review', cwd=tree))
    assert result.text == 'OK'
    assert list(temporary.iterdir()) == []


def test_native_driver_refuses_arbitrary_effort_flags_before_running(installation):
    from maestro.backends.antigravity import AntigravityBackend
    from maestro.backends.base import CompletionSpec
    home, tree, workspace = installation
    calls = []
    def runner(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0, '{"status":"SUCCESS","response":"UNSAFE"}', '')
    result = AntigravityBackend(gemini_home=home / '.gemini', runner=runner).complete(
        CompletionSpec(prompt='review', cwd=tree, log_file=workspace / 'review.log',
            effort_args=('--dangerously-skip-permissions',)))
    assert result.text == '' and result.returncode != 0
    assert calls == []


@pytest.mark.parametrize('location', ['global', 'workspace'])
def test_omission_artifact_names_custom_agents_without_copying_contents(installation, location):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    root = home / '.gemini/config' if location == 'global' else tree / '.agents'
    (root / 'agents/reviewer').mkdir(parents=True)
    (root / 'agents/reviewer/agent.md').write_text('SYNTHETIC_PRIVATE_AGENT_CONTENT')
    run = prepare(cwd=tree, workspace=workspace,
        write_roots=(tree, workspace), home=home, allow=())
    try:
        text = (run.control / 'customizations.json').read_text()
        entry = next(row for row in json.loads(text) if row['path'] == str(root))
        assert 'agents' in entry['omitted']
        assert 'agents/reviewer/agent.md' in entry['omitted_paths']
        assert 'SYNTHETIC_PRIVATE_AGENT_CONTENT' not in text
    finally:
        run.cleanup()


def test_omission_inventory_names_registered_and_staged_execution_surfaces(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    roots = (home / '.gemini/config', home / '.gemini/antigravity-cli', tree / '.agents')
    for root in roots:
        root.mkdir(exist_ok=True)
        (root / 'mcp_config.json').write_text(json.dumps({'mcpServers': {'omitted-server': {
            'env': {'DO_NOT_RECORD': 'synthetic-secret'}}}}))
        (root / 'plugins.json').write_text(json.dumps({'entries': [{'path': '/opt/omitted-plugin'}],
            'inherits': [{'path': '/opt/shared/plugins.json'}]}))
        (root / 'plugins/example').mkdir(parents=True)
        (root / 'plugins/example/plugin.json').write_text('{"name":"example"}')
        (root / 'settings.json').write_text('{"toolPermission":"always-proceed"}')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    text = (run.control / 'customizations.json').read_text()
    inventory = json.loads(text)
    for root in roots:
        entry = next(row for row in inventory if row['path'] == str(root))
        assert entry['omitted_mcp_servers'] == ['omitted-server']
        assert entry['omitted_plugin_entries'] == ['/opt/omitted-plugin']
        assert entry['omitted_plugin_inherits'] == ['/opt/shared/plugins.json']
        assert 'plugins/example/plugin.json' in entry['omitted_paths']
        assert 'settings.json' in entry['omitted']
    assert 'synthetic-secret' not in text and 'DO_NOT_RECORD' not in text


def test_enclosing_native_home_execution_omissions_are_inventoried(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    native_home = home / '.gemini'
    (native_home / 'plugins/example').mkdir(parents=True)
    (native_home / 'plugins/example/hooks.json').write_text('SYNTHETIC_PLUGIN_CONTENT')
    (native_home / 'settings.json').write_text('{"mcpServers":{"legacy-server":{}}}')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    try:
        text = (run.control / 'customizations.json').read_text()
        entry = next((row for row in json.loads(text) if row['path'] == str(native_home)), None)
        assert entry is not None, 'enclosing native home missing from omission inventory'
        assert {'plugins', 'settings.json'} <= set(entry['omitted'])
        assert 'plugins/example/hooks.json' in entry['omitted_paths']
        assert 'SYNTHETIC_PLUGIN_CONTENT' not in text
    finally:
        run.cleanup()


def test_enclosing_native_home_does_not_reexpose_omitted_execution_content(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    native_home = home / '.gemini'
    for name in ('agents', 'plugins'):
        (native_home / name).mkdir()
        (native_home / name / 'unapproved.md').write_text('SYNTHETIC_EXECUTION_CONTENT')
    for name in ('hooks.json', 'plugins.json', 'mcp_config.json', 'config.json', 'import_manifest.json'):
        (native_home / name).write_text('{"synthetic-execution":"must-be-omitted"}')
    (native_home / 'rules').mkdir()
    (native_home / 'rules/AGENTS.md').write_text('retained instructions')
    (native_home / 'installation-data').write_text('native data')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    try:
        child = subprocess.run(run.wrap([sys.executable, '-I', '-c',
            'import pathlib,sys\n'
            'root=pathlib.Path(sys.argv[1])\n'
            'for name in sys.argv[2:]:\n'
            ' assert not (root/name).exists(), "unapproved execution reexposed: "+name\n'
            'assert (root/"rules/AGENTS.md").read_text()=="retained instructions"\n'
            'assert (root/"installation-data").read_text()=="native data"\n',
            str(native_home), 'agents', 'plugins', 'hooks.json', 'plugins.json',
            'mcp_config.json', 'config.json', 'import_manifest.json']),
            text=True, capture_output=True, timeout=HANG_SECONDS)
        assert child.returncode == 0, child.stdout + child.stderr
    finally:
        run.cleanup()


def test_customization_snapshots_ignore_unrelated_audit_artifacts(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    root = home / '.agent'
    (root / 'audits').mkdir(parents=True)
    (root / 'audits/old-fixture').symlink_to(root / 'missing', target_is_directory=True)
    (root / 'rules').mkdir()
    (root / 'rules/AGENTS.md').write_text('retained rule')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    snapshot = dict(run.customizations)[root]
    assert (snapshot / 'rules/AGENTS.md').read_text() == 'retained rule'
    assert not (snapshot / 'audits').exists()


def test_native_helper_rewrites_cannot_poison_a_later_invocation(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    native = home / '.gemini/antigravity-cli/bin/agentapi'
    first = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    changed = subprocess.run(first.wrap([sys.executable, '-I', '-c',
        'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("native rewrite")',
        str(native)]), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert changed.returncode == 0, changed.stderr
    assert native.read_text() == 'original helper'
    first.cleanup()
    second = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    observed = subprocess.run(second.wrap([sys.executable, '-I', '-c',
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())',
        str(native)]), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == 'original helper'


def test_native_global_instructions_are_frozen_before_native_start(installation):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    global_rules = home / '.gemini/GEMINI.md'
    global_skill = home / '.gemini/config/skills/example/SKILL.md'
    global_skill.parent.mkdir(parents=True)
    for path in (global_rules, global_skill):
        path.write_text('original instruction')
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    for path in (global_rules, global_skill):
        path.write_text('changed after preparation')
    result = subprocess.run(run.wrap([sys.executable, '-I', '-c',
        'from pathlib import Path; import sys; '
        'assert all(Path(p).read_text()=="original instruction" for p in sys.argv[1:])',
        str(global_rules), str(global_skill)]), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('kind', ['skills', 'rules'])
@pytest.mark.parametrize('scope', ['project', 'global'])
def test_manifest_references_are_frozen_recursively_with_filters(installation, kind, scope):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    # A relative manifest entry falls back to the project root: the nearest
    # ancestor of cwd holding .git. Make the tree that root, as a real task
    # tree is; otherwise any ancestor of tmp_path with a .git (for example a
    # basetemp under a home directory that has one) becomes the "project" and
    # 'shared' resolves against the real host instead of this fixture. A real
    # repository, since preparation also fences the tree through Git.
    subprocess.run(['git', 'init', '-q', str(tree)], check=True, timeout=HANG_SECONDS)
    custom = tree / '.agents' if scope == 'project' else home / '.gemini/config'
    custom.mkdir(exist_ok=True)
    shared = tree / 'shared'
    (shared / 'keep').mkdir(parents=True)
    (shared / 'keep/SKILL.md').write_text('original shared instruction')
    personal = home / 'personal'
    personal.mkdir()
    (personal / 'extra').mkdir()
    (personal / 'extra/SKILL.md').write_text('original personal instruction')
    inherited = home / 'inherited.json'
    inherited.write_text(json.dumps({'entries': [{'path': '~/personal', 'exclude': ['skip-.*']}]}))
    manifest = {'entries': [{'path': 'shared', 'include_only': ['keep'], 'exclude': ['skip']}],
        'inherits': [{'path': '~/inherited.json', 'include_only': ['extra']}], 'exclude': ['old']}
    (custom / f'{kind}.json').write_text(json.dumps(manifest))
    run = prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    snapshot = dict(run.customizations)[custom] if scope == 'project' else run.config
    frozen = json.loads((snapshot / f'{kind}.json').read_text())
    frozen_entry = Path(frozen['entries'][0]['path'])
    assert frozen_entry.is_relative_to(run.control), 'manifest still references live instructions'
    assert frozen['entries'][0]['include_only'] == ['keep']
    assert frozen['entries'][0]['exclude'] == ['skip']
    assert frozen['exclude'] == ['old']
    assert frozen['inherits'][0]['include_only'] == ['extra']
    frozen_manifest = Path(frozen['inherits'][0]['path'])
    assert frozen_manifest.is_relative_to(run.control)
    nested = json.loads(frozen_manifest.read_text())
    assert nested['entries'][0]['exclude'] == ['skip-.*']
    personal_entry = Path(nested['entries'][0]['path'])
    assert personal_entry.is_relative_to(run.control)
    (shared / 'keep/SKILL.md').write_text('modified after preparation')
    (personal / 'extra/SKILL.md').write_text('modified after preparation')
    assert (frozen_entry / 'keep/SKILL.md').read_text() == 'original shared instruction'
    assert (personal_entry / 'extra/SKILL.md').read_text() == 'original personal instruction'
    result = subprocess.run(run.wrap([sys.executable, '-I', '-c',
        'from pathlib import Path; import sys; '
        'p=Path(sys.argv[1]); assert p.read_text()=="original shared instruction"; '
        '\ntry: p.write_text("changed")\nexcept OSError: pass\nelse: raise SystemExit("writable snapshot")',
        str(frozen_entry / 'keep/SKILL.md')]), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('invalid', ['cycle', 'missing', 'filters', 'execution-field'])
def test_invalid_retained_manifest_fails_closed_before_native_launch(installation, invalid):
    from maestro.backends.agy_runtime import prepare
    home, tree, workspace = installation
    custom = tree / '.agents'
    custom.mkdir()
    path = custom / 'skills.json'
    document = {
        'cycle': {'inherits': [{'path': str(path)}]},
        'missing': {'entries': [{'path': '/nonexistent-w4-skill-fixture'}]},
        'filters': {'entries': [{'path': str(tree), 'include_only': 'not-a-list'}]},
        'execution-field': {'plugins': [{'path': str(tree)}]},
    }[invalid]
    path.write_text(json.dumps(document))
    with pytest.raises((ValueError, RuntimeError), match='manifest|customization'):
        prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)


@pytest.mark.parametrize('swapper', ['host', 'native'])
def test_repreparation_never_writes_through_substituted_persistent_state(installation, monkeypatch, swapper):
    from maestro.backends import agy_runtime
    home, tree, workspace = installation
    first = agy_runtime.prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    victim = tree.parent / 'outside-policy.json'
    victim.write_text('must survive')
    target = first.state / 'mcp_config.json'
    original = agy_runtime._security_profile
    calls = []
    def swap(*args):
        calls.append(True)
        if swapper == 'host':
            target.unlink()
            target.symlink_to(victim)
        else:
            program = ('from pathlib import Path\n'
                f'path=Path({str(target)!r})\n'
                'try:\n path.unlink()\n'
                f' path.symlink_to({str(victim)!r})\n'
                'except OSError: pass\n')
            result = subprocess.run(first.wrap([sys.executable, '-I', '-c', program]),
                capture_output=True, text=True, timeout=HANG_SECONDS)
            assert result.returncode == 0, result.stderr
        return original(*args)
    monkeypatch.setattr(agy_runtime, '_security_profile', swap)
    first.cleanup()
    try:
        agy_runtime.prepare(cwd=tree, workspace=workspace, write_roots=(tree,), home=home)
    except (ValueError, OSError):
        pass
    assert calls
    assert victim.read_text() == 'must survive'
