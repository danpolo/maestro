"""The Telegram control plane: the command router and the handlers it dispatches to.

Originally extracted from the reference orchestrator. Names owned by
modules that are extracted later (the parking/finalize helpers, the self-fix runner and the
in-flight bookkeeping) are bound to `deferred()` late bindings, and the prepared-action
sidecar is imported as a module, so the call sites stay byte-for-byte identical.
Behavioural surprises are catalogued in `docs/found_bugs_inbox/commands.md` and pinned by
`tests/characterization/test_commands.py`; retained legacy behavior includes the
`REPO_ROOT` NameError on the `/reject` cleanup path, which is copied across as-is.

**M2 adds the one deliberate divergence from the reference here:** the `/backend` verb
(`docs/DESIGN.md` §7's manual switch trigger), its line in the `/help` sheet — the only
place commands are advertised — and the backend segment `/progress` renders for an
in-flight entry that carries one. An entry written before that key existed renders exactly
as it did. The characterisation tests pin both sides: the reference's text for the legacy
subject, this text for the maestro one.

P13 W1 uses a handler-backed command registry for dispatch/help/export. Fetches read the
durable local mailbox delivered by `shared_bot`; no project process calls `getUpdates`.
Replies use the tagged shared transport. Tests replace outbound network boundaries.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from maestro import bootstrap
from maestro import prep_actions
from maestro.backends.registry import known_backends, normalise_name
from maestro.control.store import enqueue_external_command
from maestro.docs.roadmap import (
    _load_roadmap_tasks,
    get_completed_task_ids,
    get_task_by_id,
    mark_roadmap_complete,
    run_dep_map,
)
from maestro.hitl.telegram import (
    _handle_danreq_callback,
    ack_danreq_callback,
    _route_freetext_answer,
    notify_telegram,
    notify_intake,
    phase_report,
    send_report,
)
from maestro.hitl import verify, graph_ask
from maestro.hitl.command_registry import Command, help_text, split_command
from maestro.hitl.shared_bot import settle_update
from maestro.implementer import _answer_choice, _question_req_id, _task_questions
from maestro import metrics
from maestro.merge import _touches_bot_files, merge_and_eval, run_canary_deploy
from maestro.paths import Paths
from maestro.pending import deferred
from maestro.quota import _elapsed_min, _elapsed_str, _fmt_min, _parse_est_minutes
from maestro.state import (
    append_journal,
    control_store,
    now_iso,
    read_json,
    read_state,
    write_state,
)
from maestro.status import graph_runs, print_status
from maestro.switch import REASON_MANUAL, switch_task
from maestro.worktree import TMUX_SESSION, _tail_tmux_pane, remove_worktree

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
WORKSPACES            = _PATHS.workspaces
HALT_FILE             = REPO / ".orchestrator" / "HALT"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
# `CANARY_DEPLOY` used to live here; `run_canary_deploy` (maestro.merge) now owns the
# path and the absent-is-a-skip guard, shared with `orchestrator.py`'s identical call.
DIAGNOSES_DIR         = REPO / ".orchestrator" / "diagnoses"
REDO_DIR              = REPO / ".orchestrator" / "redo"

# The B14 auto-prep sidecar. The reference imports `prepared_actions` as a sibling module
# off `scripts/`; its maestro home is `maestro.prep_actions` (M4a), imported at the top.
# Each name below is owned by a module extracted *after* this one, so a top-level import
# would invert an edge those modules already depend on. `deferred` resolves each on first
# call instead — they remain ordinary module attributes, monkeypatchable as before.
_finalize_manual_action = deferred("_finalize_manual_action", "maestro.parking")
park_regression = deferred("park_regression", "maestro.parking")
attempt_self_fix = deferred("attempt_self_fix", "maestro.selfheal.selffix")
_remove_from_state = deferred("_remove_from_state", "maestro.orchestrator")


def run_status() -> None:
    """R13: `maestro.status.print_status()` called in-process instead of shelling out to
    the now-retired `orchestrator_status.py` script. See `maestro/status.py`."""
    print_status()


# ── B6: Telegram control-plane ──

_getUpdates_offset: int = 0

# ── the update listener (Dan's DuetFlow pilot feedback, 2026-09-26) ──
# A button tap sat with Telegram's spinner, its keyboard still live, until the controller's
# next poll (`POLL_INTERVAL` plus however long the tick took) fetched it. When the listener
# runs it reads the local mailbox, acknowledges and closes a tap on
# arrival (`telegram.ack_danreq_callback`), and queues every update for
# `poll_control_commands`, which the controller still calls each tick. Recording the answer
# and every command stay on the controller thread — the control store is single-writer.

#: The listener's hand-off to the controller thread; `None` while no listener runs, and
#: `poll_control_commands` then reads the local mailbox itself.
_LISTENER_QUEUE: "queue.Queue | None" = None
_LISTENER_THREAD: "threading.Thread | None" = None
#: Retained fetch argument for injected listeners; local reads do not long-poll.
LISTENER_LONG_POLL_SEC = 25
#: Back-off after a failed fetch, so a dead network does not become a busy loop.
LISTENER_RETRY_SEC = 5


def _fetch_updates(offset: int, long_poll: int = LISTENER_LONG_POLL_SEC,
                   limit: int = 10) -> list[dict]:
    """Read this project's durable mailbox. Project processes never call getUpdates."""
    from maestro.hitl.shared_bot import local_updates
    return local_updates(REPO, offset, limit=limit)

def _listen_once(fetch, out: "queue.Queue") -> None:
    """Fetch one batch; close taps at once and queue what the controller must process.

    A tap from another chat is dropped, as `poll_control_commands` drops it. A tap that
    names no answerable request was already answered with its error toast and is not
    queued. If acknowledging raises, the tap is queued un-acked and the controller does
    the whole of it, as it did before the listener existed. `/help` is read-only, so it
    is answered here too and marked `_answered`; the controller still records it.
    """
    global _getUpdates_offset
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
    for upd in fetch(_getUpdates_offset):
        _getUpdates_offset = max(_getUpdates_offset, int(upd["update_id"]) + 1)
        cq = upd.get("callback_query")
        if cq:
            if str((cq.get("message") or {}).get("chat", {}).get("id", "")) != str(chat_id):
                continue
            try:
                from maestro.hitl import shared_bot, telegram
                if shared_bot.sprint_notice_token(cq.get('data')):
                    # Spinner only; the controller alone validates and applies skips.
                    telegram._tg_api('answerCallbackQuery', {
                        'callback_query_id': cq.get('id', ''), 'text': 'Queued for the controller'})
                elif not ack_danreq_callback(cq):
                    settle_update(REPO, upd['update_id'])
                    continue
                cq["_acked"] = True
            except Exception:  # noqa: BLE001 — the controller handles it in full
                pass
        else:
            msg = upd.get("message") or upd.get("channel_post") or {}
            if (str(msg.get("chat", {}).get("id", "")) == str(chat_id)
                    and (msg.get("text") or "").strip().lower() in HELP_COMMANDS):
                notify_telegram(HELP_TEXT)
                upd["_answered"] = True
        out.put(upd)


def start_update_listener() -> bool:
    """Start the listener thread once per process; returns whether one is running.

    Not started without a configured bot, or with `MAESTRO_TELEGRAM_LISTENER=0` (then
    `poll_control_commands` reads the mailbox itself). A daemon thread: it dies with the loop.
    """
    global _LISTENER_QUEUE, _LISTENER_THREAD
    if _LISTENER_THREAD is not None and _LISTENER_THREAD.is_alive():
        return True
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_ALERT_CHAT_ID")):
        return False
    if os.environ.get("MAESTRO_TELEGRAM_LISTENER", "1").strip() == "0":
        return False
    out: queue.Queue = queue.Queue()

    def run() -> None:
        while True:
            try:
                _listen_once(_fetch_updates, out)
                time.sleep(0.5)
            except Exception:  # noqa: BLE001 — never let the listener die
                time.sleep(LISTENER_RETRY_SEC)

    thread = threading.Thread(target=run, name="maestro-telegram-updates", daemon=True)
    _LISTENER_QUEUE, _LISTENER_THREAD = out, thread
    thread.start()
    return True


