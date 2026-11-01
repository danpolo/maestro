"""Telegram is answered while the loop is not running, and `/help` is answered at once.

Dan sent `/help` to a halted DuetFlow loop and got nothing: only the loop reads Telegram,
and a HALT stops the loop *and* the watchdog. Now the watchdog stays up while the HALT
sentinel is present and a bot is configured, answering `/help` and `/status` itself and
telling Dan every other command is queued. A queued command is left unconsumed in
the local mailbox and recorded in the command inbox as
`received`, so the restarted loop fetches it again and runs it as usual.

Nothing here reaches the network: fetches are lists and replies are recorded.
"""
from __future__ import annotations

import fcntl
import queue

import pytest

from maestro import state as state_module
from maestro import watchdog
from maestro.control.store import ControlStore
from maestro.hitl import commands

CHAT = "4242"


def _msg(uid, text, chat=CHAT):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


@pytest.fixture
def halted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", CHAT)
    monkeypatch.setattr(commands, "_HALTED_OFFSET", 0)
    monkeypatch.setattr(commands, "_HALTED_REPLIED", set())
    sent: list[str] = []
    monkeypatch.setattr(commands, "notify_telegram", sent.append)
    enqueued: list[tuple] = []
    monkeypatch.setattr(commands, "enqueue_external_command",
                        lambda kind, payload, **k: enqueued.append((kind, payload, k)))
    monkeypatch.setattr(commands, "read_state",
                        lambda: {"phase": {"id": "P1"}, "in_flight": [], "parked_tasks": ["T9"]})
    return type("H", (), {"sent": sent, "enqueued": enqueued})


# ── the loop's own path: one help sheet, and the listener answers it at once ──────────


def test_the_loop_and_the_halted_path_send_the_same_help_sheet(halted, monkeypatch):
    monkeypatch.setattr(commands, "_LISTENER_QUEUE", queue.Queue())
    monkeypatch.setattr(commands, "_ingest_control_command", lambda *a: None)
    commands._LISTENER_QUEUE.put(_msg(1, "/help"))
    commands.poll_control_commands([])
    commands.answer_while_halted(lambda offset: [_msg(2, "/help")])

    loop_sheet, halted_reply = halted.sent
    assert loop_sheet == commands.HELP_TEXT
    header, _, rest = halted_reply.partition("\n\n")
    assert "halted" in header and "queued" in header and "restarts" in header
    assert rest == commands.HELP_TEXT


def test_the_listener_answers_help_on_arrival_and_the_loop_does_not_repeat_it(
    halted, monkeypatch
):
    q: queue.Queue = queue.Queue()
    commands._listen_once(lambda offset: [_msg(7, "/help"), _msg(8, "/status")], q)
    assert halted.sent == [commands.HELP_TEXT]   # /status waits for the loop

    ingested = []
    monkeypatch.setattr(commands, "_LISTENER_QUEUE", q)
    monkeypatch.setattr(commands, "_ingest_control_command",
                        lambda uid, text: ingested.append(uid))
    commands.poll_control_commands([])
    assert ingested == [7, 8]                     # still recorded in the inbox
    assert len(halted.sent) == 2 and halted.sent[1].startswith("🤖 Orchestrator status")


# ── while halted: answer /help and /status, queue the rest, lose nothing ─────────────


def test_help_and_status_are_answered_and_everything_else_is_queued(halted):
    updates = [_msg(1, "/help"), _msg(2, "/status"), _msg(3, "/approve T1"),
               _msg(4, "/help"), {"update_id": 5, "callback_query": {"id": "cq"}}]
    commands.answer_while_halted(lambda offset: updates)

    help1, status, queued, help2 = halted.sent
    assert help1.endswith(commands.HELP_TEXT) and help2 == help1
    assert status.splitlines()[0].startswith("⏸") and "Parked: T9" in status
    assert "/approve T1" in queued and "restarts" in queued
    # Consumed up to the first command only the loop can run; it and all after it stay
    # in the durable mailbox for the restarted loop.
    assert commands._HALTED_OFFSET == 3
    assert halted.enqueued == [("telegram_control", {"update_id": 3, "text": "/approve T1"},
                                {"idempotency_key": "telegram:update:3",
                                 "repo": commands.REPO})]


