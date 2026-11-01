"""Dan's Telegram feedback from the DuetFlow pilot (task 03's V-DAN, 2026-09-26).

- **Latency.** A button tap is acknowledged, and its keyboard removed, the moment the update
  arrives — not when the controller's next poll gets round to it. A listener thread reads
  the shared poller's local mailbox and does the Telegram side at once; the answer is recorded (journal, control
  store) by the controller thread, which alone may write the store.
- **Feedback.** Once answered, the original request message is edited: its text plus
  "✅ Approved" or "❌ Rejected — <reason>", buttons gone — whether the answer came by button,
  by a reply, or by `/verify`.

Nothing here reaches the network: `_tg_api` is replaced by a recorder.
"""
from __future__ import annotations

import json
import queue

import pytest

from maestro.hitl import commands, telegram, verify

SHA = "0123456789abcdef0123456789abcdef01234567"
CHECKS = [{"id": "V-DAN", "check": "the playlist is registered", "instructions": "",
           "steps": [{"do": "Register it", "run": "app use 3"}]}]


class _Bot:
    def __init__(self):
        self.sent = []

    def __call__(self, text, markup=None):
        self.sent.append((text, markup))
        return 700 + len(self.sent)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "QUESTIONS_DIR", tmp_path / "questions")
    journal: list = []
    monkeypatch.setattr(telegram, "append_journal", lambda e, d, *a, **k: journal.append((e, d)))
    monkeypatch.setattr(telegram, "notify_telegram", lambda *a, **k: None)
    api: list = []
    monkeypatch.setattr(telegram, "_tg_api", lambda method, params: api.append((method, params)))
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "4242")
    state: dict = {}
    bot = _Bot()
    chan = verify.DanVerificationChannel(
        send=bot, configured=lambda: True, journal=lambda *a: None,
        read_state=lambda: json.loads(json.dumps(state)),
        write_state=lambda new: (state.clear(), state.update(new)),
    )
    chan.poll(task_id="T", title="the task", run_id="run_1", snapshot=SHA,
              worktree="/w", checks=CHECKS)
    return type("W", (), {"api": api, "journal": journal, "state": state, "chan": chan,
                          "bot": bot, "req": verify.request_id("T", SHA)})


def _cq(req, index, message_id=701):
    return {"id": "cq9", "data": f"danreq:{req}:{index}",
            "message": {"message_id": message_id, "chat": {"id": 4242}}}


def _edits(api):
    return [p for m, p in api if m == "editMessageText"]


def test_a_tap_acknowledges_then_edits_the_request_with_the_outcome(world):
    telegram._handle_danreq_callback(_cq(world.req, 0))

    assert [m for m, _ in world.api] == ["answerCallbackQuery", "editMessageText"]
    [edit] = _edits(world.api)
    question = world.bot.sent[0][0]
    assert edit["text"] == question + "\n\n<b>✅ Approved</b>"
    assert edit["parse_mode"] == "HTML"
    assert edit["message_id"] == 701 and edit["chat_id"] == 4242
    assert json.loads(edit["reply_markup"]) == {"inline_keyboard": []}


def test_a_bare_reject_says_a_reason_is_awaited_and_the_reply_completes_it(world):
    telegram._handle_danreq_callback(_cq(world.req, 1))
    assert _edits(world.api)[-1]["text"].endswith("<b>❌ Rejected</b> — reply with the reason")

    world.chan.poll(task_id="T", title="the task", run_id="run_1", snapshot=SHA,
                    worktree="/w", checks=CHECKS)  # sends the reason request
    telegram._route_freetext_answer("wrong <playlist>", None)

    edit = _edits(world.api)[-1]
    assert edit["message_id"] == 701 and edit["chat_id"] == "4242"
    assert edit["text"].endswith("<b>❌ Rejected</b> — wrong &lt;playlist&gt;")


@pytest.mark.parametrize("command, outcome", [
    ("T approve", "<b>✅ Approved</b>"),
    ("T reject it picked the wrong one", "<b>❌ Rejected</b> — it picked the wrong one"),
])
def test_a_verify_command_edits_the_request_too(world, command, outcome):
    verify.handle_verify_command(command, state=world.state)

    [edit] = _edits(world.api)
    assert edit["message_id"] == 701
    assert edit["text"] == world.bot.sent[0][0] + "\n\n" + outcome
    assert json.loads(edit["reply_markup"]) == {"inline_keyboard": []}


def test_a_manual_step_answer_is_shown_too(world):
    world.chan.poll_action(task_id="M", title="", run_id="run_2", attempt_id="att",
                           action="plug it in", worktree="/w")
    req = verify.action_request_id("M", "att")
    telegram._handle_danreq_callback(_cq(req, 0, message_id=702))

    assert _edits(world.api)[-1]["text"].endswith("<b>✅ Done</b>")


