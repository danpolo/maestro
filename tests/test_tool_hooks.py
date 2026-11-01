"""Authorization at the native agy hook payload boundary, without inference."""
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from maestro import tool_policy
from tests.hang_bound import HANG_SECONDS


@pytest.fixture
def boundary(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    return tool_policy.ToolPolicy(
        allow=("Read", "Glob", "Grep", "Bash(echo *)", "Bash(git status *)"),
        write_roots=(tree,),
    ), tree


def decision(boundary, name, args):
    from maestro import tool_hooks
    guard, tree = boundary
    return tool_hooks.authorize_agy({"toolCall": {"name": name, "args": args}},
                                    policy=guard, cwd=tree)


@pytest.mark.parametrize("tool,key", [
    ("write_to_file", "TargetFile"), ("replace_file_content", "TargetFile"),
    ("multi_replace_file_content", "TargetFile"),
])
def test_native_edits_require_assigned_nonsecret_paths(boundary, tool, key):
    _, tree = boundary
    assert decision(boundary, tool, {key: str(tree / "new.py")}).allowed
    assert not decision(boundary, tool, {key: str(tree.parent / "other.py")}).allowed
    assert not decision(boundary, tool, {key: str(tree / ".env")}).allowed


@pytest.mark.parametrize("tool,key", [
    ("view_file", "AbsolutePath"), ("view_file_outline", "AbsolutePath"),
    ("view_code_item", "File"), ("grep_search", "SearchPath"),
    ("find_by_name", "SearchDirectory"), ("list_dir", "DirectoryPath"),
])
def test_native_read_tools_cannot_escape_literal_secret_denials(boundary, tool, key):
    _, tree = boundary
    assert decision(boundary, tool, {key: str(tree / "source.py")}).allowed
    assert not decision(boundary, tool, {key: str(tree / ".env")}).allowed
    assert not decision(boundary, tool, {key: 7}).allowed


def test_shell_uses_its_actual_cwd_and_checks_every_command(boundary):
    _, tree = boundary
    assert decision(boundary, "run_command", {"CommandLine": "git status && echo ok",
                                              "Cwd": str(tree)}).allowed
    assert not decision(boundary, "run_command", {"CommandLine": "echo ok > result",
                                                  "Cwd": str(tree.parent)}).allowed
    assert not decision(boundary, "run_command", {"CommandLine": "echo ok; sudo whoami",
                                                  "Cwd": str(tree)}).allowed


@pytest.mark.parametrize("payload", [None, [], {}, {"toolCall": {}},
    {"toolCall": {"name": "write_to_file", "args": []}},
    {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "relative.py"}}},
    {"toolCall": {"name": "unrecognized", "args": {}}},
])
def test_malformed_or_unknown_native_calls_refuse(boundary, payload):
    from maestro import tool_hooks
    guard, tree = boundary
    assert not tool_hooks.authorize_agy(payload, policy=guard, cwd=tree).allowed


def test_empty_status_poll_is_safe_but_input_never_creates_an_unchecked_shell(boundary):
    assert decision(boundary, "command_status", {"CommandId": "owned"}).allowed
    assert decision(boundary, "send_command_input", {"CommandId": "owned", "Input": ""}).allowed
    assert not decision(boundary, "send_command_input", {
        "CommandId": "owned", "Input": "sudo whoami\n"}).allowed
    assert not decision(boundary, "send_command_input", {"CommandId": "owned"}).allowed


def test_installed_task_transport_refuses_input_and_new_task_actions(boundary):
    assert decision(boundary, "manage_task", {"TaskId": "owned/task-2", "Action": "send_input",
                                              "Input": ""}).allowed
    assert not decision(boundary, "manage_task", {"TaskId": "owned/task-2", "Action": "send_input",
                                                  "Input": "sudo whoami\n"}).allowed
    assert not decision(boundary, "manage_task", {"TaskId": "owned/task-2", "Action": "run",
                                                  "CommandLine": "sudo whoami"}).allowed
    assert not decision(boundary, "manage_task", {"TaskId": "owned/task-2", "Action": "send_input"}).allowed


def test_installed_status_action_can_observe_an_async_task_without_sending_input(boundary):
    # Exact Action/TaskId shape observed from installed agy 1.3.5.
    assert decision(boundary, "manage_task", {
        "TaskId": "owned/task-2", "Action": "status"}).allowed
    for name in ("manage_task", "command_status"):
        identifier = "TaskId" if name == "manage_task" else "CommandId"
        assert not decision(boundary, name, {
            identifier: "owned/task-2", "Action": "status", "Input": "sudo whoami\n"}).allowed


