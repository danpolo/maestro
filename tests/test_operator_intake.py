"""Operator reports enter normal tasks; intake never answers an active verification.

These tests catch missing persistence, pre-approval dispatch, stale-scope approval and
active-task mutation. Only Telegram transport and model completion are substituted.
"""
from __future__ import annotations

import json
import queue

import pytest

from maestro.control.store import ControlStore
from maestro.hitl import commands


def proposal(**changes):
    task = {'title': 'Make playlist tracks playable',
            'short_desc': 'Diagnose the same-ID playlist playback failure and repair the demonstrated cause.',
            'scope': ['app.py', 'tests/'], 'deps': [], 'risk': 'low',
            'acceptance_criteria': ['Same-ID track plays in the playlist for the affected account.',
                                    'Preserve operator intent and role attribution.'],
            'verifications': [{'id': 'V-SUITE', 'kind': 'auto', 'check': 'The whole suite passes.',
                               'cmd': 'python -m pytest'},
                              {'id': 'V-DAN', 'kind': 'dan', 'check': 'Dan observes playback working.',
                               'steps': [{'do': 'Play the captured example from the playlist.'}]}]}
    task.update(changes)
    return json.dumps({'task': task, 'questions': []})


def captured(world):
    repo, orch, sent, store = world
    from maestro import intake
    intake.handle(repo, '/report bug same ID 13HD3tYTiz8oSRxWzXEq7g plays from search but not Duet', 42, sent.append)
    return intake


def message(uid, text, chat=4242):
    return {'update_id': uid, 'message': {'chat': {'id': chat}, 'text': text}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = tmp_path / 'project'
    orch = repo / '.orchestrator'
    orch.mkdir(parents=True)
    (repo / 'docs').mkdir()
    (repo / 'docs/ROADMAP.md').write_text('# Roadmap\n\n```yaml\nid: existing\ntitle: Existing work\nscope: [app.py]\nstatus: open\nmode: autonomous\ndeps: []\n```\n')
    (orch / 'state.json').write_text('{"paused_by_user":true}')
    (orch / 'HALT').write_text('deliberate halt')
    (orch / 'questions').mkdir()
    (orch / 'questions/real.answer').write_text('untouched')
    sent = []
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '4242')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test')
    monkeypatch.setattr(commands, 'REPO', repo)
    monkeypatch.setattr(commands, '_HALTED_OFFSET', 0)
    monkeypatch.setattr(commands, '_HALTED_REPLIED', set())
    monkeypatch.setattr(commands, '_LISTENER_QUEUE', queue.Queue())
    monkeypatch.setattr(commands, 'notify_telegram', sent.append)
    monkeypatch.setattr(commands, 'notify_intake', sent.append)
    monkeypatch.setattr(commands, 'append_journal', lambda *a, **k: None)
    monkeypatch.setattr(commands, '_ingest_control_command', lambda *a: None)
    store = ControlStore.open_controller(orch / 'control.sqlite3')
    monkeypatch.setattr(commands, '_settle_control_command', lambda *a: None)
    yield repo, orch, sent, store
    store.close()


def test_telegram_report_is_durable_before_ack_and_does_not_queue_unapproved_work(world):
    repo, orch, sent, store = world
    original = (repo / 'docs/ROADMAP.md').read_bytes()
    commands._LISTENER_QUEUE.put(message(42, '/report bug track 13HD3tYTiz8oSRxWzXEq7g is grey in Duet but plays from search'))
    commands.poll_control_commands([])
    path = orch / 'intake/I-42.json'
    assert path.exists(), 'Telegram report must be captured durably'
    data = json.loads(path.read_text())
    assert data['kind'] == 'bug' and '13HD3tYTiz8oSRxWzXEq7g' in data['text']
    assert data['status'] == 'captured'
    assert 'I-42' in sent[-1]
    assert (repo / 'docs/ROADMAP.md').read_bytes() == original
    assert store.task('I-42') is None
    assert (orch / 'questions/real.answer').read_text() == 'untouched'
    assert (orch / 'HALT').read_text() == 'deliberate halt'


