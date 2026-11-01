"""Proposal notices go out through the host's send-to-me (Dan, 2026-10-01).

`maestro models consume` runs host-wide from modelctl's scan timer, where no project
`.env` (and so no Maestro Telegram credential) is loaded. The notice therefore uses the
same `send-to-me` modelctl already uses for its own detection notice. A fake `send-to-me`
on PATH stands in for the real one; nothing leaves the machine.
"""
import json
import os
import stat
import sys

from maestro import cli, model_commands
from maestro.model_policy import PolicyStore

from tests.test_model_detection import Outbox


def _fake_send_to_me(bin_dir, log, *, exit_code=0):
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / 'send-to-me'
    script.write_text(f'#!{sys.executable}\n'
                      'import json, sys\n'
                      f'open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
                      f'sys.exit({exit_code})\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def _sent(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _records(home):
    return [json.loads(p.read_text()) for p in sorted((home / 'proposals').glob('*.json'))]


def _consume(monkeypatch, home, owner, bin_dir):
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(home))
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin')
    monkeypatch.setattr(model_commands, 'ModelctlBridge', lambda: owner)
    return cli.main(['models', 'consume'])


def test_consume_sends_one_notice_with_the_review_command(tmp_path, monkeypatch, capsys):
    home, bin_dir, log = tmp_path / 'models', tmp_path / 'bin', tmp_path / 'sent.log'
    _fake_send_to_me(bin_dir, log)
    assert _consume(monkeypatch, home, Outbox(), bin_dir) == 0
    [record] = _records(home)
    assert record['notification_pending'] is False
    [argv] = _sent(log)
    assert argv[0] == '--text'
    text = argv[1]
    assert 'codex:gpt-6.2-sol' in text
    assert 'codex:gpt-6.1-sol' in text
    assert f"maestro models review --proposal {record['proposal_id']}" in text
    assert json.loads(capsys.readouterr().out)['notification_pending'] == 0
    # Delivered once: a later consume sends nothing more.
    assert _consume(monkeypatch, home, Outbox(), bin_dir) == 0
    assert len(_sent(log)) == 1


def test_failed_delivery_stays_pending_and_retries_without_recreating(tmp_path, monkeypatch, capsys):
    home, bin_dir, log = tmp_path / 'models', tmp_path / 'bin', tmp_path / 'sent.log'
    _fake_send_to_me(bin_dir, log, exit_code=3)
    owner = Outbox()
    assert _consume(monkeypatch, home, owner, bin_dir) == 0
    [before] = _records(home)
    assert before['notification_pending'] is True
    assert len(_sent(log)) == 1
    assert json.loads(capsys.readouterr().out)['notification_pending'] == 1

    _fake_send_to_me(bin_dir, log)
    assert _consume(monkeypatch, home, owner, bin_dir) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['proposals_created'] == 0
    assert out['notification_pending'] == 0
    [after] = _records(home)
    assert after == dict(before, notification_pending=False)
    assert len(_sent(log)) == 2


def test_missing_send_to_me_keeps_the_notice_pending(tmp_path, monkeypatch, capsys):
    home, bin_dir = tmp_path / 'models', tmp_path / 'empty-bin'
    bin_dir.mkdir()
    assert _consume(monkeypatch, home, Outbox(), bin_dir) == 0
    [record] = _records(home)
    assert record['notification_pending'] is True
    assert record['status'] == 'pending_approval'
    assert json.loads(capsys.readouterr().out)['notification_pending'] == 1


def test_a_proposal_decided_before_delivery_is_not_announced(tmp_path, monkeypatch, capsys):
    from maestro.model_detection import consume
    home, bin_dir, log = tmp_path / 'models', tmp_path / 'bin', tmp_path / 'sent.log'
    store = PolicyStore(home)
    consume(store, Outbox())
    [record] = _records(home)
    record['status'] = 'rejected'
    (home / 'proposals' / (record['proposal_id'] + '.json')).write_text(json.dumps(record))

    _fake_send_to_me(bin_dir, log)
    assert _consume(monkeypatch, home, Outbox(), bin_dir) == 0
    assert _sent(log) == []
    [after] = _records(home)
    assert after['status'] == 'rejected'
    assert after['notification_pending'] is False
    assert after['notification'] == 'not sent: decided before delivery'