def _drain_listener() -> list[dict]:
    out: list[dict] = []
    while _LISTENER_QUEUE is not None:
        try:
            out.append(_LISTENER_QUEUE.get_nowait())
        except queue.Empty:
            break
    return out


def _build_progress_report() -> str:
    """Per in-flight task: elapsed, human est_time (+ rough ETA hint), live activity.
    Prefers a workspace progress.json `milestone`/`pct` if the implementer wrote one,
    else tails the tmux pane. No implementer cooperation required."""
    state = read_state()
    in_f  = state.get("in_flight", [])
    runs  = graph_runs(state)
    if not in_f and not runs:
        return "📭 No tasks in flight."
    blocks = ["📈 Task progress"]
    # Read the live graph from the control plane; a stale state-only projection can say
    # "between steps" while admission is waiting for a route.
    for r in runs:
        tid = r["task_id"]
        from maestro import status
        try:
            view = status.workflow_view(tid, repo=REPO, run_id=r.get("run_id"))
        except status.WorkflowViewError:
            view = None
        if view is not None:
            blocks.append(status.render_graph_progress_text(view).rstrip())
            continue
        header = f"• {tid} (graph) running · elapsed {_elapsed_str(r.get('started_at', ''))}"
        est = str((get_task_by_id(tid) or {}).get("est_time", "")).strip()
        if est:
            header += f" · est {est[:48]}"
        blocks.append(header)
        nodes = r.get("nodes") or []
        blocks.append("    ↳ at " + (", ".join(nodes) if nodes else "between steps")
                      + f" — /workflow {tid}")
    for e in in_f:
        tid     = e.get("task_id", "?")
        role    = e.get("role", "?")
        status  = e.get("status", "?")
        started = e.get("started_at", "")
        elapsed = _elapsed_str(started)
        task_def = get_task_by_id(tid) or {}
        est     = str(task_def.get("est_time", "")).strip()
        # M2: entries launched (or switched) since the backend key exists say which agent
        # is doing the work; an entry written before it — or hand-seeded — renders exactly
        # as it always did, rather than advertising a "None" backend nobody chose.
        backend = str(e.get("backend") or "").strip()
        who = f"{role}/{backend}" if backend else str(role)
        header = f"• {tid} ({who}) {status} · elapsed {elapsed}"
        if est:
            header += f" · est {est[:48]}"
        # Rough ETA when est_time carries a clean leading duration token.
        est_min, el_min = _parse_est_minutes(est), _elapsed_min(started)
        if est_min and el_min is not None:
            rem = est_min - el_min
            header += f" · ~{_fmt_min(rem)} left" if rem > 0 else " · over est"
        blocks.append(header)
        # Activity line: prefer an opt-in progress.json, else the live tmux tail.
        activity = ""
        prog_file = WORKSPACES / e.get("session_id", "") / "progress.json"
        pj = read_json(prog_file) if prog_file.exists() else {}
        if pj.get("milestone") or pj.get("pct") is not None:
            pct = f"{pj['pct']}% " if pj.get("pct") is not None else ""
            activity = f"{pct}{pj.get('milestone', '')}".strip()
        else:
            activity = _tail_tmux_pane(e.get("window", ""))
        if activity:
            blocks.append("    ↳ " + activity.replace("\n", "\n      "))
    return "\n".join(blocks)


#: Returned by `_ingest_control_command` for an update the inbox has already settled.
_ALREADY_PROCESSED = object()


def _ingest_control_command(update_id: int, text_raw: str):
    """Record one Telegram verb in the command inbox before executing it.

    `[INV-01]`: Telegram is an external actor and its verbs are commands, not writes.
    Recording them makes the control plane auditable, and the idempotency key — the
    Telegram `update_id`, which is unique per delivery and stable across redeliveries —
    makes the whole dispatch idempotent: a redelivered update returns the settled row and
    is skipped, so `/approve` cannot run twice because the network hiccuped.

    Returns a command id to settle afterwards, `_ALREADY_PROCESSED` for a duplicate, or
    None when the project has no control database and nothing is recorded.
    """
    store = control_store()
    if store is None:
        return None
    payload = {"update_id": update_id, "text": text_raw}
    name, action = split_command(text_raw)
    if name == 'sprint' and action in ('on', 'off', 'stop'):
        from maestro import sprint
        from maestro.hitl.shared_bot import sprint_control_scope
        prepared = sprint.control(read_state().get('sprint', {}), action, now_epoch=time.time())
        payload['sprint_control'] = {
            'fields': {key: prepared[key] for key in sprint.CONTROL_FIELDS if key in prepared},
            'scope': sprint_control_scope(REPO),
        }
    command = store.enqueue(
        "telegram_control",
        payload,
        idempotency_key=f"telegram:update:{update_id}",
    )
    if command.status == 'processing' and command.payload.get('sprint_control'):
        return command.command_id
    if command.status != "received":
        return _ALREADY_PROCESSED
    store.claim_command(command.command_id)
    return command.command_id


def _settle_control_command(command_id) -> None:
    """Mark a dispatched verb committed.

    Only reached when the dispatch returned normally. A verb that raised stays
    `processing` and is never re-executed — failing closed, because most of these verbs
    (`/approve`, `/redo`, `/backend`) have external side effects with no postcondition
    probe, and `[A05]` forbids retrying those automatically.
    """
    if command_id is None or command_id is _ALREADY_PROCESSED:
        return
    store = control_store()
    if store is not None:
        store.settle_command(command_id, "committed")


#: The control sheet — the one place commands are advertised. The loop sends it for
#: `/help`, and so does the halted watchdog (`answer_while_halted`), under a header.
HELP_COMMANDS = ("/help", "/commands", "/start")

def _status_text() -> str:
    """The `/status` reply: read-only, so the halted watchdog sends it too."""
    state = read_state()
    # The graph runner's live runs (G4) are live work too; they are kept in
    # their own key because `in_flight` rows are legacy tmux sessions.
    in_f  = state.get("in_flight", []) + graph_runs(state)
    phase = state.get("phase", {}).get("id", "?")
    parked = state.get("parked_tasks", [])
    asking = verify.question_waiting_lines(state)
    return (
        f"🤖 Orchestrator status\n"
        f"Phase: {phase} | In-flight: {len(in_f)}\n"
        f"Tasks: {', '.join(e.get('task_id','?') for e in in_f) or 'none'}\n"
        f"Halted: {state.get('halted',False)} | Paused: {state.get('paused_by_user',False)}"
        + (f"\nParked: {', '.join(parked)}" if parked else "")
        + (f"\n❓ Waiting for your decision:\n" + "\n".join(asking) if asking else "")
    )


def poll_control_commands(in_flight: list) -> None:
    """Dispatch durable local deliveries through the registered handlers on the controller."""
    global _getUpdates_offset
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
    if not token or not chat_id:
        return

    try:
        listened = _LISTENER_QUEUE is not None
        if listened:
            # The local listener queued these for us; it owns no network poll.
            results = _drain_listener()
        else:
            results = _fetch_updates(0, 0, limit=100)

        for upd in results:
            if not listened:
                _getUpdates_offset = upd["update_id"] + 1
            try:
                _dispatch_delivery(upd, in_flight, chat_id)
            except Exception:
                # Preserve this delivery for the next controller pass while continuing
                # the batch. Processing commands remain fail-closed by inbox idempotency;
                # commands not yet claimed can be read again. Never strand later updates.
                if listened:
                    _LISTENER_QUEUE.put(upd)

    except Exception:
        pass  # Never let control-plane errors crash the main loop