def test_halted_report_survives_redelivery_and_rejects_other_chats(world):
    repo, orch, sent, store = world
    updates = [message(43, '/report feature notify me about a completed refresh'),
               message(44, '/report bug foreign report', chat=999)]
    commands.answer_while_halted(lambda offset: updates)
    commands._HALTED_REPLIED.clear()  # simulate a watchdog restart before Telegram acknowledges
    commands.answer_while_halted(lambda offset: updates)
    files = list((orch / 'intake').glob('I-*.json'))
    assert len(files) == 1, 'HALT must not discard or duplicate a report'
    assert json.loads(files[0].read_text())['status'] == 'captured'
    assert store.task('I-43') is None
    assert not (orch / 'intake/I-44.json').exists()


def test_malformed_report_is_explained_without_creating_a_task(world):
    repo, orch, sent, store = world
    commands._LISTENER_QUEUE.put(message(45, '/report bug'))
    commands.poll_control_commands([])
    assert sent and '/report' in sent[-1] and 'description' in sent[-1].lower()
    assert not list((orch / 'intake').glob('I-*.json'))


def test_preparation_requires_approval_before_publishing_to_normal_task_store(world):
    repo, orch, sent, store = world
    intake = captured(world)
    original = (repo / 'docs/ROADMAP.md').read_bytes()
    intake.process(repo, store, sent.append, generate=lambda prompt: proposal())
    data = intake.load(repo, 'I-42')
    assert data['status'] == 'awaiting_approval' and data['revision'] == 1
    assert store.task('I-42') is None
    assert (repo / 'docs/ROADMAP.md').read_bytes() == original
    assert any('/intake approve I-42 1' in line for line in sent)
    intake.handle(repo, '/intake approve I-42 1', 99, sent.append)
    intake.process(repo, store, sent.append, generate=lambda prompt: pytest.fail('approval must not re-call the model'))
    assert intake.load(repo, 'I-42')['status'] == 'queued'
    assert dict(store.task('I-42'))['status'] == 'planned'
    from maestro import taskgraph
    graph = taskgraph.build_graph(taskgraph.load_document(repo / 'docs/ROADMAP.md'))
    assert graph.spec('I-42').task_class == 'bug'
    assert '13HD3tYTiz8oSRxWzXEq7g' in graph.spec('I-42').notes
    assert graph.spec('existing').title == 'Existing work'
    assert (orch / 'HALT').read_text() == 'deliberate halt'
    assert (orch / 'state.json').read_text() == '{"paused_by_user":true}'
    assert (orch / 'questions/real.answer').read_text() == 'untouched'
    assert store.conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