def test_each_answer_given_while_halted_is_journaled_once(halted, monkeypatch):
    journal: list = []
    monkeypatch.setattr(commands, "append_journal", lambda e, d, *a, **k: journal.append((e, d)))
    updates = [_msg(1, "/help"), _msg(2, "/status")]
    commands.answer_while_halted(lambda offset: updates)
    commands.answer_while_halted(lambda offset: updates)   # a refetch is not re-answered

    assert journal == [("halted_command_answered", "/help"),
                       ("halted_command_answered", "/status")]


def test_a_refetched_update_is_not_answered_twice(halted):
    updates = [_msg(3, "/pause"), _msg(4, "/help")]
    offsets = []

    def fetch(offset):
        offsets.append(offset)
        return updates

    commands.answer_while_halted(fetch)
    commands.answer_while_halted(fetch)
    assert offsets == [0, 0]
    assert len(halted.sent) == 2 and len(halted.enqueued) == 1


def test_other_chats_and_empty_updates_are_consumed_silently(halted):
    commands.answer_while_halted(lambda offset: [_msg(1, "/help", chat="1"),
                                                 {"update_id": 2, "edited_message": {}}])
    assert halted.sent == [] and halted.enqueued == []
    assert commands._HALTED_OFFSET == 3


def test_a_queued_command_is_run_by_the_loop_when_it_restarts(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    orch = repo / ".orchestrator"
    orch.mkdir(parents=True)
    monkeypatch.setattr(state_module, "STATE_JSON", orch / "state.json")
    monkeypatch.setattr(state_module, "JOURNAL", orch / "journal.ndjson")
    monkeypatch.setattr(state_module, "CONTROL_DB", orch / "control.sqlite3")
    state_module._reset_control_store()
    ControlStore.open_controller(orch / "control.sqlite3").close()
    state_module._reset_control_store()
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", CHAT)
    monkeypatch.setattr(commands, "REPO", repo)
    monkeypatch.setattr(commands, "_HALTED_OFFSET", 0)
    monkeypatch.setattr(commands, "_HALTED_REPLIED", set())
    monkeypatch.setattr(commands, "notify_telegram", lambda *a: None)
    try:
        commands.answer_while_halted(lambda offset: [_msg(11, "/pause")])
        # The restarted loop (the controller) fetches update 11 again and runs it.
        command_id = commands._ingest_control_command(11, "/pause")
        assert command_id not in (None, commands._ALREADY_PROCESSED)
    finally:
        state_module._reset_control_store()


# ── the watchdog: stays up through a HALT to answer, and only while the loop is down ──


@pytest.fixture
def wd(monkeypatch, tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    monkeypatch.setattr(watchdog, "REPO", tmp_path)
    monkeypatch.setattr(watchdog, "HALT_FILE", orch / "HALT")
    monkeypatch.setattr(watchdog, "JOURNAL", orch / "journal.ndjson")
    monkeypatch.setattr(watchdog, "STATE_JSON", orch / "state.json")
    monkeypatch.setattr(watchdog, "ORCHESTRATOR_LOCK", orch / "orchestrator.lock")
    monkeypatch.setattr(watchdog, "CONTROL_DB", orch / "control.sqlite3")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", CHAT)
    monkeypatch.setenv("MAESTRO_TELEGRAM_LISTENER", "1")
    journal: list[str] = []
    monkeypatch.setattr(watchdog, "append_journal", lambda e, d: journal.append(e))
    return type("W", (), {"halt": orch / "HALT", "lock": orch / "orchestrator.lock",
                          "journal": journal})


def test_the_loop_counts_as_running_while_its_lock_is_held(wd):
    assert watchdog.loop_running() is False
    with open(wd.lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert watchdog.loop_running() is True
    assert watchdog.loop_running() is False


def test_halted_telegram_is_off_without_a_bot_or_with_the_listener_disabled(wd, monkeypatch):
    assert watchdog.halted_telegram_enabled() is True
    monkeypatch.setenv("MAESTRO_TELEGRAM_LISTENER", "0")
    assert watchdog.halted_telegram_enabled() is False
    monkeypatch.setenv("MAESTRO_TELEGRAM_LISTENER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert watchdog.halted_telegram_enabled() is False


def test_serving_answers_only_while_halted_and_the_loop_is_down(wd, monkeypatch):
    wd.halt.touch()
    running = iter([True, False, False])
    monkeypatch.setattr(watchdog, "loop_running", lambda: next(running))
    answered = []

    def answer():
        answered.append(1)
        if len(answered) == 2:
            wd.halt.unlink()

    monkeypatch.setattr(commands, "answer_while_halted", answer)
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    watchdog.serve_while_halted(wd.halt.exists)
    assert answered == [1, 1]   # skipped the round the loop was still up


class _Stop(BaseException):
    pass


def test_halted_mailbox_service_waits_between_empty_local_reads(wd, monkeypatch):
    wd.halt.touch()
    monkeypatch.setattr(watchdog, 'loop_running', lambda: False)
    events = []

    def answer():
        events.append('read')
        if events.count('read') == 2:
            wd.halt.unlink()

    monkeypatch.setattr(commands, 'answer_while_halted', answer)
    monkeypatch.setattr(watchdog.time, 'sleep', lambda seconds: events.append(seconds))
    watchdog.serve_while_halted(wd.halt.exists)
    assert events[0] == 'read'
    assert isinstance(events[1], (int, float)) and 0 < events[1] <= 1
    assert events[2] == 'read'


def test_a_halted_watchdog_answers_then_supervises_again_once_halt_is_removed(
    wd, monkeypatch
):
    wd.halt.touch()
    served = []
    monkeypatch.setattr(watchdog, "serve_while_halted",
                        lambda still: (served.append(still()), wd.halt.unlink()))
    monkeypatch.setattr(watchdog, "request_lease_reconciliation", lambda reason: False)
    monkeypatch.setattr(watchdog, "orchestrator_alive", lambda: False)
    monkeypatch.setattr(watchdog, "graph_run_live", lambda: False)

    def launch():
        raise _Stop

    monkeypatch.setattr(watchdog, "launch_orchestrator", launch)
    with pytest.raises(_Stop):
        watchdog.main()
    assert served == [True]
    assert wd.journal[:3] == ["watchdog_start", "watchdog_halt_respected",
                              "watchdog_halt_cleared"]


def test_without_a_bot_the_halted_watchdog_still_just_stops(wd, monkeypatch):
    wd.halt.touch()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setattr(watchdog, "request_lease_reconciliation", lambda reason: False)
    assert watchdog.main() == 0
    assert wd.journal == ["watchdog_start", "watchdog_halt_respected"]


def test_a_soft_halted_watchdog_answers_too_until_state_halted_clears(wd, monkeypatch):
    state = wd.halt.parent / "state.json"
    state.write_text('{"halted": true}')
    served = []
    monkeypatch.setattr(watchdog, "serve_while_halted",
                        lambda still: (served.append(still()), state.write_text("{}")))
    monkeypatch.setattr(watchdog, "request_lease_reconciliation", lambda reason: False)
    monkeypatch.setattr(watchdog, "orchestrator_alive", lambda: False)
    monkeypatch.setattr(watchdog, "graph_run_live", lambda: False)

    def launch():
        raise _Stop

    monkeypatch.setattr(watchdog, "launch_orchestrator", launch)
    with pytest.raises(_Stop):
        watchdog.main()
    assert served == [True]
    assert "watchdog_idle_halted" in wd.journal