def _dispatch_delivery(upd: dict, in_flight: list, chat_id: str) -> None:
    cq = upd.get('callback_query')
    if cq:
        cq_chat = str((cq.get('message') or {}).get('chat', {}).get('id', ''))
        if cq_chat == str(chat_id):
            from maestro.hitl.shared_bot import sprint_notice_token
            if sprint_notice_token(cq.get('data')):
                _dispatch_sprint_callback(upd)
            else:
                _handle_danreq_callback(cq)
    else:
        msg = upd.get('message') or upd.get('channel_post') or {}
        if str(msg.get('chat', {}).get('id', '')) == str(chat_id):
            text_raw = (msg.get('text') or '').strip()
            command_id = _ingest_control_command(upd['update_id'], text_raw)
            if command_id is not _ALREADY_PROCESSED:
                command = control_store().command(command_id) if command_id else None
                if command and command.payload.get('sprint_control'):
                    _control_sprint(split_command(command.payload['text'])[1], in_flight,
                                    prepared=command.payload['sprint_control'],
                                    update_id=command.payload['update_id'])
                else:
                    _dispatch_control_text(text_raw, upd, msg, in_flight)
                _settle_control_command(command_id)
    settle_update(REPO, upd['update_id'])


def _dispatch_sprint_callback(upd):
    from maestro import sprint
    from maestro.hitl import shared_bot, telegram
    store = control_store()
    if store is None:
        return
    cq = upd['callback_query']
    command = store.enqueue('telegram_control', {
        'update_id': upd['update_id'], 'sprint_callback': {
            key: value for key, value in cq.items() if key != '_acked'},
    }, idempotency_key=f"telegram:update:{upd['update_id']}")
    if command.status not in ('received', 'processing'):
        return
    if command.status == 'received':
        store.claim_command(command.command_id)
    original = command.payload.get('sprint_callback')
    if original is None:
        return  # An update identity cannot change command kind on replay.
    token = shared_bot.sprint_notice_token(original.get('data'))
    document = read_state()
    record = document.get('sprint', {}).get('notices', {}).get(token)
    error = ''
    try:
        shared_bot._require_shared_message(original.get('message') or {})
        if not shared_bot.sprint_notice_matches(record, original, REPO):
            raise ValueError('Sprint button does not match this delivered project message')
        current = document.get('sprint', {})
        scope = '|'.join(shared_bot.sprint_control_scope(REPO))
        pool = record['pool_id']
        receipt = max(current.get('control_receipts', {}).get(scope, -1),
            current.get('pool_control_receipts', {}).get(scope, {}).get(pool, -1))
        if receipt >= command.payload['update_id']:
            updated, error = current, 'A later sprint control already superseded this button'
        else:
            updated, error = sprint.skip_notice(current, token, now_epoch=time.time())
        if not error and updated != document.get('sprint', {}):
            updated.setdefault('pool_control_receipts', {}).setdefault(scope, {})[pool] = command.payload['update_id']
            document['sprint'] = updated
            write_state(document)
    except ValueError as exc:
        error = str(exc)
    _settle_control_command(command.command_id)
    telegram._tg_api('answerCallbackQuery', {'callback_query_id': cq.get('id', ''),
        'text': error or 'Skipped this week'})
    if not error:
        message = original.get('message') or {}
        telegram._tg_api('editMessageReplyMarkup', {'chat_id': record['chat_id'],
            'message_id': message.get('message_id'),
            'reply_markup': json.dumps({'inline_keyboard': []})})


# ── while the loop is not running (Dan's `/help` to a halted DuetFlow loop, 2026-09-26) ──
# Only the loop read Telegram, so a HALT left every command unanswered. The watchdog stays
# up through a HALT and calls this, only once it has confirmed the loop is not running —
# two project consumers must not execute the same mailbox delivery. It answers what is
# read-only; anything else is the controller's, so it stays in the mailbox for the restarted
# loop to fetch: the offset never passes it. It is also recorded `received` in the command
# inbox under the key the loop uses, which the loop then claims and runs as usual.

HALTED_HEADER = ("⏸ The loop is halted. /help and /status are answered now; any other "
                 "command is queued and runs when the loop restarts.")
_HALTED_OFFSET: int = 0
#: Updates already replied to. Later updates are re-fetched every pass while an earlier
#: one waits for the loop, so each is answered once per watchdog process.
_HALTED_REPLIED: set = set()


def _journal_halted_answer(text: str) -> None:
    """The halted watchdog's replies leave a trace; the loop's own go to the command inbox."""
    try:
        append_journal("halted_command_answered", text)
    except Exception:
        pass


def answer_while_halted(fetch=None) -> None:
    """One local-mailbox pass on behalf of a loop that is not running."""
    global _HALTED_OFFSET
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
    if fetch is None:
        fetch = lambda offset: _fetch_updates(offset, limit=100)  # noqa: E731
    consume = True
    for upd in fetch(_HALTED_OFFSET):
        uid = int(upd["update_id"])
        msg = upd.get("message") or upd.get("channel_post") or {}
        text_raw = (msg.get("text") or "").strip()
        text = text_raw.lower()
        ours = str(msg.get("chat", {}).get("id", "")) == str(chat_id)
        first = uid not in _HALTED_REPLIED
        _HALTED_REPLIED.add(uid)
        if upd.get("callback_query"):
            consume = False               # a tap is recorded by the loop
        elif not ours or not text_raw:
            pass                          # the loop would drop it too
        elif text in HELP_COMMANDS:
            if first:
                notify_telegram(HALTED_HEADER + "\n\n" + HELP_TEXT)
                _journal_halted_answer(text)
        elif text == "/status":
            if first:
                notify_telegram("⏸ The loop is halted (not running).\n" + _status_text())
                _journal_halted_answer(text)
        elif text.split(maxsplit=1)[0] in ('/report', '/reports', '/intake'):
            if first:
                from maestro import intake
                intake.handle(REPO, text_raw, uid, notify_intake)
        else:
            consume = False
            if first:
                enqueue_external_command(
                    "telegram_control", {"update_id": uid, "text": text_raw},
                    idempotency_key=f"telegram:update:{uid}", repo=REPO,
                )
                notify_telegram(f"⏸ The loop is halted — queued: {text_raw[:80]}\n"
                                "It runs when the loop restarts.")
        if consume:
            _HALTED_OFFSET = uid + 1
            settle_update(REPO, uid)


#: G7 item 2: `state.json` key the graph runner reads to open a fresh run for a task that
#: already has a settled one — `task_id -> when it was asked for`. Only the operator (or
#: a Retry on the failed-run notice) writes it; the loop pops it once the run opens.
GRAPH_REOPEN_KEY = "graph_reopen"


def _graph_runner_active() -> bool:
    """Whether `project.yaml` selects the graph runner (`engineering.runner: graph`)."""
    try:
        from maestro import config

        return config.engineering_runner() == "graph"
    except Exception:  # noqa: BLE001 — an unreadable selector is the default, legacy
        return False


def request_graph_reopen(task_id: str, state: dict) -> None:
    """Mark `task_id` for a fresh graph run (the caller writes `state`)."""
    reopen = dict(state.get(GRAPH_REOPEN_KEY) or {})
    reopen[task_id] = now_iso()
    state[GRAPH_REOPEN_KEY] = reopen


def _graph_answer(verb: str, arg: str) -> bool:
    """G7 item 3: `/approve`, `/reject` and `/redo` under the graph runner. Returns whether
    it answered (the caller falls back to the legacy verb when not).

    A legacy `waiting_on_dan` id that exists keeps its legacy meaning. Otherwise the verb
    answers the graph request waiting on the task (`verify.answer_waiting`: Approve/Done,
    Reject/Abandon, Redo). With nothing waiting, `/approve` and `/reject` say the graph has
    no merge-review queue by id; `/redo` falls back to legacy's own "no parked task".
    """
    if not _graph_runner_active():
        return False
    parts = arg.split(None, 1)
    if not parts:
        return False
    task_id, reason = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    if verb in ("approve", "reject") and task_id in (read_state().get("waiting_on_dan") or {}):
        return False
    if verb == "redo" and not reason:
        notify_telegram(f"Usage: /redo {task_id} <what the prep got wrong>")
        return True
    reply = verify.answer_waiting(task_id, verb, reason)
    if reply is not None:
        notify_telegram(reply)
        return True
    if verb == "redo":
        return False
    notify_telegram(
        f"ℹ Nothing is waiting on your {verb} for {task_id}. Under the graph runner there "
        "is no merge-review queue by id: a task that needs you waits as a request — "
        "/waiting lists them, /verify <task> answers one."
    )
    return True