def test_amendment_invalidates_earlier_scope_approval(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, store, sent.append, generate=lambda prompt: proposal())
    intake.handle(repo, '/intake amend I-42 Preserve the original recording ID', 50, sent.append)
    intake.handle(repo, '/intake approve I-42 1', 51, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'captured'
    intake.process(repo, store, sent.append, generate=lambda prompt: proposal())
    assert intake.load(repo, 'I-42')['revision'] == 2
    intake.handle(repo, '/intake approve I-42 1', 52, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    assert store.task('I-42') is None
    intake.handle(repo, '/intake approve I-42 2', 53, sent.append)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'queued'


def test_approved_addition_waits_for_active_run_without_touching_its_spec_or_answer(world):
    repo, orch, sent, store = world
    intake = captured(world)
    store.register_task('existing')
    store.conn.execute("INSERT INTO task_runs VALUES ('run_old','existing','wf_old',1,'running','now',NULL)")
    original = (repo / 'docs/ROADMAP.md').read_bytes()
    intake.process(repo, store, sent.append, generate=lambda prompt: proposal())
    intake.handle(repo, '/intake approve I-42 1', 60, sent.append)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'approved'
    assert store.task('I-42') is None
    assert (repo / 'docs/ROADMAP.md').read_bytes() == original
    assert (orch / 'questions/real.answer').read_text() == 'untouched'
    store.conn.execute("UPDATE task_runs SET status='succeeded' WHERE run_id='run_old'")
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'queued'
    assert dict(store.task('existing'))['current_spec_revision'] == 1


@pytest.mark.parametrize('raw', [
    'not JSON', proposal(scope=['../outside']), proposal(scope=['/etc/']),
    proposal(deps=['unknown-task']), proposal(verifications=[]), proposal(acceptance_criteria=[]),
])
def test_invalid_proposal_remains_reported_and_cannot_be_approved(world, raw):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, store, sent.append, generate=lambda prompt: raw)
    assert intake.load(repo, 'I-42')['status'] == 'needs_retry'
    intake.handle(repo, '/intake approve I-42 1', 62, sent.append)
    assert store.task('I-42') is None
    assert any('/intake retry I-42' in text for text in sent)


def test_open_material_questions_block_approval_until_amended(world):
    repo, orch, sent, store = world
    intake = captured(world)
    raw = json.loads(proposal())
    raw['questions'] = ['Should a different recording ever be substituted?']
    intake.process(repo, store, sent.append, generate=lambda prompt: json.dumps(raw))
    intake.handle(repo, '/intake approve I-42 1', 65, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    assert store.task('I-42') is None
    assert any('amend' in text for text in sent)


def test_rejection_persists_without_launching_or_regenerating(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, store, sent.append, generate=lambda prompt: proposal())
    intake.handle(repo, '/intake reject I-42 Outside this pilot', 66, sent.append)
    intake.process(repo, store, sent.append, generate=lambda prompt: pytest.fail('rejected reports must stay rejected'))
    assert intake.load(repo, 'I-42')['status'] == 'rejected'
    assert store.task('I-42') is None


def test_no_controller_can_prepare_while_halted_but_cannot_publish(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda prompt: proposal())
    intake.handle(repo, '/intake approve I-42 1', 67, sent.append)
    intake.process(repo, None, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'approved'
    assert store.task('I-42') is None


def test_comment_only_roadmap_example_allows_proposal_and_approved_publication(world):
    repo, orch, sent, store = world
    roadmap = repo / 'docs/ROADMAP.md'
    example = ('## Example (commented out — not a real task)\n\n```yaml\n'
               '# id: example-001\n# deps: [nonexistent]\n# title: Example\n```\n\n')
    roadmap.write_text(example + roadmap.read_text())
    original = roadmap.read_bytes()
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda prompt: proposal())
    report = intake.load(repo, 'I-42')
    assert report['status'] == 'awaiting_approval'
    assert report['revision'] == 1
    assert roadmap.read_bytes() == original
    assert store.task('I-42') is None
    intake.handle(repo, '/intake approve I-42 1', 67, sent.append)
    intake.process(repo, store, sent.append, prepare=False)
    assert intake.load(repo, 'I-42')['status'] == 'queued'
    assert store.task('I-42') is not None
    assert roadmap.read_text().startswith(example)
    assert (orch / 'HALT').read_text() == 'deliberate halt'


def test_repeated_apply_is_idempotent_after_database_persistence_failure(world, monkeypatch):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, store, sent.append, generate=lambda prompt: proposal())
    intake.handle(repo, '/intake approve I-42 1', 69, sent.append)
    from maestro import taskgraph
    real_sync = taskgraph.sync_to_store
    monkeypatch.setattr(taskgraph, 'sync_to_store', lambda *a, **k: (_ for _ in ()).throw(OSError('disk error')))
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'publishing'
    monkeypatch.setattr(taskgraph, 'sync_to_store', real_sync)
    intake.process(repo, store, sent.append)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'queued'
    assert (repo / 'docs/ROADMAP.md').read_text().count('id: I-42') == 1
    assert dict(store.task('I-42'))['current_spec_revision'] == 1


def test_malformed_dan_steps_and_check_fields_cannot_be_approved(world):
    repo, orch, sent, store = world
    intake = captured(world)
    for changes in [dict(steps='just do it'), dict(steps=[{}]), dict(id=['bad']), dict(check=7)]:
        raw = json.loads(proposal())
        raw['task']['verifications'][1].update(changes)
        intake.process(repo, None, sent.append, generate=lambda p: json.dumps(raw))
        assert intake.load(repo, 'I-42')['status'] == 'needs_retry'
        intake.handle(repo, '/intake retry I-42', 100, sent.append)


def test_scope_cannot_escape_through_a_project_symlink(world, tmp_path):
    repo, orch, sent, store = world
    (repo / 'escape').symlink_to(tmp_path)
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal(scope=['escape/outside.py']))
    assert intake.load(repo, 'I-42')['status'] == 'needs_retry'


