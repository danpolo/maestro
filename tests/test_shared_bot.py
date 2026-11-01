from pathlib import Path

from maestro import cli
from maestro.hitl import telegram
from maestro.hitl import shared_bot
import json
import queue
import subprocess
import sys
import pytest


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'shared'))
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '42')
    repos = []
    for name in ('alpha', 'beta'):
        repo = tmp_path / name
        repo.mkdir()
        (repo / 'project.yaml').write_text(f'project: {{name: {name}}}\n')
        shared_bot.register_project(repo)
        repos.append(repo)
    return repos


def message(uid, text, reply=None, chat=42):
    return {'update_id': uid, 'message': {'chat': {'id': chat}, 'text': text,
                                        'reply_to_message': reply}}


def request(repo, ident, mid, **extra):
    path = repo / '.orchestrator' / 'questions' / f'{ident}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'id': ident, 'message_id': mid, 'status': 'pending',
                                'options': ['Approve', 'Reject'], **extra}))


def test_one_poller_routes_explicit_projects_and_rejects_ambiguity(projects):
    poll = getattr(shared_bot, 'poll_once', None)
    assert callable(poll), 'shared poller is absent'
    notices, offsets = [], []
    updates = [message(1, '/pause alpha'), message(2, '/priority [beta] 2'),
               message(3, '/halt'), message(4, '/pause [missing]'), message(5, '/pause beta', chat=99)]
    poll(lambda offset: offsets.append(offset) or updates, lambda text: notices.append(text) or True)
    assert [u['message']['text'] for u in shared_bot.local_updates(projects[0])] == ['/pause']
    assert [u['message']['text'] for u in shared_bot.local_updates(projects[1])] == ['/priority 2']
    assert len(notices) == 2 and all(n.startswith('[maestro]') for n in notices)
    assert 'alpha' in notices[0] and 'beta' in notices[0]
    poll(lambda offset: offsets.append(offset) or [], lambda text: True)
    assert offsets == [0, 6]


def test_callback_and_free_text_reply_use_exact_message_owner_with_duplicate_ids(projects):
    poll = getattr(shared_bot, 'poll_once', None)
    assert callable(poll), 'shared poller is absent'
    request(projects[0], 'same', 101)
    request(projects[1], 'same', 202)
    callback = {'update_id': 1, 'callback_query': {'id': 'cq', 'data': 'danreq:same:0',
        'message': {'message_id': 202, 'chat': {'id': 42}, 'text': '[beta] question',
                    'from': {'is_bot': True}}}}
    reply = {'message_id': 101, 'text': '[alpha] question', 'from': {'is_bot': True}}
    updates = [callback, message(2, 'my reason', reply), message(3, '/approve beta T', reply)]
    notices = []
    poll(lambda _: updates, lambda text: notices.append(text) or True)
    assert len(shared_bot.local_updates(projects[0])) == 1
    assert shared_bot.local_updates(projects[1])[0]['callback_query']['data'] == 'danreq:same:0'
    assert len(shared_bot.local_updates(projects[1])) == 1  # conflicting project refused
    assert notices and 'conflict' in notices[0].lower()


def test_redelivery_is_durable_and_only_settlement_removes_mailbox(projects):
    poll = getattr(shared_bot, 'poll_once', None)
    assert callable(poll), 'shared poller is absent'
    poll(lambda _: [message(9, '/pause alpha')], lambda _: True)
    poll(lambda _: [message(9, '/pause alpha')], lambda _: True)
    assert len(shared_bot.local_updates(projects[0])) == 1
    shared_bot.settle_update(projects[0], 9)
    assert shared_bot.local_updates(projects[0]) == []


def test_competing_poller_is_refused_before_fetching(projects):
    lock = getattr(shared_bot, 'poller_lock', None)
    assert callable(lock), 'shared poller has no ownership lock'
    with lock():
        with pytest.raises(RuntimeError, match='already'):
            with lock():
                pytest.fail('second consumer acquired the bot')


def test_project_controller_fetches_local_delivery_without_getupdates(projects, monkeypatch):
    from maestro.hitl import commands
    monkeypatch.setattr(commands, 'REPO', projects[0])
    monkeypatch.setattr(commands.subprocess, 'run',
                        lambda *a, **kw: pytest.fail('controller attempted network polling'))
    assert commands._fetch_updates(0, 0) == []