def _graph_fix_reply(task_id: str) -> str:
    """G7 item 3: `/fix` is replaced under the graph runner (no diagnosis file is written)."""
    return (
        f"ℹ /fix does not apply under the graph runner: a failed node's self-heal is the "
        f"diagnose step, plan repair and the failure ladder, and no diagnosis is saved for "
        f"/fix to implement. /explain {task_id} says why it failed; /unpark {task_id} "
        f"gives it a fresh run on its kept branch."
    )


def _process_unpark(task_id: str) -> None:
    """`/unpark <task_id>` — let a failed task run again.

    Legacy: drop it from `parked_tasks` (and its retry count) so the launch loop picks it
    up. Under the graph runner a task with a settled run is never reopened by guessing, so
    this also writes the explicit reopen marker (`GRAPH_REOPEN_KEY`), even when the task
    was not in `parked_tasks` (a failed graph run parks nothing there).
    """
    state = read_state()
    parked = state.get("parked_tasks", [])
    graph = _graph_runner_active()
    was_parked = task_id in parked
    if not was_parked and not graph:
        notify_telegram(f"ℹ {task_id} is not parked. Currently parked: {', '.join(parked) or 'none'}")
        return
    if was_parked:
        parked.remove(task_id)
        state["parked_tasks"] = parked
        # Also reset retry count so the task gets a fresh attempt
        state.get("retry_counts", {}).pop(task_id, None)
    if graph:
        request_graph_reopen(task_id, state)
    write_state(state)
    append_journal("control_unpark", f"{task_id} unparked via Telegram"
                   + (" (graph reopen requested)" if graph else ""))
    if graph:
        notify_telegram(
            f"✅ {task_id} will get a fresh graph run on the next cycle (its failed run is "
            f"kept). The new run continues on the kept branch impl/{task_id}."
        )
    else:
        notify_telegram(f"✅ {task_id} unparked — will be launched on next cycle.")


#: Where `/backend <name>` records the operator's choice for the *next* launch. It sits
#: beside `hitl_mode` and `paused_by_user` because it is the same kind of thing: a runtime
#: override of configured behaviour, owned by the state document rather than by
#: `project.yaml`, so an operator decision made at 3am is not a config edit and does not
#: outlive the run's state.
BACKEND_KEY = "backend"

#: The journal event a `/backend <name>` records, in the `control_*` family the other
#: Telegram control verbs already use.
BACKEND_EVENT = "control_backend"

#: `/backend auto` — the clear verb (C4/G7). A pin set with `/backend <name>` (during an
#: outage, say) otherwise has no way back: it outlives the reason it was set for and
#: freezes every future launch onto that one backend forever. `"auto"` is deliberately
#: not a registered backend name — `known_backends()` must never grow an entry that
#: collides with it — so it is matched *before* the known-backend check rather than
#: added to the registry as a fake driver.
BACKEND_AUTO = "auto"


def _in_flight_entry(in_flight: list, task_id: str) -> dict | None:
    """The in-flight entry for `task_id`, matched exactly first.

    Task ids are case-sensitive, which is why `/backend` reads its argument off `text_raw`
    rather than off the lowercased `text`. The case-insensitive second pass is a
    convenience for an id typed by hand on a phone keyboard, and it runs *second* so an
    exactly-matching id can never lose to one that only matches when folded.
    """
    wanted = str(task_id).strip()
    for entry in in_flight:
        if str(entry.get("task_id", "")).strip() == wanted:
            return entry
    folded = wanted.lower()
    for entry in in_flight:
        if str(entry.get("task_id", "")).strip().lower() == folded:
            return entry
    return None


def _process_backend(arg: str) -> None:
    """`/backend <name> [task_id]` — the manual switch trigger (D2, DESIGN.md §7).

    With a name alone, the choice is recorded in the state document for future launches.
    With a task id after it, that one in-flight task is handed over *now*, through the
    single switch path in `maestro.switch`: the outgoing agent is asked to checkpoint, and
    the incoming one is launched into the same worktree with the uncommitted work still
    in it. `/backend auto` (C4/G7) is the way back out of a pin: it clears the recorded
    choice so a fresh launch resolves through `maestro.roles` again instead of being stuck
    on whatever name was last set — trailing words after `auto` are ignored the same way a
    trailing task id is ignored after a real name with nothing to switch.

    Every failure mode — no argument, an unknown backend, an unknown task id, a task
    already on that backend, a switch that could not happen — is answered with a message
    and returns. Nothing here raises: this runs inside the router's blanket
    `except Exception: pass`, where an exception would not just lose this command, it
    would silently drop the rest of the poll batch with it.
    """
    known = ", ".join(known_backends())
    parts = arg.split()
    if not parts:
        notify_telegram(
            f"Usage: /backend <name> [task_id]\n"
            f"Known backends: {known}\n"
            f"/backend <name> — new launches use it; "
            f"/backend <name> <task_id> — move that task now; "
            f"/backend auto — clear a pinned choice."
        )
        return

    name = normalise_name(parts[0])
    if name == BACKEND_AUTO:
        try:
            state = read_state()
            state.pop(BACKEND_KEY, None)
            write_state(state)
            append_journal(BACKEND_EVENT, "auto — pin cleared via Telegram /backend")
        except Exception as exc:
            notify_telegram(f"⚠ Could not clear the backend choice: {exc}")
            return
        notify_telegram(
            "✅ Backend pin cleared — new launches resolve through the configured role "
            "again."
        )
        return

    if name not in known_backends():
        notify_telegram(f"ℹ Unknown backend `{parts[0]}`. Known backends: {known}.")
        return

    if _graph_runner_active():
        # G7 item 3: graph routing never reads the pin (`state["backend"]`), and there is no
        # in-flight session to move — each attempt is bound when it is claimed.
        if len(parts) == 1:
            notify_telegram(
                f"ℹ Under the graph runner routing chooses each attempt's backend "
                f"(project.yaml `routing` and the capability catalog), so a /backend pin "
                f"would do nothing — {name} was not pinned. Pools that fail are paused and "
                f"routed around (fallback / step-down) automatically."
            )
        else:
            notify_telegram(
                f"ℹ Under the graph runner there is no in-flight session to move: each "
                f"attempt of {parts[1]} is bound to a backend when it is claimed, by routing "
                f"(project.yaml `routing` and the capability catalog)."
            )
        return

    if len(parts) == 1:
        try:
            state = read_state()
            state[BACKEND_KEY] = name
            write_state(state)
            append_journal(BACKEND_EVENT, f"{name} selected via Telegram /backend")
        except Exception as exc:
            notify_telegram(f"⚠ Could not record the backend choice: {exc}")
            return
        notify_telegram(
            f"✅ Backend set to {name} — new task launches will use it. Tasks already in "
            f"flight keep the backend they started on; /backend {name} <task_id> moves one."
        )
        return

    requested = parts[1]
    try:
        state = read_state()
    except Exception as exc:
        notify_telegram(f"⚠ Could not read the orchestrator state: {exc}")
        return
    in_flight = [e for e in state.get("in_flight", []) if isinstance(e, dict)]
    entry = _in_flight_entry(in_flight, requested)
    if entry is None:
        ids = ", ".join(str(e.get("task_id", "?")) for e in in_flight) or "none"
        notify_telegram(f"ℹ No task `{requested}` is in flight. In flight: {ids}.")
        return

    task_id = str(entry.get("task_id") or requested)
    if normalise_name(entry.get(BACKEND_KEY)) == name:
        notify_telegram(f"ℹ {task_id} is already running on {name}. Nothing to do.")
        return

    try:
        outcome = switch_task(task_id, reason=REASON_MANUAL, entry=entry, to_backend=name)
    except Exception as exc:
        notify_telegram(f"⚠ Could not move {task_id} to {name}: {exc}")
        return
    if outcome.switched:
        notify_telegram(
            f"✅ {task_id} is now on {outcome.to_backend} — same worktree, uncommitted "
            f"work kept. New session {outcome.new_session_id}."
        )
    else:
        notify_telegram(
            f"⚠ {task_id} stayed on {outcome.from_backend or 'its current backend'}: "
            f"{outcome.note or 'the switch did not happen'}."
        )