def test_delivery_failure_retains_proposal_and_retries_without_model_call(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, lambda text: False, generate=lambda p: proposal())
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    intake.process(repo, None, sent.append, generate=lambda p: pytest.fail('do not regenerate'))
    assert '/intake approve I-42 1' in sent[-1]
    count = len(sent)
    intake.process(repo, None, sent.append)
    assert len(sent) == count


def test_corrupt_report_does_not_starve_other_reports(world):
    repo, orch, sent, store = world
    intake = captured(world)
    (orch / 'intake/I-1.json').write_text('{"id":"I-1"}')
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    intake.handle(repo, '/reports', 99, sent.append)
    assert 'I-42' in sent[-1] and 'I-1' in sent[-1]


def test_redelivered_amendment_does_not_invalidate_a_fresh_proposal(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake amend I-42 preserve recording', 88, sent.append)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake amend I-42 preserve recording', 88, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    assert len(intake.load(repo, 'I-42')['amendments']) == 1


def test_identical_existing_task_is_never_adopted_as_publication_recovery(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 89, sent.append)
    from maestro import taskgraph
    taskgraph.apply(repo, lambda txn: txn.add_task(intake.load(repo, 'I-42')['proposal']['task']))
    intake.process(repo, store, sent.append)
    intake.process(repo, store, sent.append)
    assert store.task('I-42') is None
    assert intake.load(repo, 'I-42')['status'] == 'approved'


def test_existing_store_task_is_not_rerevisioned_by_intake(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 89, sent.append)
    store.register_task('I-42')
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'approved'
    assert (repo / 'docs/ROADMAP.md').read_text().count('id: I-42') == 0
    assert store.task('I-42')['current_spec_revision'] == 1


def test_roadmap_edit_during_publication_is_preserved(world, monkeypatch):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 89, sent.append)
    from maestro import taskgraph
    real_commit = taskgraph.RoadmapTransaction.commit
    def edit_before_write(txn):
        result = real_commit(txn)
        (repo / 'docs/ROADMAP.md').write_text('# Operator edit\n' + (repo / 'docs/ROADMAP.md').read_text())
        return result
    monkeypatch.setattr(taskgraph.RoadmapTransaction, 'commit', edit_before_write)
    intake.process(repo, store, sent.append)
    assert (repo / 'docs/ROADMAP.md').read_text().startswith('# Operator edit')
    assert store.task('I-42') is None


