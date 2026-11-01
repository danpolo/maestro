"""Real graph controller/worker death; only native inference/protocol is synthetic.

No existing login is used. Child controllers die without closing the database,
and child workers die inside the production private-Git publication boundary.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.workflows import handlers, runner
from maestro.workflows.models import NodeSpec, RetryPolicy, WorkflowRevision
from tests.test_git_staging import git
from tests.test_codex_app_server import server
from tests.hang_bound import HANG_SECONDS

pytestmark = pytest.mark.usefixtures('isolated_native_host_home')


def controller_entry():
    from maestro.backends import catalog
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.compiler import load_revision
    from tests.graph_engineering.conftest import fixture_cli_entry, fixture_routing

    repo, action = Path(sys.argv[1]), sys.argv[2]
    store = ControlStore.open_controller(repo / '.orchestrator/control.sqlite3')
    entry = fixture_cli_entry(backend_id='codex', usage_pool_id='codex',
        effort_controls=catalog.EffortControls(supported=True,
            delivery_mechanism='config_key', config_key='model_reasoning_effort',
            native_values=('low', 'medium', 'high')))
    routing = fixture_routing(entries=[entry])
    routing.task_spec = lambda tid: normalize_spec({'id': tid, 'title': tid,
        'verifications': [{'id': 'V-write', 'kind': 'auto', 'cmd': 'true'}]})
    dispatched = []

    def tmux(cmd, **kwargs):
        if cmd[:2] == ['tmux', 'new-window']:
            import shlex
            dispatched.append(shlex.split(cmd[-1]))
        return subprocess.CompletedProcess(cmd, 0, '', '')

    kwargs = dict(repo=repo, routing=routing,
        dispatcher=handlers.TmuxDispatcher(runner=tmux),
        artifacts=ArtifactStore(repo / '.orchestrator/artifacts', store))
    if action == 'start':
        node = NodeSpec(node_id='writer', kind='agent', handler_ref='maestro.handlers.agent',
            agent_definition_ref='implementer', permissions_ref='task_worktree_writer',
            resource_request={'resource_id': 'worktree:T-001', 'writer_lease': True},
            effect_class='agent_editing_worktree', retry_policy=RetryPolicy(max_attempts=3))
        revision = WorkflowRevision(workflow_id='native-recovery', revision=1,
            task_id='T-001', task_spec_revision=1, compiler_version='fixture',
            policy_hash='', code_base_sha=git(repo, 'rev-parse', 'HEAD').stdout.strip(),
            execution_objective='paid_efficiency', nodes=(node,), edges=(),
            terminal_requirements=('writer',))
        run = runner.start_run(store, revision, run_id='native-recovery', **kwargs)
    else:
        revision = load_revision(store, 'native-recovery', 1)
        run = runner.WorkflowRunner(store, revision, run_id='native-recovery', **kwargs)
    report = run.tick()
    rows = [dict(row) for row in store.conn.execute('SELECT * FROM attempts ORDER BY rowid')]
    output = dict(dispatched=dispatched, phases=run.phases(), attempts=rows,
                  outcome=report.outcome, refused=report.refused)
    (repo.parent / (action + '.json')).write_text(json.dumps(output))
    if action in ('start', 'retry'):
        os._exit(71)  # abrupt controller death, including a retry already dispatched
    store.close()


def worker_entry():
    from maestro.backends import codex, git_staging, registry
    from maestro.workflows import worker

    executable, native, phase = sys.argv[1:4]
    registry.get_backend = lambda name: codex.CodexBackend(binary=executable, codex_home=Path(native))
    dump = git_staging._dump
    native_git = git_staging._git

    def published(path, value):
        dump(path, value)
        if phase == 'ready' and path.name == 'active.json' and value.get('state') == 'ready':
            os._exit(73)

    def promoted(tree, *args, **kwargs):
        result = native_git(tree, *args, **kwargs)
        if phase == 'post-cas' and args[0] == 'update-ref':
            os._exit(73)
        return result

    git_staging._dump, git_staging._git = published, promoted
    raise SystemExit(worker.main(sys.argv[4:]))


def child(function, *args):
    return subprocess.run([sys.executable, '-c',
        'from tests.test_native_graph_recovery import ' + function + '; ' + function + '()',
        *map(str, args)], capture_output=True, text=True, timeout=HANG_SECONDS)


@pytest.mark.parametrize('phase', ['ready', 'post-cas'])
def test_graph_controller_restart_restores_native_writer_before_model_retry(
    tmp_path, monkeypatch, server, phase,
):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'core.hooksPath', '/dev/null')
    (repo / '.gitignore').write_text('.orchestrator/\n')
    (repo / 'file.txt').write_text('baseline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'baseline')
    start = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    git(repo, 'branch', 'impl/sibling')
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'auth.json').write_text('synthetic')
    permissions = tmp_path / 'permissions.json'
    permissions.write_text(json.dumps({'version': 1, 'allow': ['Bash(echo *)']}))
    monkeypatch.setenv('MAESTRO_PERMISSIONS_FILE', str(permissions))
    original = server.read_text().replace('mode = sys.argv[1]', "mode = 'ok'")

    def invoke(argv, crash_phase):
        attempt = argv[argv.index('--attempt-id') + 1]
        workspace = handlers.AttemptInbox(handlers.attempts_root(repo), attempt).path
        # The synthetic native server writes and commits through the real private
        # Git mount, then reports DONE *before* parent publication and the receipt.
        body = original.replace("        p = m['params']", "        p = m['params']\n" +
            "        import subprocess\n" +
            "        Path(p['cwd'], 'file.txt').write_text('MODEL-WRITER\\n')\n" +
            "        subprocess.run(['git', 'add', 'file.txt'], cwd=p['cwd'], check=True, stdout=sys.stderr)\n" +
            "        subprocess.run(['git', 'commit', '-m', 'MODEL-WRITER'], cwd=p['cwd'], check=True, stdout=sys.stderr)\n" +
            f"        Path({str(workspace / 'DONE')!r}).write_text('provisional')")
        server.write_text('#!' + sys.executable + '\n' + body)
        server.chmod(0o755)
        # The normal argv is the pinned module launcher. Keep all its resolved
        # graph binding/manifest/effort/session arguments, replacing only entry.
        flags = argv[argv.index('--repo'):]
        result = child('worker_entry', server, native, crash_phase, *flags)
        return result, workspace

    first = child('controller_entry', repo, 'start')
    assert first.returncode == 71, first.stderr + first.stdout
    started = json.loads((tmp_path / 'start.json').read_text())
    assert len(started['dispatched']) == 1, started
    first_argv = started['dispatched'][0]
    tree = Path(first_argv[first_argv.index('--worktree') + 1])
    failed, workspace = invoke(first_argv, phase)
    assert failed.returncode == 73, failed.stderr + failed.stdout + str(list(workspace.iterdir()))
    assert (workspace / 'DONE').exists()
    assert not handlers.AttemptInbox(handlers.attempts_root(repo), started['attempts'][0]['attempt_id']).receipt()
    assert (tree / 'file.txt').read_text() == 'MODEL-WRITER\n'

    second = child('controller_entry', repo, 'retry')
    assert second.returncode == 71, second.stderr + second.stdout
    retried = json.loads((tmp_path / 'retry.json').read_text())
    assert len(retried['dispatched']) == 1, retried
    assert retried['phases']['writer'] != 'succeeded'
    assert len(retried['attempts']) == 2
    assert retried['attempts'][0]['failure_kind'] == 'worker_crashed'
    assert retried['attempts'][0]['start_sha'] == start
    assert retried['attempts'][1]['start_sha'] == start
    assert (tree / 'file.txt').read_text() == 'baseline\n'
    assert git(tree, 'status', '--porcelain').stdout == ''
    succeeded, retry_workspace = invoke(retried['dispatched'][0], 'none')
    assert succeeded.returncode == 0, succeeded.stderr + succeeded.stdout
    from maestro.backends import git_staging
    assert git_staging.publication_state(retry_workspace) == 'promoted'
    assert (tree / 'file.txt').read_text() == 'MODEL-WRITER\n'
    final = child('controller_entry', repo, 'settle')
    assert final.returncode == 0, final.stderr + final.stdout
    settled = json.loads((tmp_path / 'settle.json').read_text())
    assert settled['outcome'] == 'succeeded', settled
    assert settled['dispatched'] == []
    assert [a['status'] for a in settled['attempts']] == ['failed', 'succeeded']
    assert git(repo, 'rev-parse', 'main').stdout.strip() == start
    assert git(repo, 'rev-parse', 'impl/sibling').stdout.strip() == start
    from tests.native_test_home import assert_codex_discovery_isolated
    for inbox in (workspace, retry_workspace):
        assert_codex_discovery_isolated(inbox, tmp_path / 'host-home')