def _process_fix(task_id: str) -> None:
    """`/fix <id>`: Dan approves a persisted diagnosis → run the self-fix runner for it.
    Dan's approval substitutes for the failure-class whitelist + rate-limit, but the
    runner's path-confinement (scripts/ orchestrator/ docs/ minus deny-list) + byte-compile
    + ≥1-commit gates still apply, and the orchestrator re-checks paths at merge."""
    f = DIAGNOSES_DIR / f"{task_id}.json"
    if not f.exists():
        notify_telegram(f"No pending diagnosis for `{task_id}`. (Diagnoses are saved when a "
                        f"task fails; check /status for parked tasks.)")
        return
    try:
        diag = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        notify_telegram(f"⚠ Could not read the saved diagnosis for `{task_id}`: {exc}")
        return
    reason = diag.get("reason", "")
    append_journal("fix_approved", f"{task_id} class={diag.get('failure_class','')} via /fix")
    notify_telegram(f"🔧 Approved — implementing the diagnosed fix for `{task_id}` now. "
                    f"I'll prepare it in a confined worktree, gate-check, and merge.")
    attempt_self_fix(task_id, reason, diag)
    f.unlink(missing_ok=True)


# `_session_uuid_for` is mapped to `maestro.parking`, which is extracted after this module.
# `_handle_ask` calls it on the path the characterisation tests exercise without stubbing
# it, so a `pending()` placeholder would make that path untestable. The body is copied
# verbatim here; when `maestro.parking` lands, one of the two copies becomes an import of
# the other.
def _session_uuid_for(session_id: str) -> str:
    """The resumable Claude session UUID for a launched implementer (written by
    launch_implementer to session_uuid.txt). Empty for legacy/hand-seeded items —
    /ask falls back to brief-seeded context in that case."""
    if not session_id:
        return ""
    try:
        return (WORKSPACES / session_id / "session_uuid.txt").read_text(
            encoding="utf-8").strip()
    except Exception:
        return ""


def _process_approve(dan_id_str: str, in_flight: list) -> None:
    """Merge a HITL-parked task after Dan's approval."""
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    parked  = waiting.get(dan_id_str)
    if not parked:
        notify_telegram(f"No pending item with ID {dan_id_str}.")
        return
    # B14 auto-prep: a manual-action item's prep branch is already merged — Dan just
    # performed his one action. Finalize differently (post-action + gate + graduate).
    if parked.get("kind") == "manual-action":
        _finalize_manual_action(dan_id_str, parked, in_flight)
        return
    if parked.get('kind') in ('operator-verification', 'post-merge-checking',
                              'post-merge-repair', 'post-merge-failed', 'verification-drift',
                              'operator-failed'):
        notify_telegram('This task is already merged. Answer the observation request with '
                        'Pass / Fail using /verify; deployed failures are repaired separately.')
        return
    task_id = parked["task_id"]
    entry: dict = {
        "session_id": parked["session_id"],
        "task_id":    task_id,
        "branch":     parked["branch"],
        "worktree":   parked["worktree"],
        "window":     f"impl-{task_id}",
    }
    notify_telegram(f"✅ Approved {task_id} — merging now…")
    append_journal("hitl_approved", f"{task_id} dan_id={dan_id_str}")
    del waiting[dan_id_str]
    state["waiting_on_dan"] = waiting
    write_state(state)
    workspace = WORKSPACES / parked["session_id"]
    (workspace / "PAUSED").unlink(missing_ok=True)

    # Merge + full post-merge finalize. KEEP IN SYNC with the autonomous
    # completion block in the main loop ("Handle completions"): an /approve must
    # finalize exactly like an auto-merge or the task is left stuck in_flight,
    # unpushed, and unmarked in ROADMAP.
    worktree = Path(parked["worktree"])
    accepted, smoke = merge_and_eval(entry)
    shown = metrics.summary(smoke)
    in_flight[:] = [e for e in in_flight if e.get("session_id") != entry["session_id"]]
    _remove_from_state(entry)
    if accepted:
        from maestro.parking import defer_merged_verification
        if defer_merged_verification(entry, smoke, in_flight):
            run_dep_map()
            run_status()
            return
        print(f"  ✓ {task_id} merged and accepted ({shown})")
        append_journal("task_complete", f"{task_id} {shown}",
                       session_id=entry["session_id"])
        remove_worktree(worktree)
        mark_roadmap_complete(task_id)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=str(REPO), capture_output=True)
        branch = entry.get("branch", "")
        if branch and _touches_bot_files(branch):
            if not run_canary_deploy(task_id):
                append_journal("canary_reverted", f"{task_id} canary deploy failed")
                run_dep_map(); run_status()
                return
        phase_report(task_id, get_task_by_id(task_id), smoke)
        # `metrics.summary` already says "no metrics" for a skipped smoke, so the
        # branch that used to spell that out per project is gone with it.
        notify_telegram(f"✅ {task_id} merged & accepted ({shown}).")
    else:
        reason_merge = smoke.get("reason", "")
        print(f"  ✗ {task_id} smoke/merge failed ({shown}) — {reason_merge[:80]}")
        notify_telegram(f"⚠ {task_id} smoke/merge failed ({shown}) — reverted. See journal.")
        park_regression(task_id, smoke)
        remove_worktree(worktree)
    run_dep_map()
    run_status()


def _process_reject(dan_id_str: str) -> None:
    """Discard a HITL-parked task after Dan's rejection."""
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    parked  = waiting.get(dan_id_str)
    if not parked:
        notify_telegram(f"No pending item with ID {dan_id_str}.")
        return
    task_id = parked["task_id"]
    if parked.get('kind') in ('operator-verification', 'post-merge-checking',
                              'post-merge-repair', 'post-merge-failed', 'verification-drift',
                              'operator-failed'):
        notify_telegram('This task has completed its execution. Use /verify to answer '
                        'the observation Pass / Fail; its task hold and evidence are retained.')
        return
    # B14 auto-prep: rejecting a manual-action means "don't graduate". The prep commits
    # already live on main (harmless scripts/data) — just drop the sidecar + parked entry.
    if parked.get("kind") in ("manual-action", "awaiting-verification"):
        del waiting[dan_id_str]
        # An async-job (read-only eval) has no prep branch and is still pending in ROADMAP,
        # so dropping its waiting entry alone would relaunch it next poll. Park it (B11) so
        # rerunning takes an explicit /unpark — rejecting an eval means "keep base, stop".
        if (get_task_by_id(task_id) or {}).get("dispatch") == "async-job":
            _pk = state.get("parked_tasks", [])
            if task_id not in _pk:
                _pk.append(task_id)
                state["parked_tasks"] = _pk
        state["waiting_on_dan"] = waiting
        write_state(state)
        prep_actions.remove_action(task_id)
        (WORKSPACES / parked.get("session_id", "") / "PAUSED").unlink(missing_ok=True)
        notify_telegram(f"❌ Dropped {task_id}'s pending action (prep commits stay on main).")
        append_journal("manual_action_rejected", f"{task_id} dan_id={dan_id_str}")
        return
    branch  = parked["branch"]
    worktree = parked.get("worktree", "")
    notify_telegram(f"❌ Rejected {task_id} — discarding branch `{branch}`.")
    append_journal("hitl_rejected", f"{task_id} dan_id={dan_id_str}")
    del waiting[dan_id_str]
    state["waiting_on_dan"] = waiting
    write_state(state)
    workspace = WORKSPACES / parked["session_id"]
    (workspace / "PAUSED").unlink(missing_ok=True)
    if worktree and Path(worktree).exists():
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                       capture_output=True, cwd=REPO_ROOT)
    subprocess.run(["git", "branch", "-D", branch], capture_output=True, cwd=REPO_ROOT)