def test_progress_tracks_run_acceptance_and_graduation_without_claiming_early_resolution(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, store, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 89, sent.append)
    intake.process(repo, store, sent.append)
    store.conn.execute("INSERT INTO task_runs VALUES ('intake_run','I-42','wf',1,'running','now',NULL)")
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'running'
    store.conn.execute("UPDATE tasks SET status='accepted' WHERE task_id='I-42'")
    store.conn.execute("UPDATE task_runs SET status='succeeded' WHERE run_id='intake_run'")
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'accepted'
    (orch / 'completed_tasks.json').write_text('[{"id":"I-42"}]')
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'graduated'
    intake.handle(repo, '/intake amend I-42 more scope', 93, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'graduated'


def test_public_cli_tick_prepares_while_halted_and_never_starts_a_worker(world, monkeypatch, capsys):
    repo, orch, sent, store = world
    intake = captured(world)
    monkeypatch.setattr(intake, '_generate', lambda repo, prompt: proposal())
    from maestro import cli
    monkeypatch.setattr(cli.bootstrap, 'reexec_into_adopted_version', lambda argv: None)
    assert cli.main(['intake', 'tick', '--repo', str(repo)]) == 0
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    assert '/intake approve I-42 1' in capsys.readouterr().out
    assert (orch / 'HALT').read_text() == 'deliberate halt'
    assert store.conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


@pytest.mark.parametrize('ok,body,expected', [(True, {'ok': True}, True), (True, {'ok': False}, False),
                                             (False, {'ok': False}, False)])
def test_intake_delivery_checks_the_telegram_api_result(monkeypatch, ok, body, expected):
    from maestro.hitl import telegram
    from types import SimpleNamespace
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test')
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '4242')
    monkeypatch.setattr(telegram.requests, 'post', lambda *a, **k: SimpleNamespace(ok=ok, json=lambda: body))
    assert telegram.notify_intake('proposal') is expected


def test_cli_publish_uses_idle_store_and_preserves_halt(world, monkeypatch, capsys):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 101, sent.append)
    from maestro import cli
    monkeypatch.setattr(cli.bootstrap, 'reexec_into_adopted_version', lambda argv: None)
    assert cli.main(['intake', 'tick', '--publish', '--repo', str(repo)]) == 0
    with ControlStore.open_client(orch / 'control.sqlite3') as client:
        assert client.task('I-42')['status'] == 'planned'
    assert (orch / 'HALT').read_text() == 'deliberate halt'


def test_cli_publish_refuses_another_live_controller(world, monkeypatch, capsys):
    import os
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    store.conn.execute("UPDATE writer_leases SET writer_pid=? WHERE resource_id='controller'", (os.getppid(),))
    from maestro import cli
    monkeypatch.setattr(cli.bootstrap, 'reexec_into_adopted_version', lambda argv: None)
    assert cli.main(['intake', 'tick', '--publish', '--repo', str(repo)]) == 3
    assert 'publication deferred' in capsys.readouterr().out
    assert store.task('I-42') is None
    assert intake.load(repo, 'I-42')['status'] == 'approved'


def test_cli_publish_defers_for_legacy_work(world, monkeypatch):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    (orch / 'state.json').write_text('{"in_flight":[{"task_id":"existing"}]}')
    from maestro import cli
    monkeypatch.setattr(cli.bootstrap, 'reexec_into_adopted_version', lambda argv: None)
    assert cli.main(['intake', 'tick', '--publish', '--repo', str(repo)]) == 3
    with ControlStore.open_client(orch / 'control.sqlite3') as client:
        assert client.task('I-42') is None


def test_applied_roadmap_drift_defers_without_overwriting_the_edit(world):
    repo, orch, sent, store = world
    intake = captured(world)
    from maestro import taskgraph
    taskgraph.apply(repo)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    path = repo / 'docs/ROADMAP.md'
    edited = path.read_text() + '\nOperator edit\n'
    path.write_text(edited)
    intake.process(repo, store, sent.append)
    assert path.read_text() == edited
    assert intake.load(repo, 'I-42')['status'] == 'approved'
    assert store.task('I-42') is None


def test_partial_first_task_revision_write_recovers_without_rerevisioning(world, monkeypatch):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    from maestro import taskgraph
    real_write = taskgraph._write_revision
    monkeypatch.setattr(taskgraph, '_write_revision', lambda *a: (_ for _ in ()).throw(OSError('disk error')))
    intake.process(repo, store, sent.append)
    assert store.task('I-42')['current_spec_revision'] == 1
    assert intake.load(repo, 'I-42')['status'] == 'publishing'
    monkeypatch.setattr(taskgraph, '_write_revision', real_write)
    intake.process(repo, store, sent.append)
    assert store.task('I-42')['current_spec_revision'] == 1
    assert intake.load(repo, 'I-42')['status'] == 'queued'


