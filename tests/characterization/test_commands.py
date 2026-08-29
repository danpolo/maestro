"""Pinned behaviour of the Telegram control plane: the command router and its handlers.

Characterisation, not specification. `poll_control_commands` is the highest-complexity
function in the reference (cc=46) and it is wrapped end-to-end in a bare
``except Exception: pass``, so a large part of "what it does" is *what it silently stops
doing*. Those swallowed failures are pinned here as behaviour, not repaired.

**Nothing here reaches the network and nothing shells out.** The router fetches updates by
running ``curl`` through the module's ``subprocess`` reference; every test replaces that
reference with a recorder that answers the ``getUpdates`` call from a canned payload and
returns rc=0 for everything else (``tmux new-window``, ``git branch -D``,
``orchestrator_status.py``). Assertions are made on the argv that *would* have run.

Two conventions, both borrowed from the sibling characterisation modules:

* Path globals are read off the subject via ``_path`` with a sandbox-relative fallback, so
  the same test body works against the reference (one flat namespace) and against the
  extracted package (where ``STATE_JSON`` and friends live in a sibling module).
* ``prep_actions`` is a *separate* module in the reference, so the ``sandbox`` fixture does
  not rebase its path global. Every test that can reach it installs the ``prep`` fixture,
  which swaps the subject's reference for an in-memory store — otherwise the sidecar would
  be read out of (and, on ``/reject``, written into) a live repo.

Where the reference's wording is the operator-facing contract (journal event names, the
``getUpdates`` query string, the ``/approve`` + ``/ask`` + ``/reject`` affordance line) it
is pinned exactly; where it is incidental prose the assertion is on structure.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.maestro_module("hitl.commands")


# The reference's operator-facing strings are emoji-prefixed. Spelled out by name so the
# test source stays legible while still pinning the exact bytes Dan sees.
ROBOT = "\N{ROBOT FACE}"                        # /status header
CHART_UP = "\N{CHART WITH UPWARDS TREND}"       # /progress header
MAILBOX = "\N{OPEN MAILBOX WITH LOWERED FLAG}"  # "no tasks in flight"
PAUSE = "\N{DOUBLE VERTICAL BAR}"               # /pause confirmation
PLAY = "\N{BLACK RIGHT-POINTING TRIANGLE}"      # /resume confirmation
STOP = "\N{OCTAGONAL SIGN}"                     # /halt confirmation
CHECK = "\N{WHITE HEAVY CHECK MARK}"            # approvals / unpark / hitl
CROSS = "\N{CROSS MARK}"                        # rejections
WARN = "\N{WARNING SIGN}"                       # gate + launch failures
WRENCH = "\N{WRENCH}"                           # /fix + /redo confirmations
THINKING = "\N{THINKING FACE}"                  # /ask confirmation
# The reference emits this one with a trailing VS-16, so it renders as an emoji glyph.
TOOLS = "\N{HAMMER AND WRENCH}\N{VARIATION SELECTOR-16}"  # /manual "Dan-must-perform"
TARGET = "\N{DIRECT HIT}"                       # the single prepared Dan action
HOURGLASS = "\N{HOURGLASS WITH FLOWING SAND}"   # queued / in-prep / awaiting answers
NO_ENTRY = "\N{NO ENTRY}"                       # blocked by deps
INFO = "\N{INFORMATION SOURCE}"                 # informational replies
MAESTRO = "\N{MUSICAL SCORE}"                   # /help header


# --- locating the subject's paths ------------------------------------------------------


def _path(subject, name: str, fallback: Path) -> Path:
    """A path global off the subject, or `fallback` when the subject does not own it."""
    value = getattr(subject, name, None)
    return value if isinstance(value, Path) else fallback


def _state_json(subject, sandbox) -> Path:
    return _path(subject, "STATE_JSON", sandbox.orch_dir / "state.json")


def _journal(subject, sandbox) -> Path:
    return _path(subject, "JOURNAL", sandbox.orch_dir / "journal.ndjson")


def _halt_file(subject, sandbox) -> Path:
    return _path(subject, "HALT_FILE", sandbox.orch_dir / "HALT")


def _roadmap(subject, sandbox) -> Path:
    return _path(subject, "ROADMAP_FILE", sandbox.repo / "docs" / "ROADMAP.md")


def _completed(subject, sandbox) -> Path:
    return _path(subject, "COMPLETED_TASKS", sandbox.orch_dir / "completed_tasks.json")


def _questions_dir(subject, sandbox) -> Path:
    return _path(subject, "QUESTIONS_DIR", sandbox.orch_dir / "questions")


def _diagnoses_dir(subject, sandbox) -> Path:
    return _path(subject, "DIAGNOSES_DIR", sandbox.orch_dir / "diagnoses")


def _redo_dir(subject, sandbox) -> Path:
    return _path(subject, "REDO_DIR", sandbox.orch_dir / "redo")


def _workspaces(subject, sandbox) -> Path:
    return _path(subject, "WORKSPACES", sandbox.workspaces)


# --- fixture state on disk -------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago_iso(minutes: int) -> str:
    moment = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _put_state(subject, sandbox, **fields) -> Path:
    path = _state_json(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _get_state(subject, sandbox) -> dict:
    return json.loads(_state_json(subject, sandbox).read_text(encoding="utf-8"))


def _journal_events(subject, sandbox) -> list[dict]:
    path = _journal(subject, sandbox)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _put_roadmap(subject, sandbox, *blocks: str) -> Path:
    """A ROADMAP.md whose ```yaml fences carry `blocks`, in order."""
    path = _roadmap(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join("```yaml\n" + block.strip("\n") + "\n```\n\n" for block in blocks)
    path.write_text("# Roadmap\n\n" + body, encoding="utf-8")
    return path


def _put_completed(subject, sandbox, *task_ids: str) -> Path:
    path = _completed(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": t} for t in task_ids]), encoding="utf-8")
    return path


def _put_answer(subject, sandbox, req_id: str, choice: str) -> Path:
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / f"{req_id}.answer"
    path.write_text(json.dumps({"choice": choice}), encoding="utf-8")
    return path


# --- the control-plane harness ---------------------------------------------------------


CHAT = "4242"


def _update(text: str, uid: int = 100, chat: str = CHAT, key: str = "message", **extra) -> dict:
    message = {"chat": {"id": chat}, "text": text}
    message.update(extra)
    return {"update_id": uid, key: message}


def _fake_subprocess(monkeypatch, subject, stdout_for_curl):
    """Swap the subject's `subprocess` reference for a recorder. Scoped to the subject."""
    calls: list[tuple] = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "curl":
            return subprocess.CompletedProcess(list(cmd), 0, stdout_for_curl(), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fake = SimpleNamespace(
        run=run,
        calls=calls,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
    )
    monkeypatch.setattr(subject, "subprocess", fake)
    return fake


@pytest.fixture
def prep(subject, monkeypatch):
    """An in-memory stand-in for the `prepared_actions` sidecar module.

    The reference imports it as a sibling module whose `PREPARED_ACTIONS_FILE` global the
    `sandbox` fixture cannot see, so an unstubbed call would read — and on `/reject` write
    — a live repo's sidecar.
    """
    store: dict[str, dict] = {}
    namespace = SimpleNamespace(
        store=store,
        get_action=lambda task_id, path=None: store.get(task_id),
        has_action=lambda task_id, path=None: task_id in store,
        remove_action=lambda task_id, path=None: store.pop(task_id, None),
        set_action=lambda task_id, action, **kw: store.setdefault(task_id, {"action": action}),
    )
    monkeypatch.setattr(subject, "prep_actions", namespace)
    return namespace


@pytest.fixture
def control(subject, sandbox, monkeypatch):
    """Credentials, a zeroed getUpdates offset, a captured notifier and a fake subprocess."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", CHAT)
    monkeypatch.setattr(subject, "_getUpdates_offset", 0)

    sent: list[str] = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: sent.append(msg))

    box = SimpleNamespace(sent=sent, in_flight=[], stdout='{"ok": true, "result": []}')
    fake = _fake_subprocess(monkeypatch, subject, lambda: box.stdout)
    box.calls = fake.calls

    def deliver(*updates, raw=None):
        box.stdout = raw if raw is not None else json.dumps({"ok": True, "result": list(updates)})
        subject.poll_control_commands(box.in_flight)

    def curl_urls():
        return [
            args[0][-1]
            for args, _ in fake.calls
            if args and isinstance(args[0], (list, tuple)) and args[0][0] == "curl"
        ]

    box.deliver = deliver
    box.curl_urls = curl_urls
    box.offset = lambda: subject._getUpdates_offset
    return box


@pytest.fixture
def notifier(subject, monkeypatch):
    """Capture `notify_telegram` for the handlers called directly, without the router."""
    sent: list[str] = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: sent.append(msg))
    return sent


# =======================================================================================
# poll_control_commands — transport, envelope and dispatch
# =======================================================================================


def test_poll_does_nothing_at_all_without_a_bot_token(subject, sandbox, control, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    control.deliver(_update("/status"))
    assert control.calls == []
    assert control.sent == []


def test_poll_does_nothing_at_all_without_an_alert_chat_id(subject, sandbox, control, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    control.deliver(_update("/status"))
    assert control.calls == []
    assert control.sent == []


def test_poll_fetches_updates_with_a_bounded_non_blocking_curl(subject, sandbox, control):
    control.deliver()
    (args, kwargs), = control.calls
    assert args[0][:4] == ["curl", "-s", "--max-time", "5"]
    assert args[0][-1] == (
        "https://api.telegram.org/botT0KEN/getUpdates?offset=0&timeout=0&limit=10"
    )
    assert kwargs == {"capture_output": True, "text": True, "timeout": 10}


def test_poll_advances_the_getupdates_offset_past_the_last_update_seen(subject, sandbox, control):
    _put_state(subject, sandbox, phase={"id": "P1"})
    control.deliver(_update("/status", uid=100), _update("/status", uid=105))
    assert control.offset() == 106
    control.deliver()
    assert control.curl_urls()[-1].endswith("?offset=106&timeout=0&limit=10")


def test_poll_swallows_a_non_json_getupdates_body_and_leaves_the_offset_alone(
    subject, sandbox, control
):
    control.deliver(raw="<html>502 Bad Gateway</html>")
    assert control.sent == []
    assert control.offset() == 0


def test_poll_returns_early_when_telegram_reports_ok_false(subject, sandbox, control):
    control.deliver(raw=json.dumps({"ok": False, "description": "Unauthorized",
                                    "result": [_update("/status")]}))
    assert control.sent == []
    assert control.offset() == 0


def test_poll_ignores_messages_from_any_chat_other_than_the_alert_chat(subject, sandbox, control):
    _put_state(subject, sandbox, phase={"id": "P1"})
    control.deliver(_update("/status", chat="9999"))
    assert control.sent == []
    assert control.offset() == 101


def test_poll_reads_a_channel_post_when_there_is_no_message(subject, sandbox, control):
    _put_state(subject, sandbox, phase={"id": "P7"})
    control.deliver(_update("/status", key="channel_post"))
    assert control.sent and "Phase: P7" in control.sent[0]


def test_poll_routes_a_callback_query_from_the_alert_chat_to_the_danreq_handler(
    subject, sandbox, control, monkeypatch
):
    seen = []
    monkeypatch.setattr(subject, "_handle_danreq_callback", lambda cq: seen.append(cq))
    cq = {"id": "cb1", "data": "danreq:x:0", "message": {"chat": {"id": CHAT}}}
    control.deliver({"update_id": 100, "callback_query": cq})
    assert seen == [cq]


def test_poll_drops_a_callback_query_whose_message_chat_does_not_match(
    subject, sandbox, control, monkeypatch
):
    seen = []
    monkeypatch.setattr(subject, "_handle_danreq_callback", lambda cq: seen.append(cq))
    control.deliver({"update_id": 100,
                     "callback_query": {"id": "cb1", "message": {"chat": {"id": "9999"}}}})
    assert seen == []
    assert control.offset() == 101


def test_poll_drops_a_callback_query_that_carries_no_message_envelope(
    subject, sandbox, control, monkeypatch
):
    """`(cq.get("message") or {})` collapses to an empty chat id, which never matches."""
    seen = []
    monkeypatch.setattr(subject, "_handle_danreq_callback", lambda cq: seen.append(cq))
    control.deliver({"update_id": 100, "callback_query": {"id": "cb1", "data": "danreq:x:0"}})
    assert seen == []


def test_poll_treats_an_update_with_neither_message_nor_channel_post_as_a_foreign_chat(
    subject, sandbox, control
):
    control.deliver({"update_id": 100, "edited_message": {"chat": {"id": CHAT},
                                                          "text": "/status"}})
    assert control.sent == []
    assert control.offset() == 101


def test_poll_lowercases_the_command_before_dispatching(subject, sandbox, control):
    _put_state(subject, sandbox, phase={"id": "P3"})
    control.deliver(_update("  /STATUS  "))
    assert control.sent and control.sent[0].startswith(f"{ROBOT} Orchestrator status")


def test_poll_ignores_an_unknown_slash_command_without_replying(subject, sandbox, control):
    control.deliver(_update("/frobnicate now"))
    assert control.sent == []
    assert control.offset() == 101


def test_poll_ignores_a_bare_verb_that_the_router_only_accepts_with_an_argument(
    subject, sandbox, control
):
    """`/approve`, `/reject`, `/fix`, `/unpark` and `/hitl` are matched with a trailing
    space, so the bare verb falls through every branch and is answered with silence."""
    control.deliver(
        _update("/approve", uid=100), _update("/reject", uid=101),
        _update("/fix", uid=102), _update("/unpark", uid=103), _update("/hitl", uid=104),
    )
    assert control.sent == []


def test_poll_ignores_an_update_that_carries_no_text_at_all(subject, sandbox, control):
    control.deliver({"update_id": 100, "message": {"chat": {"id": CHAT},
                                                   "photo": [{"file_id": "x"}]}})
    assert control.sent == []


def test_poll_routes_any_non_command_text_to_the_freetext_answer_handler(
    subject, sandbox, control, monkeypatch
):
    routed = []
    monkeypatch.setattr(subject, "_route_freetext_answer",
                        lambda text, reply_to: routed.append((text, reply_to)))
    reply_to = {"message_id": 7}
    control.deliver(_update("Option B, and please retry", reply_to_message=reply_to))
    assert routed == [("Option B, and please retry", reply_to)]


def test_poll_hands_the_freetext_handler_the_original_casing_not_the_lowered_copy(
    subject, sandbox, control, monkeypatch
):
    routed = []
    monkeypatch.setattr(subject, "_route_freetext_answer",
                        lambda text, reply_to: routed.append(text))
    control.deliver(_update("Use the CSV Export"))
    assert routed == ["Use the CSV Export"]


def test_poll_swallows_a_handler_exception_and_abandons_every_later_update(
    subject, sandbox, control
):
    """There is no per-update try/except: `read_state` blowing up on a missing state.json
    kills the whole batch, and the offset is left past the *failing* update, so the
    unprocessed remainder is never re-delivered."""
    assert not _state_json(subject, sandbox).exists()
    control.deliver(_update("/pause", uid=100), _update("/halt", uid=101))
    assert control.sent == []
    assert control.offset() == 101
    assert not _halt_file(subject, sandbox).exists()


# =======================================================================================
# poll_control_commands — one test per command verb
# =======================================================================================


@pytest.mark.parametrize("verb", ["/help", "/commands", "/start"])
def test_help_commands_and_start_all_print_the_same_control_sheet(
    subject, sandbox, control, verb
):
    control.deliver(_update(verb))
    (sheet,) = control.sent
    assert sheet.startswith(f"{MAESTRO} Maestro controls")
    for documented in ("/status", "/progress", "/pause", "/resume", "/halt", "/hitl on|off",
                       "/waiting", "/approve", "/fix", "/ask", "/redo", "/reject",
                       "/unpark", "/manual", "/detail", "/help"):
        assert documented in sheet


@pytest.mark.parametrize("verb", ["/help", "/commands", "/start"])
def test_help_advertises_backend_in_the_extracted_package_and_not_in_the_reference(
    subject, sandbox, control, verb
):
    """The sheet is the only place a command is advertised, so `/backend` existing and
    `/backend` being documented are the same fact (M2 plan, Task 6 Step 6). The reference
    has no such verb and must not claim one."""
    control.deliver(_update(verb))
    (sheet,) = control.sent
    assert (
        "/backend <name> [task_id] — pick the agent backend for new launches, "
        "or move one in-flight task to it now (same worktree, uncommitted "
        "work kept)\n"
    ) in sheet


def test_status_reports_phase_in_flight_ids_and_the_halted_and_paused_flags(
    subject, sandbox, control
):
    _put_state(
        subject, sandbox,
        phase={"id": "P8"},
        in_flight=[{"task_id": "P8B1"}, {"task_id": "P8B2"}],
        halted=False, paused_by_user=True,
    )
    control.deliver(_update("/status"))
    (msg,) = control.sent
    assert msg == (
        f"{ROBOT} Orchestrator status\n"
        "Phase: P8 | In-flight: 2\n"
        "Tasks: P8B1, P8B2\n"
        "Halted: False | Paused: True"
    )


def test_status_says_none_for_an_empty_queue_and_omits_the_parked_line(
    subject, sandbox, control
):
    _put_state(subject, sandbox, phase={"id": "P1"})
    control.deliver(_update("/status"))
    (msg,) = control.sent
    assert "Tasks: none" in msg
    assert "Parked:" not in msg
    assert "Phase: P1 | In-flight: 0" in msg


def test_status_appends_a_parked_line_only_when_something_is_parked(subject, sandbox, control):
    _put_state(subject, sandbox, phase={"id": "P1"}, parked_tasks=["P4A", "P4B"])
    control.deliver(_update("/status"))
    assert control.sent[0].endswith("\nParked: P4A, P4B")


def test_status_falls_back_to_a_question_mark_when_the_phase_block_is_missing(
    subject, sandbox, control
):
    _put_state(subject, sandbox)
    control.deliver(_update("/status"))
    assert "Phase: ? | In-flight: 0" in control.sent[0]


def test_progress_forwards_the_report_built_by_build_progress_report(subject, sandbox, control):
    _put_state(subject, sandbox, in_flight=[])
    control.deliver(_update("/progress"))
    assert control.sent == [f"{MAILBOX} No tasks in flight."]


def test_pause_sets_paused_by_user_journals_the_control_event_and_confirms(
    subject, sandbox, control
):
    _put_state(subject, sandbox, paused_by_user=False)
    control.deliver(_update("/pause"))
    assert _get_state(subject, sandbox)["paused_by_user"] is True
    assert [(e["event"], e["detail"]) for e in _journal_events(subject, sandbox)] == [
        ("control_pause", "paused via Telegram /pause")
    ]
    assert control.sent[0].startswith(f"{PAUSE} Orchestrator paused.")


def test_resume_clears_paused_by_user_journals_the_control_event_and_confirms(
    subject, sandbox, control
):
    _put_state(subject, sandbox, paused_by_user=True)
    control.deliver(_update("/resume"))
    assert _get_state(subject, sandbox)["paused_by_user"] is False
    assert [e["event"] for e in _journal_events(subject, sandbox)] == ["control_resume"]
    assert control.sent == [f"{PLAY} Orchestrator resumed."]


def test_halt_writes_the_sentinel_stamped_with_the_current_time(subject, sandbox, control):
    halt = _halt_file(subject, sandbox)
    control.deliver(_update("/halt"))
    assert halt.exists()
    datetime.strptime(halt.read_text(encoding="utf-8"), "%Y-%m-%dT%H:%M:%SZ")
    assert [e["event"] for e in _journal_events(subject, sandbox)] == ["control_halt"]
    assert control.sent[0].startswith(f"{STOP} HALT sentinel written.")


def test_halt_needs_no_state_file_and_creates_the_sentinels_parent_directory(
    subject, sandbox, control, monkeypatch, tmp_path
):
    deeper = tmp_path / "elsewhere" / "nested" / "HALT"
    monkeypatch.setattr(subject, "HALT_FILE", deeper)
    control.deliver(_update("/halt"))
    assert deeper.exists()


def test_waiting_lists_every_parked_item_by_dan_id_task_and_parked_date(
    subject, sandbox, control
):
    _put_state(subject, sandbox, waiting_on_dan={
        "3": {"task_id": "P12C", "parked_at": "2026-06-01T10:11:12+00:00"},
        "4": {"task_id": "P14", "parked_at": "2026-06-02T00:00:00+00:00"},
    })
    control.deliver(_update("/waiting"))
    assert control.sent == [
        "*2 task(s) waiting for your review:*\n"
        "• ID 3: `P12C` (parked 2026-06-01)\n"
        "• ID 4: `P14` (parked 2026-06-02)"
    ]


def test_manual_is_reached_by_the_router_and_reports_an_empty_queue(subject, sandbox,
                                                                    control, prep):
    _put_roadmap(subject, sandbox, "id: A1\ntitle: Auto\n")
    control.deliver(_update("/manual"))
    assert control.sent == ["No Dan-must-perform tasks in the queue."]


def test_detail_without_an_argument_answers_with_usage(subject, sandbox, control):
    control.deliver(_update("/detail"))
    assert control.sent == ["Usage: /detail <task_id>\nExample: /detail P8B2"]


def test_detail_uppercases_its_argument_before_looking_the_task_up(subject, sandbox, control):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild the index\n")
    control.deliver(_update("/detail p8b2"))
    assert control.sent[0].startswith("P8B2 — Rebuild the index")


def test_ask_is_routed_with_the_original_casing_of_the_question(
    subject, sandbox, control, prep, monkeypatch
):
    asked = []
    monkeypatch.setattr(subject, "_handle_ask", lambda arg: asked.append(arg))
    control.deliver(_update("/ask P12C Cell 5 raises KeyError 'X'"))
    assert asked == ["P12C Cell 5 raises KeyError 'X'"]


def test_redo_is_routed_with_the_original_casing_of_the_message(
    subject, sandbox, control, monkeypatch
):
    redone = []
    monkeypatch.setattr(subject, "_handle_redo", lambda arg: redone.append(arg))
    control.deliver(_update("/redo 9 The Drive Path Is Wrong"))
    assert redone == ["9 The Drive Path Is Wrong"]


def test_approve_forwards_the_lowercased_id_and_the_live_in_flight_list(
    subject, sandbox, control, monkeypatch
):
    """`/approve` is dispatched off the lowercased text, so a *task-id* style argument is
    handed to `_process_approve` in lower case — the numeric dan_id it expects is
    unaffected, but `/approve P12C` looks up `p12c`."""
    seen = []
    monkeypatch.setattr(subject, "_process_approve",
                        lambda dan_id, in_flight: seen.append((dan_id, in_flight)))
    control.deliver(_update("/approve P12C", uid=100), _update("/approve  7  ", uid=101))
    assert [dan_id for dan_id, _ in seen] == ["p12c", "7"]
    assert seen[0][1] is control.in_flight


def test_reject_forwards_the_lowercased_id_unchanged(subject, sandbox, control, monkeypatch):
    seen = []
    monkeypatch.setattr(subject, "_process_reject", lambda dan_id: seen.append(dan_id))
    control.deliver(_update("/reject P12C"))
    assert seen == ["p12c"]


def test_fix_uppercases_its_argument_unlike_approve_and_reject(
    subject, sandbox, control, monkeypatch
):
    seen = []
    monkeypatch.setattr(subject, "_process_fix", lambda task_id: seen.append(task_id))
    control.deliver(_update("/fix p12c"))
    assert seen == ["P12C"]


def test_unpark_removes_the_task_and_resets_its_retry_count(subject, sandbox, control):
    _put_state(subject, sandbox, parked_tasks=["P4A", "P4B"], retry_counts={"P4A": 2})
    control.deliver(_update("/unpark p4a"))
    state = _get_state(subject, sandbox)
    assert state["parked_tasks"] == ["P4B"]
    assert state["retry_counts"] == {}
    assert [e["event"] for e in _journal_events(subject, sandbox)] == ["control_unpark"]
    assert control.sent == [f"{CHECK} P4A unparked — will be launched on next cycle."]


def test_unpark_of_something_not_parked_reports_the_current_parked_set(
    subject, sandbox, control
):
    _put_state(subject, sandbox, parked_tasks=["P4B"])
    control.deliver(_update("/unpark P9Z"))
    assert control.sent == [f"{INFO} P9Z is not parked. Currently parked: P4B"]
    assert _journal_events(subject, sandbox) == []


def test_unpark_says_none_when_nothing_is_parked_at_all(subject, sandbox, control):
    _put_state(subject, sandbox)
    control.deliver(_update("/unpark P9Z"))
    assert control.sent == [f"{INFO} P9Z is not parked. Currently parked: none"]


def test_hitl_on_and_off_toggle_the_state_flag_without_journalling(subject, sandbox, control):
    _put_state(subject, sandbox)
    control.deliver(_update("/hitl on"))
    assert _get_state(subject, sandbox)["hitl_mode"] is True
    control.deliver(_update("/hitl off", uid=101))
    assert _get_state(subject, sandbox)["hitl_mode"] is False
    assert _journal_events(subject, sandbox) == []
    assert control.sent[0].startswith(f"{CHECK} HITL mode ON —")
    assert control.sent[1].startswith(f"{CHECK} HITL mode OFF —")
    assert len(control.sent) == 2


def test_hitl_with_any_other_argument_is_read_but_silently_ignored(subject, sandbox, control):
    _put_state(subject, sandbox, hitl_mode=True)
    control.deliver(_update("/hitl maybe"))
    assert control.sent == []
    assert _get_state(subject, sandbox)["hitl_mode"] is True


# =======================================================================================
# _build_progress_report
# =======================================================================================


def test_progress_report_says_nothing_is_in_flight_when_the_queue_is_empty(
    subject, sandbox, notifier
):
    _put_state(subject, sandbox, in_flight=[])
    assert subject._build_progress_report() == f"{MAILBOX} No tasks in flight."


def test_progress_report_headers_carry_id_role_status_and_elapsed(subject, sandbox):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "implementer", "status": "running",
        "started_at": _ago_iso(75), "session_id": "s1", "window": "",
    }])
    report = subject._build_progress_report()
    assert report.splitlines()[0] == f"{CHART_UP} Task progress"
    assert report.splitlines()[1] == "• P8B2 (implementer) running · elapsed 1h15m"


def test_progress_report_header_names_the_backend_only_in_the_extracted_package(
    subject, sandbox
):
    """M2 records which agent is doing the work on the `in_flight` entry, and `/progress`
    is where an operator reads it. The reference does not write that key and does not
    render it, so the two subjects pin two different headers for the same entry."""
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "implementer", "status": "running",
        "started_at": _ago_iso(75), "session_id": "s1", "window": "",
        "backend": "codex",
    }])
    header = subject._build_progress_report().splitlines()[1]
    assert header == "• P8B2 (implementer/codex) running · elapsed 1h15m"


@pytest.mark.parametrize("backend", [None, "", "   "])
def test_progress_report_header_is_unchanged_for_an_entry_with_no_usable_backend(
    subject, sandbox, backend
):
    """Every entry written before M2 — and any an operator hand-seeds — has no backend
    key. It must render byte-for-byte as it always did: no KeyError, and never the string
    "None"."""
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    entry = {
        "task_id": "P8B2", "role": "implementer", "status": "running",
        "started_at": _ago_iso(75), "session_id": "s1", "window": "",
    }
    if backend is not None:
        entry["backend"] = backend
    _put_state(subject, sandbox, in_flight=[entry])
    assert subject._build_progress_report().splitlines()[1] == (
        "• P8B2 (implementer) running · elapsed 1h15m"
    )


def test_progress_report_falls_back_to_question_marks_for_a_bare_in_flight_entry(
    subject, sandbox
):
    _put_roadmap(subject, sandbox, "id: X\ntitle: T\n")
    _put_state(subject, sandbox, in_flight=[{}])
    assert subject._build_progress_report().splitlines()[1] == "• ? (?) ? · elapsed ?"


def test_progress_report_appends_a_truncated_est_and_a_remaining_time_hint(subject, sandbox):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\nest_time: ~2h of focused work\n")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(30), "session_id": "s1", "window": "",
    }])
    header = subject._build_progress_report().splitlines()[1]
    assert "· est ~2h of focused work" in header
    assert header.endswith("· ~1h30m left")


def test_progress_report_says_over_est_once_the_estimate_is_exhausted(subject, sandbox):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\nest_time: ~1h\n")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(180), "session_id": "s1", "window": "",
    }])
    assert subject._build_progress_report().splitlines()[1].endswith("· over est")


def test_progress_report_omits_the_eta_hint_when_est_time_carries_no_duration_token(
    subject, sandbox
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\nest_time: a while\n")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(30), "session_id": "s1", "window": "",
    }])
    header = subject._build_progress_report().splitlines()[1]
    assert header.endswith("· est a while")


def test_progress_report_prefers_a_workspace_progress_json_over_the_tmux_tail(
    subject, sandbox
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    workspace = _workspaces(subject, sandbox) / "s1"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "progress.json").write_text(
        json.dumps({"pct": 40, "milestone": "index rebuilt"}), encoding="utf-8")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(1), "session_id": "s1", "window": "impl-P8B2",
    }])
    assert "    ↳ 40% index rebuilt" in subject._build_progress_report()


def test_progress_report_renders_a_zero_pct_milestone_because_it_tests_for_none(
    subject, sandbox
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    workspace = _workspaces(subject, sandbox) / "s1"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "progress.json").write_text(json.dumps({"pct": 0}), encoding="utf-8")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(1), "session_id": "s1", "window": "impl-P8B2",
    }])
    assert "    ↳ 0%" in subject._build_progress_report()


def test_progress_report_indents_every_continuation_line_of_a_multiline_activity(
    subject, sandbox, monkeypatch
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    monkeypatch.setattr(subject, "_tail_tmux_pane", lambda window, n=3: "one\ntwo")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(1), "session_id": "s1", "window": "impl-P8B2",
    }])
    assert subject._build_progress_report().endswith("    ↳ one\n      two")


def test_progress_report_emits_no_activity_line_when_there_is_nothing_to_show(
    subject, sandbox
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: T\n")
    _put_state(subject, sandbox, in_flight=[{
        "task_id": "P8B2", "role": "impl", "status": "running",
        "started_at": _ago_iso(1), "session_id": "s1", "window": "",
    }])
    assert len(subject._build_progress_report().splitlines()) == 2


def test_progress_report_raises_when_the_roadmap_is_missing_and_poll_swallows_it(
    subject, sandbox, control
):
    """`get_task_by_id` reads ROADMAP.md unguarded, so `/progress` is answered with
    silence on a repo whose roadmap has not been written yet."""
    _put_state(subject, sandbox, in_flight=[{"task_id": "P1", "session_id": "s1"}])
    assert not _roadmap(subject, sandbox).exists()
    with pytest.raises(FileNotFoundError):
        subject._build_progress_report()
    control.deliver(_update("/progress"))
    assert control.sent == []


# =======================================================================================
# _show_waiting
# =======================================================================================


def test_show_waiting_reports_an_empty_queue(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={})
    subject._show_waiting()
    assert notifier == ["No tasks currently waiting for your review."]


def test_show_waiting_truncates_parked_at_to_its_date_prefix(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={
        "9": {"task_id": "P14", "parked_at": "2026-07-04T23:59:59.123456+00:00"}})
    subject._show_waiting()
    assert notifier == ["*1 task(s) waiting for your review:*\n• ID 9: `P14` (parked 2026-07-04)"]


def test_show_waiting_raises_on_a_waiting_entry_that_never_recorded_parked_at(
    subject, sandbox, notifier
):
    _put_state(subject, sandbox, waiting_on_dan={"9": {"task_id": "P14"}})
    with pytest.raises(KeyError):
        subject._show_waiting()


# =======================================================================================
# _resolve_waiting
# =======================================================================================


def test_resolve_waiting_matches_a_dan_id_key_exactly(subject, sandbox):
    _put_state(subject, sandbox, waiting_on_dan={"9": {"task_id": "P14"}})
    assert subject._resolve_waiting("9") == ("9", {"task_id": "P14"})


def test_resolve_waiting_falls_back_to_a_case_insensitive_task_id_match(subject, sandbox):
    _put_state(subject, sandbox, waiting_on_dan={"9": {"task_id": "P12C"}})
    dan_id, entry = subject._resolve_waiting("p12c")
    assert (dan_id, entry["task_id"]) == ("9", "P12C")


def test_resolve_waiting_returns_a_none_pair_when_nothing_matches(subject, sandbox):
    _put_state(subject, sandbox, waiting_on_dan={"9": {"task_id": "P12C"}})
    assert subject._resolve_waiting("P99") == (None, None)


def test_resolve_waiting_prefers_the_dan_id_key_over_a_task_id_that_shadows_it(
    subject, sandbox
):
    _put_state(subject, sandbox, waiting_on_dan={
        "9": {"task_id": "OTHER"}, "3": {"task_id": "9"}})
    assert subject._resolve_waiting("9") == ("9", {"task_id": "OTHER"})


# =======================================================================================
# _show_manual
# =======================================================================================


def test_show_manual_reports_nothing_to_do_when_the_roadmap_has_no_dan_work(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: A1\ntitle: Autonomous\ndispatch: auto\n")
    subject._show_manual()
    assert notifier == ["No Dan-must-perform tasks in the queue."]


def test_show_manual_treats_a_missing_roadmap_as_an_empty_one(subject, sandbox, notifier, prep):
    """`_load_roadmap_tasks` swallows the OSError, so the `Could not parse ROADMAP`
    branch below it is unreachable through a missing or unreadable file."""
    assert not _roadmap(subject, sandbox).exists()
    subject._show_manual()
    assert notifier == ["No Dan-must-perform tasks in the queue."]


def test_show_manual_lists_a_manual_task_as_queued_for_auto_prep(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Run the Colab\ndispatch: manual\n")
    _put_state(subject, sandbox)
    subject._show_manual()
    assert notifier == [f"{TOOLS} Dan-must-perform (1):\n"
                        f"• P12C — Run the Colab\n    {HOURGLASS} queued for auto-prep"]


def test_show_manual_counts_a_needs_dan_task_even_without_an_explicit_dispatch(
    subject, sandbox, notifier, prep
):
    """`_normalize_task` rewrites `mode: needs-dan` to `dispatch: manual` on the way in."""
    _put_roadmap(subject, sandbox, "id: P14\ntitle: Physical work\nmode: needs-dan\n")
    _put_state(subject, sandbox)
    subject._show_manual()
    assert f"{TOOLS} Dan-must-perform (1):" in notifier[0]
    assert "• P14 — Physical work" in notifier[0]


def test_show_manual_shows_the_prepared_action_alone_when_nothing_is_parked(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Run the Colab\ndispatch: manual\n")
    _put_state(subject, sandbox)
    prep.store["P12C"] = {"action": "Open the notebook and run all cells"}
    subject._show_manual()
    assert notifier[0].endswith(f"    {TARGET} Open the notebook and run all cells")


def test_show_manual_adds_the_approve_ask_reject_affordances_once_the_item_is_parked(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Run the Colab\ndispatch: manual\n")
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C"}})
    prep.store["P12C"] = {"action": "Run all cells"}
    subject._show_manual()
    assert notifier[0].endswith(
        f"    {TARGET} Run all cells\n"
        f"   {CHECK} /approve 7  ·  \N{BLACK QUESTION MARK ORNAMENT} /ask 7 <msg>  "
        f"·  {CROSS} /reject 7"
    )


def test_show_manual_falls_back_to_the_roadmaps_dan_action_when_no_sidecar_exists(
    subject, sandbox, notifier, prep
):
    _put_roadmap(
        subject, sandbox,
        "id: P12C\ntitle: Run the Colab\ndispatch: manual\ndan_action: Click Runtime > Run all\n",
    )
    _put_state(subject, sandbox)
    subject._show_manual()
    assert notifier[0].endswith(f"    {TARGET} Click Runtime > Run all")


def test_show_manual_reports_auto_prep_running_for_an_in_flight_task_with_no_action(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Run the Colab\ndispatch: manual\n")
    _put_state(subject, sandbox, in_flight=[{"task_id": "P12C"}])
    subject._show_manual()
    assert notifier[0].endswith(f"    {HOURGLASS} auto-prep running")


def test_show_manual_reports_blocked_deps_only_for_deps_still_present_in_the_roadmap(
    subject, sandbox, notifier, prep
):
    """A dep that has already graduated out of ROADMAP.md is not in `pending_ids`, so it
    never counts as blocking here — unlike `_show_detail`, which only checks completion."""
    _put_roadmap(
        subject, sandbox,
        "id: P12C\ntitle: Run the Colab\ndispatch: manual\ndeps: [P11, GRADUATED]\n",
        "id: P11\ntitle: Build it\n",
    )
    _put_state(subject, sandbox)
    subject._show_manual()
    assert notifier[0].endswith(f"    {NO_ENTRY} blocked by P11")


def test_show_manual_accepts_a_single_dep_written_as_a_bare_string(
    subject, sandbox, notifier, prep
):
    _put_roadmap(
        subject, sandbox,
        "id: P12C\ntitle: Run the Colab\ndispatch: manual\ndeps: P11\n",
        "id: P11\ntitle: Build it\n",
    )
    _put_state(subject, sandbox)
    subject._show_manual()
    assert notifier[0].endswith(f"    {NO_ENTRY} blocked by P11")


def test_show_manual_clears_a_dep_recorded_in_completed_tasks(
    subject, sandbox, notifier, prep
):
    _put_roadmap(
        subject, sandbox,
        "id: P12C\ntitle: Run the Colab\ndispatch: manual\ndeps: [P11]\n",
        "id: P11\ntitle: Build it\n",
    )
    _put_completed(subject, sandbox, "P11")
    _put_state(subject, sandbox)
    subject._show_manual()
    assert notifier[0].endswith(f"    {HOURGLASS} queued for auto-prep")


def test_show_manual_lists_question_tasks_with_their_per_question_answer_state(
    subject, sandbox, notifier, prep
):
    _put_roadmap(
        subject, sandbox,
        "id: P20\ntitle: Needs answers\nquestions:\n"
        "  - id: q1\n    prompt: Which store?\n  - id: q2\n    prompt: Which model?\n",
    )
    _put_state(subject, sandbox)
    _put_answer(subject, sandbox, "q-P20-q1", "Postgres")
    subject._show_manual()
    assert notifier == [
        f"{HOURGLASS} Awaiting your answers (1):\n"
        "• P20 — Needs answers — 1/2 answered (q1=✓, q2=…)"
    ]


def test_show_manual_separates_the_two_sections_with_a_blank_line(
    subject, sandbox, notifier, prep
):
    _put_roadmap(
        subject, sandbox,
        "id: P12C\ntitle: Colab\ndispatch: manual\n",
        "id: P20\ntitle: Answers\nquestions:\n  - id: q1\n    prompt: Which store?\n",
    )
    _put_state(subject, sandbox)
    subject._show_manual()
    assert f"\n\n{HOURGLASS} Awaiting your answers (1):" in notifier[0]


# =======================================================================================
# _show_detail
# =======================================================================================


def test_show_detail_reports_an_unknown_task_as_possibly_graduated(subject, sandbox, notifier):
    _put_roadmap(subject, sandbox, "id: A1\ntitle: T\n")
    subject._show_detail("P99")
    assert notifier == [
        "No task P99 in ROADMAP (it may be graduated — see docs/PROJECT.md)."
    ]


def test_show_detail_matches_the_task_id_case_insensitively(subject, sandbox, notifier):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\n")
    subject._show_detail("p8b2")
    assert notifier[0].startswith("P8B2 — Rebuild")


def test_show_detail_dumps_mode_dispatch_and_est_defaults_for_an_autonomous_task(
    subject, sandbox, notifier
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\n")
    subject._show_detail("P8B2")
    assert notifier == [
        "P8B2 — Rebuild\n"
        "mode: autonomous | dispatch: auto | est: unknown\n"
        "\nNo structured verifications in this task block."
    ]


def test_show_detail_includes_short_desc_when_the_task_carries_one(subject, sandbox, notifier):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\nshort_desc: Reindex everything\n")
    subject._show_detail("P8B2")
    assert "Reindex everything" in notifier[0]


def test_show_detail_renders_auto_verifications_with_their_command(subject, sandbox, notifier):
    _put_roadmap(
        subject, sandbox,
        "id: P8B2\ntitle: Rebuild\nverifications:\n"
        "  - id: v1\n    kind: auto\n    check: tests pass\n    cmd: pytest -q\n",
    )
    subject._show_detail("P8B2")
    assert notifier[0].endswith("\nVerifications:\n  [auto] v1: tests pass\n    cmd: pytest -q")


def test_show_detail_omits_the_cmd_line_for_a_manual_verification(subject, sandbox, notifier):
    _put_roadmap(
        subject, sandbox,
        "id: P8B2\ntitle: Rebuild\nverifications:\n"
        "  - id: v1\n    kind: manual\n    check: eyeball it\n    cmd: pytest -q\n",
    )
    subject._show_detail("P8B2")
    assert notifier[0].endswith("  [manual] v1: eyeball it")


def test_show_detail_defaults_a_verifications_kind_and_id(subject, sandbox, notifier):
    _put_roadmap(subject, sandbox,
                 "id: P8B2\ntitle: Rebuild\nverifications:\n  - check: it works\n")
    subject._show_detail("P8B2")
    assert notifier[0].endswith("  [auto] ?: it works")


def test_show_detail_keeps_the_verifications_header_even_when_every_entry_is_skipped(
    subject, sandbox, notifier
):
    _put_roadmap(subject, sandbox,
                 "id: P8B2\ntitle: Rebuild\nverifications:\n  - just a string\n")
    subject._show_detail("P8B2")
    assert notifier[0].endswith("mode: autonomous | dispatch: auto | est: unknown\n\nVerifications:")


def test_show_detail_shows_a_dan_task_as_queued_for_auto_prep(subject, sandbox, notifier, prep):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    _put_state(subject, sandbox)
    subject._show_detail("P12C")
    assert notifier == [f"P12C — Colab\n{HOURGLASS} Queued for auto-prep. "
                        f"(`/detail P12C full` for the raw brief.)"]


def test_show_detail_shows_the_prepared_action_and_its_decision_affordances_when_parked(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C"}})
    prep.store["P12C"] = {"action": "Run all cells"}
    subject._show_detail("P12C")
    assert notifier == [
        f"P12C — Colab\n{TARGET} Your action (ID 7): Run all cells\n"
        f"{CHECK} Worked → /approve 7  ·  \N{BLACK QUESTION MARK ORNAMENT} Problem → /ask 7 <msg>  "
        f"·  {CROSS} Abandon → /reject 7"
    ]


def test_show_detail_shows_the_prepared_action_alone_when_it_is_not_parked(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    _put_state(subject, sandbox)
    prep.store["P12C"] = {"action": "Run all cells"}
    subject._show_detail("P12C")
    assert notifier == [f"P12C — Colab\n{TARGET} Your action: Run all cells"]


def test_show_detail_reports_auto_prep_in_progress_for_an_in_flight_dan_task(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    _put_state(subject, sandbox, in_flight=[{"task_id": "P12C"}])
    subject._show_detail("P12C")
    assert notifier[0].startswith(f"P12C — Colab\n{HOURGLASS} Auto-prep in progress")


def test_show_detail_blocks_on_any_dep_absent_from_completed_tasks_even_if_graduated(
    subject, sandbox, notifier, prep
):
    """The one-action view checks completion only, so a dep that has already graduated out
    of ROADMAP.md still reads as blocking — the opposite of `_show_manual`."""
    _put_roadmap(subject, sandbox,
                 "id: P12C\ntitle: Colab\ndispatch: manual\ndeps: [GRADUATED]\n")
    _put_state(subject, sandbox)
    subject._show_detail("P12C")
    assert notifier[0] == (f"P12C — Colab\n{NO_ENTRY} Blocked by: GRADUATED "
                           f"(auto-prep starts once deps complete).")


def test_show_detail_full_forces_the_verbose_dump_for_a_dan_task(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    prep.store["P12C"] = {"action": "Run all cells"}
    subject._show_detail("P12C FULL")
    assert notifier[0].startswith("P12C — Colab\nmode: autonomous | dispatch: manual")


def test_show_detail_recognises_the_full_suffix_in_any_casing(subject, sandbox, notifier, prep):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    prep.store["P12C"] = {"action": "Run all cells"}
    subject._show_detail("p12c full")
    assert "mode: autonomous | dispatch: manual" in notifier[0]


def test_show_detail_treats_a_missing_roadmap_as_a_graduated_task(subject, sandbox, notifier):
    assert not _roadmap(subject, sandbox).exists()
    subject._show_detail("P12C")
    assert notifier[0].startswith("No task P12C in ROADMAP")


# =======================================================================================
# _handle_ask
# =======================================================================================


def _ask_dir(subject, sandbox) -> Path:
    return _path(subject, "REPO", sandbox.repo) / ".orchestrator" / "ask"


@pytest.fixture
def launcher(subject, monkeypatch):
    """A recorder for the fire-and-forget tmux launch used by /ask and /redo."""
    calls: list[tuple] = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0] if args else None, 0, "", "")

    monkeypatch.setattr(subject, "subprocess", SimpleNamespace(
        run=run, calls=calls, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL))
    return calls


def test_handle_ask_without_a_message_answers_with_usage(subject, sandbox, notifier):
    subject._handle_ask("P12C")
    assert notifier[0].startswith("Usage: /ask <id> <your message>")


def test_handle_ask_with_an_empty_argument_answers_with_usage(subject, sandbox, notifier):
    subject._handle_ask("")
    assert notifier[0].startswith("Usage: /ask <id> <your message>")


def test_handle_ask_reports_when_no_parked_task_matches(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={})
    subject._handle_ask("P99 what now")
    assert notifier == [f"{INFO} /ask: no parked task matches `P99`. Use /waiting to list them."]


def test_handle_ask_writes_a_request_file_and_launches_a_dedicated_tmux_window(
    subject, sandbox, notifier, prep, launcher, tmp_path
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _put_state(subject, sandbox, waiting_on_dan={"7": {
        "task_id": "P12C", "session_id": "s1", "session_uuid": "uuid-1",
        "worktree": str(worktree)}})
    prep.store["P12C"] = {"action": "Run all cells"}

    subject._handle_ask("7 cell 5 fails")

    (request,) = sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))
    payload = json.loads(request.read_text(encoding="utf-8"))
    assert payload["uuid"] == "uuid-1"
    assert payload["task"] == "P12C"
    assert payload["dan_id"] == "7"
    assert payload["question"] == "cell 5 fails"
    assert payload["action"] == "Run all cells"
    assert payload["worktree"] == str(worktree)
    assert payload["brief"].endswith("/s1/brief.txt")
    assert payload["log"] == str(request.with_name(request.name[:-5] + ".answer.log"))

    (args, kwargs), = launcher
    assert args[0].startswith("tmux new-window -t agents -n ask-P12C ")
    assert "-m maestro.hitl.ask" in args[0]
    assert kwargs == {"shell": True, "check": True}


def test_handle_ask_reports_resuming_the_prep_session_when_a_uuid_is_known(
    subject, sandbox, notifier, prep, launcher, tmp_path
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _put_state(subject, sandbox, waiting_on_dan={"7": {
        "task_id": "P12C", "session_id": "s1", "session_uuid": "u", "worktree": str(worktree)}})
    subject._handle_ask("7 hello")
    assert notifier[0] == (f"{THINKING} Asking Maestro about `P12C` (resuming its prep "
                           f"session)… I'll reply here shortly.")
    assert [(e["event"], e["detail"]) for e in _journal_events(subject, sandbox)] == [
        ("ask_launched", "P12C dan_id=7 uuid=y")
    ]


def test_handle_ask_reads_the_session_uuid_off_the_workspace_when_the_entry_omits_it(
    subject, sandbox, notifier, prep, launcher
):
    workspace = _workspaces(subject, sandbox) / "s1"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "session_uuid.txt").write_text("from-workspace\n", encoding="utf-8")
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C", "session_id": "s1"}})
    subject._handle_ask("7 hello")
    (request,) = sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))
    assert json.loads(request.read_text(encoding="utf-8"))["uuid"] == "from-workspace"


def test_handle_ask_says_it_is_reading_the_brief_when_there_is_no_resumable_uuid(
    subject, sandbox, notifier, prep, launcher
):
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C", "session_id": "s1"}})
    subject._handle_ask("7 hello")
    assert "(reading the task brief)" in notifier[0]
    assert _journal_events(subject, sandbox)[0]["detail"].endswith("uuid=n")


def test_handle_ask_recreates_a_vanished_worktree_directory_when_a_uuid_is_resumable(
    subject, sandbox, notifier, prep, launcher, tmp_path
):
    """The Claude session JSONL is keyed to the worktree path, so the reference recreates
    the empty directory rather than downgrading cwd to REPO."""
    worktree = tmp_path / "gone"
    _put_state(subject, sandbox, waiting_on_dan={"7": {
        "task_id": "P12C", "session_id": "s1", "session_uuid": "u", "worktree": str(worktree)}})
    subject._handle_ask("7 hello")
    assert worktree.is_dir()
    (request,) = sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))
    assert json.loads(request.read_text(encoding="utf-8"))["worktree"] == str(worktree)


def test_handle_ask_falls_back_to_the_repo_root_when_there_is_no_usable_worktree(
    subject, sandbox, notifier, prep, launcher, tmp_path
):
    _put_state(subject, sandbox, waiting_on_dan={"7": {
        "task_id": "P12C", "session_id": "s1", "worktree": str(tmp_path / "gone")}})
    subject._handle_ask("7 hello")
    (request,) = sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))
    payload = json.loads(request.read_text(encoding="utf-8"))
    assert payload["worktree"] == str(_path(subject, "REPO", sandbox.repo))


def test_handle_ask_leaves_the_brief_empty_when_the_entry_has_no_session_id(
    subject, sandbox, notifier, prep, launcher
):
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C"}})
    subject._handle_ask("7 hello")
    (request,) = sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))
    assert json.loads(request.read_text(encoding="utf-8"))["brief"] == ""


def test_handle_ask_prefers_the_waiting_entry_summary_when_no_sidecar_action_exists(
    subject, sandbox, notifier, prep, launcher
):
    _put_state(subject, sandbox, waiting_on_dan={"7": {
        "task_id": "P12C", "session_id": "s1", "summary": "Upload the CSV"}})
    subject._handle_ask("7 hello")
    (request,) = sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))
    assert json.loads(request.read_text(encoding="utf-8"))["action"] == "Upload the CSV"


def test_handle_ask_reports_a_failed_tmux_launch_and_sends_no_confirmation(
    subject, sandbox, notifier, prep, monkeypatch
):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "tmux")

    monkeypatch.setattr(subject, "subprocess", SimpleNamespace(
        run=boom, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL))
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C", "session_id": "s1"}})
    subject._handle_ask("7 hello")
    assert notifier[0].startswith(f"{WARN} /ask failed to launch:")
    assert _journal_events(subject, sandbox) == []


def test_handle_ask_resolves_a_task_id_argument_as_well_as_a_dan_id(
    subject, sandbox, notifier, prep, launcher
):
    _put_state(subject, sandbox, waiting_on_dan={"7": {"task_id": "P12C", "session_id": "s1"}})
    subject._handle_ask("p12c hello")
    assert json.loads(
        sorted(_ask_dir(subject, sandbox).glob("P12C-*.json"))[0].read_text(encoding="utf-8")
    )["dan_id"] == "7"


# =======================================================================================
# _handle_redo
# =======================================================================================


def test_handle_redo_without_a_message_answers_with_usage(subject, sandbox, notifier):
    subject._handle_redo("9")
    assert notifier[0].startswith("Usage: /redo <id> <what's wrong>")


def test_handle_redo_reports_when_no_parked_task_matches(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={})
    subject._handle_redo("P99 rewrite it")
    assert notifier == [f"{INFO} /redo: no parked task matches `P99`. Use /waiting to list them."]


def test_handle_redo_writes_a_request_file_and_launches_the_redo_helper(
    subject, sandbox, notifier, prep, launcher, tmp_path
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _put_state(subject, sandbox, waiting_on_dan={"9": {
        "task_id": "P12C", "session_id": "s1", "session_uuid": "u", "worktree": str(worktree)}})
    prep.store["P12C"] = {"action": "Run all cells"}

    subject._handle_redo("9 the Drive path is wrong")

    (request,) = sorted(_redo_dir(subject, sandbox).glob("P12C-*.request.json"))
    payload = json.loads(request.read_text(encoding="utf-8"))
    assert payload["redo_id"] == request.name[: -len(".request.json")]
    assert payload["task"] == "P12C"
    assert payload["uuid"] == "u"
    assert payload["dan_id"] == "9"
    assert payload["message"] == "the Drive path is wrong"
    assert payload["action"] == "Run all cells"
    assert payload["worktree"] == str(worktree)

    (args, kwargs), = launcher
    assert args[0].startswith("tmux new-window -t agents -n redo-P12C ")
    assert "maestro.selfheal.redo" in args[0]
    assert kwargs == {"shell": True, "check": True}


def test_handle_redo_does_not_recreate_a_vanished_worktree_the_way_ask_does(
    subject, sandbox, notifier, prep, launcher, tmp_path
):
    """`_handle_redo` records `worktree` verbatim — there is no existence check and no
    fallback to REPO, so the helper is handed a path that is not there."""
    worktree = tmp_path / "gone"
    _put_state(subject, sandbox, waiting_on_dan={"9": {
        "task_id": "P12C", "session_id": "s1", "session_uuid": "u", "worktree": str(worktree)}})
    subject._handle_redo("9 rewrite it")
    assert not worktree.exists()
    (request,) = sorted(_redo_dir(subject, sandbox).glob("P12C-*.request.json"))
    assert json.loads(request.read_text(encoding="utf-8"))["worktree"] == str(worktree)


def test_handle_redo_confirms_and_journals_the_launch(
    subject, sandbox, notifier, prep, launcher
):
    _put_state(subject, sandbox, waiting_on_dan={"9": {"task_id": "P12C", "session_id": "s1"}})
    subject._handle_redo("9 rewrite it")
    assert notifier[0].startswith(f"{WRENCH} Maestro is reworking `P12C`")
    assert [(e["event"], e["detail"]) for e in _journal_events(subject, sandbox)] == [
        ("redo_launched", "P12C dan_id=9 uuid=n")
    ]


def test_handle_redo_reports_a_failed_tmux_launch(subject, sandbox, notifier, prep, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("tmux missing")

    monkeypatch.setattr(subject, "subprocess", SimpleNamespace(
        run=boom, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL))
    _put_state(subject, sandbox, waiting_on_dan={"9": {"task_id": "P12C", "session_id": "s1"}})
    subject._handle_redo("9 rewrite it")
    assert notifier[0].startswith(f"{WARN} /redo failed to launch:")
    assert _journal_events(subject, sandbox) == []


# =======================================================================================
# _process_fix
# =======================================================================================


def test_process_fix_reports_when_no_diagnosis_was_ever_persisted(subject, sandbox, notifier):
    subject._process_fix("P8B2")
    assert notifier[0].startswith("No pending diagnosis for `P8B2`.")


def test_process_fix_reports_an_unreadable_diagnosis_without_running_anything(
    subject, sandbox, notifier, monkeypatch
):
    ran = []
    monkeypatch.setattr(subject, "attempt_self_fix",
                        lambda *a: ran.append(a))
    diagnoses = _diagnoses_dir(subject, sandbox)
    diagnoses.mkdir(parents=True, exist_ok=True)
    (diagnoses / "P8B2.json").write_text("{not json", encoding="utf-8")
    subject._process_fix("P8B2")
    assert notifier[0].startswith(f"{WARN} Could not read the saved diagnosis for `P8B2`:")
    assert ran == []
    assert (diagnoses / "P8B2.json").exists()


def test_process_fix_runs_the_self_fix_runner_and_consumes_the_diagnosis(
    subject, sandbox, notifier, monkeypatch
):
    ran = []
    monkeypatch.setattr(subject, "attempt_self_fix", lambda *a: ran.append(a))
    diagnoses = _diagnoses_dir(subject, sandbox)
    diagnoses.mkdir(parents=True, exist_ok=True)
    diag = {"reason": "gate failed", "failure_class": "flaky-test",
            "suggested_fix": "raise the timeout", "target_files": ["scripts/x.py"]}
    (diagnoses / "P8B2.json").write_text(json.dumps(diag), encoding="utf-8")

    subject._process_fix("P8B2")

    assert ran == [("P8B2", "gate failed", diag)]
    assert not (diagnoses / "P8B2.json").exists()
    assert [(e["event"], e["detail"]) for e in _journal_events(subject, sandbox)] == [
        ("fix_approved", "P8B2 class=flaky-test via /fix")
    ]
    assert notifier[0].startswith(f"{WRENCH} Approved — implementing the diagnosed fix for `P8B2`")


def test_process_fix_leaves_the_diagnosis_on_disk_when_the_runner_raises(
    subject, sandbox, notifier, monkeypatch
):
    def boom(*args):
        raise RuntimeError("worktree busy")

    monkeypatch.setattr(subject, "attempt_self_fix", boom)
    diagnoses = _diagnoses_dir(subject, sandbox)
    diagnoses.mkdir(parents=True, exist_ok=True)
    (diagnoses / "P8B2.json").write_text(json.dumps({"reason": "r"}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        subject._process_fix("P8B2")
    assert (diagnoses / "P8B2.json").exists()


# =======================================================================================
# _process_approve
# =======================================================================================


@pytest.fixture
def merge_stubs(subject, monkeypatch):
    """Stub every collaborator on the post-approval finalize path."""
    recorded = SimpleNamespace(
        accepted=True,
        smoke={"metrics": {"recall_at_5": 0.91}},
        touches_bot_files=False,
        merged=[], removed_from_state=[], removed_worktrees=[], graduated=[],
        phase_reports=[], regressions=[], dep_maps=[], statuses=[], finalized=[],
    )

    def merge_and_eval(entry):
        recorded.merged.append(entry)
        return recorded.accepted, recorded.smoke

    monkeypatch.setattr(subject, "merge_and_eval", merge_and_eval)
    monkeypatch.setattr(subject, "_remove_from_state",
                        lambda entry: recorded.removed_from_state.append(entry))
    monkeypatch.setattr(subject, "remove_worktree",
                        lambda wt: recorded.removed_worktrees.append(wt))
    monkeypatch.setattr(subject, "mark_roadmap_complete",
                        lambda tid: recorded.graduated.append(tid))
    monkeypatch.setattr(subject, "_touches_bot_files",
                        lambda branch: recorded.touches_bot_files)
    monkeypatch.setattr(subject, "phase_report",
                        lambda tid, task, smoke: recorded.phase_reports.append(tid))
    monkeypatch.setattr(subject, "park_regression",
                        lambda tid, smoke: recorded.regressions.append((tid, smoke)))
    monkeypatch.setattr(subject, "run_dep_map", lambda: recorded.dep_maps.append(True))
    monkeypatch.setattr(subject, "run_status", lambda: recorded.statuses.append(True))
    monkeypatch.setattr(subject, "_finalize_manual_action",
                        lambda dan_id, parked, in_flight:
                        recorded.finalized.append((dan_id, parked, in_flight)))
    return recorded


def _parked_entry(tmp_path, task_id="P8B2", session_id="s1"):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return {"task_id": task_id, "session_id": session_id,
            "branch": f"task/{task_id}", "worktree": str(worktree),
            "parked_at": _now_iso()}


def test_process_approve_reports_an_unknown_dan_id(subject, sandbox, notifier, merge_stubs):
    _put_state(subject, sandbox, waiting_on_dan={})
    subject._process_approve("7", [])
    assert notifier == ["No pending item with ID 7."]
    assert merge_stubs.merged == []


def test_process_approve_hands_a_manual_action_item_to_the_manual_finalizer(
    subject, sandbox, notifier, merge_stubs
):
    parked = {"task_id": "P12C", "kind": "manual-action", "session_id": "s1"}
    _put_state(subject, sandbox, waiting_on_dan={"7": parked})
    in_flight = []
    subject._process_approve("7", in_flight)
    assert merge_stubs.finalized == [("7", parked, in_flight)]
    assert merge_stubs.merged == []
    assert _get_state(subject, sandbox)["waiting_on_dan"] == {"7": parked}


def test_process_approve_merges_finalizes_and_graduates_an_accepted_task(
    subject, sandbox, notifier, merge_stubs, tmp_path
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\n")
    parked = _parked_entry(tmp_path)
    _put_state(subject, sandbox, waiting_on_dan={"7": parked})
    workspace = _workspaces(subject, sandbox) / "s1"
    workspace.mkdir(parents=True, exist_ok=True)
    paused = workspace / "PAUSED"
    paused.write_text("", encoding="utf-8")
    in_flight = [{"session_id": "s1", "task_id": "P8B2"}, {"session_id": "s2"}]

    subject._process_approve("7", in_flight)

    assert merge_stubs.merged == [{
        "session_id": "s1", "task_id": "P8B2", "branch": "task/P8B2",
        "worktree": parked["worktree"], "window": "impl-P8B2",
    }]
    assert not paused.exists()
    assert in_flight == [{"session_id": "s2"}]
    assert _get_state(subject, sandbox)["waiting_on_dan"] == {}
    assert merge_stubs.graduated == ["P8B2"]
    assert merge_stubs.removed_worktrees == [Path(parked["worktree"])]
    assert merge_stubs.phase_reports == ["P8B2"]
    assert merge_stubs.dep_maps == [True] and merge_stubs.statuses == [True]
    assert notifier[0] == f"{CHECK} Approved P8B2 — merging now…"
    assert notifier[-1] == f"{CHECK} P8B2 merged & accepted (recall_at_5=0.91)."
    events = [e["event"] for e in _journal_events(subject, sandbox)]
    assert events == ["hitl_approved", "task_complete"]


def test_process_approve_pushes_main_after_an_accepted_merge(
    subject, sandbox, notifier, merge_stubs, monkeypatch, tmp_path
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\n")
    calls = _fake_subprocess(monkeypatch, subject, lambda: "").calls
    _put_state(subject, sandbox, waiting_on_dan={"7": _parked_entry(tmp_path)})
    subject._process_approve("7", [])
    assert calls[0][0][0] == ["git", "push", "origin", "main"]


def test_process_approve_says_smoke_skipped_when_the_task_reports_no_metrics(
    subject, sandbox, notifier, merge_stubs, tmp_path
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\n")
    merge_stubs.smoke = {}
    _put_state(subject, sandbox, waiting_on_dan={"7": _parked_entry(tmp_path)})
    subject._process_approve("7", [])
    # Was "smoke skipped (non-retrieval)" — a per-project branch spelled in one
    # project's vocabulary. `metrics.summary` says the same thing for any project.
    assert notifier[-1] == f"{CHECK} P8B2 merged & accepted (no metrics)."


def test_process_approve_aborts_the_finalize_when_a_bot_file_canary_deploy_fails(
    subject, sandbox, notifier, merge_stubs, monkeypatch, tmp_path
):
    _put_roadmap(subject, sandbox, "id: P8B2\ntitle: Rebuild\n")
    merge_stubs.touches_bot_files = True

    def run(*args, **kwargs):
        cmd = args[0]
        rc = 1 if any("canary" in str(part) for part in cmd) else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(subject, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL))
    _put_state(subject, sandbox, waiting_on_dan={"7": _parked_entry(tmp_path)})

    subject._process_approve("7", [])

    assert merge_stubs.phase_reports == []
    assert "canary_reverted" in [e["event"] for e in _journal_events(subject, sandbox)]
    assert merge_stubs.dep_maps == [True] and merge_stubs.statuses == [True]
    assert not any("merged & accepted" in m for m in notifier)


def test_process_approve_parks_a_regression_when_the_merge_is_not_accepted(
    subject, sandbox, notifier, merge_stubs, tmp_path
):
    merge_stubs.accepted = False
    merge_stubs.smoke = {"metrics": {"recall_at_5": 0.2}, "reason": "recall dropped"}
    _put_state(subject, sandbox, waiting_on_dan={"7": _parked_entry(tmp_path)})
    subject._process_approve("7", [])
    assert merge_stubs.regressions == [("P8B2", merge_stubs.smoke)]
    assert merge_stubs.graduated == []
    assert notifier[-1] == (f"{WARN} P8B2 smoke/merge failed (recall_at_5=0.2) — reverted. "
                            f"See journal.")
    assert [e["event"] for e in _journal_events(subject, sandbox)] == ["hitl_approved"]


def test_process_approve_drops_the_waiting_entry_before_the_merge_is_attempted(
    subject, sandbox, notifier, merge_stubs, monkeypatch, tmp_path
):
    """State is written first, so a merge that blows up leaves the item un-parked and
    unrecoverable through /approve."""
    _put_state(subject, sandbox, waiting_on_dan={"7": _parked_entry(tmp_path)})

    def boom(entry):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(subject, "merge_and_eval", boom)
    with pytest.raises(RuntimeError):
        subject._process_approve("7", [])
    assert _get_state(subject, sandbox)["waiting_on_dan"] == {}


# =======================================================================================
# _process_reject
# =======================================================================================


def test_process_reject_reports_an_unknown_dan_id(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={})
    subject._process_reject("7")
    assert notifier == ["No pending item with ID 7."]


def test_process_reject_of_a_manual_action_drops_the_sidecar_and_keeps_the_prep_commits(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P12C\ntitle: Colab\ndispatch: manual\n")
    _put_state(subject, sandbox, waiting_on_dan={
        "7": {"task_id": "P12C", "kind": "manual-action", "session_id": "s1"}})
    prep.store["P12C"] = {"action": "Run all cells"}
    workspace = _workspaces(subject, sandbox) / "s1"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "PAUSED").write_text("", encoding="utf-8")

    subject._process_reject("7")

    assert prep.store == {}
    assert not (workspace / "PAUSED").exists()
    state = _get_state(subject, sandbox)
    assert state["waiting_on_dan"] == {}
    assert state.get("parked_tasks", []) == []
    assert notifier == [f"{CROSS} Dropped P12C's pending action (prep commits stay on main)."]
    assert [e["event"] for e in _journal_events(subject, sandbox)] == ["manual_action_rejected"]


def test_process_reject_of_an_awaiting_verification_item_takes_the_same_path(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox, "id: P14\ntitle: Eval\ndispatch: manual\n")
    _put_state(subject, sandbox, waiting_on_dan={
        "8": {"task_id": "P14", "kind": "awaiting-verification", "session_id": "s2"}})
    subject._process_reject("8")
    assert notifier[0].startswith(f"{CROSS} Dropped P14's pending action")


def test_process_reject_parks_an_async_job_so_it_is_not_relaunched_next_poll(
    subject, sandbox, notifier, prep
):
    _put_roadmap(subject, sandbox,
                 "id: P14\ntitle: Eval\ndispatch: async-job\nmode: needs-dan\n")
    _put_state(subject, sandbox, waiting_on_dan={
        "8": {"task_id": "P14", "kind": "awaiting-verification", "session_id": "s2"}})
    subject._process_reject("8")
    assert _get_state(subject, sandbox)["parked_tasks"] == ["P14"]


def test_process_reject_of_a_manual_action_raises_when_the_roadmap_is_missing(
    subject, sandbox, notifier, prep
):
    """`get_task_by_id` reads ROADMAP.md unguarded on the async-job check, so the reject
    blows up *after* the waiting entry has already been dropped from the in-memory state
    but *before* it is written back."""
    assert not _roadmap(subject, sandbox).exists()
    _put_state(subject, sandbox, waiting_on_dan={
        "7": {"task_id": "P12C", "kind": "manual-action", "session_id": "s1"}})
    with pytest.raises(FileNotFoundError):
        subject._process_reject("7")
    assert list(_get_state(subject, sandbox)["waiting_on_dan"]) == ["7"]


def test_process_reject_of_a_normal_parked_task_raises_a_name_error_on_repo_root(
    subject, sandbox, notifier, tmp_path
):
    """`_process_reject` cleans up with `cwd=REPO_ROOT`, a global that is never defined
    anywhere in the reference module. Every non-manual-action rejection therefore raises
    NameError — after notifying Dan, journalling the rejection, dropping the waiting entry
    and persisting the new state, so the branch is never actually deleted."""
    parked = _parked_entry(tmp_path)
    _put_state(subject, sandbox, waiting_on_dan={"7": parked})
    workspace = _workspaces(subject, sandbox) / "s1"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "PAUSED").write_text("", encoding="utf-8")

    with pytest.raises(NameError):
        subject._process_reject("7")

    assert notifier == [f"{CROSS} Rejected P8B2 — discarding branch `task/P8B2`."]
    assert [e["event"] for e in _journal_events(subject, sandbox)] == ["hitl_rejected"]
    assert _get_state(subject, sandbox)["waiting_on_dan"] == {}
    assert not (workspace / "PAUSED").exists()


def test_reject_of_a_normal_parked_task_is_swallowed_by_the_router(
    subject, sandbox, control, tmp_path
):
    parked = _parked_entry(tmp_path)
    _put_state(subject, sandbox, waiting_on_dan={"7": parked})
    control.deliver(_update("/reject 7"))
    assert control.sent == [f"{CROSS} Rejected P8B2 — discarding branch `task/P8B2`."]
    assert _get_state(subject, sandbox)["waiting_on_dan"] == {}


# =======================================================================================
# run_status
# =======================================================================================


def test_run_status_runs_the_status_script_in_the_repo_without_capturing_its_output(
    subject, sandbox, monkeypatch
):
    """Legacy: a real subprocess, argv and cwd pinned. Maestro: R13 (`maestro/status.py`)
    moved the report in-process — there is no more `STATUS_SCRIPT` to shell out to, so
    the pinned contract becomes "no subprocess call, `print_status()` runs instead"."""
    fake = _fake_subprocess(monkeypatch, subject, lambda: "")
    calls = []
    monkeypatch.setattr(subject, "print_status", lambda: calls.append(True))
    subject.run_status()
    assert calls == [True]
    assert fake.calls == []
    return

    fake = _fake_subprocess(monkeypatch, subject, lambda: "")
    subject.run_status()
    (args, kwargs), = fake.calls
    assert args[0] == [
        str(_path(subject, "VENV_PYTHON", sandbox.repo / ".venv" / "bin" / "python3")),
        str(_path(subject, "STATUS_SCRIPT", sandbox.repo / "scripts" / "orchestrator_status.py")),
    ]
    assert kwargs == {"cwd": str(_path(subject, "REPO", sandbox.repo))}


def test_run_status_does_not_shield_the_caller_from_a_launch_failure(
    subject, sandbox, monkeypatch
):
    """Legacy: `subprocess.run` itself raising (e.g. a missing interpreter) is not
    caught. Maestro: R13 removed the interpreter launch entirely, but the same
    "not caught" contract holds for whatever `maestro.status.print_status` raises —
    `run_status()` has no try/except of its own."""
    def boom():
        raise FileNotFoundError("no interpreter")

    monkeypatch.setattr(subject, "print_status", boom)
    with pytest.raises(FileNotFoundError):
        subject.run_status()
    return

    def boom(*args, **kwargs):
        raise FileNotFoundError("no interpreter")

    monkeypatch.setattr(subject, "subprocess", SimpleNamespace(
        run=boom, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(FileNotFoundError):
        subject.run_status()