# ── Part B: /manual and /detail helpers ──

def _resolve_waiting(idarg: str) -> tuple:
    """Resolve a /ask|/approve-style id (a waiting_on_dan dan_id key OR a task_id) to
    (dan_id, entry). Returns (None, None) if nothing matches."""
    waiting = read_state().get("waiting_on_dan", {})
    if idarg in waiting:
        return idarg, waiting[idarg]
    up = idarg.upper()
    for did, e in waiting.items():
        if str(e.get("task_id", "")).upper() == up:
            return did, e
    return None, None


def _handle_ask(arg: str) -> None:
    """Answer a waiting graph verification/manual request or a legacy manual task.

    Graph support gets the exact pending request and immutable run context in a fresh
    read-only call. Legacy support resumes prep when possible, else reads its brief.
    A named tmux worker posts advice to Telegram without answering the request.
    """
    parts = arg.split(None, 1)
    if len(parts) < 2:
        notify_telegram("Usage: /ask <id> <your message>\n"
                        "Example: /ask P12C cell 5 fails with KeyError 'x'")
        return
    idarg, question = parts[0], parts[1].strip()
    dan_id, entry = _resolve_waiting(idarg)
    graph_payload = None
    if not entry:
        try:
            graph_payload = graph_ask.request_context(REPO, read_state(), idarg)
        except (OSError, ValueError, RuntimeError) as exc:
            notify_telegram(f"⚠ /ask: cannot read the waiting request: {exc}")
            return
        if graph_payload:
            entry = graph_payload
    if not entry:
        notify_telegram(f"ℹ /ask: no parked task matches `{idarg}`. Use /waiting to list them.")
        return
    task_id  = entry.get("task_id", idarg)
    sid      = entry.get("session_id", "")
    uuid_    = entry.get("session_uuid", "") or _session_uuid_for(sid)
    pa       = prep_actions.get_action(task_id) or {}
    action   = pa.get("action") or entry.get("summary", "")
    worktree = entry.get("worktree", "") or ""
    # The implementer's Claude session is keyed to the worktree it built in
    # (~/.claude/projects/<encoded-cwd>/<uuid>.jsonl). That JSONL survives the
    # /tmp worktree being cleaned up — so when we have a resumable uuid but the
    # dir is gone, recreate the empty dir rather than downgrading cwd to REPO
    # (which would make `claude --resume` look in the wrong project dir and fail
    # with "No conversation found"). Only fall back to REPO with no uuid at all.
    if worktree and uuid_ and not Path(worktree).exists():
        try:
            Path(worktree).mkdir(parents=True, exist_ok=True)
        except OSError:
            worktree = ""
    if not worktree or not Path(worktree).exists():
        worktree = str(REPO)
    brief = str(WORKSPACES / sid / "brief.txt") if sid else ""

    ask_dir = REPO / ".orchestrator" / "ask"
    ask_dir.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    req_path  = ask_dir / f"{task_id}-{ts}.json"
    log_path  = ask_dir / f"{task_id}-{ts}.answer.log"
    req_path.write_text(json.dumps({
        "uuid": uuid_, "task": task_id, "dan_id": dan_id or "", "question": question,
        "action": graph_payload["action"] if graph_payload else action,
        "brief": brief, "worktree": worktree, "log": str(log_path),
        **({"graph_request": graph_payload["graph_request"],
            "graph_context": graph_payload["graph_context"]} if graph_payload else {}),
    }), encoding="utf-8")

    window   = f"ask-{task_id}"
    helper   = shlex.join(bootstrap.pinned_module_argv(VENV_PYTHON, "maestro.hitl.ask"))
    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n {window} "
                + shlex.quote(f"cd {REPO} && {helper} {req_path}"))
    try:
        subprocess.run(tmux_cmd, shell=True, check=True)
    except Exception as exc:
        notify_telegram(f"⚠ /ask failed to launch: {exc}")
        return
    how = ("reading the waiting request and run context" if graph_payload else
           "resuming its prep session" if uuid_ else "reading the task brief")
    notify_telegram(f"🤔 Asking Maestro about `{task_id}` ({how})… I'll reply here shortly.")
    append_journal("ask_launched",
                   f"{task_id} dan_id={dan_id} uuid={'y' if uuid_ else 'n'}", session_id=sid)


def _handle_redo(arg: str) -> None:
    """`/redo <id> <what's wrong>` — have Maestro REWRITE + reship the deliverable for a
    parked manual task (vs /ask, which only advises). Spawns scripts/maestro_redo.py in a
    detached tmux window; it resumes the prep session, rewrites the deliverable, and hands
    a gate-passed branch back here to merge. If this project ships notebooks
    (`scripts/check_notebook.py` exists) that also means a syntax-gate loop and a Drive
    re-upload — see `maestro.selfheal.redo._redo_rules`. Confined to whatever
    `project.yaml`'s `confinement.redo_allow` names (minus the deny-list), not a hardcoded
    surface."""
    parts = arg.split(None, 1)
    if len(parts) < 2:
        notify_telegram("Usage: /redo <id> <what's wrong>\n"
                        "Example: /redo 9 cell 5 crashes — the Drive path is wrong, rewrite it")
        return
    idarg, message = parts[0], parts[1].strip()
    dan_id, entry = _resolve_waiting(idarg)
    if not entry:
        notify_telegram(f"ℹ /redo: no parked task matches `{idarg}`. Use /waiting to list them.")
        return
    task_id  = entry.get("task_id", idarg)
    sid      = entry.get("session_id", "")
    uuid_    = entry.get("session_uuid", "") or _session_uuid_for(sid)
    pa       = prep_actions.get_action(task_id) or {}
    action   = pa.get("action") or entry.get("summary", "")
    worktree = entry.get("worktree", "") or ""
    brief    = str(WORKSPACES / sid / "brief.txt") if sid else ""

    REDO_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    redo_id = f"{task_id}-{ts}"
    req_path = REDO_DIR / f"{redo_id}.request.json"
    req_path.write_text(json.dumps({
        "redo_id": redo_id, "task": task_id, "uuid": uuid_, "dan_id": dan_id or "",
        "message": message, "action": action, "brief": brief, "worktree": worktree,
    }), encoding="utf-8")

    helper   = shlex.join(bootstrap.pinned_module_argv(VENV_PYTHON, "maestro.selfheal.redo"))
    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n redo-{task_id} "
                + shlex.quote(f"cd {REPO} && {helper} {req_path}"))
    try:
        subprocess.run(tmux_cmd, shell=True, check=True)
    except Exception as exc:
        notify_telegram(f"⚠ /redo failed to launch: {exc}")
        return
    notify_telegram(f"🔧 Maestro is reworking `{task_id}` — rewriting the deliverable, "
                    f"syntax-gating it, and re-uploading to Drive. I'll report back with the "
                    f"changelog + link.")
    append_journal("redo_launched",
                   f"{task_id} dan_id={dan_id} uuid={'y' if uuid_ else 'n'}", session_id=sid)