def test_unthreaded_free_text_with_several_pending_requests_is_not_guessed(projects):
    request(projects[0], 'one', 101)
    request(projects[0], 'two', 102)
    with pytest.raises(shared_bot.AddressError, match='reply'):
        shared_bot.address_update(message(1, 'yes'), shared_bot.registered_projects())


def test_unthreaded_free_text_ignores_questions_that_hold_nothing(projects):
    """GDT-12 review M4b: an unanswered improvement question (`holds: False`) is no
    candidate for unthreaded text: it neither makes the text ambiguous nor picks its project.
    A reply to its exact message still reaches it."""
    request(projects[0], 'dq-improve-IP-0001', 101, holds=False)
    request(projects[1], 'real', 202)
    found = shared_bot.registered_projects()
    target, _ = shared_bot.address_update(message(1, 'yes'), found)
    assert target['repo'] == projects[1]
    (projects[1] / '.orchestrator' / 'questions' / 'real.json').unlink()
    with pytest.raises(shared_bot.AddressError, match='Select a project'):
        shared_bot.address_update(message(2, 'yes'), found)
    reply = {'message_id': 101, 'text': '[alpha] question', 'from': {'is_bot': True}}
    target, _ = shared_bot.address_update(message(3, 'not now', reply), found)
    assert target['repo'] == projects[0]


def test_reply_to_an_answered_request_cannot_answer_a_different_pending_one(tmp_path, monkeypatch):
    questions = tmp_path / 'questions'
    questions.mkdir()
    (questions / 'pending.json').write_text(json.dumps({'id': 'pending', 'status': 'pending', 'message_id': 102}))
    monkeypatch.setattr(telegram, 'QUESTIONS_DIR', questions)
    monkeypatch.setattr(telegram, 'append_journal', lambda *a: None)
    monkeypatch.setattr(telegram, 'notify_telegram', lambda *a: None)
    monkeypatch.setattr(telegram, '_close_request_message', lambda *a: None)
    telegram._route_freetext_answer('yes', {'message_id': 101})
    assert not (questions / 'pending.answer').exists()


def test_shared_credentials_win_over_project_credentials(tmp_path, monkeypatch):
    shared = tmp_path / 'shared.env'
    shared.write_text('TELEGRAM_BOT_TOKEN=shared\nTELEGRAM_ALERT_CHAT_ID=42\n')
    repo = tmp_path / 'itv'
    repo.mkdir()
    (repo / '.env').write_text('TELEGRAM_BOT_TOKEN=project\nTELEGRAM_ALERT_CHAT_ID=99\nAPP_KEY=app\n')
    monkeypatch.setenv('MAESTRO_TELEGRAM_ENV', str(shared))
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('MAESTRO_REPO', str(repo))
    cli._load_project_env(repo)
    import os
    assert os.environ['TELEGRAM_BOT_TOKEN'] == 'shared'
    assert os.environ['TELEGRAM_ALERT_CHAT_ID'] == '42'
    assert os.environ['APP_KEY'] == 'app'


def test_notification_starts_with_project_tag(tmp_path, monkeypatch):
    (tmp_path / 'project.yaml').write_text('project: {name: itv}\n')
    monkeypatch.setattr(telegram, 'REPO', tmp_path)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake')
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '42')
    posted = []
    class Response:
        ok = True
        def json(self):
            return {'ok': True}
    monkeypatch.setattr(telegram.requests, 'post',
                        lambda url, **kw: posted.append(kw['data']) or Response())
    assert telegram.notify_intake('hello')
    assert posted[0]['text'] == '[itv] hello'


@pytest.mark.parametrize('source', [
    {'id': 7, 'is_bot': False}, {'id': 8, 'is_bot': True}, {},
])
def test_forged_or_old_bot_reply_tags_cannot_select_a_project(projects, monkeypatch, source):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '7:fake')
    reply = {'message_id': 101, 'text': '[alpha] status', 'from': source}
    with pytest.raises(shared_bot.AddressError, match='shared bot'):
        shared_bot.address_update(message(1, '/pause', reply), shared_bot.registered_projects())


def test_shared_bot_reply_context_can_select_a_project(projects, monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '7:fake')
    reply = {'message_id': 101, 'text': '[alpha] status', 'from': {'id': 7, 'is_bot': True}}
    target, routed = shared_bot.address_update(message(1, '/pause', reply), shared_bot.registered_projects())
    assert target['repo'] == projects[0] and routed['message']['text'] == '/pause'