def test_scope_constraints_survive_approval_and_normal_task_publication(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal(constraints=['Preserve the exact recording ID']))
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    intake.process(repo, store, sent.append)
    assert 'Preserve the exact recording ID' in (repo / 'docs/ROADMAP.md').read_text()


def test_partial_queued_report_write_recovers_the_same_task(world, monkeypatch):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    real_write = intake._write
    def fail_queue(path, data):
        if data['status'] == 'queued':
            raise OSError('report disk error')
        return real_write(path, data)
    monkeypatch.setattr(intake, '_write', fail_queue)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'publishing'
    monkeypatch.setattr(intake, '_write', real_write)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'queued'
    assert (repo / 'docs/ROADMAP.md').read_text().count('id: I-42') == 1
    assert store.task('I-42')['current_spec_revision'] == 1


def test_publication_recovers_after_operator_adopts_an_edit_before_the_addition(world, monkeypatch):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    from maestro import taskgraph
    real_apply = taskgraph.apply
    path = repo / 'docs/ROADMAP.md'
    def edit_then_apply(*a, **kw):
        path.write_text(path.read_text() + '\nOperator edit\n')
        return real_apply(*a, **kw)
    monkeypatch.setattr(taskgraph, 'apply', edit_then_apply)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'publishing'
    monkeypatch.setattr(taskgraph, 'apply', real_apply)
    real_apply(repo, allow_drift=True)
    intake.process(repo, store, sent.append)
    assert intake.load(repo, 'I-42')['status'] == 'queued'
    assert store.task('I-42')['current_spec_revision'] == 1
    assert 'Operator edit' in path.read_text()


def test_failed_capture_receipt_is_redelivered_from_the_durable_mailbox(world):
    repo, orch, sent, store = world
    from maestro import intake
    intake.handle(repo, '/report feature Add a refresh notice', 222, lambda text: False)
    assert intake.load(repo, 'I-222')['status'] == 'captured'
    intake.process(repo, None, sent.append, prepare=False)
    assert sent and 'Saved I-222' in sent[-1]


@pytest.mark.parametrize('field,value', [('status', []), ('kind', []), ('notification', {}),
                                        ('proposal', {}), ('action_updates', None), ('error', [])])
def test_corrupt_optional_fields_do_not_abort_other_report_commands(world, field, value):
    repo, orch, sent, store = world
    intake = captured(world)
    data = intake.load(repo, 'I-42')
    data['id'] = 'I-1'
    data[field] = value
    (orch / 'intake/I-1.json').write_text(json.dumps(data))
    intake.process(repo, None, sent.append, generate=lambda p: proposal())
    assert intake.load(repo, 'I-42')['status'] == 'awaiting_approval'
    intake.handle(repo, '/reports', 300, sent.append)
    assert 'I-42' in sent[-1] and 'I-1' in sent[-1]
    intake.handle(repo, '/intake I-1', 301, sent.append)
    assert 'Invalid report record' in sent[-1]


def test_explicit_cli_telegram_tick_retains_a_failed_proposal_delivery(world, monkeypatch, capsys):
    repo, orch, sent, store = world
    intake = captured(world)
    monkeypatch.setattr(intake, '_generate', lambda repo, prompt: proposal())
    from maestro import cli
    from maestro.hitl import telegram
    monkeypatch.setattr(cli.bootstrap, 'reexec_into_adopted_version', lambda argv: None)
    monkeypatch.setattr(telegram, 'notify_intake', lambda text: False)
    assert cli.main(['intake', 'tick', '--telegram', '--repo', str(repo)]) == 0
    assert intake.load(repo, 'I-42')['notification']
    assert '/intake approve I-42 1' in capsys.readouterr().out
    monkeypatch.setattr(telegram, 'notify_intake', sent.append)
    assert cli.main(['intake', 'tick', '--telegram', '--repo', str(repo)]) == 0
    assert 'notification' not in intake.load(repo, 'I-42')
    assert '/intake approve I-42 1' in sent[-1]