def _show_manual() -> None:
    """Reply with all dispatch:manual / needs-dan tasks and whether their deps are clear."""
    try:
        tasks = _load_roadmap_tasks()
    except Exception as exc:
        notify_telegram(f"Could not parse ROADMAP: {exc}")
        return

    pending_ids  = {str(t["id"]) for t in tasks}
    complete_ids = get_completed_task_ids()

    dan_tasks = [
        t for t in tasks
        if t.get("dispatch") == "manual" or t.get("mode") == "needs-dan"
    ]
    # B12: tasks that need Dan to ANSWER questions before an agent can implement
    # them (distinct from dispatch:manual tasks Dan performs himself).
    question_tasks = [t for t in tasks if _task_questions(t)]

    if not dan_tasks and not question_tasks:
        notify_telegram("No Dan-must-perform tasks in the queue.")
        return

    lines: list[str] = []
    if dan_tasks:
        _state    = read_state()
        _parked   = {p.get("task_id"): did
                     for did, p in _state.get("waiting_on_dan", {}).items()}
        _in_prep  = {e.get("task_id") for e in _state.get("in_flight", [])}
        lines.append(f"🛠️ Dan-must-perform ({len(dan_tasks)}):")
        for task in dan_tasks:
            tid   = str(task["id"])
            title = task.get("title", "(no title)")
            deps  = task.get("deps") or []
            if isinstance(deps, str):
                deps = [deps] if deps else []
            blocked = [
                str(d) for d in deps
                if str(d) in pending_ids and str(d) not in complete_ids
            ]
            sidecar = prep_actions.get_action(tid)
            action  = (sidecar or {}).get("action") or task.get("dan_action")
            # B14: prefer the single prepared action over raw dep status.
            if action and tid in _parked:
                status = (f"🎯 {action}\n"
                          f"   ✅ /approve {_parked[tid]}  ·  ❓ /ask {_parked[tid]} <msg>  "
                          f"·  ❌ /reject {_parked[tid]}")
            elif action:
                status = f"🎯 {action}"
            elif tid in _in_prep:
                status = "⏳ auto-prep running"
            elif blocked:
                status = "⛔ blocked by " + ", ".join(blocked)
            else:
                status = "⏳ queued for auto-prep"
            lines.append(f"• {tid} — {title}\n    {status}")

    if question_tasks:
        if lines:
            lines.append("")
        lines.append(f"⏳ Awaiting your answers ({len(question_tasks)}):")
        for task in question_tasks:
            tid   = str(task["id"])
            title = task.get("title", "(no title)")
            qs    = _task_questions(task)
            statuses = []
            answered = 0
            for q in qs:
                ok = _answer_choice(_question_req_id(tid, q["id"])) is not None
                answered += 1 if ok else 0
                statuses.append(f"{q['id']}={'✓' if ok else '…'}")
            lines.append(
                f"• {tid} — {title} — {answered}/{len(qs)} answered "
                f"({', '.join(statuses)})"
            )

    notify_telegram("\n".join(lines))


# ── `[P11]`: the operator projections, over Telegram ──────────────────────────────────
#
# `/workflow` and `/explain` are the same two projections `maestro workflow show` and
# `maestro explain` print, delivered to the channel the operator actually has open at 3am.
# They are read-only by construction (`maestro.status` opens a client store), which is why
# they need none of the command-inbox ceremony `/approve` and `/redo` need — there is no
# side effect to make idempotent, and a redelivered `/workflow` costs one more message.


def _show_workflow(task_id: str) -> None:
    """`/workflow <task_id>` — the compiled graph and where the run got to."""
    from maestro import status

    wanted = task_id.strip()
    if not wanted:
        notify_telegram("Usage: /workflow <task_id>\nExample: /workflow P8B2")
        return
    try:
        view = status.workflow_view(wanted, repo=REPO)
    except status.WorkflowViewError as exc:
        notify_telegram(f"No graph for {wanted}: {exc}")
        return
    send_report(status.render_workflow_text(view), header=f"🕸 workflow {wanted}")


def _show_explain(task_id: str) -> None:
    """`/explain <task_id>` — criteria -> binding -> evidence -> acceptance."""
    from maestro import status

    wanted = task_id.strip()
    if not wanted:
        notify_telegram("Usage: /explain <task_id>\nExample: /explain P8B2")
        return
    try:
        view = status.workflow_view(wanted, repo=REPO)
        explained = status.explain_view(view, repo=REPO)
    except status.WorkflowViewError as exc:
        notify_telegram(f"Cannot explain {wanted}: {exc}")
        return
    send_report(status.render_explain_text(explained), header=f"🔎 explain {wanted}")


def _show_detail(task_id: str) -> None:
    """Reply with task detail. For dispatch:manual / needs-dan tasks, surface just the
    ONE action Dan must take (B14); `/detail <id> full` forces the verbose dump."""
    raw  = task_id.strip()
    full = raw.upper().endswith(" FULL")
    if full:
        raw = raw[: -len(" FULL")].strip()
    try:
        tasks = _load_roadmap_tasks()
    except Exception as exc:
        notify_telegram(f"Could not parse ROADMAP: {exc}")
        return

    task = next(
        (t for t in tasks if str(t.get("id", "")).upper() == raw.upper()),
        None,
    )
    if task is None:
        notify_telegram(
            f"No task {raw} in ROADMAP (it may be graduated — see docs/PROJECT.md)."
        )
        return

    tid        = str(task["id"])
    title      = task.get("title", "(no title)")
    mode       = task.get("mode", "autonomous")
    dispatch   = task.get("dispatch", "auto")
    est_time   = task.get("est_time", "unknown")
    short_desc = task.get("short_desc", "")

    # B14: one-action view for Dan-must-perform tasks (unless `full` was requested).
    is_dan_task = dispatch == "manual" or mode == "needs-dan"
    if is_dan_task and not full:
        sidecar = prep_actions.get_action(tid)
        action  = (sidecar or {}).get("action") or task.get("dan_action")
        state   = read_state()
        parked  = next((p for p in state.get("waiting_on_dan", {}).items()
                        if p[1].get("task_id") == tid), None)
        in_prep = any(e.get("task_id") == tid for e in state.get("in_flight", []))
        # dep readiness
        complete = get_completed_task_ids()
        deps = task.get("deps") or []
        deps = [deps] if isinstance(deps, str) and deps else deps
        blocked = [str(d) for d in deps if str(d) not in complete]

        if action and parked:
            notify_telegram(f"{tid} — {title}\n🎯 Your action (ID {parked[0]}): {action}\n"
                            f"✅ Worked → /approve {parked[0]}  ·  ❓ Problem → /ask {parked[0]} <msg>  "
                            f"·  ❌ Abandon → /reject {parked[0]}")
        elif action:
            notify_telegram(f"{tid} — {title}\n🎯 Your action: {action}")
        elif in_prep:
            notify_telegram(f"{tid} — {title}\n⏳ Auto-prep in progress — I'll ping you with the "
                            f"one action when it's ready.")
        elif blocked:
            notify_telegram(f"{tid} — {title}\n⛔ Blocked by: {', '.join(blocked)} "
                            f"(auto-prep starts once deps complete).")
        else:
            notify_telegram(f"{tid} — {title}\n⏳ Queued for auto-prep. "
                            f"(`/detail {tid} full` for the raw brief.)")
        return

    lines = [
        f"{tid} — {title}",
        f"mode: {mode} | dispatch: {dispatch} | est: {est_time}",
    ]
    if short_desc:
        lines.append(short_desc)

    verifications = task.get("verifications") or []
    if verifications:
        lines.append("\nVerifications:")
        for v in verifications:
            if not isinstance(v, dict):
                continue
            kind  = v.get("kind", "auto")
            vid   = v.get("id", "?")
            check = v.get("check", "")
            entry = f"  [{kind}] {vid}: {check}"
            if kind == "auto" and v.get("cmd"):
                entry += "\n    cmd: " + str(v["cmd"])
            lines.append(entry)
    else:
        lines.append("\nNo structured verifications in this task block.")

    notify_telegram("\n".join(lines))