def test_a_failed_listener_command_does_not_strand_later_mailbox_deliveries(projects, monkeypatch):
    from maestro.hitl import commands
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake')
    monkeypatch.setattr(commands, 'REPO', projects[0])
    monkeypatch.setattr(commands, '_getUpdates_offset', 0)
    monkeypatch.setattr(commands, '_LISTENER_QUEUE', queue.Queue())
    monkeypatch.setattr(commands, '_ingest_control_command', lambda *a: None)
    seen = []
    def dispatch(text, *args):
        seen.append(text)
        if text == '/pause':
            raise RuntimeError('handler failed')
    monkeypatch.setattr(commands, '_dispatch_control_text', dispatch)
    shared_bot.poll_once(lambda _: [message(1, '/pause alpha'), message(2, '/status alpha')], lambda _: True)
    commands._listen_once(commands._fetch_updates, commands._LISTENER_QUEUE)
    commands.poll_control_commands([])
    assert seen == ['/pause', '/status']
    assert [u['update_id'] for u in shared_bot.local_updates(projects[0])] == [1]
    assert commands._LISTENER_QUEUE.qsize() == 1


def test_delivery_and_offset_survive_a_crash_between_the_two_writes(projects, monkeypatch):
    original = shared_bot._atomic_json
    def write(path, value):
        if path.name == 'offset.json':
            raise OSError('disk unavailable')
        original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(shared_bot, '_atomic_json', write)
        with pytest.raises(OSError):
            shared_bot.poll_once(lambda _: [message(9, '/pause alpha')], lambda _: True)
    offsets = []
    shared_bot.poll_once(lambda offset: offsets.append(offset) or [message(9, '/pause alpha')], lambda _: True)
    assert offsets == [0]
    assert len(shared_bot.local_updates(projects[0])) == 1
    assert json.loads((shared_bot.shared_dir() / 'offset.json').read_text()) == 10


def test_failed_mailbox_write_does_not_acknowledge_the_network_update(projects, monkeypatch):
    def write(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(shared_bot, '_atomic_json', write)
    with pytest.raises(OSError):
        shared_bot.poll_once(lambda _: [message(9, '/pause alpha')], lambda _: True)
    assert not (shared_bot.shared_dir() / 'offset.json').exists()


@pytest.mark.parametrize('update', [
    {'update_id': 1, 'message': {'chat': {'id': 42}, 'text': ['bad']}},
    {'update_id': 1, 'callback_query': {'data': ['bad'], 'message': {
        'chat': {'id': 42}, 'from': {'is_bot': True}}}},
])
def test_malformed_update_is_refused_without_blocking_later_delivery(projects, update):
    notices = []
    shared_bot.poll_once(lambda _: [update, message(2, '/pause beta')],
                         lambda text: notices.append(text) or True)
    assert len(notices) == 1 and 'malformed' in notices[0].lower()
    assert [u['update_id'] for u in shared_bot.local_updates(projects[1])] == [2]


def test_shared_file_wins_in_fresh_subprocess_sends(tmp_path, monkeypatch):
    import os
    shared = tmp_path / 'shared.env'
    shared.write_text('TELEGRAM_BOT_TOKEN=7:shared\nTELEGRAM_ALERT_CHAT_ID=42\n')
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'project.yaml').write_text('project: {name: alpha}\n')
    (repo / '.env').write_text('TELEGRAM_BOT_TOKEN=8:stale\nTELEGRAM_ALERT_CHAT_ID=99\n')
    env = {**os.environ, 'MAESTRO_HOME': str(tmp_path / 'home'), 'MAESTRO_REPO': str(repo),
           'MAESTRO_TELEGRAM_ENV': str(shared), 'TELEGRAM_BOT_TOKEN': '8:stale',
           'TELEGRAM_ALERT_CHAT_ID': '99'}
    probe = '''
from maestro.hitl import telegram, dan_request
class Response:
    ok = True
    text = '{"ok": true, "result": {"message_id": 101}}'
    def json(self): return {'ok': True, 'result': {'message_id': 101}}
sent = []
def post(url, **kwargs):
    assert url == 'https://api.telegram.org/bot7:shared/sendMessage'
    assert kwargs['data']['chat_id'] == '42'
    assert kwargs['data']['text'].startswith('[alpha] ')
    sent.append(True)
    return Response()
telegram.requests.post = post
assert telegram.notify_intake('hello')
assert dan_request._send_telegram('8:stale', '99', 'question') == 101
assert len(sent) == 2
'''
    completed = subprocess.run([sys.executable, '-c', probe], env=env, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