def test_the_listener_acknowledges_at_once_and_the_controller_records_later(world):
    # The listener thread: Telegram side only, plus the answer file (no journal, no store).
    assert telegram.ack_danreq_callback(_cq(world.req, 0)) is True
    assert [m for m, _ in world.api] == ["answerCallbackQuery", "editMessageText"]
    assert world.journal == []
    assert verify._answer(world.req) == "Approve"

    # The controller thread records it without talking to Telegram again.
    telegram._handle_danreq_callback({**_cq(world.req, 0), "_acked": True})
    assert len(world.api) == 2
    assert ("danreq_answered", f"{world.req}: Approve") in world.journal


def test_the_listener_answers_an_unknown_request_and_hands_nothing_on(world):
    assert telegram.ack_danreq_callback(_cq("verify-ghost", 0)) is False
    assert world.api == [("answerCallbackQuery", {"callback_query_id": "cq9",
                                                  "text": "Request not found"})]


def test_poll_control_commands_drains_the_listener_queue(world, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(commands, "_LISTENER_QUEUE", q)
    fetched = []
    monkeypatch.setattr(commands, "_fetch_updates", lambda *a, **k: fetched.append(1) or [])
    handled = []
    monkeypatch.setattr(commands, "_handle_danreq_callback", lambda cq, **k: handled.append(cq))
    q.put({"update_id": 5, "callback_query": {**_cq(world.req, 0), "_acked": True}})

    commands.poll_control_commands([])

    assert fetched == []  # the local listener already queued this delivery
    assert handled and handled[0]["_acked"] is True


def test_the_listener_routes_taps_at_once_and_queues_everything_else(world, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "4242")
    q: queue.Queue = queue.Queue()
    updates = [
        {"update_id": 1, "callback_query": _cq(world.req, 0)},
        {"update_id": 2, "message": {"chat": {"id": 4242}, "text": "/status"}},
        {"update_id": 3, "callback_query": {**_cq(world.req, 0),
                                            "message": {"chat": {"id": 1}}}},  # foreign chat
    ]
    commands._listen_once(lambda offset: updates, q)

    items = []
    while not q.empty():
        items.append(q.get())
    assert [u["update_id"] for u in items] == [1, 2]
    assert items[0]["callback_query"]["_acked"] is True
    assert [m for m, _ in world.api] == ["answerCallbackQuery", "editMessageText"]


def test_the_listener_is_not_started_when_disabled_or_unconfigured(monkeypatch):
    monkeypatch.setattr(commands, "_LISTENER_THREAD", None)
    monkeypatch.setattr(commands, "_LISTENER_QUEUE", None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "1")
    assert commands.start_update_listener() is False  # the suite sets MAESTRO_TELEGRAM_LISTENER=0
    monkeypatch.delenv("MAESTRO_TELEGRAM_LISTENER")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID")
    assert commands.start_update_listener() is False
    assert commands._LISTENER_QUEUE is None


# ── the dependency map: rendered from today's ROADMAP, resent, and pinned ─────────────
# The DuetFlow loop printed "render_dependency_map: no mermaid block found" every refresh:
# the generator still demanded the legacy fields `short_desc`, `est_time` and
# `eval_relevance`, refused a ROADMAP in the current format, and so no map was ever
# written, rendered or sent — Dan got the "Roadmap updated" text without a picture.

CURRENT_FORMAT_ROADMAP = """# Roadmap

```yaml
id: 01-auth
mode: needs-dan
deps: []
title: "Auth: tokens for both accounts"
verifications:
  - {id: V1, kind: auto, cmd: "true"}
```

```yaml
id: 02-reader
mode: autonomous
deps: [01-auth]
title: "Reader: paginate the pool"
```
"""


def test_the_map_is_generated_from_a_current_format_roadmap(tmp_path, monkeypatch):
    from maestro.docs import depmap

    for name, rel in (("ROADMAP", "docs/ROADMAP.md"), ("DEP_MAP", "docs/dependency_map.md"),
                      ("STATE_JSON", "state.json"), ("COMPLETED_TASKS", "completed.json")):
        monkeypatch.setattr(depmap, name, tmp_path / rel)
    monkeypatch.setattr(depmap, "REPO_ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    depmap.ROADMAP.write_text(CURRENT_FORMAT_ROADMAP, encoding="utf-8")

    rc, _out = depmap.run_cli([])

    assert rc == 0
    body = depmap._extract_first_mermaid_block(depmap.DEP_MAP.read_text(encoding="utf-8"))
    assert "01-auth --> 02-reader" in body
    # No "?" placeholders for the legacy fields a current ROADMAP does not have.
    assert '02-reader["02-reader — Reader: paginate the pool<br/>autonomous"]' in body


@pytest.fixture
def map_world(tmp_path, monkeypatch):
    png = tmp_path / "docs" / "dependency_map.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(telegram, "DEP_MAP_PNG", png)
    monkeypatch.setattr(telegram, "DEP_MAP_MD", tmp_path / "docs" / "dependency_map.md")
    monkeypatch.setattr(telegram, "LAST_MAP_SIG", tmp_path / "last_map_roadmap.sha")
    monkeypatch.setattr(telegram, "PINNED_MAP", tmp_path / "pinned_map.json")
    monkeypatch.setattr(telegram, "_roadmap_signature", lambda: "sig")
    monkeypatch.setattr(telegram, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "append_journal", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "4242")
    api: list = []
    monkeypatch.setattr(telegram, "_tg_api", lambda method, params: api.append((method, params)))
    uploads: list = []
    ids = iter([55, 56])

    def run(argv, **kwargs):
        uploads.append(argv[-1].rsplit("/", 1)[-1])
        stdout = json.dumps({"ok": True, "result": {"message_id": next(ids)}}, separators=(",", ":"))
        return type("R", (), {"stdout": stdout, "returncode": 0})()

    monkeypatch.setattr(telegram.subprocess, "run", run)
    return type("M", (), {"api": api, "uploads": uploads, "png": png, "tmp": tmp_path})


def test_each_sent_map_is_pinned_and_the_previous_one_unpinned(map_world):
    telegram.notify_telegram_with_map("🗺 first")
    assert map_world.uploads == ["sendPhoto"]
    assert map_world.api == [("pinChatMessage", {"chat_id": "4242", "message_id": 55,
                                                 "disable_notification": "true"})]
    assert json.loads(telegram.PINNED_MAP.read_text())["message_id"] == 55

    telegram.notify_telegram_with_map("🗺 second")
    assert map_world.api[1:] == [
        ("pinChatMessage", {"chat_id": "4242", "message_id": 56, "disable_notification": "true"}),
        ("unpinChatMessage", {"chat_id": "4242", "message_id": 55}),
    ]
    assert json.loads(telegram.PINNED_MAP.read_text())["message_id"] == 56


def test_a_png_older_than_its_map_is_not_sent_as_current(map_world):
    import os

    telegram.DEP_MAP_MD.write_text("# map", encoding="utf-8")
    old = telegram.DEP_MAP_MD.stat().st_mtime - 60
    os.utime(map_world.png, (old, old))

    telegram.notify_telegram_with_map("🗺 update")

    assert map_world.uploads == [] and map_world.api == []


def test_run_dep_map_journals_a_render_failure_once_per_reason(tmp_path, monkeypatch):
    from maestro.docs import roadmap

    monkeypatch.setattr(roadmap, "ROADMAP_FILE", tmp_path / "ROADMAP.md")

    journal: list = []
    monkeypatch.setattr(roadmap, "append_journal", lambda e, d, *a, **k: journal.append((e, d)))
    monkeypatch.setattr(roadmap, "_call_main", lambda *a, **k: None)
    monkeypatch.setattr(roadmap, "_LAST_RENDER_FAILURE", None, raising=False)
    monkeypatch.setattr(roadmap.doc_depmap, "render_png",
                        lambda: (_ for _ in ()).throw(SystemExit(1)))

    roadmap.run_dep_map()
    assert journal == []  # no ROADMAP, no map expected

    roadmap.ROADMAP_FILE.write_text("# Roadmap\n", encoding="utf-8")
    roadmap.run_dep_map()
    roadmap.run_dep_map()

    assert [e for e, _ in journal] == ["dep_map_render_failed"]


def test_run_dep_map_journals_what_mmdc_said_when_it_fails(tmp_path, monkeypatch):
    """mmdc's stderr goes to the journal, not just the (truncated) command line."""
    from maestro.docs import roadmap

    mmdc = tmp_path / "mmdc"
    mmdc.write_text("#!/bin/sh\necho 'TimeoutError: Timed out after 30000 ms while waiting for the WS endpoint' >&2\n"
                    "exit 1\n", encoding="utf-8")
    mmdc.chmod(0o755)
    depmap_md = tmp_path / "dependency_map.md"
    depmap_md.write_text("```mermaid\nflowchart TD\n  a --> b\n```\n", encoding="utf-8")
    monkeypatch.setattr(roadmap.doc_depmap, "MMDC", mmdc)
    monkeypatch.setattr(roadmap.doc_depmap, "DEP_MAP", depmap_md)
    monkeypatch.setattr(roadmap.doc_depmap, "DEP_MAP_PNG", tmp_path / "dependency_map.png")
    monkeypatch.setattr(roadmap, "ROADMAP_FILE", tmp_path / "ROADMAP.md")
    roadmap.ROADMAP_FILE.write_text("# Roadmap\n", encoding="utf-8")
    journal: list = []
    monkeypatch.setattr(roadmap, "append_journal", lambda e, d, *a, **k: journal.append((e, d)))
    monkeypatch.setattr(roadmap, "_call_main", lambda *a, **k: None)
    monkeypatch.setattr(roadmap, "_LAST_RENDER_FAILURE", None, raising=False)

    roadmap.run_dep_map()

    [(event, detail)] = journal
    assert event == "dep_map_render_failed"
    assert "Timed out after 30000 ms" in detail and "rc=1" in detail