def test_reused_report_id_with_different_text_is_refused_without_overwriting_evidence(world):
    repo, orch, sent, store = world
    intake = captured(world)
    original = intake.load(repo, 'I-42')['text']
    intake.handle(repo, '/report feature a different request', 42, sent.append)
    assert intake.load(repo, 'I-42')['text'] == original
    assert 'collision' in sent[-1].lower()


def test_replayed_report_receipt_preserves_an_undelivered_proposal(world):
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, None, lambda text: False, generate=lambda p: proposal())
    intake.handle(repo, '/report bug same ID 13HD3tYTiz8oSRxWzXEq7g plays from search but not Duet', 42, lambda text: False)
    intake.process(repo, None, sent.append, prepare=False)
    assert any('/intake approve I-42 1' in text for text in sent)


def test_progress_reads_authoritative_legacy_work_and_waits_before_graduation(world):
    from maestro.control.models import EventType
    repo, orch, sent, store = world
    intake = captured(world)
    intake.process(repo, store, sent.append, generate=lambda p: proposal())
    intake.handle(repo, '/intake approve I-42 1', 102, sent.append)
    intake.process(repo, store, sent.append)
    store.append_event(EventType.LEGACY_STATE_WRITTEN, 'legacy_state',
                       payload={'document': {'in_flight': [{'task_id': 'I-42'}]}})
    intake.process(repo, None, sent.append, prepare=False)
    assert intake.load(repo, 'I-42')['status'] == 'running'
    store.append_event(EventType.LEGACY_STATE_WRITTEN, 'legacy_state',
                       payload={'document': {'waiting_on_dan': {'9': {'task_id': 'I-42'}}}})
    intake.process(repo, None, sent.append, prepare=False)
    assert intake.load(repo, 'I-42')['status'] == 'waiting'
    (orch / 'completed_tasks.json').write_text('[{"id":"I-42"}]')
    intake.process(repo, None, sent.append, prepare=False)
    assert intake.load(repo, 'I-42')['status'] == 'graduated'


def test_capture_is_the_report_branch_and_accepts_an_improvement_report_id(world):
    """GDT-12 S4 (§7.2): an approved code lever enters Maestro's own queue as an intake
    report written by `capture`, the `/report` branch's body; its id names the project and
    the proposal."""
    repo, orch, sent, store = world
    from maestro import intake
    text = 'IP-0001: the reviewer brief names what later rounds found.'
    data = intake.capture(repo, 'change', text, report_id='I-itv-IP-0001')
    assert (data['id'], data['kind'], data['text'], data['status']) == (
        'I-itv-IP-0001', 'change', text, 'captured')
    assert intake.load(repo, 'I-itv-IP-0001') == data
    assert 'Saved I-itv-IP-0001 (change)' in data['notification']
    # The same report again is a no-op; another text under that id is a collision.
    assert intake.capture(repo, 'change', text, report_id='I-itv-IP-0001') == data
    with pytest.raises(intake.IntakeError, match='collision'):
        intake.capture(repo, 'change', 'something else', report_id='I-itv-IP-0001')
    for bad in ('I-itv-IP-1', 'I-ITV-IP-0001', 'I-../x-IP-0001', 'X-1', 'I-itv'):
        with pytest.raises(intake.IntakeError):
            intake.capture(repo, 'change', text, report_id=bad)
    with pytest.raises(intake.IntakeError):
        intake.capture(repo, 'wish', text, report_id='I-1')
    # `/report` is capture: the same record shape, then the acknowledgement.
    intake.handle(repo, '/report bug the reviewer missed it', 46, sent.append)
    report = intake.load(repo, 'I-46')
    assert set(report) == set(data) - {'notification'}
    assert 'Saved I-46 (bug)' in sent[-1]
    assert store.task('I-itv-IP-0001') is None and store.task('I-46') is None