def test_hook_process_refuses_invalid_snapshot_or_input_without_failing_open(boundary, tmp_path):
    _, tree = boundary
    snapshot = tmp_path / "policy.json"
    snapshot.write_text(json.dumps({"version": 1, "cwd": str(tree),
                                    "allow": ["Read"], "write_roots": [str(tree)]}))
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    for content in ('{malformed', json.dumps({"toolCall": {"name": "write_to_file", "args": {
            "TargetFile": str(tree / "ok.txt")}}})):
        done = subprocess.run([sys.executable, "-m", "maestro.tool_hooks", "agy", str(snapshot)],
                              input=content, capture_output=True, text=True, env=environment)
        assert done.returncode == 0, done.stderr
        response = json.loads(done.stdout)
        assert response["decision"] == ("deny" if content == '{malformed' else "allow")
    snapshot.write_text('{malformed')
    done = subprocess.run([sys.executable, "-m", "maestro.tool_hooks", "agy", str(snapshot)],
                          input='{}', capture_output=True, text=True, env=environment)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["decision"] == "deny"


@pytest.mark.parametrize('primary_allowed,security_decision,expected', [
    (True, 'allow', True), (True, 'deny', False), (False, 'allow', False),
    (True, 'invalid', False),
])
def test_security_hooks_can_only_narrow_the_shared_policy(boundary, tmp_path,
    primary_allowed, security_decision, expected):
    from maestro import tool_hooks
    policy, tree = boundary
    hook = tmp_path / 'security.py'
    marker = tmp_path / 'called'
    hook.write_text('import json,sys\nfrom pathlib import Path\n'
        f'Path({str(marker)!r}).write_text(json.loads(sys.stdin.read())["toolCall"]["name"])\n'
        f'print(json.dumps({{"decision": {security_decision!r}, "reason": "security verdict"}}))\n')
    command = 'echo ok' if primary_allowed else 'sudo whoami'
    result = tool_hooks.authorize_agy_with_security({'toolCall': {'name': 'run_command',
        'args': {'CommandLine': command, 'Cwd': str(tree)}}}, policy=policy, cwd=tree,
        security_hooks=({'argv': [sys.executable, '-I', str(hook)], 'timeout': 2},))
    assert result.allowed is expected
    assert marker.exists() is primary_allowed


def test_snapshot_denials_never_replace_the_global_never_allow_rules(boundary, tmp_path):
    from maestro import tool_hooks
    _, tree = boundary
    snapshot = tmp_path / 'policy.json'
    snapshot.write_text(json.dumps({'version': 1, 'cwd': str(tree),
        'allow': ['Bash(*)', 'Read'], 'write_roots': [str(tree)],
        'deny': [f'Edit({tree}/control.json)']}))
    policy, _, _ = tool_hooks._snapshot(snapshot)
    assert not policy.command('sudo whoami', cwd=tree).allowed
    assert not policy.file('Read', tree / '.env').allowed


@pytest.mark.parametrize('hang', [False, True])
def test_security_hook_leaves_no_running_scanner_descendants(boundary, tmp_path, hang):
    import signal
    import time
    from maestro import tool_hooks
    policy, tree = boundary
    child = tmp_path / 'scanner.py'
    ready = tmp_path / 'ready'
    child.write_text('import os,time\nfrom pathlib import Path\n'
        f'Path({str(ready)!r}).write_text(str(os.getpid()))\n'
        'time.sleep(60)\n')
    hook = tmp_path / 'security.py'
    hook.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
        f'subprocess.Popen([sys.executable, {str(child)!r}], '
        'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        f'while not Path({str(ready)!r}).exists(): time.sleep(.01)\n'
        + ('time.sleep(60)\n' if hang else 'print(\'{"decision":"allow"}\')\n'))
    pid = None
    try:
        result = tool_hooks.authorize_agy_with_security({'toolCall': {'name': 'run_command',
            'args': {'CommandLine': 'echo ok', 'Cwd': str(tree)}}}, policy=policy, cwd=tree,
            security_hooks=({'argv': [sys.executable, '-I', str(hook)], 'timeout': 1},))
        assert result.allowed is (not hang)
        pid = int(ready.read_text())
        def running():
            try:
                return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
            except (FileNotFoundError, ProcessLookupError):
                return False
        deadline = time.monotonic() + HANG_SECONDS
        while running() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not running(), 'security scanner survived its hook verdict/timeout'
    finally:
        if pid is None and ready.exists():
            pid = int(ready.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