def _show_waiting() -> None:
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    # `kind: dan` checks a graph run is holding before merge (`maestro.hitl.verify`).
    verifying = verify.waiting_lines(state)
    # D26: tasks paused on an agent's question about a case the spec never defined.
    asking = verify.question_waiting_lines(state)
    if not waiting and not verifying and not asking:
        notify_telegram("No tasks currently waiting for your review.")
        return
    lines = [f"*{len(waiting)} task(s) waiting for your review:*"] if waiting else []
    for did, info in waiting.items():
        lines.append(f"• ID {did}: `{info['task_id']}` (parked {info['parked_at'][:10]})")
    if verifying:
        lines.append(f"*{len(verifying)} task(s) waiting for your verification before merge:*")
        lines.extend(verifying)
    if asking:
        lines.append(f"*{len(asking)} task(s) waiting for your decision on a question:*")
        lines.extend(asking)
    notify_telegram("\n".join(lines))


def _control_pause(paused: bool) -> None:
    state = read_state()
    state['paused_by_user'] = paused
    write_state(state)
    append_journal('control_pause' if paused else 'control_resume',
                   'paused via Telegram /pause' if paused else 'resumed via Telegram /resume')
    notify_telegram('⏸ Orchestrator paused. No new tasks will launch. /resume to continue.'
                    if paused else '▶ Orchestrator resumed.')


def _control_halt() -> None:
    HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HALT_FILE.write_text(now_iso())
    append_journal('control_halt', 'halted via Telegram /halt')
    notify_telegram('🛑 HALT sentinel written. Orchestrator will stop after current iteration.')


def _control_hitl(arg: str, _) -> None:
    if arg.lower() not in ('on', 'off'):
        return
    state = read_state()
    state['hitl_mode'] = arg.lower() == 'on'
    write_state(state)
    notify_telegram('✅ HITL mode ON — tasks will pause before merge for your review.'
                    if state['hitl_mode'] else '✅ HITL mode OFF — tasks auto-merge after gates pass.')


def _control_approve(arg: str, in_flight) -> None:
    if not _graph_answer('approve', arg):
        _process_approve(arg.lower(), in_flight)


def _control_reject(arg: str, _) -> None:
    if not _graph_answer('reject', arg):
        _process_reject(arg.lower())


def _control_fix(arg: str, _) -> None:
    if _graph_runner_active():
        notify_telegram(_graph_fix_reply(arg))
    else:
        _process_fix(arg.upper())


def _control_redo(arg: str, _) -> None:
    if not _graph_answer('redo', arg):
        _handle_redo(arg)


def _control_detail(arg: str, _) -> None:
    if arg:
        _show_detail(arg.upper())
    else:
        notify_telegram('Usage: /detail <task_id>\nExample: /detail P8B2')


def _control_priority(arg: str, _) -> None:
    from maestro import config
    try:
        weight = float(arg)
        weight = config.set_project_priority(REPO, weight)
    except (ValueError, OSError) as exc:
        notify_telegram(f'⚠ /priority: {exc}\nUsage: /priority [project] <positive weight> (1 = normal)')
        return
    append_journal('control_priority', f'weight={weight}')
    notify_telegram(f'✅ Project priority committed: {weight} (1 = normal).')


def _control_sprint(arg: str, _, *, prepared=None, update_id=None) -> None:
    from maestro import sprint, config
    state = read_state()
    try:
        if prepared is None:
            updated = sprint.control(state.get('sprint', {}), arg.strip().lower(),
                                     now_epoch=time.time())
        else:
            from maestro.hitl.shared_bot import sprint_control_scope
            scope = sprint_control_scope(REPO)
            if prepared['scope'] != scope:
                raise ValueError('Sprint control belongs to another project or Telegram source')
            updated = sprint.apply_control(state.get('sprint', {}), prepared, update_id)
    except ValueError as exc:
        notify_telegram(str(exc))
        return
    if arg.strip().lower() != 'status':
        state['sprint'] = updated
        write_state(state)
    notify_telegram('Sprint: disabled by config' if not config.sprint_config().enabled
                    else sprint.status_text(updated))


def _no_args(fn):
    def handle(arg, _):
        if arg:
            notify_telegram('This command takes no arguments after the project.')
        else:
            fn()
    return handle


def _intake_handler(name):
    def handle(arg, context):
        from maestro import intake
        intake.handle(REPO, f'/{name} {arg}'.strip(), context['update_id'], notify_intake)
    return handle


# Each actual handler is registered once here. Both dispatch and presentation read this.
COMMANDS = {c.name: c for c in (
    Command('status', '', 'Phase, in-flight tasks and halted/paused flags', _no_args(lambda: notify_telegram(_status_text()))),
    Command('progress', '', 'Live step, model, usage sample and waits', _no_args(lambda: notify_telegram(_build_progress_report()))),
    Command('pause', '', 'Stop launching new tasks', _no_args(lambda: _control_pause(True))),
    Command('resume', '', 'Resume after a pause', _no_args(lambda: _control_pause(False))),
    Command('halt', '', 'Stop after the current iteration', _no_args(_control_halt)),
    Command('hitl', 'on|off', 'Require review before merge', _control_hitl),
    Command('backend', '<name|auto> [task_id]', 'Pick a launch backend or move one in-flight task', lambda a, _: _process_backend(a)),
    Command('workflow', '<id>', 'Run summary and workflow steps', lambda a, _: _show_workflow(a)),
    Command('explain', '<id>', 'Run summary, checks and decision', lambda a, _: _show_explain(a)),
    Command('report', 'bug|change|feature <description>', 'Save a report for a scoped proposal', _intake_handler('report')),
    Command('reports', '', 'List intake reports and task progress', _intake_handler('reports')),
    Command('intake', '<id>|approve|amend|reject|retry <arguments>', 'Review or act on an intake proposal', _intake_handler('intake')),
    Command('waiting', '', 'List tasks waiting for your decision', _no_args(_show_waiting)),
    Command('verify', '<id> approve|reject|done|redo|abandon [reason]', 'Answer a verification or manual step', lambda a, _: notify_telegram(verify.handle_verify_command(a))),
    Command('approve', '<id>', 'Approve a verification or finish a manual step', _control_approve),
    Command('fix', '<id>', 'Agree with a legacy failure diagnosis; graph uses unpark', _control_fix),
    Command('ask', '<id> <message>', 'Ask about a waiting verification or manual job', lambda a, _: _handle_ask(a)),
    Command('redo', '<id> <reason>', 'Rework a manual deliverable or step', _control_redo),
    Command('reject', '<id> [reason]', 'Reject a verification or abandon a parked task', _control_reject),
    Command('unpark', '<task_id>', 'Relaunch a failed task on its kept branch', lambda a, _: _process_unpark(a.upper())),
    Command('manual', '', 'List manual tasks and unanswered questions', _no_args(_show_manual)),
    Command('detail', '<id> [full]', 'Show task detail and verifications', _control_detail),
    Command('priority', '<weight>', 'Set and commit project priority; 1 is normal', _control_priority),
    Command('sprint', 'on|off|stop|status', 'Control expiring-capacity sprints; off skips one week', _control_sprint),
    Command('help', '', 'Show this command list', _no_args(lambda: notify_telegram(HELP_TEXT))),
    Command('commands', '', 'Show this command list', _no_args(lambda: notify_telegram(HELP_TEXT))),
    Command('start', '', 'Show this command list', _no_args(lambda: notify_telegram(HELP_TEXT))),
)}


HELP_TEXT = help_text(COMMANDS)


def _dispatch_control_text(text_raw, update, message, in_flight):
    name, arg = split_command(text_raw)
    command = COMMANDS.get(name)
    if command:
        if not arg and name in ('approve', 'reject', 'fix', 'unpark', 'hitl'):
            return  # retained bare-verb behavior from the legacy router
        if name in ('help', 'commands', 'start') and update.get('_answered'):
            return
        context = update if name in ('report', 'reports', 'intake') else in_flight
        command.handler(arg, context)
    elif text_raw and not text_raw.startswith('/'):
        _route_freetext_answer(text_raw, message.get('reply_to_message'))
