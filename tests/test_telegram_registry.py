from maestro import cli
from maestro.hitl import commands


def test_botfather_export_and_help_are_the_same_registered_handlers(capsys):
    assert cli.main(['telegram', 'commands', '--botfather']) == 0
    lines = capsys.readouterr().out.splitlines()
    registry = getattr(commands, 'COMMANDS', {})
    assert registry, 'dispatch has no command registry'
    assert lines == [f'{c.name} - {c.description}' for c in registry.values()]
    assert len(lines) == len(set(lines))
    assert 'priority' in registry
    for command in registry.values():
        assert callable(command.handler)
        assert f'/{command.name}' in commands.HELP_TEXT


def test_priority_is_dispatched_from_the_registered_handler(monkeypatch, tmp_path):
    import queue
    from maestro import config, state
    monkeypatch.setattr(state, 'JOURNAL', tmp_path / 'journal.ndjson')
    q = queue.Queue()
    q.put({'update_id': 77, 'message': {'chat': {'id': 42}, 'text': '/priority 2.5'}})
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake')
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '42')
    monkeypatch.setattr(commands, '_LISTENER_QUEUE', q)
    monkeypatch.setattr(commands, '_ingest_control_command', lambda *a: None)
    recorded, sent = [], []
    monkeypatch.setattr(config, 'set_project_priority', lambda repo, weight: recorded.append(weight) or weight)
    monkeypatch.setattr(commands, 'notify_telegram', sent.append)
    commands.poll_control_commands([])
    assert recorded == [2.5]
    assert '2.5' in sent[0]
